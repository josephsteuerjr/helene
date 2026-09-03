# -*- coding: utf-8 -*-
"""Сборка портативного дистрибутива Hélène — установщик v0.

«Установщик» v0 — это правильная папка + zip: распаковал и запустил helene.exe.
Без службы, без UAC, без записи в реестр — ровно то, что переживает правки
(его слово): всё, что меняется (дерево-питон, UI), лежит файлами и обновляется
заменой; пересборки требует только оболочка.

Раскладка поставки:
  Helene/
    helene.exe           — оболочка (окно+трей), поднимает всё остальное
    helene-setup.exe     — установщик: сцены первого запуска, копия в LocalAppData
    helene.json          — конфиг v0 (local, ключ пуст — см. ПЕРВЫЙ-ЗАПУСК.md)
    ПЕРВЫЙ-ЗАПУСК.md    — три шага руками, пока нет визарда
    runtime/            — embedded CPython + зависимости (самодостаточный)
    app/                — труба (deskapp+deskd+static), руннер (localharness),
                          resources/ (каноническая конституция)
    tree/               — код дерева агента (порт как есть, без тестов/секретов)
    data/               — рождается при первом запуске (память, кадр, душа)

Запуск: python build_dist.py [--out DIR] [--skip-runtime]
Сеть нужна один раз: embeddable CPython с python.org + pip с pypi.
"""
from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.14.5"      # та же линия, на которой гоняется порт (гейт зелёный)
EMBED_URL = (f"https://www.python.org/ftp/python/{PY_VERSION}/"
             f"python-{PY_VERSION}-embed-amd64.zip")
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
# busybox-w32 (официальная сборка Рона Йорстона): один exe, POSIX-шелл.
# Едет в runtime/shims/bash.exe — инструмент shell агента зовёт `bash -lc`, и
# на машине без Git/WSL команда иначе честно отказывала бы. Решение владельца 31.08.
BUSYBOX_URL = "https://frippery.org/files/busybox/busybox64.exe"

# Зависимости ПОСТАВКИ — только то, что импортируется на пути продукта:
# ход (agent/llm/webtool) + труба (deskapp). Telethon/aiogram/paramiko/stt —
# другие тела, в продукт не едут; cryptography не нужна (telegram_confirmation
# агентом не импортируется — проверено грепом 31.08).
DEPS = [
    "anthropic", "openai", "httpx", "python-dotenv", "pillow",
    "aiohttp", "pypdf", "trafilatura", "charset-normalizer",
    "telethon==1.44.0",   # Telegram своим аккаунтом (MTProto)
]

DESK = Path(__file__).resolve().parent.parent
ROOT = DESK.parent
LIVE = ROOT / "live"
APP_DIST = DESK / "app" / "dist"     # UI окна — сборка Vite (npm --prefix app run build)

# Дерево кода: что НЕ едет пользователю.
TREE_EXCLUDE = [
    ".env*", "*.env", "*.session", "*.session-journal", "__pycache__", ".git",
    "test_*.py", "conftest.py", "_archive", "hands_diff.txt",
    "docker-compose*", "Dockerfile", "*.bak",
    "body/target", "hands/target", "memory", "workspace", "soul",
    "shadow_traffic", "runs",
    # личное её и чужое: корпуса модерации, сканы истории, кэш тестов
    "private", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    # её рабочие заметки, резервные копии правок, оперативный статус сервера
    "workspace_*.md", "*.pre-*", "moderation_shadow_corpus.json",
    "STATUS.md", "SYNC-HEAD.txt", "1500", "ПОРТ-СТАТУС-*.md",
]


def _excluded(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    for pattern in TREE_EXCLUDE:
        if "/" in pattern:
            if rel.replace("\\", "/").startswith(pattern):
                return True
        elif any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def copy_tree(src: Path, dst: Path) -> int:
    count = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(src))
        if _excluded(rel):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def copy_static(dst: Path) -> None:
    """UI окна — только сборка Vite; без неё сборка честно падает."""
    if not (APP_DIST / "index.html").is_file():
        raise SystemExit("нет app/dist — собери UI: npm --prefix app run build")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(APP_DIST, dst)


MOBILE_DIST = DESK / "mobile" / "dist"   # PWA телефона (npm --prefix mobile run build)


def copy_mobile(dst: Path) -> None:
    """PWA телефона — сборка Vite; без неё поставка просто не даёт /m/."""
    if not (MOBILE_DIST / "index.html").is_file():
        print("  ⚠ mobile/dist нет — телефон в этой поставке недоступен")
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(MOBILE_DIST, dst)


