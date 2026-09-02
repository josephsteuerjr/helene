# -*- coding: utf-8 -*-
"""Окно Frame как транспорт агента: тот же шов, на котором висит Telethon.

`agent._TELETHON` — обычный словарь вызываемых, который живой раннер наполняет на
старте (mtproto_runner: reply, send_message, read_chat, get_id, transport_state…).
Ни одна её рука не знает, кто там лежит: она зовёт «отправить» и получает расписку.
Значит десктопный продукт не нуждается ни в одной правке её кода — он просто кладёт
в этот словарь свои функции. Это тот же приём, что труба `praxis.desk.v1` для
оболочки: один шов, за которым может стоять что угодно.

Что здесь есть по-настоящему:
  * доставка её реплики в Пульт (архив комнаты + событие жизни + расписка ей);
  * чтение своей же комнаты (`read_context`, `read_chat`, поиск по переписке);
  * честный отказ на адресатов, которых в этом продукте нет.

Чего здесь НЕТ и почему: крючки Telegram-специфики (вступить в чат, реакции, аватар,
модерация, telegram_account) НЕ регистрируются. Её тулы на отсутствующем крючке
отвечают «Telegram-мост сейчас недоступен» — это правда об этом продукте, и она
лучше, чем заглушка, которая делает вид, что действие состоялось.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("frame.transport")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as sink:
        sink.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


class Desk:
    """Одна комната продукта: разговор владельца с ней в окне Пульта."""

    def __init__(self, tree: Path, stream: str, speaker: str, title: str,
                 memory_life=None, agent_name: str = "Агент"):
        self.tree = Path(tree)
        self.stream = str(stream)
        self.speaker = str(speaker)
        self.title = str(title)
        self.agent_name = str(agent_name)
        self._life = memory_life
        self.sent: list[str] = []          # что ушло рукой в ЭТОМ ходе

    # ------------------------------------------------------------- запись
    def archive(self, text: str, *, outgoing: bool, now: dt.datetime | None = None,
                sender: str = "") -> None:
        """Лента комнаты в её формате: memory/groups/<поток>.jsonl + реестр состояния.

        Пульт читает комнаты именно отсюда (deskd.readers.chats/chat_tail), а её
        `group_context` — из того же архива. Один файл на обоих читателей.
        """
        stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        row = {"timestamp": stamp, "outgoing": bool(outgoing), "text": str(text)}
        if not outgoing:
            row["sender_name"] = sender or self.speaker
        archive = self.tree / "memory" / "groups" / (self.stream + ".jsonl")
        _append_jsonl(archive, row)
        try:
            stat = archive.stat()
            with archive.open(encoding="utf-8") as src:
                count = sum(1 for _ in src)
        except OSError:
            stat, count = None, 0
        _write_json(self.tree / "memory" / ".state" / "group_context"
                    / (self.stream + ".json"),
                    {"peer_id": self.stream,
                     "archive": "memory/groups/" + self.stream + ".jsonl",
                     "message_count": count, "participant_count": 2, "topic_count": 0,
                     "archive_mtime_ns": stat.st_mtime_ns if stat else 0,
                     "archive_size": stat.st_size if stat else 0})

    def life(self, text: str, *, direction: str, actor: str, source_id: str,
             now: dt.datetime | None = None) -> None:
        """Реплика — событие жизни (kind=conversation_message) в ЕЁ памяти дерева.

        Так же, как это делает живой транспорт: восприятие пишет память, кадр читает
        горячий слой. Без этого разговор жил бы только в архиве комнаты, а её кадр
        собирался бы из пустоты.
        """
        if self._life is None:
            return
        moment = now or dt.datetime.now(dt.timezone.utc)
        try:
            self._life.record_message(
                self.stream, text, actor=actor, direction=direction, source="window",
                source_id=source_id, is_dm=True, ts=moment.timestamp(),
                dedupe_key=f"window:{source_id}:{direction}")
        except Exception:
            log.exception("событие жизни не записалось (ход продолжается)")

    # ------------------------------------------------------------- чтение
    def rows(self, limit: int = 200) -> list[dict]:
        archive = self.tree / "memory" / "groups" / (self.stream + ".jsonl")
        try:
            with archive.open(encoding="utf-8") as src:
                lines = src.readlines()[-max(1, int(limit)):]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def lines(self, limit: int = 200) -> list[str]:
        """Лента строками «Имя: текст» — тот же вид, в каком её видит живой раннер."""
        out = []
        for row in self.rows(limit):
            who = self.agent_name if row.get("outgoing") else (row.get("sender_name")
                                                               or self.speaker)
            text = str(row.get("text") or "").strip()
            if text:
                out.append(f"{who}: {text}")
        return out

    # ------------------------------------------------------------ доставка
    def deliver(self, text: str, *, source_id: str = "", label: str = "") -> str:
        """Её слово доехало до окна. Возвращаем расписку в том же виде, что транспорт.

        Расписка — не косметика: рука `reply` дописывает к ней подсказку про `end_turn`,
        а `send_message` отличает по типу отказ от квитанции. Пустая или невнятная
        расписка сделала бы её ход слепым к тому, состоялась ли отправка.
        """
        now = dt.datetime.now(dt.timezone.utc)
        body = str(text or "")
        self.archive(body, outgoing=True, now=now)
        self.life(body, direction="out", actor=self.agent_name,
                  source_id=source_id or f"deliver-{int(time.time() * 1000)}", now=now)
        self.sent.append(body)
        # Канал назван в расписке НЕ для красоты: когда транспортов стало два, а
        # адресат один и тот же человек, безадресное «Отправила → владелец» позволило
        # ей честно поверить, что слово ушло в Telegram, когда оно легло в окно.
        return f"Отправлено → {label or self.title} (окно Frame, id {len(self.sent)})"


def install(agent_mod, desk: Desk) -> None:
    """Положить Пульт в `agent._TELETHON`. Вызывается один раз на старте раннера."""

    hooks = agent_mod._TELETHON

    def _reply(chat_id, text, reply_to="") -> str:
        if str(chat_id) != desk.stream:
            return agent_mod.DirectSendRefusal(
                f"не отправила: в этом продукте одна комната — «{desk.title}», "
                f"а адрес «{chat_id}» ей не принадлежит.")
        return desk.deliver(text, label=desk.speaker)

    def _send_message(to, text) -> str:
        target = str(to or "").strip()
        if target in ("", desk.stream, desk.speaker, desk.title):
            return desk.deliver(text, label=desk.speaker)
        # ⚠ Отказ СТРОКОЙ особого типа, а не исключением и не обычной квитанцией:
        # `DirectSendRefusal` — её способ отличить «не ушло» от «ушло», не нюхая текст.
        # Обычная строка здесь засчиталась бы как доставка, и её запись хода соврала бы.
        return agent_mod.DirectSendRefusal(
            f"не отправила: наружу писать нечем — в этом продукте нет транспорта до "
            f"«{target}». Есть только окно Пульта; чтобы сказать это владельцу, отвечай "
            f"обычной рукой ответа.")

    def _send_file(path, caption="", to="", media_kind="document",
                   voice_note=False) -> str:
        src = Path(str(path))
        if not src.is_file():
            return f"Нет файла {path}."
        note = f"[файл] {src.name} — {src}"
        if str(caption or "").strip():
            note += "\n" + str(caption).strip()
        # v1: файл остаётся на месте, в окно уезжает названный путь. Показ вложений
        # внутри чата Пульта — отдельная работа; обещать её распиской нельзя.
        return desk.deliver(note, label=desk.speaker)

    def _fetch_context(chat_id, limit: int = 50) -> str:
        if str(chat_id) != desk.stream:
            return "(нет такого чата)"
        return "\n".join(desk.lines(int(limit)))

    def _read_chat(chat_ref, limit: int = 30) -> str:
        if str(chat_ref) not in (desk.stream, desk.title, desk.speaker):
            return ("(не нашла такой чат — в этом продукте одна комната: "
                    f"«{desk.title}»)")
        return "\n".join(desk.lines(int(limit)))

    def _search_chats(query: str) -> str:
        needle = str(query or "").strip().lower()
        if not needle or needle in desk.title.lower() or needle in desk.stream.lower():
            return f"{desk.title}: {desk.stream}"
        return "(ничего не нашла — здесь одна комната: " + desk.title + ")"

    def _search_private_messages(query: str, limit: int = 20) -> str:
        needle = str(query or "").strip().lower()
        if not needle:
            return "Нужна непустая строка поиска."
        hits = [line for line in desk.lines(2000) if needle in line.lower()]
        if not hits:
            return "(ничего не нашла)"
        return "\n".join(hits[-max(1, int(limit)):])

    def _get_id(name_or_username: str):
        ref = str(name_or_username or "").strip()
        if ref in (desk.stream, desk.title, desk.speaker):
            return desk.stream
        return None

    hooks["reply"] = _reply
    hooks["send_message"] = _send_message
    hooks["send_file"] = _send_file
    hooks["fetch_context"] = _fetch_context
    hooks["read_chat"] = _read_chat
    hooks["search_chats"] = _search_chats
    hooks["search_private_messages"] = _search_private_messages
    hooks["get_id"] = _get_id
    hooks["resolve_entity"] = _get_id
    # Сенсор транспорта для сторожа тишины: локальное окно всегда «на связи», и
    # окна намеренного разрыва (как у Telegram при перелогине) здесь не бывает.
    hooks["transport_state"] = lambda: {"connected": True, "intentional_window": False}
    log.info("транспорт: Пульт вложен в её шов _TELETHON (%d крючка)", len(hooks))
