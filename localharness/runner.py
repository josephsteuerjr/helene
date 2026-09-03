# -*- coding: utf-8 -*-
"""Локальный харнесс — руннер v1: в окне Пульта (и в Telegram-боте) идёт ЕЁ ход.

Порт как есть. Руннер не воспроизводит её ход, а запускает её собственный —
`agent.voice_turn_envelope`, тот самый, которым живёт агент на сервере:

  * руки — все её (recall, дневник, желания, файлы, мастерская, shell, коддинг…),
    выданные её же сборщиком `offered_tools_for` (контракт A1: список один на всех);
  * тул-цикл — её: расписки, чекпойнты, exact-once, потолки, честные ошибки рук;
  * кадр — её `frame_shadow` (K‖E‖A стабильны и кэшируются, T — подвижный хвост);
  * память — её (`memory_life`, recall-индекс, дневник, досье, леджер желаний);
  * прожитый ход, `runs/`, `turns.jsonl`, `llm_calls.jsonl` — пишет она сама, в тех
    же форматах, которые Пульт уже читает.

Наша работа — три шва, ни строчки её кода:
  1. ОКНО (`transport.py`) — Пульт вложен в `agent._TELETHON` вместо Telethon;
  2. БОТ (`botapi.py`, опция) — Telegram Bot API поверх: один токен от BotFather
     вместо номера/api_id/api_hash, маршрутизатор по адресу (window → окно,
     числовые id → бот);
  3. КОНФИГ (`boot.py`) — helene.json кладётся туда, где дерево его читает.

Организм ОДИН: у окна и бота общая память, общий кадр, общие желания. Ходы идут
строго по одному — очередь здесь, а не в её дереве.

Запуск (оболочкой): python runner.py --config <путь к helene.json>
Дерево данных — из PRAXIS_DESK_TREE (кладёт оболочка) или из `tree` конфига.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boot
import botapi
import transport

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("frame.runner")

STREAM = "window"          # комната окна: ключ архива memory/groups/window.jsonl
_POLL_SEC = 1.0
_HEARTBEAT_SEC = 10.0

_agent = None          # её дерево, загруженное в этот процесс
_life = None           # memory_life — память дерева
_desk: transport.Desk | None = None
_bot = None            # botapi.BotTransport | None
_speaker = "владелец"
_title = "Helene"
_agent_name = "Агент"
_tree: Path | None = None
_busy: dict = {"busy": False, "run": "", "since": 0.0}   # для единой модели состояния
_deliver_unspoken = True   # agent.deliver_unspoken в helene.json; см. _turn_in_window


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_tree(code_dir: Path, tree: Path, cfg: dict):
    """Поднять дерево агента в этом процессе.

    Порядок здесь не косметический:
      1. PRAXIS_BASE и ручки — ДО импорта: половина модулей читает среду на импорте;
      2. sitecustomize — LF-шим порта (на POSIX no-op), до любых записей;
      3. судьба `.env` рядом с кодом — тоже до импорта: на импорте `agent` зовёт
         `load_dotenv(override=True)`, и чужой файл перекрыл бы конфиг продукта;
      4. импорт `agent`;
      5. ручки ПОВТОРНО — страховка на случай, когда `.env` читать всё же попросили.
    Разбор ловушки — в шапке `boot.py`.
    """
    global _agent, _life
    knobs = boot.env_knobs(cfg)
    os.environ["PRAXIS_BASE"] = str(tree)
    boot.apply_env(knobs, where="до импорта")
    sys.path.insert(0, str(code_dir))
    try:
        import sitecustomize  # noqa: F401 — win32 LF-шим, no-op на POSIX
    except Exception:
        log.warning("sitecustomize дерева не загрузился — записи пойдут с CRLF", exc_info=True)
    boot.dotenv_gate(code_dir, cfg)
    boot.ensure_shell()
    import agent
    import memory_life
    boot.apply_env(knobs, where="после импорта")
    _agent, _life = agent, memory_life
    return agent, memory_life


def _dialogue(chat_id: str) -> tuple[list[dict], str]:
    """Разговор ролями: (история, то-на-что-она-отвечает-сейчас).

    Правило ЕЁ раннера, дословно (`_turns_to_dialogue` в mtproto_runner): граница
    проходит по её последней реплике; подряд идущие реплики одного автора склеиваются
    в один блок; она здесь ещё не говорила — ролей нет, и ход идёт сплошным текстом.
    Своя копия правила здесь потому, что живой раннер тащит за собой Telethon целиком,
    а правило — двадцать строк.
    """
    try:
        records = _life.hot_records(chat_id, _life.HOT_HARD_HI)
    except Exception:
        log.warning("горячий слой не прочитался [%s] — иду сплошным текстом",
                    chat_id, exc_info=True)
        return [], ""
    rows = [(r.get("direction") == "out", str(r.get("line") or ""))
            for r in records if str(r.get("line") or "").strip()]
    if not rows:
        return [], ""
    last_self = -1
    for i, (is_self, _text) in enumerate(rows):
        if is_self:
            last_self = i
    if last_self < 0:
        return [], ""
    history: list[dict] = []
    run_role, run_lines = "", []

    def flush() -> None:
        if run_lines:
            history.append({"role": run_role,
                            "content": (chr(10) * 2).join(run_lines).strip()})

    for is_self, text in rows[:last_self + 1]:
        role = "assistant" if is_self else "user"
        if role != run_role:
            flush()
            run_role, run_lines = role, []
        run_lines.append(text)
    flush()
    current = "\n".join(text for _is_self, text in rows[last_self + 1:])
    if not history or not current.strip():
        return [], ""
    return history, current


def _last_n() -> int:
    try:
        return max(1, int(os.getenv("PRAXIS_LAST_N", "80") or 80))
    except ValueError:
        return 80


# Подсказка в кадр (одобрена владельцем 02.09): слабые модели пишут ответ текстом,
# а под поднятым рычагом reply текст без руки — заметка себе, и слово теряется.
# Идёт в блок runtime continuity кадра через параметр orient, дерево не правится.
_WINDOW_ORIENT = ("Это окно Helene на компьютере владельца. Слово владельцу уходит "
                  "ТОЛЬКО рукой reply; текст без руки — заметка себе, до окна он не дойдёт.")


def _run_turn(chat_id: str, convo: str, speaker: str, ctx) -> "object | None":
    """Один её ход с уже собранным контекстом. -> envelope или None (упал)."""
    history, current = _dialogue(chat_id)
    orient = _WINDOW_ORIENT if chat_id == STREAM else ""
    try:
        return _agent.voice_turn_envelope(
            chat_id, convo, speaker, ctx=ctx, history=history, current_text=current,
            orient=orient)
    except Exception:
        log.exception("ход упал в дереве [%s]", chat_id)
        return None


def _close_run(envelope, chat_id: str, *, delivered_text: str = "",
               spoken_by_hand: int = 0, media_count: int = 0) -> None:
    """Закрыть durable-прогон так, как это делает живой раннер на границе.

    Без этого карточка хода вечно оставалась `running`, и КАЖДЫЙ старт руннера
    «восстанавливал» вчерашний успешный ход в paused. Ровно те же вызовы, что у
    mtproto: доставили текст мы — started → text_accepted → finalize; текста на
    границе нет (рука уже сказала, или она промолчала) — completed(silent=True),
    редьюсер сам сведёт расписки. deferred/failed не трогаем: их состояние
    принадлежит durable-механике и закрывается её путями.
    """
    run_id = str(getattr(envelope, "run_id", "") or "")
    if not run_id or getattr(envelope, "deferred", False) \
            or getattr(envelope, "failed", False):
        return
    try:
        if delivered_text:
            _agent.run_delivery_started(run_id, chat_id=chat_id,
                                        text_chars=len(delivered_text),
                                        media_count=int(media_count))
            _agent.run_delivery_text_accepted(run_id, text=delivered_text)
            _agent.run_delivery_finalize_recovered(run_id)
        else:
            reason = (f"reply hand delivered {spoken_by_hand} message(s); "
                      "boundary carries no text") if spoken_by_hand else \
                "agent chose silence"
            _agent.run_delivery_completed(run_id, silent=True,
                                          silent_reason=reason)
    except Exception:
        log.exception("прогон не закрылся расписками [%s]", run_id)


def _deliver_outbound(envelope, chat_id: str) -> int:
    """Медиа, спуленное ходом (`send_media`): документы/фото/аудио этого чата.

    В живом раннере это делает mtproto на исходящей границе; здесь — мы, тем же
    правилом: только то, что адресовано этому чату, и с распиской в лог.
    """
    delivered = 0
    for item in getattr(envelope, "outbound", ()) or ():
        target = str(getattr(item, "target_chat_id", "") or chat_id)
        try:
            if (_bot is not None and target != STREAM
                    and botapi.is_telegram_key(target)):
                receipt = _bot.deliver_file(
                    Path(item.path), chat_id=target,
                    caption=str(getattr(item, "caption", "") or ""),
                    media_kind=str(getattr(item, "kind", "document") or "document"),
                    voice_note=bool(getattr(item, "voice_note", False)))
            else:
                note = f"[файл] {Path(item.path).name} — {item.path}"
                caption = str(getattr(item, "caption", "") or "").strip()
                receipt = _desk.deliver(note + ("\n" + caption if caption else ""))
            delivered += 1
            log.info("медиа хода доставлено: %s", str(receipt)[:120])
        except Exception:
            log.exception("медиа хода не доставилось [%s]", target)
    return delivered


def handle_desk(message: str) -> None:
    """Одна записка из окна — один ход агента."""
    now = _now()
    source_id = f"window-{int(now.timestamp() * 1000)}"
    # Восприятие пишет память ДО кадра — как в живом раннере: кадр читает горячий
    # слой, и текущая реплика обязана быть в нём, иначе она отвечала бы на пустоту.
    _desk.archive(message, outgoing=False, now=now)
    _desk.life(message, direction="in", actor=_speaker, source_id=source_id, now=now)
    _turn_in_window(source_id, speaker=_speaker)


def _turn_in_window(source_id: str, *, speaker: str, birth: bool = False) -> None:
    """Ход в комнате окна по уже записанному в память входящему."""
    convo = "\n".join(_desk.lines(_last_n()))
    ctx = _agent.ChannelContext(chat_id=STREAM, is_dm=True, owner=True, known=True,
                                addressed=True, title=_title)
    _desk.sent.clear()
    started = time.time()
    _set_busy(True)
    envelope = None
    try:
        envelope = _run_turn(STREAM, convo, speaker, ctx)
    finally:
        _set_busy(False, str(getattr(envelope, "run_id", "") or ""))
    if envelope is None:
        _desk.deliver("⚠ ход не дошёл до конца — подробности в логе руннера.",
                      source_id=source_id)
        return
    spoken = list(_desk.sent)
    text = str(getattr(envelope, "text", "") or "").strip()
    run_id = str(getattr(envelope, "run_id", "") or "")
    media_count = _deliver_outbound(envelope, STREAM)
    if not spoken and not text and not media_count and (birth or _deliver_unspoken):
        # Слово написано текстом, а не рукой reply: под поднятым рычагом это
        # заметка себе, и до окна она не дошла бы. Продукт по умолчанию
        # доставляет её на границе (agent.deliver_unspoken в helene.json), потому
        # что слабые модели теряют так каждое третье слово; при рождении — всегда.
        text = _unspoken_note()
        if text:
            log.info("%s: слово пришло заметкой хода — доставляю на границе",
                     "рождение" if birth else "ход")
    if getattr(envelope, "deferred", False):
        # Durable-чекпойнт придержал ход до подтверждения побочного эффекта. Молчать
        # об этом нельзя: окно выглядело бы зависшим, а ход на самом деле жив.
        _desk.deliver("⏸ ход приостановлен на чекпойнте и ждёт подтверждения "
                      f"(прогон {run_id}).", source_id=source_id)
    elif getattr(envelope, "failed", False):
        _desk.deliver(f"⚠ ход не состоялся (прогон {run_id or 'без id'}) — "
                      "подробности в карточке хода.", source_id=source_id)
    elif not spoken and text:
        # Рычаг речи опущен (или ход закрылся текстом): реплика — возврат хода,
        # доставляем её мы. Под поднятым рычагом сюда не попадаем: слово ушло рукой.
        _desk.deliver(text, source_id=source_id)
    elif not spoken and not text and not media_count:
        log.info("ход %s: она промолчала (это её решение, не сбой)", run_id)
    _close_run(envelope, STREAM,
               delivered_text=(text if not spoken else ""),
               spoken_by_hand=len(spoken), media_count=media_count)
    log.info("ход %s [окно]: %.1f с, реплик рукой %d%s", run_id or "—",
             time.time() - started, len(spoken),
             "" if not media_count else f", медиа {media_count}")


def handle_owner_note(chat_id: str, message: str) -> None:
    """Реплика владельца ИЗ ОКНА в telegram-комнату (записка `__to__` композера).

    Окно — ещё одна дверь владельца в любую его комнату (слово владельца 31.08):
    реплика записывается его именем в память комнаты, ход идёт там же, ответ
    уезжает в Telegram. В Telegram сама реплика не отправляется — бот не имеет
    права говорить чужими словами, и расписки это отличие сохраняют (source
    события жизни = window, не botapi).
    """
    if _bot is None:
        log.warning("записка адресована «%s», а бота нет — некуда везти", chat_id)
        return
    now = _now()
    _bot.rooms.describe(chat_id, title=_room_title(chat_id),
                        is_dm=bool(_bot.rooms.meta(chat_id).get("is_dm", True)),
                        sender=(_speaker, _bot.owner_id))
    _bot.rooms.record(chat_id, message, outgoing=False, sender=_speaker,
                      source_id=f"desk-{int(now.timestamp() * 1000)}",
                      ts=now.timestamp(), source="window")
    handle_bot(chat_id)


def _room_title(chat_id: str) -> str:
    """Имя комнаты, переживающее рестарт: карта в RAM → контакт-бук бота (диск).

    Без этого личка после перезапуска руннера звалась числом id — в заголовке,
    панели и записи хода, — пока Telegram не приносил новое сообщение.
    """
    known = str(_bot.rooms.meta(chat_id).get("title") or "")
    if known:
        return known
    peer = botapi.peer_thread(chat_id)[0]
    label = _bot.contacts.label(peer)
    return label if label != peer else ""


def handle_bot(chat_id: str) -> None:
    """Ход в бот-чате: сообщение уже в памяти (его записал поток приёма)."""
    meta = _bot.rooms.meta(chat_id)
    is_dm = bool(meta.get("is_dm", True))
    sender_name, sender_id = meta.get("last_sender") or ("кто-то", "")
    owner = bool(_bot.owner_id) and str(sender_id) == str(_bot.owner_id)
    convo = "\n".join(_bot.rooms.lines(chat_id, _last_n()))
    if not convo.strip():
        return
    ctx = _agent.ChannelContext(chat_id=chat_id, is_dm=is_dm, owner=owner,
                                known=True, addressed=True,
                                title=_room_title(chat_id) or str(chat_id))
    if is_dm:
        _bot.typing(chat_id)
    _bot.sent_now.clear()
    started = time.time()
    _set_busy(True)
    try:
        envelope = _run_turn(chat_id, convo, sender_name, ctx)
    finally:
        _set_busy(False)
    if envelope is None:
        if is_dm and owner:
            try:
                _bot.deliver_text(chat_id, "⚠ ход не дошёл до конца — "
                                           "подробности в логе руннера.")
            except Exception:
                log.exception("не доложила владельцу о падении хода")
        return
    spoken = [t for c, t in _bot.sent_now if c == str(chat_id)]
    text = str(getattr(envelope, "text", "") or "").strip()
    run_id = str(getattr(envelope, "run_id", "") or "")
    media_count = _deliver_outbound(envelope, chat_id)
    if getattr(envelope, "deferred", False) or getattr(envelope, "failed", False):
        # Чужим людям внутренности не выкладываем — как в живом раннере: сбой
        # виден в карточке хода и логе, владельцу в личке — словами.
        state = "приостановлен" if getattr(envelope, "deferred", False) else "не состоялся"
        log.warning("ход %s [бот %s]: %s", run_id or "—", chat_id, state)
        if is_dm and owner:
            try:
                _bot.deliver_text(chat_id, f"⚠ ход {state} (прогон {run_id}).")
            except Exception:
                log.exception("не доложила владельцу о сбое хода")
    delivered_boundary = ""
    if not spoken and text:
        try:
            _bot.deliver_text(chat_id, text)
            delivered_boundary = text
        except Exception:
            log.exception("возврат хода не доставился [%s]", chat_id)
    elif not spoken and not text and not media_count:
        log.info("ход %s [бот %s]: она промолчала (это её решение, не сбой)",
                 run_id, chat_id)
    _close_run(envelope, chat_id, delivered_text=delivered_boundary,
               spoken_by_hand=len(spoken), media_count=media_count)
    log.info("ход %s [бот %s]: %.1f с, реплик рукой %d%s", run_id or "—", chat_id,
             time.time() - started, len(spoken),
             "" if not media_count else f", медиа {media_count}")


def _write_anatomy(tree: Path, cfg: dict) -> None:
    """Снимок устройства для вкладки «Устройство» Пульта — КОДОМ, не пересказом.

    Прозрачность — стержень продукта (слово владельца 31.08: «ясный список тулов,
    что они делают, чтение скиллов и их место, условие вызова, нутрянка
    простыми словами»). Список рук берётся у ЕЁ сборщика offered_tools_for —
    того же, что собирает руки модели (контракт A1: один ответ на вопрос «что
    у неё есть»). Описания — те же байты, что читает модель: это и есть
    «условие вызова» руки, других условий нет.
    """
    try:
        ctx = _agent.ChannelContext(chat_id=STREAM, is_dm=True, owner=True,
                                    known=True, addressed=True, title=_title)
        rows = []
        for tool in _agent.offered_tools_for(ctx):
            schema = tool.get("input_schema") or {}
            rows.append({
                "name": tool.get("name") or f"[{tool.get('type') or 'hosted'}]",
                "desc": str(tool.get("description") or ""),
                "params": sorted((schema.get("properties") or {}).keys()),
                "required": list(schema.get("required") or ()),
            })
        skills_index = ""
        try:
            skills_index = (tree / "soul" / "skills" / "INDEX.md").read_text(
                encoding="utf-8")
        except OSError:
            pass
        telegram = dict(cfg.get("telegram") or {})
        _write_json(tree / "memory" / ".state" / "anatomy.json", {
            "written_at": _now().isoformat(timespec="seconds"),
            "agent_name": boot.agent_name(cfg),
            "owner_name": boot.owner_name(cfg),
            "model": {k: v for k, v in (cfg.get("model") or {}).items()
                      if k not in ("key", "api_key")},
            "knobs": boot.env_knobs(cfg),
            "transports": (["окно Helene"]
                           + (["Telegram-аккаунт"] if str(telegram.get("mode") or "bot") == "account"
                              and telegram.get("api_id") else
                              ["Telegram-бот"] if telegram.get("bot_token") else [])),
            "sandbox": _sandbox_state(),
            "tools": rows,
            "skills_index": skills_index,
        })
        log.info("устройство: %d рук записано для окна", len(rows))
    except Exception:
        log.exception("анатомия не записалась (продукт работает дальше)")


def _heartbeat_forever(inbox: Path) -> None:
    """Квитанция читателя — фоном и с занятостью.

    deskapp по ней решает две вещи: жив ли руннер (возраст `at`) и думает ли агент
    прямо сейчас (`busy`). Раньше квитанция писалась из главного цикла — и во
    время долгого хода старела, так что окно считало живой руннер мёртвым.
    """
    while True:
        try:
            _write_json(inbox / ".reader.json", {
                "pid": os.getpid(), "at": time.time(),
                "busy": bool(_busy["busy"]), "run": str(_busy["run"] or ""),
                "since": float(_busy["since"] or 0.0)})
        except Exception:
            log.debug("квитанция читателя не записалась", exc_info=True)
        time.sleep(_HEARTBEAT_SEC)


def _set_busy(on: bool, run: str = "") -> None:
    _busy["busy"], _busy["run"] = bool(on), str(run or "")
    _busy["since"] = time.time() if on else 0.0
    if _tree is None:
        return
    try:
        inbox = Path(_tree) / "memory" / ".control" / "desk_inbox"
        _write_json(inbox / ".reader.json", {
            "pid": os.getpid(), "at": time.time(),
            "busy": bool(_busy["busy"]), "run": _busy["run"], "since": _busy["since"]})
    except Exception:
        pass


_BIRTH_NOTE = (
    "Это твой первый запуск на устройстве {device}. Тебе предлагается осмотреться "
    "и познакомиться.")


def _unspoken_note() -> str:
    """Заметка последнего хода окна, если ход кончился без реплики.

    Под поднятым рычагом reply её текст без руки — заметка себе, не слово. При
    рождении это ровно тот текст, которым она представилась: отдать его на
    границе честнее, чем оставить первый запуск немым. Читаем её же запись хода.
    """
    try:
        path = Path(_tree) / "memory" / ".state" / "turns.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, TypeError):
        return ""
    for line in reversed(lines[-5:]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if str(row.get("chat_id") or "") != STREAM:
            continue
        if str(row.get("held") or "") == "unspoken":
            return str(row.get("note") or "").strip()
        return ""
    return ""


def handle_birth() -> None:
    """Первый ход при рождении: агент представляется сам, не дожидаясь вопроса.

    Это обычный ход в комнате окна, только записка приходит не от владельца, а от
    Helene: так она честно лежит в памяти как событие первого запуска, а ответ
    идёт тем же путём, что и любой другой (рука reply в окно; возврат хода —
    доставляем мы). Отметка born.json защищает от повторного рождения.
    """
    note = _BIRTH_NOTE.format(device=platform.node() or "этот компьютер")
    now = _now()
    source_id = f"birth-{int(now.timestamp() * 1000)}"
    _desk.archive(note, outgoing=False, now=now, sender="Helene")
    _desk.life(note, direction="in", actor="Helene", source_id=source_id, now=now)
    _turn_in_window(source_id, speaker="Helene", birth=True)


def _maybe_birth(tree: Path) -> None:
    marker = tree / "memory" / ".state" / "born.json"
    if marker.exists():
        return
    try:
        import llm
        if not llm.configured():
            log.info("рождение отложено: мозг не настроен — представлюсь при первом "
                     "запуске с ключом")
            return
    except Exception:
        return
    try:
        handle_birth()
        _write_json(marker, {"at": _now().isoformat(timespec="seconds"),
                             "agent": _agent_name, "owner": _speaker})
        log.info("рождение: первый ход состоялся")
    except Exception:
        log.exception("первый ход при рождении упал (повторю на следующем запуске)")


def _read_message(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _sandbox_state() -> dict:
    try:
        import fence
        return fence.state()
    except Exception:
        return {"enabled": False, "container": False, "reason": "модуль недоступен"}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def main() -> None:
    global _desk, _bot, _speaker, _title, _agent_name, _tree, _deliver_unspoken
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8"))

    tree = Path(os.environ.get("HELENE_TREE") or os.environ.get("PRAXIS_DESK_TREE")
                or cfg.get("tree") or "data")
    if not tree.is_absolute():
        tree = (config_path.parent / tree).resolve()
    _tree = tree
    boot.ensure_layout(tree, cfg)

    code_raw = str(cfg.get("code") or "../../live")
    code_dir = Path(code_raw)
    if not code_dir.is_absolute():
        code_dir = (config_path.parent / code_dir).resolve()
    if not (code_dir / "agent.py").is_file():
        # Дерево — это и есть харнесс. Без него нечего запускать, и подменять её ход
        # своим («кадр-лайт») значило бы держать вторую реализацию продукта.
        log.error("дерева агента нет: %s — руннер не поднимется", code_dir)
        raise SystemExit(2)

    owner = cfg.get("owner") or {}
    _speaker = boot.owner_name(cfg)
    _title = str(owner.get("room") or "Helene")
    _agent_name = boot.agent_name(cfg)
    _deliver_unspoken = bool((cfg.get("agent") or {}).get("deliver_unspoken", True))

    log.info("%s", boot.project_brain(tree, cfg))
    agent, memory_life = _load_tree(code_dir, tree, cfg)
    _desk = transport.Desk(tree, STREAM, _speaker, _title, memory_life=memory_life,
                           agent_name=_agent_name)
    transport.install(agent, _desk)
    # Песочница: shell в AppContainer, файловые руки — в папке Helene. Не вышло —
    # причина в анатомии, продукт работает дальше.
    try:
        import fence
        fence.install(agent, tree, cfg)
    except Exception:
        log.exception("песочница не поднялась — руки без ограды")
    tg = dict(cfg.get("telegram") or {})
    if str(tg.get("mode") or "bot") == "account" and tg.get("api_id") and tg.get("api_hash"):
        # Свой аккаунт агента по MTProto: сессия после входа в настройках.
        try:
            import mtproto
            _bot = mtproto.MtprotoTransport(agent, tree, memory_life, cfg)
            _bot.start()
            botapi.install(agent, _desk, _bot)
        except Exception:
            _bot = None
            log.exception("аккаунт Telegram не поднялся — работаю только окном")
    elif str(tg.get("bot_token") or "").strip():
        # Бот — опция: нет токена, нет и попытки. Ошибка старта бота не роняет
        # окно: продукт остаётся рабочим локально, а причина названа в логе.
        try:
            _bot = botapi.BotTransport(agent, tree, memory_life, cfg)
            _bot.start()
            botapi.install(agent, _desk, _bot)
        except Exception:
            _bot = None
            log.exception("Telegram-бот не поднялся — работаю только окном")
    try:
        import llm
        log.info("мозг: %s", "готов" if llm.configured() else "НЕ настроен (нет ключа)")
    except Exception:
        log.exception("мозг не опросился")
    _write_anatomy(tree, cfg)
    try:
        # Порт как есть: живой раннер тоже переигрывает прерванные ходы на старте.
        # Окно закрыли посреди хода — она доводит его при следующем запуске.
        recovered = agent.recover_durable_state()
        if recovered:
            log.warning("восстановлено прерванных ходов: %d", len(recovered))
    except Exception:
        log.exception("восстановление прерванных ходов не прошло (работаю дальше)")

    inbox = tree / "memory" / ".control" / "desk_inbox"
    processed = inbox / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    log.info("локальный харнесс: дерево данных %s · код %s · транспорты: окно%s",
             tree, code_dir, "" if _bot is None else " + бот @" + _bot.username)
    import threading
    threading.Thread(target=_heartbeat_forever, args=(inbox,), name="heartbeat",
                     daemon=True).start()
    # Рождение — после того, как всё поднято и квитанция читателя уже пишется:
    # окно видит «думает», а не мёртвый руннер, пока идёт первый ход.
    _maybe_birth(tree)
    while True:
        for path in sorted(inbox.glob("*.md")):
            if path.name.startswith(".tmp-"):
                continue
            try:
                message = _read_message(path)
                os.replace(path, processed / path.name)
            except OSError:
                continue
            if not message:
                continue
            # `<stamp>__to__<комната>.md` — адресная записка композера: реплика
            # владельца в telegram-комнату. Без суффикса — комната окна.
            target = ""
            if "__to__" in path.stem:
                target = path.stem.split("__to__", 1)[1]
            try:
                if target and target != STREAM:
                    handle_owner_note(target, message)
                else:
                    handle_desk(message)
            except Exception:
                log.exception("ход окна упал")
        while _bot is not None:
            chat_id = _bot.pop_pending()
            if chat_id is None:
                break
            try:
                handle_bot(chat_id)
            except Exception:
                log.exception("ход бота упал [%s]", chat_id)
        time.sleep(_POLL_SEC)


if __name__ == "__main__":
    main()
