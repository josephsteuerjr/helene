# -*- coding: utf-8 -*-
"""Читатели дерева агента для окна Hélène. ТОЛЬКО чтение.

Правила, выученные до этого приложения:
- никаких замков её раннера (`run_listing` берёт .run.lock — сюда нельзя);
- никакого `revision_hint` и глобов по всему дереву;
- каталог прогона вычисляется из его id (месяц зашит в имени);
- любой хвост журнала читается с конца файла, а не через весь файл.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any

_RUN_ID_RE = re.compile(r"^run-(\d{4})(\d{2})\d{2}T\d{6,}Z?-[0-9a-f]{8}$")
_MD_ROOTS = ("memory/", "workspace/", "soul/")
_MD_CAP = 400_000


def tree() -> Path:
    override = os.environ.get("HELENE_TREE") or os.environ.get("PRAXIS_DESK_TREE")
    if override:
        return Path(override)
    data = Path("/data")
    if data.is_dir():
        return data
    # локальная разработка: клон прода лежит рядом с desk/
    return Path(__file__).resolve().parent.parent.parent / "live"


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def tail_lines(path: Path, n: int, *, max_bytes: int = 4_000_000) -> list[str]:
    """Последние n строк файла без чтения его целиком."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    take = min(size, max_bytes)
    try:
        with path.open("rb") as fh:
            fh.seek(size - take)
            raw = fh.read(take)
    except OSError:
        return []
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    if take < size and lines:
        lines = lines[1:]  # первая строка среза почти наверняка рваная
    return lines[-n:]


def tail_jsonl(path: Path, n: int) -> list[dict]:
    rows: list[dict] = []
    for line in tail_lines(path, n):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ------------------------------------------------------------------ прогоны

def run_dir(run_id: str) -> Path | None:
    match = _RUN_ID_RE.match(run_id or "")
    if not match:
        return None
    month = f"{match.group(1)}-{match.group(2)}"
    path = tree() / "memory" / "runs" / month / run_id
    return path if path.is_dir() else None


def list_runs(limit: int = 80, kind: str = "", before: str = "") -> list[dict]:
    """Свежие прогоны, новые первыми. Имя каталога сортирует по времени само."""
    root = tree() / "memory" / "runs"
    if not root.is_dir():
        return []
    months = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    titles = chat_titles()
    out: list[dict] = []
    for month in months:
        try:
            # Только каноничные id: рядом живут каталоги старого praxis_app
            # (`run-app-server-…`), их манифесты — другой мир.
            names = sorted((p.name for p in month.iterdir()
                            if _RUN_ID_RE.match(p.name)), reverse=True)
        except OSError:
            continue
        for name in names:
            if before and name >= before:
                continue
            manifest = _load_json(month / name / "manifest.json")
            context = manifest.get("context") or {}
            run_kind = str(context.get("kind") or "")
            if kind and run_kind != kind:
                continue
            goal = str(context.get("goal") or "").strip()
            terminal = manifest.get("terminal") or {}
            chat_id = context.get("delivery_chat_id") or context.get("origin_chat_id")
            out.append({
                "id": name,
                "created_at": manifest.get("created_at") or "",
                "status": manifest.get("status") or "",
                "kind": run_kind,
                "chat_id": chat_id,
                "chat_title": _title_for(chat_id, titles),
                "forge_task_id": context.get("forge_task_id") or "",
                "goal_head": goal.splitlines()[0][:140] if goal else "",
                "terminal_status": terminal.get("status") or "",
                "model_profile": context.get("model_profile") or "",
            })
            if len(out) >= limit:
                return out
    return out


def chat_titles(n: int = 4000) -> dict[str, str]:
    """chat_id -> живое имя, из хвоста turns.jsonl.

    У комнат имя лежит в `title`, у личек `title` пуст — имя собеседника в `who`."""
    titles: dict[str, str] = {}
    for row in tail_jsonl(tree() / "memory" / ".state" / "turns.jsonl", n):
        chat_id = row.get("chat_id")
        if chat_id is None:
            continue
        key = str(chat_id)
        title = str(row.get("title") or "").strip()
        if not title and not key.startswith("-"):
            title = str(row.get("who") or "").strip()
        if title:
            titles[key] = title
    return titles


