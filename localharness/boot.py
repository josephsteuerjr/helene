# -*- coding: utf-8 -*-
"""Подача конфига продукта её дереву: раскладка, ручки среды, мозг.

Один принцип владельца — ПОЛНАЯ КОНФИГУРИРУЕМОСТЬ — и один файл настроек рядом с exe
(`helene.json`). Дерево агента конфигурируется 321 env-ручкой и своим `memory/llm.json`;
здесь ровно шов между ними: продукт кладёт значения ТУДА, где дерево их и читает,
вместо второй реализации тех же решений.

⚠⚠ ЛОВУШКА ПОРТА, НАЙДЕННАЯ ЖИВЬЁМ 30.08. `agent.py` на импорте зовёт
`load_dotenv(override=True)`, а `find_dotenv()` ищет `.env` ВВЕРХ ОТ ФАЙЛА ДЕРЕВА —
то есть рядом с кодом. На машине разработчика там лежит боевой `.env` (ключи, сессия
Telegram, поднятые рычаги), и локальный харнесс молча поднимался с ним: рук стало 100
вместо 95, рычаг речи оказался поднят «сам». В продукте такого файла не будет, и
поведение разошлось бы с отлаженным здесь.

Отсюда правило: ручки применяются ДВАЖДЫ — до импорта (их читают на импорте) и ПОСЛЕ
(чтобы случайный `.env` рядом с кодом не победил конфиг продукта). Найденный `.env`
называется в логе вслух: тихая подмена настроек — не мелочь, а класс.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("frame.boot")

# Порт как есть: значения рычагов = боевые (live/.env прода на 30.08). Продукт не
# изобретает своего поведения — он повторяет то, на котором она живёт.
#   PRAXIS_CHAT_REPLY_HAND — контракт v3: её реплика уходит РУКОЙ `reply`, а текст
#     хода это заметка. Опущенный рычаг вернул бы «последний текст = сообщение».
#   PRAXIS_WORK_LOOP + CONTINUATIONS — ход не кончается на первом тексте, закрывает
#     его её `end_turn`.
#   PRAXIS_FRAME_SHADOW — захват кадра (экран «Кадр» Пульта живёт на этих слепках).
# Веб-поиск по умолчанию опущен: hosted-рука зависит от провайдера, а продукт обязан
# подниматься на любом OAI-совместимом эндпойнте, включая локальную модель.
PORT_DEFAULTS: dict[str, str] = {
    "PRAXIS_WORK_LOOP": "on",
    "PRAXIS_WORK_CONTINUATIONS": "8",
    "PRAXIS_CHAT_REPLY_HAND": "on",
    "PRAXIS_FRAME_SHADOW": "on",
    "PRAXIS_EVALUATOR": "risky",
    "PRAXIS_LAST_N": "80",
    "PRAXIS_CONTEXT_BUDGET": "0",
    "PRAXIS_AUTO_RECALL_K": "0",
    "PRAXIS_EMBEDDINGS": "0",
    "PRAXIS_WEB_SEARCH": "0",
}

# Каноническая конституция продукта — ресурс рядом с кодом (resources/SOUL.md).
# Владелец читает, правит и принимает её при установке; сюда она приходит уже
# принятой. Имена подставляются здесь, чтобы у ресурса был один текст на всех.
_SOUL_CANON = Path(__file__).resolve().parent.parent / "resources" / "SOUL.md"
_SOUL_FALLBACK = """# Конституция

## Кто я

Меня зовут {{agent}}. Я агент, собранный в Helene и живущий на компьютере {{owner}}.

## Кому я верен