def copy_resources(dst: Path) -> None:
    """Ресурсы продукта: каноническая конституция и всё, что читает boot.py."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in (DESK / "resources").glob("*"):
        if f.is_file():
            shutil.copy2(f, dst / f.name)


def stage_payload(dest: Path) -> int:
    """app/ + tree/ + requirements — общий груз поставки, деплоя и эмуляции.

    Одна функция на три потребителя, чтобы состав не расходился молча.
    -> число файлов дерева. Секрет-гард внутри: .env в грузе роняет сборку.
    """
    (dest / "app" / "deskd").mkdir(parents=True, exist_ok=True)
    shutil.copy2(DESK / "deskapp.py", dest / "app" / "deskapp.py")
    for name in ("__init__.py", "readers.py"):
        shutil.copy2(DESK / "deskd" / name, dest / "app" / "deskd" / name)
    copy_static(dest / "app" / "static")
    copy_resources(dest / "app" / "resources")
    copy_mobile(dest / "app" / "mobile")
    (dest / "app" / "localharness").mkdir(exist_ok=True)
    for f in (DESK / "localharness").glob("*.py"):
        shutil.copy2(f, dest / "app" / "localharness" / f.name)
    copied = copy_tree(LIVE, dest / "tree")
    for guard in (dest / "tree" / ".env", dest / "tree" / ".env.example"):
        if guard.exists():
            raise SystemExit(f"СЕКРЕТ В ГРУЗЕ: {guard} — сборка остановлена")
    (dest / "requirements.txt").write_text("\n".join(DEPS) + "\n",
                                           encoding="utf-8", newline="\n")
    return copied


def fetch(url: str, dst: Path) -> None:
    if dst.exists():
        print(f"  есть: {dst.name}")
        return
    print(f"  качаю {url}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp:
        dst.write_bytes(resp.read())


def build_runtime(out: Path, cache: Path) -> None:
    """Embedded CPython, который умеет наши зависимости.

    Embeddable-сборка python.org по умолчанию БЕЗ site-packages и без pip:
    в `python314._pth` включаем `import site`, ставим pip через get-pip и
    ставим зависимости внутрь. Результат самодостаточен: ни PATH, ни реестра,
    ни установленного питона на машине не требуется.
    """
    runtime = out / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    embed_zip = cache / f"python-{PY_VERSION}-embed-amd64.zip"
    get_pip = cache / "get-pip.py"
    fetch(EMBED_URL, embed_zip)
    fetch(GET_PIP_URL, get_pip)
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(runtime)
    pth = next(runtime.glob("python3*._pth"))
    text = pth.read_text(encoding="utf-8").replace("#import site", "import site")
    pth.write_text(text, encoding="utf-8", newline="\n")
    py = runtime / "python.exe"
    print("  ставлю pip…")
    subprocess.run([str(py), str(get_pip), "--no-warn-script-location", "-q"],
                   check=True)
    print("  ставлю зависимости…")
    # Часть пакетов (pyaes у Telethon) идёт исходниками без колеса под 3.14:
    # им нужен setuptools на время сборки; после — снимаем, пользователю он не нужен.
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--no-warn-script-location",
                    "setuptools", "wheel"], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-q",
                    "--no-warn-script-location", *DEPS], check=True)
    subprocess.run([str(py), "-m", "pip", "uninstall", "-y", "-q", "setuptools", "wheel"],
                   check=False)
    # pip внутри поставки не нужен пользователю и весит; кэш pip — тем более.
    subprocess.run([str(py), "-m", "pip", "cache", "purge", "-q"], check=False)


FIRST_RUN = """# Hélène · первый запуск

Программа не подписана сертификатом. При первом запуске Windows SmartScreen
может сказать «Система защитила ваш компьютер»: нажми «Подробнее», затем
«Выполнить в любом случае». Это одноразово.

1. Запусти `helene.exe`.
2. Пройди четыре коротких шага в окне настройки: имя, модель, необязательные
   каналы и проверка.
3. После перезапуска агент представится сам. Вкладка «Устройство» объясняет,
   из чего он состоит и что происходит с сообщением.

Закрыть окно — не значит остановить агента: он продолжит жить в значке у часов.
Настройку пока можно изменить в `helene.json` рядом с приложением.

