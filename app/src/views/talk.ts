// Разговор: переписка комнаты в центре, ходы событиями справа, ошибки хода —
// на месте, под сообщением, человеческим словом и с действием.
import { api } from "../api";
import { esc, fmtDay, fmtN, fmtTime, md, q } from "../lib";
import { S, WINDOW_ROOM, type Run } from "../state";

interface Msg {
  timestamp?: string;
  outgoing?: boolean;
  text?: string;
  sender_name?: string;
  sender_id?: string | number;
  topic_title?: string;
  reply_to_message_id?: number;
  media?: string;
  edited_at?: string;
}

interface Turn {
  run_id: string;
  kind?: string;
  in?: string;
  out?: string;
  ts?: string | number;
  delivery?: string;
  held?: string;
  note?: string;
}

const EV_KIND: Record<string, string> = {
  chat_turn: "сообщение",
  wake: "пробуждение",
  task_window: "окно",
  moderation: "модерация",
};

let refreshTimer = 0;
let root: HTMLElement | null = null;

function roomRuns(): Run[] {
  const key = S.room;
  return S.runs.filter((r) => {
    if (r.kind !== "chat_turn" || r.chat_id == null) return false;
    const k = String(r.chat_id);
    return k === key || (key === WINDOW_ROOM && k === "pult");
  });
}

/** Ходы, которые не дошли до конца — показываем на месте, под перепиской. */
function failedNotices(): string {
  const failed = roomRuns().filter((r) => r.terminal_status === "failed" || r.status === "failed");
  if (!failed.length) return "";
  const last = failed[0];
  const brain = S.agentState?.brain;
  const why = brain?.last_error ? `: ${esc(brain.last_error)}` : "";
  return `<div class="notice err">
    <span class="dot failed"></span>
    <span>Ход ${fmtTime(last.created_at)} не дошёл до конца${why}. ${failed.length > 1 ? `Таких ходов: ${failed.length}.` : ""}</span>
    <button class="notice-action" data-go="journal" type="button">Открыть журнал</button>
    ${brain?.last_error ? '<button class="notice-action" data-go="settings" type="button">Настройки</button>' : ""}
  </div>`;
}

function brainNotice(): string {
  const s = S.agentState;
  if (!s) return "";
  if (s.level === "error" || s.level === "warn") {
    return `<div class="notice ${s.level === "error" ? "err" : ""}">
      <span class="dot ${s.level === "error" ? "failed" : ""}"></span>
      <span>${esc(s.phrase)}${s.brain?.last_error_raw ? ` <span class="muted">· ${esc(s.brain.last_error_raw.slice(0, 120))}</span>` : ""}</span>
      ${s.action ? `<button class="notice-action" data-go="${esc(s.action.target)}" type="button">${esc(s.action.label)}</button>` : ""}
    </div>`;
  }
  return "";
}

export async function render(container: HTMLElement): Promise<void> {
  root = container;
  const title = q<HTMLElement>("#head-title");
  title.textContent = S.roomName;
  dispatchEvent(new Event("frame-room"));
  const peer = S.room;
  const rows = await api<Msg[]>("/api/chat/" + encodeURIComponent(peer) + "?n=250").catch(() => [] as Msg[]);
  const feed: string[] = [];
  let day = "";
  for (const m of rows) {
    const d = fmtDay(m.timestamp);
    if (d !== day) {
      feed.push(`<div class="day">${esc(d)}</div>`);
      day = d;
    }
    const system = !m.outgoing && m.sender_name === "Hélène";
    const own = !m.outgoing && !system; // владелец — справа, агент — слева
    const name = m.outgoing ? S.agent : system ? "Hélène" : m.sender_name || String(m.sender_id ?? "");
    const topic = m.topic_title ? ` <span class="badge">${esc(m.topic_title)}</span>` : "";
    const media = m.media ? ` <span class="muted">[${esc(m.media)}]</span>` : "";
    const edited = m.edited_at ? " · ред." : "";
    // Имя в подписи — только у чужих людей в общих комнатах: в личке сторона
    // пузыря говорит сама за себя, а имя агента и так стоит на полке.
    const showName = !m.outgoing && !system && peer !== WINDOW_ROOM && name !== S.agentState?.owner;
    feed.push(`<div class="msg ${own ? "own" : ""} ${system ? "system" : ""}">
      <div class="msg-head">${showName ? `<b>${esc(name)}</b>` : ""}${topic}<span>${fmtTime(m.timestamp)}${edited}</span></div>
      <div class="msg-body">${md(m.text || "")}${media}</div>
    </div>`);
  }
  const notices = brainNotice() + failedNotices();
  container.innerHTML = `<div class="center">${notices}<div class="feed">${
    feed.join("") ||
    `<div class="empty"><b>Здесь пока тихо</b>${peer === WINDOW_ROOM ? "Напиши первое сообщение внизу." : "Архива этой комнаты ещё нет."}</div>`
  }</div></div>`;
  for (const b of container.querySelectorAll<HTMLButtonElement>("[data-go]")) {
    b.addEventListener("click", () => dispatchEvent(new CustomEvent("frame-go", { detail: b.dataset.go })));
  }
  container.scrollTop = container.scrollHeight;
  await renderPanel();
}

// ---------------------------------------------------------------- правая панель