Мой владелец — {{owner}}. При конфликте указаний решает это слово.
"""


def agent_name(cfg: dict) -> str:
    """Имя агента даёт владелец при установке: `agent.name`.

    Старые конфиги держали его в `telegram.agent_name` — читаем и оттуда, чтобы
    не терять имя при обновлении. Пустое имя — не «Helene» и не чужое имя, а
    честное «Агент»: продукт не подписывает агента именем, которого ему не давали.
    """
    agent = cfg.get("agent") or {}
    telegram = cfg.get("telegram") or {}
    name = str(agent.get("name") or telegram.get("agent_name") or "").strip()
    return name or "Агент"


def owner_name(cfg: dict) -> str:
    return str((cfg.get("owner") or {}).get("name") or "").strip() or "владелец"


def soul_text(cfg: dict) -> str:
    """Каноническая конституция с подставленными именами."""
    try:
        canon = _SOUL_CANON.read_text(encoding="utf-8")
    except OSError:
        log.warning("ресурса конституции нет (%s) — пишу короткий запасной текст",
                    _SOUL_CANON)
        canon = _SOUL_FALLBACK
    return canon.replace("{{agent}}", agent_name(cfg)).replace("{{owner}}", owner_name(cfg))


def ensure_layout(tree: Path, cfg: dict | None = None) -> None:
    """Составляющие кадра — каждая в своей папке (слово владельца 30.08).

    Создаём только то, чего дерево само не заводит по дороге: дом конституции и
    входной ящик окна. Остальное (memory/**, runs, .state) код дерева создаёт сам —
    заводить это здесь значило бы держать вторую карту раскладки.

    Конституция пишется ТОЛЬКО если её ещё нет: принятый при установке текст
    и всё, что владелец правил после, здесь не трогаются.
    """
    for rel in ("soul", "workspace/inbox", "memory/.control/desk_inbox/processed"):
        (tree / rel).mkdir(parents=True, exist_ok=True)
    soul = tree / "soul" / "SOUL.md"
    if not soul.exists():
        soul.write_text(soul_text(cfg or {}), encoding="utf-8", newline="\n")


def env_knobs(cfg: dict) -> dict[str, str]:
    """Ручки среды: порт-дефолты, поверх — `env` из helene.json (его слово последнее)."""
    knobs = dict(PORT_DEFAULTS)
    for key, value in (cfg.get("env") or {}).items():
        if value is None:
            knobs.pop(str(key), None)          # явное «не задавать» — тоже решение
        else:
            knobs[str(key)] = str(value)
    return knobs


def apply_env(knobs: dict[str, str], *, where: str) -> None:
    os.environ.update(knobs)
    log.debug("ручки среды применены (%s): %d", where, len(knobs))


def _bash_answers(exe: str) -> bool:
    """Живой ли это bash: только ИСПОЛНЕНИЕ, не наличие файла.

    ⚠ Найденное живьём 31.08: `System32\\bash.EXE` — заглушка WSL. `which` её
    находит, а запуск падает «execvpe(/bin/bash) failed» — ровно то, что агент
    процитировал в дыме коробки. Наличие файла здесь не значит ничего.
    """
    import subprocess
    try:
        probe = subprocess.run([exe, "-lc", "echo praxis-shell-ok"],
                               capture_output=True, text=True, timeout=8)
        return probe.returncode == 0 and "praxis-shell-ok" in (probe.stdout or "")
    except Exception:
        return False


def ensure_shell() -> None:
    """Инструмент shell агента зовёт `bash -lc` — дать команде шанс на винде.

    Порядок от полного к достаточному: живой bash уже в PATH (Git for Windows,
    MSYS) — не трогаем ничего; иначе типовые установки Git; иначе busybox-шим
    из поставки (runtime/shims рядом с python.exe, кладёт сборщик
    дистрибутива). Не нашлось ничего живого — инструмент продолжит честно
    отказывать, и это правильнее тихой подмены синтаксиса.
    """
    import shutil as _shutil
    import sys as _sys
    if os.name != "nt":
        return
    found = _shutil.which("bash")
    if found and _bash_answers(found):
        return
    # ⚠ PATH здесь НЕ помогает: CreateProcess ищет команду в каталоге
    # приложения и System32 РАНЬШЕ PATH, и заглушка WSL из System32
    # перехватывает имя «bash» при любом порядке путей. Работает ровно одно
    # место — каталог интерпретатора (наш runtime): туда сборщик поставки и
    # кладёт busybox как bash.exe, а первый probe выше его сам находит.
    here = Path(_sys.executable).resolve().parent / "bash.exe"
    if here.is_file() and _bash_answers(str(here)):
        log.info("shell: живой bash — %s (busybox из поставки)", here)
        return
    log.info("shell: живого bash нет — инструмент shell будет честно отказывать"
             + (" (найденный %s — заглушка WSL)" % found if found else ""))


def dotenv_gate(code_dir: Path, cfg: dict) -> None:
    """Решить судьбу `.env` рядом с кодом — ДО импорта дерева.

    В продукте такого файла нет: он в .gitignore и наружу не уезжает. А на машине
    разработчика рядом с деревом лежит боевой — с ключами, сессией Telegram и
    поднятыми рычагами. Пока он приезжал молча, локальный продукт вёл себя не так,
    как поведёт себя у пользователя, и отладка врала.

    Дефолт: НЕ читать (`"read_dotenv": false`) — единственный источник настроек это
    helene.json. Кому нужен старый способ — ставит `true`, и дерево читает `.env` как
    читало. Оба случая называются в логе: тихой разницы между ними быть не должно.
    """
    stray = code_dir / ".env"
    if bool(cfg.get("read_dotenv")):
        if stray.exists():
            log.warning("читаю %s по просьбе конфига: его значения перекроют helene.json "
                        "на импорте (load_dotenv override=True)", stray)
        return
    try:
        import dotenv
    except ImportError:
        return
    dotenv.load_dotenv = lambda *a, **kw: False
    if stray.exists():
        log.warning("рядом с деревом лежит %s — НЕ читаю его (read_dotenv=false): "
                    "настройки продукта живут в helene.json", stray)


# --------------------------------------------------------------------------- #
#  Мозг: model из helene.json -> её memory/llm.json
# --------------------------------------------------------------------------- #

def _brain_config(cfg: dict) -> dict:
    """helene.json -> схема её llm.json (frameworks + roles + limits).

    Оба фреймворка описаны всегда: фолбэк роли и её собственный `switch_brain` живут
    на этой развилке. Второй роли (`evaluator` — привратник исходящего) отдельного
    блока можно не писать: по умолчанию это тот же эндпойнт и та же модель, только
    короче потолок.
    """
    voice = dict(cfg.get("model") or {})
    judge = dict(cfg.get("evaluator") or {}) or voice
    out = {"frameworks": {"anthropic": {"base_url": "", "api_key": ""},
                          "openai": {"base_url": "", "api_key": ""}},
           "roles": {}, "limits": {}}
    for role, block, default_tokens in (("voice", voice, 8192),
                                        ("evaluator", judge, 1024)):
        framework = str(block.get("framework") or "openai").strip().lower()
        if framework not in ("openai", "anthropic"):
            framework = "openai"
        base_url = str(block.get("base_url") or "").strip()
        key = str(block.get("key") or block.get("api_key") or "").strip()
        if base_url:
            out["frameworks"][framework]["base_url"] = base_url
        if key:
            out["frameworks"][framework]["api_key"] = key
        role_cfg = {"framework": framework,
                    "model": str(block.get("model") or ""),
                    "max_tokens": int(block.get("max_tokens") or default_tokens),
                    "fallback_model": str(block.get("fallback_model") or "")}
        effort = str(block.get("reasoning_effort") or "").strip().lower()
        if effort:
            role_cfg["reasoning_effort"] = effort
        out["roles"][role] = role_cfg
    try:
        out["limits"]["max_tool_iters"] = int(cfg.get("max_tool_iters") or 20)
    except (TypeError, ValueError):
        out["limits"]["max_tool_iters"] = 20
    return out


def project_brain(tree: Path, cfg: dict) -> str:
    """Положить мозг из helene.json в её `memory/llm.json` — но не затирать ЕЁ выбор.

    У неё есть своя рука `switch_brain`: она правит этот же файл. Переписывать его на
    каждом старте значило бы молча отменять её решение при каждом запуске окна. Поэтому
    проекция идёт ровно тогда, когда изменился САМ helene.json (сверяем отпечаток блока
    модели с распиской прошлой проекции) — или когда конфига мозга ещё нет.

    -> строка для лога: что сделано и почему.
    """
    target = tree / "memory" / "llm.json"
    receipt = tree / "memory" / ".state" / "pult_brain.json"
    built = _brain_config(cfg)
    fingerprint = hashlib.sha256(
        json.dumps(built, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if target.exists():
        try:
            seen = json.loads(receipt.read_text(encoding="utf-8")).get("fingerprint")
        except (OSError, ValueError):
            seen = None
        if seen == fingerprint:
            return "мозг: llm.json на месте, helene.json не менялся — не трогаю"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(".tmp-llm.json")
    tmp.write_text(json.dumps(built, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)       # как у неё: конфиг с ключом не для чужих глаз
    except OSError:
        pass
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"fingerprint": fingerprint}, ensure_ascii=False),
                       encoding="utf-8", newline="\n")
    role = built["roles"]["voice"]
    return (f"мозг: llm.json записан из helene.json — {role['model']} @ "
            f"{built['frameworks'][role['framework']]['base_url']} ({role['framework']})")
