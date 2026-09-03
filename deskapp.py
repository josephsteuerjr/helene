# -*- coding: utf-8 -*-
"""Helene — труба окна: читает дерево агента и отдаёт его окну. Референс — Claude Code.

Отдельный демон по форме Атланты: свой процесс, свой порт, дерево агента смонтировано
только на чтение. Пишет ровно в два места: memory/.control/desk_inbox/ (mid-turn
сигнал — читается её раннером после мержа кандидата) и workspace/inbox/ (записка —
честная деградация, пока кандидат не смёржен).

Запуск:
  HELENE_TREE=/data HELENE_TOKEN=... python deskapp.py [port]
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deskd import readers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("frame.desk")

# UI окна — сборка Vite (app/dist); в экспортной поставке build_dist.py кладёт
# её в app/static. Старой папки static больше нет.
_HERE = Path(__file__).resolve().parent
STATIC = next((d for d in (_HERE / "static", _HERE / "app" / "dist", _HERE.parent / "app" / "dist")
               if (d / "index.html").is_file()), _HERE / "static")
TOKEN = (os.environ.get("HELENE_TOKEN") or os.environ.get("PRAXIS_DESK_TOKEN") or "").strip()
COOKIE = "desk_key"
_SSE_CLIENTS: set[asyncio.Queue] = set()
_WATCH_INTERVAL = 1.5


# ------------------------------------------------------------- телефон
# Спаривание по QR: окно просит одноразовую пару (с петли), телефон открывает
# ссылку /m/?pair=<токен> и меняет токен на свой ключ устройства. Токен живёт
# десять минут и годится ДВАЖДЫ: на iPhone страница в Safari и установленное
# на экран «Домой» приложение — разные хранилища, и второй обмен нужен ровно
# для него. Ключи устройств лежат хэшами в memory/.state/devices.json.

_PAIRS: dict[str, dict] = {}
_PAIR_TTL = 600
_PAIR_USES = 2


def _devices_path() -> Path:
    return readers.tree() / "memory" / ".state" / "devices.json"


def _devices() -> list[dict]:
    try:
        data = json.loads(_devices_path().read_text(encoding="utf-8"))
        return [d for d in data.get("devices", []) if isinstance(d, dict)]
    except (OSError, ValueError):
        return []


def _save_devices(rows: list[dict]) -> None:
    path = _devices_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    tmp.write_text(json.dumps({"devices": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _key_hash(key: str) -> str:
    import hashlib
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()


def _device_ok(supplied: str) -> bool:
    if not supplied:
        return False
    h = _key_hash(supplied)
    return any(secrets.compare_digest(h, str(d.get("hash") or "")) for d in _devices())


def _is_loopback(request: web.Request) -> bool:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    host = str(peer[0]) if peer else ""
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _authorized(request: web.Request) -> bool:
    supplied = request.query.get("key") or request.cookies.get(COOKIE) or ""
    if TOKEN and secrets.compare_digest(supplied, TOKEN):
        return True
    if _device_ok(supplied):
        return True
    if not TOKEN and _is_loopback(request):
        return True  # своя машина без токена — как раньше
    return False


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":  # CORS preflight нативной оболочки — без ключа
        return web.Response(status=204, headers=_CORS)
    if not _authorized(request):
        return web.Response(status=403, text="нет ключа", headers=_CORS)
    response = await handler(request)
    response.headers.update(_CORS)
    supplied = request.query.get("key") or ""
    if supplied and ((TOKEN and supplied == TOKEN) or _device_ok(supplied)):
        response.set_cookie(COOKIE, supplied, max_age=365 * 24 * 3600,
                            httponly=True, samesite="Lax")
    return response


def _new_pair() -> dict:
    now = time.time()
    for token, row in list(_PAIRS.items()):
        if row["expires"] < now or row["uses"] <= 0:
            _PAIRS.pop(token, None)
    token = secrets.token_urlsafe(24)
    _PAIRS[token] = {"expires": now + _PAIR_TTL, "uses": _PAIR_USES}
    return {"token": token, "path": "/m/?pair=" + token,
            "expires_in": _PAIR_TTL, "uses": _PAIR_USES}


def _revoke_device(wanted: str) -> dict:
    rows = [d for d in _devices() if str(d.get("id")) != str(wanted or "")]
    _save_devices(rows)
    return {"left": len(rows)}


def _device_rows() -> list[dict]:
    return [{k: d.get(k) for k in ("id", "name", "created")} for d in _devices()]


async def api_pair_new(request):
    """Новая пара — только со своей машины (окно программы)."""
    if not _is_loopback(request):
        raise web.HTTPForbidden(text="пару выдаёт только окно на этой машине")
    return _json(_new_pair())


async def api_pair_redeem(request):
    """Телефон меняет токен на ключ устройства. Токен годится дважды (iPhone)."""
    token = str(request.query.get("token") or "")
    row = _PAIRS.get(token)
    if not row or row["expires"] < time.time() or row["uses"] <= 0:
        raise web.HTTPForbidden(text="код устарел или уже использован — покажи QR заново")
    row["uses"] -= 1
    key = "dk-" + secrets.token_urlsafe(30)
    ua = str(request.headers.get("User-Agent") or "")
    kind = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua
            else "Android" if "Android" in ua else "телефон")
    rows = _devices()
    rows.append({"id": secrets.token_hex(6), "name": kind, "hash": _key_hash(key),
                 "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    _save_devices(rows)
    anatomy = readers.anatomy() or {}
    response = _json({"key": key, "agent": anatomy.get("agent_name") or "Агент",
                      "uses_left": row["uses"]})
    response.set_cookie(COOKIE, key, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    return response


async def api_devices(request):
    if not _is_loopback(request):
        raise web.HTTPForbidden(text="список устройств — только с этой машины")
    return _json([{k: d.get(k) for k in ("id", "name", "created")} for d in _devices()])


async def api_device_revoke(request):
    if not _is_loopback(request):
        raise web.HTTPForbidden(text="отвязать — только с этой машины")
    payload = await request.json()
    return _json(_revoke_device(str(payload.get("id") or "")))


# ------------------------------------------------------------- PWA /m/
_HERE_DIR = Path(__file__).resolve().parent
MOBILE = next((d for d in (_HERE_DIR / "mobile", _HERE_DIR.parent / "mobile" / "dist")
               if (d / "index.html").is_file()), _HERE_DIR / "mobile")


async def mobile_index(request):
    if not (MOBILE / "index.html").is_file():
        raise web.HTTPNotFound(text="мобильная страница не собрана")
    return web.FileResponse(MOBILE / "index.html", headers={"Cache-Control": "no-cache"})


async def mobile_manifest(request):
    """Манифест PWA: start_url несёт токен пары, чтобы установленное на экран
    «Домой» приложение могло обменять его второй раз (iPhone)."""
    pair = str(request.query.get("pair") or "")
    anatomy = readers.anatomy() or {}
    name = str(anatomy.get("agent_name") or "Агент")
    start = "/m/?pair=" + pair if pair else "/m/"
    manifest = {
        "name": name, "short_name": name, "start_url": start, "scope": "/m/",
        "display": "standalone", "background_color": "#fdfcfa", "theme_color": "#fdfcfa",
        "icons": [{"src": "/m/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/m/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }
    return web.json_response(manifest, content_type="application/manifest+json",
                             dumps=lambda d: json.dumps(d, ensure_ascii=False))


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _json(data) -> web.Response:
    return web.json_response(data, dumps=lambda d: json.dumps(d, ensure_ascii=False))


# ------------------------------------------------------------------ API

async def api_runs(request):
    kind = request.query.get("kind") or ""
    before = request.query.get("before") or ""
    limit = min(200, int(request.query.get("limit") or 80))
    return _json(await asyncio.to_thread(readers.list_runs, limit, kind, before))


async def api_run(request):
    run_id = request.match_info["run_id"]
    detail = await asyncio.to_thread(readers.run_detail, run_id)
    if not detail:
        raise web.HTTPNotFound(text="нет такого прогона")
    return _json(detail)


async def api_pulse(request):
    return _json(await asyncio.to_thread(readers.pulse))


async def api_errors(request):
    return _json(await asyncio.to_thread(readers.errors))


async def api_board(request):
    return _json(await asyncio.to_thread(readers.board))


async def api_agenda(request):
    return _json(await asyncio.to_thread(readers.agenda))


async def api_forge(request):
    return _json(await asyncio.to_thread(readers.forge_tasks))


async def api_shadow(request):
    return _json(await asyncio.to_thread(readers.shadow_streams))


async def api_shadow_captures(request):
    stream = request.match_info["stream"]
    return _json(await asyncio.to_thread(readers.shadow_captures, stream))


async def api_shadow_capture(request):
    stream = request.match_info["stream"]
    name = request.query.get("name") or ""
    text = await asyncio.to_thread(readers.shadow_capture, stream, name)
    return _json({"stream": stream, "name": name, "text": text})


async def api_shadow_diff(request):
    stream = request.match_info["stream"]
    old = request.query.get("old") or ""
    new = request.query.get("new") or ""
    return _json(await asyncio.to_thread(readers.shadow_diff, stream, old, new))


async def api_shadow_metrics(request):
    return _json(await asyncio.to_thread(readers.shadow_metrics))


async def api_chats(request):
    return _json(await asyncio.to_thread(readers.chats))


async def api_chat(request):
    peer = request.match_info["peer"]
    n = min(600, int(request.query.get("n") or 200))
    return _json(await asyncio.to_thread(readers.chat_tail, peer, n))


async def api_md(request):
    return _json(await asyncio.to_thread(readers.safe_read_md,
                                         request.query.get("path") or ""))


async def api_md_tree(request):
    return _json(await asyncio.to_thread(readers.md_tree))


async def api_health(request):
    return _json(await asyncio.to_thread(readers.health))


async def api_state(request):
    return _json(await asyncio.to_thread(readers.state))


async def api_md_write(request):
    """Правка маркдауна агента из окна: конституция, навыки, заметки.

    Первый шаг конструктора: то, что окно показывает, оно же и правит. Путь
    только внутри разрешённых групп (readers.safe_write_md), только .md,
    запись атомарная. Её собственные правки себя от этого не страдают:
    файл переписывается целиком тем, что человек видел.
    """
    payload = await request.json()
    result = await asyncio.to_thread(readers.safe_write_md,
                                     str(payload.get("path") or ""),
                                     str(payload.get("text") or ""))
    if result.get("error"):
        raise web.HTTPBadRequest(text=result["error"])
    return _json(result)


async def api_anatomy(request):
    return _json(await asyncio.to_thread(readers.anatomy))


async def api_chat_turns(request):
    peer = request.match_info["peer"]
    n = min(300, int(request.query.get("n") or 120))
    return _json(await asyncio.to_thread(readers.chat_turns, peer, n))


_SAY_RE = re.compile(r"[^\w\-]+")


_CHAT_KEY_RE = re.compile(r"^-?\d+(?:__topic__\d+)?$")


async def _say(text: str, chat: str = "") -> dict:
    """Сообщение ей. Durable-файл в .control (mid-turn после мержа кандидата);
    без живой квитанции читателя — записка в workspace/inbox.

    `chat` — адрес комнаты (слово владельца 31.08: окно — ещё одна дверь владельца в
    ЛЮБУЮ его комнату). Пусто/«pult» — прежний путь. Telegram-ключ — записка
    `<stamp>__to__<ключ>.md`: руннер запишет реплику владельца в память ЭТОЙ
    комнаты и поведёт ход там; ответ уедет в Telegram. Фолбэка в workspace/inbox
    у адресных записок нет по построению: мёртвый руннер = мёртвый бот, и класть
    сообщение туда, откуда оно никогда не уедет адресату, значило бы врать
    квитанцией — отвечаем 409 честно.
    """
    text = str(text or "").strip()
    chat = str(chat or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="пустое сообщение")
    if len(text) > 20_000:
        raise web.HTTPBadRequest(text="слишком длинно (20k)")
    if chat and chat not in ("window", "pult") and not _CHAT_KEY_RE.match(chat):
        raise web.HTTPBadRequest(text=f"не похоже на адрес комнаты: {chat!r}")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    body = (f"# Сообщение с окна владельца · {stamp}\n\n{text}\n")
    targeted = bool(chat and chat not in ("window", "pult"))

    def _write() -> dict:
        written = []
        control = readers.tree() / "memory" / ".control" / "desk_inbox"
        # Квитанция читателя: её раннер (кандидат desk-midturn) пишет .reader.json
        # при старте. Свежая квитанция = канал живой, сообщение уедет mid-turn и
        # ДУБЛИРОВАТЬ его запиской нельзя. Нет квитанции — честная деградация в
        # workspace/inbox, который она читает уже сегодня.
        reader_alive = False
        try:
            age = time.time() - (control / ".reader.json").stat().st_mtime
            reader_alive = 0 <= age < 48 * 3600
        except OSError:
            pass
        # Контракт канала (её ревью 29.08): публикация ТОЛЬКО атомарной подменой.
        # Пишем во временное имя, которого glob читателя не видит, затем
        # os.replace — под финальным именем частичный файл не существует никогда,
        # и читатель, захватывающий rename-ом, не может получить обрезанный текст.
        def _publish(target: Path) -> None:
            tmp = target.with_name(".tmp-" + target.name)
            tmp.write_text(body, encoding="utf-8", newline="\n")
            os.replace(tmp, target)

        if targeted and not reader_alive:
            raise web.HTTPConflict(
                text="руннер не поднят — сообщение в комнату не уедет; "
                     "запусти локальный харнесс и повтори")
        try:
            control.mkdir(parents=True, exist_ok=True)
            name = f"{stamp}__to__{chat}.md" if targeted else f"{stamp}.md"
            _publish(control / name)
            written.append("control")
        except OSError:
            log.warning("mid-turn канал недоступен", exc_info=True)
        if not targeted and not (reader_alive and "control" in written):
            inbox = readers.tree() / "workspace" / "inbox"
            try:
                _publish(inbox / f"{stamp[:8]}_desk_{stamp[9:15]}.md")
                written.append("inbox")
            except OSError:
                log.warning("inbox недоступен", exc_info=True)
        return {"written": written, "stamp": stamp, "midturn": reader_alive,
                "chat": chat or "window"}

    result = await asyncio.to_thread(_write)
    if not result["written"]:
        raise web.HTTPInternalServerError(text="не записалось никуда")
    return result


async def api_say(request):
    try:
        payload = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="ожидается JSON")
    return _json(await _say(payload.get("text"), payload.get("chat") or ""))


# ------------------------------------------------------------------ труба

PROTOCOL = "frame.desk.v1"
_STARTED_AT = time.time()


async def _tunnel_dispatch(method: str, path: str, body, local: bool = False) -> dict:
    """Один запрос трубы -> тот же ответ, что у HTTP-ручки того же пути.

    Это ШОВ БУДУЩЕГО ПРОДУКТА: сегодня по трубе отвечает удалённый харнесс
    (этот демон на сервере), завтра — нативный харнесс на винде. Оболочка
    разницы не видит: протокол один, praxis.desk.v1 (по образцу praxis.body.v1
    её тела — исходящее соединение, ни одного открытого порта у клиента)."""
    from urllib.parse import urlsplit, parse_qs
    parts = urlsplit(path)
    route = parts.path
    query = {k: values[-1] for k, values in parse_qs(parts.query).items()}
    try:
        if method == "POST" and route == "/api/md":
            result = await asyncio.to_thread(
                readers.safe_write_md, str((body or {}).get("path") or ""),
                str((body or {}).get("text") or ""))
            if result.get("error"):
                return {"status": 400, "error": result["error"]}
            return {"status": 200, "body": result}
        if method == "POST" and route == "/api/say":
            return {"status": 200,
                    "body": await _say((body or {}).get("text"),
                                       (body or {}).get("chat") or "")}
        if route == "/api/runs":
            return {"status": 200, "body": await asyncio.to_thread(
                readers.list_runs, min(200, int(query.get("limit") or 80)),
                query.get("kind") or "", query.get("before") or "")}
        if route.startswith("/api/run/"):
            detail = await asyncio.to_thread(readers.run_detail,
                                             route.removeprefix("/api/run/"))
            return ({"status": 200, "body": detail} if detail
                    else {"status": 404, "error": "нет такого прогона"})
        if route == "/api/pulse":
            return {"status": 200, "body": await asyncio.to_thread(readers.pulse)}
        if route == "/api/errors":
            return {"status": 200, "body": await asyncio.to_thread(readers.errors)}
        if route == "/api/board":
            return {"status": 200, "body": await asyncio.to_thread(readers.board)}
        if route == "/api/agenda":
            return {"status": 200, "body": await asyncio.to_thread(readers.agenda)}
        if route == "/api/forge":
            return {"status": 200, "body": await asyncio.to_thread(readers.forge_tasks)}
        if route == "/api/chats":
            return {"status": 200, "body": await asyncio.to_thread(readers.chats)}
        if route.startswith("/api/chat/"):
            return {"status": 200, "body": await asyncio.to_thread(
                readers.chat_tail, route.removeprefix("/api/chat/"),
                min(600, int(query.get("n") or 200)))}
        if route == "/api/shadow":
            return {"status": 200, "body": await asyncio.to_thread(readers.shadow_streams)}
        if route == "/api/shadow-metrics":
            return {"status": 200, "body": await asyncio.to_thread(readers.shadow_metrics)}
        match = re.match(r"^/api/shadow/([^/]+)/(captures|capture|diff)$", route)
        if match:
            stream, action = match.group(1), match.group(2)
            if action == "captures":
                return {"status": 200, "body": await asyncio.to_thread(
                    readers.shadow_captures, stream)}
            if action == "capture":
                text = await asyncio.to_thread(readers.shadow_capture, stream,
                                               query.get("name") or "")
                return {"status": 200, "body": {"stream": stream,
                                                "name": query.get("name"),
                                                "text": text}}
            return {"status": 200, "body": await asyncio.to_thread(
                readers.shadow_diff, stream, query.get("old") or "",
                query.get("new") or "")}
        if route == "/api/md":
            return {"status": 200, "body": await asyncio.to_thread(
                readers.safe_read_md, query.get("path") or "")}
        if route == "/api/md-tree":
            return {"status": 200, "body": await asyncio.to_thread(readers.md_tree)}
        if route.startswith("/api/chat-turns/"):
            return {"status": 200, "body": await asyncio.to_thread(
                readers.chat_turns, route.removeprefix("/api/chat-turns/"),
                min(300, int(query.get("n") or 120)))}
        if route == "/api/anatomy":
            return {"status": 200, "body": await asyncio.to_thread(readers.anatomy)}
        if route == "/api/health":
            return {"status": 200, "body": await asyncio.to_thread(readers.health)}
        if route == "/api/state":
            return {"status": 200, "body": await asyncio.to_thread(readers.state)}
        # телефон: пары выдаёт и отзывает только окно на этой машине
        if route.startswith("/pair/") and not local:
            return {"status": 403, "error": "только с этой машины"}
        if route == "/pair/devices":
            return {"status": 200, "body": _device_rows()}
        if method == "POST" and route == "/pair/new":
            return {"status": 200, "body": _new_pair()}
        if method == "POST" and route == "/pair/revoke":
            return {"status": 200, "body": _revoke_device(str((body or {}).get("id") or ""))}
        return {"status": 404, "error": "нет такого пути"}
    except web.HTTPError as exc:
        return {"status": exc.status, "error": exc.text or str(exc)}
    except Exception as exc:
        log.warning("труба: %s %s упал", method, route, exc_info=True)
        return {"status": 500, "error": f"{type(exc).__name__}: {exc}"[:300]}


async def tunnel(request):
    """Одна труба на оболочку: запросы с id + события живьём, praxis.desk.v1."""
    local = _is_loopback(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await ws.send_json({"hello": PROTOCOL,
                        "server": {"started_at": _STARTED_AT,
                                   "tree": str(readers.tree())}},
                       dumps=lambda d: json.dumps(d, ensure_ascii=False))
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _SSE_CLIENTS.add(queue)

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json({"event": event},
                               dumps=lambda d: json.dumps(d, ensure_ascii=False))

    pump_task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                req = json.loads(msg.data)
            except ValueError:
                continue
            reply = await _tunnel_dispatch(
                str(req.get("method") or "GET").upper(),
                str(req.get("path") or ""), req.get("body"), local=local)
            reply["id"] = req.get("id")
            await ws.send_json(reply,
                               dumps=lambda d: json.dumps(d, ensure_ascii=False))
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        pump_task.cancel()
        _SSE_CLIENTS.discard(queue)
    return ws


# ------------------------------------------------------------------ SSE

async def sse(request):
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _SSE_CLIENTS.add(queue)
    try:
        await response.write(b": connected\n\n")
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
                data = json.dumps(event, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode("utf-8"))
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _SSE_CLIENTS.discard(queue)
    return response


def _broadcast(loop: asyncio.AbstractEventLoop, event: dict) -> None:
    for queue in list(_SSE_CLIENTS):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception:
            pass


def _watcher(loop: asyncio.AbstractEventLoop) -> None:
    """Стат-поллинг живых файлов; смена размера -> событие клиентам."""
    state: dict[str, tuple] = {}

    def probe(name: str, path: Path) -> bool:
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            stamp = None
        if state.get(name) != stamp:
            state[name] = stamp
            return True
        return False

    health_state = {"key": None, "next_at": 0.0}
    while True:
        try:
            base = readers.tree() / "memory" / ".state"
            # Сторож тишины: раз в ~30 с, событие клиентам ТОЛЬКО на смене состава
            # тревог (баннер не мигает от каждого тика).
            if time.time() >= health_state["next_at"]:
                health_state["next_at"] = time.time() + 30
                snapshot = readers.health()
                key = tuple(sorted(a["kind"] for a in snapshot.get("alarms") or []))
                if key != health_state["key"]:
                    health_state["key"] = key
                    _broadcast(loop, {"t": "health", "health": snapshot})
            if probe("llm", base / "llm_calls.jsonl"):
                _broadcast(loop, {"t": "llm"})
            if probe("skips", base / "perception_skips.jsonl"):
                _broadcast(loop, {"t": "skips"})
            if probe("shadow", base / "shadow" / "metrics.jsonl"):
                _broadcast(loop, {"t": "shadow"})
            newest = readers.list_runs(limit=1)
            if newest:
                run_id = newest[0]["id"]
                run_path = readers.run_dir(run_id)
                if run_path is not None and probe("run:" + run_id,
                                                 run_path / "events.jsonl"):
                    _broadcast(loop, {"t": "run", "run_id": run_id,
                                      "status": newest[0].get("status")})
        except Exception:
            log.debug("watcher tick failed", exc_info=True)
        time.sleep(_WATCH_INTERVAL)


# ------------------------------------------------------------------ app

async def index(request):
    """index.html с версией статики в src: app.js?v=<mtime>.

    Без версии браузер кэширует app.js эвристикой (мы не слали Cache-Control
    вовсе), и КАЖДАЯ правка UI липла у клиента до жёсткого релоада: страница
    исполняла старый скрипт, а сервер уже отдавал новый — час отладки 31.08.
    mtime в качестве версии: правка файла = новый URL = мгновенный подхват.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css", "config.js"):
        try:
            stamp = int((STATIC / name).stat().st_mtime)
        except OSError:
            continue
        html = html.replace(f'"{name}"', f'"{name}?v={stamp}"')
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


