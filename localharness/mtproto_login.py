# -*- coding: utf-8 -*-
"""Вход агента в Telegram своим аккаунтом (MTProto), двумя шагами, без
интерактивного ввода — его зовёт окно настроек через оболочку.

  python mtproto_login.py --session <путь без расширения> --api-id N --api-hash H status
  python mtproto_login.py … send --phone +79990000000
  python mtproto_login.py … code --phone +7… --code 12345 [--password <2FA>]
  python mtproto_login.py … logout

Ответ — одна строка JSON в stdout: {"ok": bool, "state": "authorized" |
"code_sent" | "unauthorized", "username": …, "id": …, "error": …}.
Хэш кода между шагами лежит рядом с сессией в файле .codehash.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _out(**payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def main(args) -> None:
    from telethon import TelegramClient
    from telethon.errors import (FloodWaitError, PhoneCodeExpiredError,
                                 PhoneCodeInvalidError, SessionPasswordNeededError)

    session = Path(args.session)
    session.parent.mkdir(parents=True, exist_ok=True)
    codehash = session.with_suffix(".codehash")
    client = TelegramClient(str(session), int(args.api_id), args.api_hash)
    await client.connect()
    try:
        if args.step == "logout":
            try:
                await client.log_out()
            except Exception:
                pass
            for p in (session.with_suffix(".session"), codehash):
                try:
                    p.unlink()
                except OSError:
                    pass
            _out(ok=True, state="unauthorized")
            return
        if await client.is_user_authorized():
            me = await client.get_me()
            _out(ok=True, state="authorized", username=me.username or "",
                 id=me.id, name=" ".join(x for x in (me.first_name, me.last_name) if x))
            return
        if args.step == "status":
            _out(ok=True, state="unauthorized")
            return
        if args.step == "send":
            sent = await client.send_code_request(args.phone)
            codehash.write_text(sent.phone_code_hash, encoding="utf-8")
            _out(ok=True, state="code_sent")
            return
        if args.step == "code":
            phash = codehash.read_text(encoding="utf-8").strip() if codehash.exists() else ""
            try:
                await client.sign_in(args.phone, args.code, phone_code_hash=phash)
            except SessionPasswordNeededError:
                if not args.password:
                    _out(ok=False, state="password_needed",
                         error="у аккаунта включён облачный пароль — введи его")
                    return
                await client.sign_in(password=args.password)
            me = await client.get_me()
            try:
                codehash.unlink()
            except OSError:
                pass
            _out(ok=True, state="authorized", username=me.username or "", id=me.id,
                 name=" ".join(x for x in (me.first_name, me.last_name) if x))
            return
        _out(ok=False, error=f"неизвестный шаг {args.step}")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        _out(ok=False, state="code_sent", error=f"код не подошёл: {exc.__class__.__name__}")
    except FloodWaitError as exc:
        _out(ok=False, error=f"Telegram просит подождать {exc.seconds} с")
    except Exception as exc:
        _out(ok=False, error=f"{exc.__class__.__name__}: {exc}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--api-id", required=True)
    ap.add_argument("--api-hash", required=True)
    ap.add_argument("--phone", default="")
    ap.add_argument("--code", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("step", choices=["status", "send", "code", "logout"])
    parsed = ap.parse_args()
    try:
        asyncio.run(main(parsed))
    except Exception as exc:  # даже импорт Telethon — честной строкой, не трейсом
        _out(ok=False, error=f"{exc.__class__.__name__}: {exc}")