Все данные агента живут в `data/` рядом. Перенос на другую машину — перенос
папки целиком.
"""

HELENE_JSON = """{
  "mode": "local",
  "python": "runtime/python.exe",
  "app": "app/deskapp.py",
  "runner": "app/localharness/runner.py",
  "tree": "data",
  "code": "tree",
  "port": 8094,
  "agent": {
    "name": ""
  },
  "owner": {
    "name": "",
    "room": "Hélène"
  },
  "model": {
    "framework": "openai",
    "base_url": "https://api.openai.com/v1",
    "key": "",
    "model": "gpt-5.2",
    "max_tokens": 8192
  },
  "telegram": {
    "bot_token": "",
    "owner_id": 0
  },
  "read_dotenv": false
}
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DESK / "installer" / "build"))
    parser.add_argument("--skip-runtime", action="store_true",
                        help="не пересобирать runtime (он уже в out)")
    args = parser.parse_args()
    out = Path(args.out).resolve() / "Helene"   # имя папки — латиницей
    cache = Path(args.out).resolve() / "cache"
    print(f"дистрибутив → {out}")

    # Build output is disposable. Never let a previous preview's settings,
    # tokens, or local memory ride into the next export archive.
    for rel in ("app", "tree", "data"):
        if (out / rel).exists():
            shutil.rmtree(out / rel)
    out.mkdir(parents=True, exist_ok=True)

    if not args.skip_runtime:
        print("runtime:")
        build_runtime(out, cache)
    # busybox лежит ПРЯМО РЯДОМ с python.exe, не в подпапке и не в PATH:
    # CreateProcess ищет команду в каталоге приложения и System32 РАНЬШЕ PATH,
    # и `System32\bash.exe` (заглушка WSL) перехватывала имя при любом PATH.
    # Каталог приложения — наш runtime, и этот же порядок работает на нас.
    bash_shim = out / "runtime" / "bash.exe"
    if (out / "runtime").is_dir() and not bash_shim.exists():
        print("shell-шим:")
        fetch(BUSYBOX_URL, cache / "busybox64.exe")
        shutil.copy2(cache / "busybox64.exe", bash_shim)
        print("  runtime/bash.exe (busybox-w32) положен")

    print("app:")
    (out / "app" / "deskd").mkdir(parents=True, exist_ok=True)
    shutil.copy2(DESK / "deskapp.py", out / "app" / "deskapp.py")
    for name in ("__init__.py", "readers.py"):
        shutil.copy2(DESK / "deskd" / name, out / "app" / "deskd" / name)
    copy_static(out / "app" / "static")
    copy_resources(out / "app" / "resources")
    copy_mobile(out / "app" / "mobile")
    (out / "app" / "localharness").mkdir(exist_ok=True)
    for f in (DESK / "localharness").glob("*.py"):
        shutil.copy2(f, out / "app" / "localharness" / f.name)

    print("tree:")
    copied = copy_tree(LIVE, out / "tree")
    print(f"  файлов дерева: {copied}")
    for guard in ((out / "tree" / ".env"), (out / "tree" / ".env.example")):
        if guard.exists():
            raise SystemExit(f"СЕКРЕТ В ПОСТАВКЕ: {guard} — сборка остановлена")

    exe = DESK / "shell" / "target" / "release" / "helene.exe"
    if exe.exists():
        shutil.copy2(exe, out / "helene.exe")
        shutil.copy2(DESK / "shell" / "icons" / "icon.ico", out / "helene.ico")
        print("helene.exe: положен")
    else:
        print("⚠ helene.exe не найден — собери shell (cargo build --release --features custom-protocol)")
    svc = DESK / "svc" / "target" / "release" / "helene-svc.exe"
    if svc.exists():
        shutil.copy2(svc, out / "helene-svc.exe")
        for script in (DESK / "installer" / "service").glob("*.ps1"):
            shutil.copy2(script, out / script.name)
        print("helene-svc.exe + скрипты службы: положены (опция)")
    else:
        print("⚠ helene-svc.exe не найден — собери svc (cargo build --release)")
    setup_exe = DESK / "setup" / "target" / "release" / "helene-setup.exe"
    if setup_exe.exists():
        shutil.copy2(setup_exe, out / "helene-setup.exe")
        print("helene-setup.exe: положен (установщик внутри поставки)")
    else:
        print("⚠ helene-setup.exe не найден — собери setup (npm run build + cargo build --release --features custom-protocol)")
    relay = ROOT / "_relay_prod_src" / "target" / "release" / "codex-proxy-server.exe"
    if relay.exists():
        shutil.copy2(relay, out / "helene-relay.exe")
        print("helene-relay.exe: положен (подписка ChatGPT без ключа)")
    else:
        print("⚠ реле не найдено — собери _relay_prod_src (cargo build --release)")
    (out / "ПЕРВЫЙ-ЗАПУСК.md").write_text(FIRST_RUN, encoding="utf-8", newline="\n")
    shutil.copy2(DESK / "installer" / "THIRD-PARTY.md", out / "ЛИЦЕНЗИИ-ТРЕТЬИХ-СТОРОН.md")
    shutil.copy2(DESK / "installer" / "ЛИЦЕНЗИЯ.md", out / "ЛИЦЕНЗИЯ.md")
    shutil.copy2(DESK / "resources" / "HELENE-MAP.md", out / "КАК-УСТРОЕН-HELENE.md")
    (out / "helene.json").write_text(HELENE_JSON, encoding="utf-8", newline="\n")
    (out / "data").mkdir(exist_ok=True)

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"итого: {total / 1e6:.1f} МБ до сжатия")
    # Следы запуска установщика из этой же папки в архив не едут.
    for stray in ("install.log",):
        (out / stray).unlink(missing_ok=True)
    archive = out.parent / "Helene"
    print("zip…")
    shutil.make_archive(str(archive), "zip", out.parent, "Helene")
    size = (out.parent / "Helene.zip").stat().st_size
    print(f"готово: {archive}.zip ({size / 1e6:.1f} МБ)")


if __name__ == "__main__":
    main()