def _title_for(chat_id, titles: dict[str, str]) -> str:
    key = str(chat_id or "")
    if not key:
        return ""
    if key in ("window", "pult"):
        return "Окно"          # комната окна Hélène — не Telegram, у неё нет чужого имени
    if key in titles:
        return titles[key]
    base = key.split("__topic__")[0]
    return titles.get(base, "")


def _inline_preview(result: dict) -> dict:
    ref = result if isinstance(result, dict) else {}
    inline = ref.get("inline") or {}
    return {
        "head": str(inline.get("head") or "")[:4000],
        "tail": str(inline.get("tail") or "")[:1000],
        "truncated": bool(inline.get("truncated")),
        "size": ref.get("size"),
        "line_count": ref.get("line_count"),
        "path": ref.get("path"),
        "media_type": ref.get("media_type"),
    }


def _model_text(result: dict) -> str:
    """Видимый текст её реплики из model_output, если инлайн-голова цельная."""
    head = ((result or {}).get("inline") or {}).get("head") or ""
    truncated = bool(((result or {}).get("inline") or {}).get("truncated"))
    if truncated:
        return ""
    try:
        data = json.loads(head)
    except ValueError:
        return ""
    text = str(data.get("text") or "")
    for block in data.get("blocks") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            extra = str(block.get("text") or "")
            if extra and extra not in text:
                text = (text + "\n" + extra).strip()
    return text


def run_detail(run_id: str, *, max_events: int = 4000) -> dict:
    path = run_dir(run_id)
    if path is None:
        return {}
    manifest = _load_json(path / "manifest.json")
    iterations: list[dict] = []
    current: dict | None = None
    tools_by_call: dict[str, dict] = {}
    events_path = path / "events.jsonl"
    rows: list[dict] = []
    try:
        with events_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        rows = []
    rows = rows[-max_events:]
    status_flow: list[dict] = []
    for row in rows:
        kind = row.get("kind")
        if kind == "model_started":
            current = {"at": row.get("at"), "seq": row.get("seq"),
                       "call_id": row.get("call_id"), "tools": [], "text": ""}
            iterations.append(current)
        elif kind == "model_output":
            if current is not None:
                text = _model_text(row.get("result") or {})
                if text:
                    current["text"] = text
        elif kind == "model_completed":
            if current is None or current.get("call_id") != row.get("call_id"):
                current = {"at": row.get("at"), "seq": row.get("seq"),
                           "call_id": row.get("call_id"), "tools": [], "text": ""}
                iterations.append(current)
            current.update({
                "model": row.get("model"), "role": row.get("role"),
                "ms": row.get("duration_ms"), "stop": row.get("stop_reason"),
                "usage": row.get("usage") or {},
                "tool_calls": row.get("tool_calls"),
                "text_chars": row.get("text_chars"),
            })
        elif kind == "tool_started":
            card = {"tool": row.get("tool"), "args": row.get("args"),
                    "at": row.get("at"), "seq": row.get("seq"),
                    "side_effect": bool(row.get("side_effect")),
                    "call_id": row.get("call_id"), "result": None}
            tools_by_call[str(row.get("call_id") or row.get("seq"))] = card
            if current is None:
                current = {"at": row.get("at"), "seq": row.get("seq"),
                           "call_id": None, "tools": [], "text": ""}
                iterations.append(current)
            current["tools"].append(card)
        elif kind == "tool_result":
            card = tools_by_call.get(str(row.get("call_id") or ""))
            preview = _inline_preview(row.get("result") or {})
            if card is not None:
                card["result"] = preview
            else:  # результат без старта — покажем как есть
                if current is None:
                    current = {"at": row.get("at"), "seq": row.get("seq"),
                               "call_id": None, "tools": [], "text": ""}
                    iterations.append(current)
                current["tools"].append({"tool": row.get("name"), "args": None,
                                         "at": row.get("at"), "seq": row.get("seq"),
                                         "result": preview})
        elif kind == "status_changed":
            status_flow.append({"at": row.get("at"), "from": row.get("from_status"),
                                "to": row.get("to_status")})
    recap = ""
    try:
        recap = (path / "RECAP.md").read_text(encoding="utf-8", errors="replace")[:120_000]
    except OSError:
        pass
    context = manifest.get("context") or {}
    return {
        "id": run_id,
        "manifest": {
            "created_at": manifest.get("created_at"),
            "status": manifest.get("status"),
            "terminal": manifest.get("terminal") or {},
            "event_seq": manifest.get("event_seq"),
            "kind": context.get("kind"),
            "goal": context.get("goal"),
            "model_profile": context.get("model_profile"),
            "chat_id": context.get("delivery_chat_id") or context.get("origin_chat_id"),
        },
        "iterations": iterations,
        "status_flow": status_flow,
        "recap": recap,
    }