async function renderPanel() {
  const panel = q<HTMLElement>("#panel");
  let turns: Turn[] = [];
  try {
    turns = await api<Turn[]>("/api/chat-turns/" + encodeURIComponent(S.room));
  } catch {
    turns = [];
  }
  const byRun = new Map(S.runs.map((r) => [r.id, r]));
  const rows = turns.slice().reverse();
  panel.innerHTML =
    `<div class="panel-head">Действия · ${esc(S.roomName)}</div>` +
    (rows
      .map((t) => {
        const run = byRun.get(t.run_id) ?? ({} as Run);
        const label =
          `${EV_KIND[run.kind || t.kind || ""] || "сообщение"} · ` +
          ((t.in || "").replace(/^[^:]{1,40}:\s*/, "").slice(0, 60) || fmtTime(run.created_at));
        const status = run.status || (t.delivery === "failed" ? "failed" : "done");
        const at = run.created_at
          ? fmtTime(run.created_at)
          : t.ts
            ? fmtTime(new Date(parseFloat(String(t.ts)) * 1000).toISOString())
            : "";
        const unspoken = t.held === "unspoken" ? ' <span class="badge">без слова</span>' : "";
        return `<div class="ev ${S.evOpen.has(t.run_id) ? "open" : ""}" data-ev="${esc(t.run_id)}">
        <div class="ev-head">
          <span class="ev-title" title="${esc(t.in || "")}">${esc(label)}${unspoken}</span>
          <span class="ev-time">${at}</span>
          <span class="dot ${status === "running" ? "live" : status === "failed" ? "failed" : ""}"></span>
        </div>
        <div class="ev-steps" ${S.evOpen.has(t.run_id) ? "" : "hidden"}></div>
      </div>`;
      })
      .join("") || '<div class="empty">Действий ещё нет</div>');
  for (const el of panel.querySelectorAll<HTMLElement>(".ev")) {
    const id = el.dataset.ev!;
    el.querySelector(".ev-head")!.addEventListener("click", () => {
      if (S.evOpen.has(id)) S.evOpen.delete(id);
      else S.evOpen.add(id);
      void renderPanel();
    });
    if (S.evOpen.has(id)) void fillSteps(panel, id);
  }
}

interface RunDetail {
  manifest?: { status?: string; terminal?: { status?: string; reason?: string } };
  iterations?: Array<{
    at?: string;
    model?: string;
    ms?: number;
    text?: string;
    usage?: { in?: number; cache_read?: number; out?: number };
    tools?: Array<{ tool?: string; args?: unknown; result?: { head?: string; tail?: string } }>;
  }>;
}

async function fillSteps(panel: HTMLElement, runId: string) {
  const box = panel.querySelector<HTMLElement>(`.ev[data-ev="${CSS.escape(runId)}"] .ev-steps`);
  if (!box) return;
  let d = S.evCache.get(runId) as RunDetail | undefined;
  if (!d) {
    box.innerHTML = '<div class="muted">читаю шаги…</div>';
    try {
      d = await api<RunDetail>("/api/run/" + encodeURIComponent(runId));
    } catch (e) {
      box.innerHTML = `<div class="muted">шаги не прочитались: ${esc((e as Error).message)}</div>`;
      return;
    }
    S.evCache.set(runId, d);
    if (d.manifest?.status === "running") S.evCache.delete(runId);
  }
  const steps: string[] = [];
  for (const it of d.iterations || []) {
    const u = it.usage || {};
    steps.push(
      `<div class="ev-step"><span class="muted mono">${fmtTime(it.at)}</span> думает · ${esc(it.model || "")}` +
        `${it.ms != null ? ` · ${(it.ms / 1000).toFixed(1)} с` : ""}` +
        `${u.in != null ? ` · вход ${fmtN(u.in)}${u.cache_read ? ` (+кэш ${fmtN(u.cache_read)})` : ""} → ${fmtN(u.out || 0)}` : ""}</div>`,
    );
    for (const t of it.tools || []) {
      const args = t.args ? JSON.stringify(t.args) : "";
      const head = (t.result && (t.result.head || t.result.tail)) || "";
      steps.push(
        `<div class="ev-step"><b class="hand mono">${esc(t.tool || "?")}</b> ` +
          `${args ? `<span class="muted">${esc(args.slice(0, 70))}${args.length > 70 ? "…" : ""}</span>` : ""}` +
          `${head ? `<div class="muted">→ ${esc(String(head).slice(0, 110))}</div>` : ""}</div>`,
      );
    }
    if (it.text) {
      steps.push(`<div class="ev-step"><span class="muted">слово:</span> ${esc(it.text.slice(0, 140))}${it.text.length > 140 ? "…" : ""}</div>`);
    }
  }
  const term = d.manifest?.terminal || {};
  if (term.status) {
    steps.push(`<div class="ev-step"><b>${esc(term.status)}</b>${term.reason ? ` <span class="muted">· ${esc(String(term.reason).slice(0, 90))}</span>` : ""}</div>`);
  }
  box.innerHTML = steps.join("") || '<div class="muted">шагов нет</div>';
}

// ---------------------------------------------------------------- живое

export function onRunEvent(runId: string) {
  if (S.view !== "talk" || !root) return;
  S.evCache.delete(runId);
  clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    if (root) void render(root);
  }, 700);
}

export function afterSend() {
  clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    if (root && S.view === "talk") void render(root);
  }, 1500);
}