def _static_file(name: str):
    async def handler(request):
        # no-cache = «можно хранить, но каждый раз ревалидируй» (ETag → 304):
        # свежесть UI без повторной перекачки тела.
        return web.FileResponse(
            STATIC / name, headers={"Cache-Control": "no-cache"})
    return handler


def _mobile_file(name: str):
    async def handler(request):
        return web.FileResponse(MOBILE / name, headers={"Cache-Control": "no-cache"})
    return handler


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/api/runs", api_runs)
    app.router.add_get("/api/run/{run_id}", api_run)
    app.router.add_get("/api/pulse", api_pulse)
    app.router.add_get("/api/errors", api_errors)
    app.router.add_get("/api/board", api_board)
    app.router.add_get("/api/agenda", api_agenda)
    app.router.add_get("/api/forge", api_forge)
    app.router.add_get("/api/shadow", api_shadow)
    app.router.add_get("/api/shadow/{stream}/captures", api_shadow_captures)
    app.router.add_get("/api/shadow/{stream}/capture", api_shadow_capture)
    app.router.add_get("/api/shadow/{stream}/diff", api_shadow_diff)
    app.router.add_get("/api/shadow-metrics", api_shadow_metrics)
    app.router.add_get("/api/chats", api_chats)
    app.router.add_get("/api/chat/{peer}", api_chat)
    app.router.add_get("/api/md", api_md)
    app.router.add_get("/api/md-tree", api_md_tree)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/pair/new", api_pair_new)
    app.router.add_get("/pair/redeem", api_pair_redeem)
    app.router.add_get("/pair/devices", api_devices)
    app.router.add_post("/pair/revoke", api_device_revoke)
    app.router.add_get("/m/", mobile_index)
    app.router.add_get("/m", mobile_index)
    app.router.add_get("/m/manifest.webmanifest", mobile_manifest)
    if (MOBILE / "assets").is_dir():
        app.router.add_static("/m/assets/", MOBILE / "assets")
    for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        if (MOBILE / name).is_file():
            app.router.add_get("/m/" + name, _mobile_file(name))
    app.router.add_post("/api/md", api_md_write)
    app.router.add_get("/api/anatomy", api_anatomy)
    app.router.add_get("/api/chat-turns/{peer}", api_chat_turns)
    app.router.add_post("/api/say", api_say)
    app.router.add_get("/events", sse)
    app.router.add_get("/tunnel", tunnel)
    if STATIC.is_dir():
        app.router.add_static("/static/", STATIC)
    # Те же ассеты с корня: index.html ссылается относительно, чтобы один и тот
    # же дистрибутив жил и в вебе, и в нативной оболочке.
    for name in ("config.js", "favicon.svg"):
        if (STATIC / name).is_file():
            app.router.add_get("/" + name, _static_file(name))
    # Сборка Vite кладёт ассеты в assets/ с хэшами в именах.
    if (STATIC / "assets").is_dir():
        app.router.add_static("/assets/", STATIC / "assets")
    return app


def _ensure_local_tree() -> None:
    """Минимальный скелет дерева для локального режима. На сервере дерево
    смонтировано ro — mkdir там падает, и это не ошибка: молча пропускаем."""
    base = readers.tree()
    for rel in ("memory/.state", "memory/.control", "memory/runs",
                "workspace/inbox", "soul"):
        try:
            (base / rel).mkdir(parents=True, exist_ok=True)
        except OSError:
            return


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(
        os.environ.get("HELENE_PORT") or os.environ.get("PRAXIS_DESK_PORT") or 8094)
    # Локальный харнесс обязан слушать ТОЛЬКО петлю: дерево без токена не должно
    # быть видно даже соседям по локальной сети. Сервер (за Caddy) — как раньше.
    host = (os.environ.get("HELENE_HOST") or os.environ.get("PRAXIS_DESK_HOST") or "0.0.0.0").strip()
    _ensure_local_tree()
    app = build_app()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=_watcher, args=(loop,), daemon=True).start()
    log.info("Helene слушает %s:%d, дерево %s, токен %s", host, port,
             readers.tree(), "задан" if TOKEN else "НЕ задан (локальная петля)")
    web.run_app(app, host=host, port=port, loop=loop, print=None)


if __name__ == "__main__":
    main()