# ------------------------------------------------------------------ пульс/ошибки

def pulse(n: int = 300) -> dict:
    import time as _time
    rows = tail_jsonl(tree() / "memory" / ".state" / "llm_calls.jsonl", n)
    last = rows[-1] if rows else {}
    by_run: dict[str, dict] = {}
    for row in rows:
        run_id = str(row.get("run") or "")
        if not run_id:
            continue
        agg = by_run.setdefault(run_id, {"calls": 0, "in": 0, "cached": 0, "out": 0,
                                         "err": 0, "last_ts": 0.0})
        agg["calls"] += 1
        agg["in"] += int(row.get("in") or 0)
        agg["cached"] += int(row.get("cached") or 0)
        agg["out"] += int(row.get("out") or 0)
        agg["err"] += 1 if row.get("err") else 0
        agg["last_ts"] = max(agg["last_ts"], float(row.get("ts") or 0))
    # Кэш двумя честными числами (просьба владельца): среднесуточный — скользящие
    # сутки назад от СЕЙЧАС; текущий — последние 15 минут (или последний вызов).
    now = _time.time()
    day_rows = [r for r in tail_jsonl(
        tree() / "memory" / ".state" / "llm_calls.jsonl", 20_000)
        if float(r.get("ts") or 0) >= now - 86_400]
    def _share(rows_):
        fresh = sum(int(r.get("in") or 0) for r in rows_)
        cached = sum(int(r.get("cached") or 0) for r in rows_)
        total = fresh + cached
        return round(100 * cached / total) if total else None
    recent = [r for r in day_rows if float(r.get("ts") or 0) >= now - 900]
    hours = round((now - float(day_rows[0].get("ts") or now)) / 3600) if day_rows else 0
    return {"last": last, "by_run": by_run, "rows": rows[-40:],
            "cache_day": _share(day_rows), "cache_day_hours": hours,
            "cache_now": _share(recent) if recent else _share(rows[-1:] if rows else []),
            "calls_day": len(day_rows)}


def errors(n: int = 2000) -> dict:
    llm_rows = tail_jsonl(tree() / "memory" / ".state" / "llm_calls.jsonl", n)
    failed = [row for row in llm_rows if row.get("err")][-120:]
    skips = tail_jsonl(tree() / "memory" / ".state" / "perception_skips.jsonl", 250)
    return {"llm": failed, "skips": skips}


# ------------------------------------------------------------------ доска

def board() -> dict:
    base = tree() / "memory" / "work"
    text = ""
    try:
        text = (base / "BOARD.md").read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        pass
    tasks: list[dict] = []
    tasks_dir = base / "tasks"
    if tasks_dir.is_dir():
        try:
            dirs = sorted(tasks_dir.iterdir(), key=lambda p: p.stat().st_mtime,
                          reverse=True)
        except OSError:
            dirs = []
        for entry in dirs[:40]:
            if not entry.is_dir():
                continue
            head = ""
            try:
                with (entry / "TASK.md").open(encoding="utf-8", errors="replace") as fh:
                    head = fh.readline().strip()[:200]
            except OSError:
                pass
            tasks.append({"id": entry.name, "head": head})
    return {"board": text, "tasks": tasks}


def agenda() -> dict:
    """memory/tasks.json — её намеченное: будильники, окна, отложенные доставки."""
    try:
        items = json.loads((tree() / "memory" / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": [], "total": 0}
    if not isinstance(items, list):
        return {"active": [], "total": 0}
    active = [row for row in items if isinstance(row, dict)
              and str(row.get("status") or "") not in
              {"done", "cancelled", "canceled", "failed", "expired", "delivered"}]
    active.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
    fields = ("id", "kind", "goal", "target", "when", "recur", "status", "created")
    return {"active": [{k: row.get(k) for k in fields} for row in active[:120]],
            "total": len(items)}


def _taskmd_frontmatter(path: Path) -> dict:
    """Скалярные строки YAML-фронтматтера TASK.md, без полного YAML-парсера."""
    out: dict[str, Any] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return out
            for _ in range(200):
                line = fh.readline()
                if not line or line.strip() == "---":
                    break
                match = re.match(r'^([a-z_]+):\s*(".*"|\S.*)$', line.rstrip("\n"))
                if not match:
                    continue
                key, raw = match.group(1), match.group(2)
                if raw.startswith('"') and raw.endswith('"'):
                    try:
                        out[key] = json.loads(raw)
                        continue
                    except ValueError:
                        pass
                out[key] = raw
    except OSError:
        pass
    return out


def forge_tasks(limit: int = 30) -> list[dict]:
    """Форж-дети: memory/work/tasks/<задача>/agents/<юнит>/ request+result."""
    root = tree() / "memory" / "work" / "tasks"
    if not root.is_dir():
        return []
    try:
        dirs = sorted((p for p in root.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out: list[dict] = []
    for entry in dirs[:limit]:
        task = _load_json(entry / "task.json")
        agents = []
        agents_dir = entry / "agents"
        if agents_dir.is_dir():
            try:
                agent_dirs = sorted(agents_dir.iterdir())
            except OSError:
                agent_dirs = []
            for unit in agent_dirs:
                request = _load_json(unit / "request.json")
                result = _load_json(unit / "result.json")
                agents.append({
                    "id": unit.name,
                    "role": request.get("role") or result.get("role"),
                    "status": result.get("status") or request.get("status") or "",
                    "created": request.get("created"),
                    "finished": result.get("finished"),
                    "error": str(result.get("error") or "")[:200],
                    "result_head": str(result.get("result") or "")[:300],
                })
        goal = str(task.get("goal") or "")[:300]
        status = task.get("status")
        if not goal:
            # Её рабочие задачи держат TASK.md с YAML-фронтматтером, а не task.json.
            meta = _taskmd_frontmatter(entry / "TASK.md")
            goal = str(meta.get("goal") or "")[:300]
            status = status or ("closed" if meta.get("closed_at") else
                                (meta.get("kind") or "task"))
        out.append({
            "id": entry.name,
            "goal": goal,
            "status": status,
            "priority": task.get("priority"),
            "agents": agents,
        })
    return out


# ------------------------------------------------------------------ тень (КАДР)

def shadow_root() -> Path:
    return tree() / "memory" / ".state" / "shadow"


def shadow_streams() -> list[dict]:
    root = shadow_root()
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == "report":
            continue
        try:
            captures = sorted(p.name for p in entry.iterdir() if p.suffix == ".md")
        except OSError:
            captures = []
        if not captures:
            continue
        out.append({"stream": entry.name, "captures": len(captures),
                    "latest": captures[-1]})
    return out


_CAPTURE_RE = re.compile(r"^[0-9TZ-]+-[0-9a-f]{8}\.md$")


def shadow_captures(stream: str, limit: int = 30) -> list[str]:
    entry = shadow_root() / os.path.basename(stream)
    if not entry.is_dir():
        return []
    names = sorted((p.name for p in entry.iterdir()
                    if p.suffix == ".md" and _CAPTURE_RE.match(p.name)), reverse=True)
    return names[:limit]


def shadow_capture(stream: str, name: str) -> str:
    if not _CAPTURE_RE.match(name or ""):
        return ""
    path = shadow_root() / os.path.basename(stream) / name
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:600_000]
    except OSError:
        return ""


def _sections(text: str) -> list[tuple[str, str]]:
    """Разрез захвата по верхним заголовкам — зоны кадра."""
    parts: list[tuple[str, list[str]]] = []
    title = "(преамбула)"
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("# "):
            if buf:
                parts.append((title, buf))
            title, buf = line[2:].strip(), []
        else:
            buf.append(line)
    parts.append((title, buf))
    return [(t, "\n".join(b)) for t, b in parts]


def shadow_diff(stream: str, old: str, new: str) -> dict:
    """Дифф двух захватов: где порвался префикс — по зонам и первым байтом."""
    old_text = shadow_capture(stream, old)
    new_text = shadow_capture(stream, new)
    if not old_text or not new_text:
        return {}
    prefix = 0
    for a, b in zip(old_text, new_text):
        if a != b:
            break
        prefix += 1
    zones = []
    old_secs = dict(_sections(old_text))
    new_secs = _sections(new_text)
    for title, body in new_secs:
        was = old_secs.get(title)
        if was is None:
            zones.append({"zone": title, "state": "new", "chars": len(body)})
        elif was == body:
            zones.append({"zone": title, "state": "same", "chars": len(body)})
        else:
            zone_prefix = 0
            for a, b in zip(was, body):
                if a != b:
                    break
                zone_prefix += 1
            zones.append({"zone": title, "state": "changed", "chars": len(body),
                          "was_chars": len(was), "prefix": zone_prefix})
    gone = [t for t in old_secs if t not in dict(new_secs)]
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=old, tofile=new, lineterm="", n=2))[:800]
    return {"prefix_bytes": prefix, "old_len": len(old_text), "new_len": len(new_text),
            "zones": zones, "gone": gone, "diff": diff_lines}


def shadow_metrics(n: int = 120) -> list[dict]:
    return tail_jsonl(shadow_root() / "metrics.jsonl", n)


# ------------------------------------------------------------------ переписки

def chats() -> list[dict]:
    """Комнаты из реестра group_context: по свежему состоянию на пир."""
    root = tree() / "memory" / ".state" / "group_context"
    if not root.is_dir():
        return []
    titles = chat_titles()
    best: dict[str, dict] = {}
    for path in root.glob("*.json"):
        name = path.name
        if name.endswith(".route.json") or name.endswith(".backfill.json"):
            continue
        data = _load_json(path)
        peer = str(data.get("peer_id") or "")
        if not peer or not data.get("archive"):
            continue
        row = {
            "peer_id": peer,
            "title": _title_for(peer, titles),
            "messages": data.get("message_count"),
            "participants": data.get("participant_count"),
            "topics": data.get("topic_count"),
            "archive": str(data.get("archive")),
            "mtime_ns": int(data.get("archive_mtime_ns") or 0),
            "size": data.get("archive_size"),
        }
        if peer not in best or row["mtime_ns"] > best[peer]["mtime_ns"]:
            best[peer] = row
    return sorted(best.values(), key=lambda r: -r["mtime_ns"])


def chat_tail(peer_id: str, n: int = 200) -> list[dict]:
    for row in chats():
        if row["peer_id"] == str(peer_id):
            rel = row["archive"].replace("\\", "/").lstrip("/")
            if ".." in rel.split("/") or not rel.startswith("memory/groups/"):
                return []
            return tail_jsonl(tree() / rel, n)
    return []


def chat_turns(peer_id: str, n: int = 120) -> list[dict]:
    """Ходы одной комнаты из turns.jsonl — подписи для правой панели.

    `in`/`out` здесь — то, на что она отвечала и что сказала В ЭТОМ ходе; goal
    прогона для подписи не годится: у чат-хода это ВЕСЬ разговор, и первая
    строка у всех ходов комнаты одинаковая (панель подписывала всё «/start»).
    """
    key = str(peer_id)
    out = []
    for row in tail_jsonl(tree() / "memory" / ".state" / "turns.jsonl", 4000):
        if str(row.get("chat_id")) != key or not row.get("run_id"):
            continue
        out.append({"run_id": row.get("run_id"), "ts": row.get("ts"),
                    "kind": row.get("kind"), "who": row.get("who"),
                    "in": str(row.get("in") or "")[:160],
                    "out": str(row.get("out") or "")[:160],
                    "note": str(row.get("note") or "")[:120],
                    "delivery": row.get("delivery")})
    return out[-max(1, int(n)):]


# ------------------------------------------------------------------ устройство

def anatomy() -> dict:
    """Снимок устройства для вкладки «Устройство»: его пишет руннер на старте
    из ЖИВОГО списка рук (offered_tools_for). Пусто = руннер ещё не поднимался."""
    return _load_json(tree() / "memory" / ".state" / "anatomy.json")


# ------------------------------------------------------------------ сторож тишины

def health() -> dict:
    """Тревоги «она молчит/зациклена, хотя не должна» — мета-класс всех глухот.

    Каждый из четырёх инцидентов глухоты (27.08 end_turn, 28.08 пустая очередь,
    28.08 битый байт, 29.08 петля рестартов) замечал ЧЕЛОВЕК по ощущению тишины.
    Этот прибор делает отсутствие видимым. Только чтение, только её артефакты.
    """
    import time as _time
    now = _time.time()
    alarms: list[dict] = []
    state = tree() / "memory" / ".state"

    # 1. Петля рестартов: строки [restart] в сегодняшнем дневнике за 20 минут.
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    recent_restarts = 0
    for line in tail_lines(tree() / "memory" / "journal" / f"{day}.md", 300):
        if "[restart]" not in line and "перезапускаюсь" not in line:
            continue
        try:  # «- 02:36 (s3) [restart] …» — часы:минуты UTC её дневника
            hh, mm = line.split("- ", 1)[1].split(" ", 1)[0].split(":")
            stamp = _dt.datetime.now(_dt.timezone.utc).replace(
                hour=int(hh), minute=int(mm), second=0)
            if 0 <= (now - stamp.timestamp()) < 1200:
                recent_restarts += 1
        except (ValueError, IndexError):
            continue
    if recent_restarts >= 3:
        alarms.append({"kind": "restart_loop",
                       "text": f"петля рестартов: {recent_restarts} за 20 минут"})

    # 2. Молчание при долге: вызовов модели давно нет, а недоставленные события есть.
    llm_rows = tail_jsonl(state / "llm_calls.jsonl", 5)
    last_call = max((float(r.get("ts") or 0) for r in llm_rows), default=0.0)
    quiet_min = (now - last_call) / 60 if last_call else None
    undelivered = 0
    try:
        delivered = set()
        raw = _load_json(state / "core_events_delivered.json")
        delivered = set((raw.get("delivered") or raw or {}).keys())
        for row in tail_jsonl(state / "core_events.jsonl", 400):
            key = str(row.get("dedup_key") or row.get("id") or "")
            if key and key not in delivered:
                undelivered += 1
    except Exception:
        pass
    if quiet_min is not None and quiet_min > 20 and undelivered > 0:
        alarms.append({"kind": "deaf_with_debt",
                       "text": (f"тишина {quiet_min:.0f} мин при {undelivered} "
                                "недоставленных событиях")})
    elif quiet_min is not None and quiet_min > 90:
        alarms.append({"kind": "long_silence",
                       "text": f"ни одного вызова модели {quiet_min:.0f} мин"})

    # 3. Шторм откладываний: perception_skips растёт лавиной (defer-петля 20 Гц).
    skips = tail_jsonl(state / "perception_skips.jsonl", 400)
    recent_skips = sum(1 for r in skips if now - float(r.get("ts") or 0) < 300)
    if recent_skips >= 150:
        alarms.append({"kind": "defer_storm",
                       "text": f"{recent_skips} откладываний за 5 минут — похоже на defer-петлю"})

    return {"alarms": alarms, "checked_at": now,
            "last_call_min_ago": round(quiet_min, 1) if quiet_min is not None else None,
            "restarts_20m": recent_restarts, "undelivered": undelivered,
            "skips_5m": recent_skips}


# ------------------------------------------------------------------ маркдауны

_MD_GROUPS = (
    ("Заметки", "memory/notes"),
    ("Дневник", "memory/journal"),
    ("Workspace", "workspace"),
    ("Inbox", "workspace/inbox"),
    ("Soul", "soul"),
    ("Работа", "memory/work"),
    ("Желания", "memory/desires"),
)


def md_tree() -> list[dict]:
    """Её маркдауны по корням: имя, размер, свежесть. Без рекурсии в runs."""
    out: list[dict] = []
    for label, rel in _MD_GROUPS:
        root = tree() / rel
        if not root.is_dir():
            continue
        files: list[dict] = []
        try:
            for path in root.rglob("*.md"):
                relative = _posix_rel(path, tree())
                if relative is None or "/runs/" in relative or "/.state/" in relative:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append({"path": relative, "name": relative[len(rel) + 1:],
                              "size": stat.st_size, "mtime": stat.st_mtime})
                if len(files) >= 400:
                    break
        except OSError:
            continue
        files.sort(key=lambda f: -f["mtime"])
        if files:
            out.append({"group": label, "root": rel, "files": files[:200]})
    return out


def _posix_rel(path: Path, base: Path) -> str | None:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def safe_read_md(rel: str) -> dict:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if ".." in rel.split("/") or not rel.startswith(_MD_ROOTS):
        return {"error": "путь вне разрешённых корней"}
    path = tree() / rel
    try:
        if path.stat().st_size > _MD_CAP:
            return {"error": f"файл больше {_MD_CAP} байт"}
        return {"path": rel, "text": path.read_text(encoding="utf-8", errors="replace")}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
#  Единая модель состояния: одна фраза человеку и одно действие рядом.
# ---------------------------------------------------------------------------

_SLEEP_KINDS = {"wake", "window"}


def _short_error(err) -> str:
    """Ошибка модели человеческими словами; хвост сырого текста — в last_error_raw."""
    text = str(err or "").strip().replace("\n", " ")
    low = text.lower()
    if "401" in low or "unauthorized" in low or "invalid api key" in low \
            or "incorrect api key" in low:
        return "ключ не подошёл"
    if "429" in low or "rate limit" in low or "quota" in low or "insufficient" in low:
        return "лимит или баланс исчерпан"
    if "timeout" in low or "timed out" in low:
        return "модель не ответила вовремя"
    if "connect" in low or "getaddrinfo" in low or "name or service" in low \
            or "network" in low:
        return "нет связи с моделью"
    if "404" in low or "not found" in low:
        return "такой модели нет по этому адресу"
    return text[:90] or "ошибка модели"


def state() -> dict:
    """Что с агентом прямо сейчас — одна фраза и одно действие.

    Только чтение артефактов: квитанция читателя руннера (жив ли, думает ли),
    хвост вызовов модели (ошибки), настройка мозга из anatomy.json, вход реле,
    ближайший будильник. Порядок важности: мёртвый руннер > нет модели >
    реле без входа > ошибка модели > думает > спит > на связи.
    """
    import time as _time
    import datetime as _dt
    now = _time.time()
    base = tree()
    st = base / "memory" / ".state"
    anatomy = _load_json(st / "anatomy.json")
    agent = str(anatomy.get("agent_name") or "Агент")
    reader = _load_json(base / "memory" / ".control" / "desk_inbox" / ".reader.json")
    reader_age = (now - float(reader.get("at") or 0.0)) if reader else None
    runner_alive = reader_age is not None and reader_age < 45
    busy = bool(reader.get("busy")) and runner_alive
    model_cfg = anatomy.get("model") or {}
    configured = bool(str(model_cfg.get("model") or "").strip())
    llm_rows = tail_jsonl(st / "llm_calls.jsonl", 12)
    last = llm_rows[-1] if llm_rows else {}
    last_ts = float(last.get("ts") or 0.0)
    last_err = str(last.get("err") or "") if last else ""
    recent_error = bool(last_err) and (now - last_ts) < 900
    relay_auth = (base / "relay" / "local_auth" / "auth.json").exists()
    relay_used = "127.0.0.1:50" in str(model_cfg.get("base_url") or "")
    next_wake = None
    for row in agenda().get("active", []):
        if str(row.get("kind") or "") not in _SLEEP_KINDS:
            continue
        when = str(row.get("when") or "")
        try:
            stamp = _dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=_dt.timezone.utc)
        if stamp.timestamp() > now and (next_wake is None or stamp < next_wake):
            next_wake = stamp
    transports = list(anatomy.get("transports") or [])

    action = None
    if not runner_alive:
        level, phrase = "error", "Не запущен"
        action = {"label": "Перезапустить", "target": "restart"}
    elif not configured:
        level, phrase = "warn", "Модель не настроена"
        action = {"label": "Настроить", "target": "settings"}
    elif relay_used and not relay_auth:
        level, phrase = "warn", "Подписка ChatGPT не подключена"
        action = {"label": "Войти", "target": "settings"}
    elif recent_error:
        level, phrase = "error", f"Модель отвечает ошибкой: {_short_error(last_err)}"
        action = {"label": "Настройки", "target": "settings"}
    elif busy:
        level, phrase = "live", "Думает"
    elif next_wake is not None:
        local = next_wake.astimezone()
        level, phrase = "ok", f"Ждёт, проснётся в {local.strftime('%H:%M')}"
    else:
        level, phrase = "ok", "На связи"
    return {
        "agent": agent,
        "owner": str(anatomy.get("owner_name") or ""),
        "level": level,
        "phrase": phrase,
        "action": action,
        "runner": {"alive": runner_alive,
                   "age_s": None if reader_age is None else round(reader_age, 1),
                   "busy": busy, "run": str(reader.get("run") or ""),
                   "since": float(reader.get("since") or 0.0)},
        "brain": {"configured": configured, "model": str(model_cfg.get("model") or ""),
                  "base_url": str(model_cfg.get("base_url") or ""),
                  "last_call_at": last_ts or None,
                  "last_error": _short_error(last_err) if recent_error else None,
                  "last_error_raw": (last_err[:300] if recent_error else None)},
        "relay": {"used": relay_used, "authorized": relay_auth},
        "telegram": {"enabled": any("Telegram" in x for x in transports)},
        "next_wake": next_wake.isoformat() if next_wake else None,
        "alarms": health().get("alarms", []),
    }


def safe_write_md(rel: str, text: str) -> dict:
    """Записать маркдаун по тем же правилам, по каким safe_read_md читает.

    Только файлы внутри групп _MD_GROUPS, только .md, без выхода из дерева;
    запись атомарная (tmp + replace), перевод строк "\n". Возвращает размер.
    """
    base = tree()
    clean = str(rel or "").replace("\\", "/").strip("/")
    if not clean or ".." in clean.split("/") or not clean.endswith(".md"):
        return {"error": "путь не похож на маркдаун агента"}
    if not any(clean == root or clean.startswith(root + "/") for _, root in _MD_GROUPS):
        return {"error": "этот файл окно не правит"}
    path = base / clean
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return {"error": "путь выходит из дерева"}
    body = str(text or "").replace("\r\n", "\n")
    if not body.endswith("\n"):
        body += "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".tmp-" + path.name)
        tmp.write_text(body, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        return {"error": f"не записалось: {exc}"}
    return {"path": clean, "size": len(body.encode("utf-8"))}
