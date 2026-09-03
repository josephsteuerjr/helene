// Hélène — основная программа. Полка слева (разделы и разговоры), разговор в
// центре, ходы справа; состояние агента одной фразой в шапке; настройки
// экраном, а не файлом.
import "./styles/app.css";
import { api, cfg, connect, inTauri, onConnection, onEvent, post, shell } from "./api";
import { esc, fmtN, fmtTs, q, toast } from "./lib";
import { S, WINDOW_ROOM, type AgentState, type Room, type Run, type View } from "./state";
import * as talk from "./views/talk";
import * as plans from "./views/plans";
import * as frame from "./views/frame";
import * as files from "./views/files";
import * as journal from "./views/journal";
import * as anatomy from "./views/anatomy";
import * as settings from "./views/settings";

const view = q<HTMLElement>("#view");
const app = q<HTMLElement>("#app");
const railNav = q<HTMLElement>("#rail-nav");
const railBottom = q<HTMLElement>("#rail-bottom");
const roomsBox = q<HTMLElement>("#rooms");
const headKicker = q<HTMLElement>("#head-kicker");
const headTitle = q<HTMLElement>("#head-title");
const statePill = q<HTMLElement>("#state");
const stateText = q<HTMLElement>("#state-text");
const stateAction = q<HTMLButtonElement>("#state-action");
const pulseBox = q<HTMLElement>("#pulse");
const alarmBox = q<HTMLElement>("#alarm");
const composer = q<HTMLElement>("#composer");
const composerTarget = q<HTMLElement>("#composer-target");
const say = q<HTMLTextAreaElement>("#say");
const send = q<HTMLButtonElement>("#send");
const panel = q<HTMLElement>("#panel");

// ---------------------------------------------------------------- тема

type Theme = "system" | "light" | "dark";
function applyTheme() {
  // По умолчанию как в Windows: светлая днём не слепит ночью. Светлый вариант —
  // самый светлый из набора (слово владельца 02.09).
  let mode: Theme = "system";
  try {
    const raw = localStorage.getItem("frame.theme");
    if (raw === "system" || raw === "dark" || raw === "light") mode = raw;
  } catch {
    // без хранилища — светлая
  }
  if (mode === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
}
applyTheme();
addEventListener("frame-theme", applyTheme);

// ---------------------------------------------------------------- окно

if (inTauri) {
  document.documentElement.classList.add("native");
  import("@tauri-apps/api/window").then(({ getCurrentWindow }) => {
    const w = getCurrentWindow();
    for (const b of document.querySelectorAll<HTMLButtonElement>(".win")) {
      const action = b.dataset.win;
      b.addEventListener("click", () => {
        if (action === "minimize") void w.minimize();
        else if (action === "maximize") void w.toggleMaximize();
        else void w.close();
      });
    }
  });
}

// ---------------------------------------------------------------- разделы

const ICONS: Record<View, string> = {
  talk: '<path d="M4 5.5h12v8H8l-4 3z"/>',
  plans: '<rect x="3.5" y="4" width="13" height="12" rx="2"/><path d="M6.5 2.8v2.5M13.5 2.8v2.5M6.5 8h7M6.5 11h4"/>',
  frame: '<path d="M3.5 6.5V4.8c0-.7.6-1.3 1.3-1.3h1.7M13.5 3.5h1.7c.7 0 1.3.6 1.3 1.3v1.7M16.5 13.5v1.7c0 .7-.6 1.3-1.3 1.3h-1.7M6.5 16.5H4.8c-.7 0-1.3-.6-1.3-1.3v-1.7"/><circle cx="10" cy="10" r="2.6"/>',
  files: '<path d="M5 3.5h7l3 3V16a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M11.8 3.8v3h3M6.7 10h6.6M6.7 13h4.5"/>',
  journal: '<path d="M10 3.2 17 16H3L10 3.2Z"/><path d="M10 7.5v4M10 14.1v.1"/>',
  anatomy: '<circle cx="10" cy="10" r="6.7"/><path d="M10 9v4M10 6.7v.1"/>',
  settings: '<circle cx="10" cy="10" r="2.6"/><path d="M10 2.8v2M10 15.2v2M2.8 10h2M15.2 10h2M4.9 4.9l1.4 1.4M13.7 13.7l1.4 1.4M4.9 15.1l1.4-1.4M13.7 6.3l1.4-1.4"/>',
};

const SECTIONS: Array<{ id: View; label: string; kicker: string }> = [
  { id: "talk", label: "Чат", kicker: "Чат" },
  { id: "plans", label: "Задачи", kicker: "План агента" },
  { id: "frame", label: "Контекст", kicker: "Что видит модель" },
  { id: "files", label: "Файлы", kicker: "Память агента в файлах" },
  { id: "journal", label: "Журнал", kicker: "Ошибки и пропуски" },
  { id: "anatomy", label: "Система", kicker: "Как это устроено" },
];

function railButton(id: View, label: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "rail-item";
  b.dataset.view = id;
  b.innerHTML = `<svg viewBox="0 0 20 20" aria-hidden="true">${ICONS[id]}</svg><span>${esc(label)}</span>`;
  b.addEventListener("click", () => void show(id));
  return b;
}

for (const s of SECTIONS) railNav.append(railButton(s.id, s.label));
railBottom.append(railButton("settings", "Настройки"));

// Панели сворачиваются как в IDE и помнят состояние.
const railBtn = q<HTMLButtonElement>("#toggle-rail");
const panelBtn = q<HTMLButtonElement>("#toggle-panel");
function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}
function setCollapsed(which: "rail" | "panel", on: boolean) {
  app.classList.toggle(which + "-collapsed", on);
  try {
    localStorage.setItem("frame." + which, on ? "1" : "0");
  } catch {
    // без хранилища панели просто не запомнятся
  }
  (which === "rail" ? railBtn : panelBtn).setAttribute("aria-pressed", String(!on));
}
setCollapsed("rail", readFlag("frame.rail"));
setCollapsed("panel", readFlag("frame.panel"));
railBtn.addEventListener("click", () => setCollapsed("rail", !app.classList.contains("rail-collapsed")));
panelBtn.addEventListener("click", () => setCollapsed("panel", !app.classList.contains("panel-collapsed")));
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
  if (e.code === "KeyB") {
    e.preventDefault();
    railBtn.click();
  } else if (e.code === "KeyJ") {
    e.preventDefault();
    panelBtn.click();
  }
});

function syncRail() {
  for (const b of document.querySelectorAll<HTMLButtonElement>(".rail-item")) {
    b.setAttribute("aria-current", b.dataset.view === S.view ? "page" : "false");
  }
}

const views: Record<View, { render: (root: HTMLElement) => Promise<void> }> = {
  talk,
  plans,
  frame,
  files,
  journal,
  anatomy,
  settings,
};

export async function show(id: View) {
  S.view = id;
  syncRail();
  const section = SECTIONS.find((s) => s.id === id);
  headKicker.textContent = section ? section.kicker : "Программа";
  if (id !== "talk") headTitle.textContent = section?.label ?? "Настройки";
  const talking = id === "talk";
  composer.hidden = !talking;
  panel.hidden = !talking;
  app.classList.toggle("with-panel", talking);
  panelBtn.hidden = !talking;
  view.innerHTML = '<div class="empty">читаю…</div>';
  try {
    await views[id].render(view);
  } catch (e) {
    view.innerHTML = `<div class="empty"><b>Не прочиталось</b>${esc((e as Error).message)}</div>`;
  }
}

// ---------------------------------------------------------------- комнаты

function roomsFromRuns(runs: Run[], chats: Array<{ peer_id: string; title?: string; messages?: number }>): Room[] {
  const byKey = new Map<string, Room>();
  byKey.set(WINDOW_ROOM, { key: WINDOW_ROOM, name: "Окно", live: false, count: 0 });
  for (const c of chats) {
    const key = String(c.peer_id);
    if (key === "pult") continue;
    if (!byKey.has(key)) byKey.set(key, { key, name: c.title || "чат " + key, live: false, count: c.messages || 0 });
  }
  for (const r of runs) {
    if (r.kind !== "chat_turn" || r.chat_id == null) continue;
    let key = String(r.chat_id);
    if (key === "pult") key = WINDOW_ROOM;
    const room = byKey.get(key) ?? { key, name: r.chat_title || "чат " + key, live: false, count: 0 };
    if (r.status === "running") room.live = true;
    room.count += 1;
    if (!byKey.has(key)) byKey.set(key, room);
  }
  return [...byKey.values()];
}

function renderRooms() {
  roomsBox.replaceChildren(
    ...S.rooms.map((room) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "room";
      b.setAttribute("aria-current", String(room.key === S.room));
      b.innerHTML = `<span class="room-name">${esc(room.name)}</span>` +
        (room.count ? `<span class="room-count">${room.count}</span>` : "") +
        `<span class="dot ${room.live ? "live" : ""}"></span>`;
      b.addEventListener("click", () => {
        S.room = room.key;
        S.roomName = room.name;
        renderRooms();
        void show("talk");
      });
      return b;
    }),
  );
  q<HTMLElement>("#rail-agent").textContent = S.agent;
}

export async function loadRooms() {
  const [runs, chats] = await Promise.all([
    api<Run[]>("/api/runs?limit=200"),
    api<Array<{ peer_id: string; title?: string; messages?: number }>>("/api/chats").catch(() => []),
  ]);
  S.runs = runs;
  S.rooms = roomsFromRuns(runs, chats);
  const current = S.rooms.find((r) => r.key === S.room);
  if (current) S.roomName = current.name;
  renderRooms();
}

// ---------------------------------------------------------------- состояние

function renderState(s: AgentState | null, connected: boolean) {
  if (!connected) {
    statePill.dataset.level = "off";
    stateText.textContent = "Нет связи с харнессом";
    stateAction.hidden = true;
    return;
  }
  if (!s) return;
  statePill.dataset.level = s.level;
  stateText.textContent = s.phrase;
  if (s.action) {
    stateAction.hidden = false;
    stateAction.textContent = s.action.label;
    stateAction.onclick = () => {
      if (s.action?.target === "settings") void show("settings");
      else if (s.action?.target === "restart") void shell("restart_self").catch((e) => toast(String(e)));
    };
  } else {
    stateAction.hidden = true;
  }
  // «Долгое молчание» без долга — сигнал разработчику про автономию, не тревога
  // для человека: агент просто ничем не занят. Остальные тревоги показываем.
  const alarms = (s.alarms || []).filter((a) => a.kind !== "long_silence");
  alarmBox.hidden = !alarms.length;
  alarmBox.innerHTML = alarms.map((a) => `<span>⚠ ${esc(a.text)}</span>`).join(" · ");
}

export async function refreshState() {
  try {
    const s = await api<AgentState>("/api/state");
    S.agentState = s;
    if (s.agent) {
      S.agent = s.agent;
      q<HTMLElement>("#rail-agent").textContent = s.agent;
    }
    renderState(s, true);
  } catch {
    // связь решает пилюля через onConnection
  }
}

async function refreshPulse() {
  try {
    const p = await api("/api/pulse");
    const l = p.last || {};
    if (!l.ts) {
      pulseBox.textContent = "";
      return;
    }
    const total = (l.in || 0) + (l.cached || 0);
    const share = total ? Math.round((100 * (l.cached || 0)) / total) : 0;
    pulseBox.innerHTML =
      `${fmtTs(l.ts)} · <b>${esc(l.model || "")}</b> · кэш <span class="cachebar"><i style="width:${share}%"></i></span>${share}% · ` +
      `${fmtN(total)}→${fmtN(l.out || 0)}${l.err ? ' · <span class="err-msg">ошибка</span>' : ""}`;
  } catch {
    // тихо
  }
}

// ---------------------------------------------------------------- композер

function syncComposer() {
  composerTarget.textContent = S.room === WINDOW_ROOM ? "" : `в «${S.roomName}»`;
}

say.addEventListener("input", () => {
  say.style.height = "auto";
  say.style.height = Math.min(say.scrollHeight, 180) + "px";
});
say.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void doSend();
  }
});
send.addEventListener("click", () => void doSend());

async function doSend() {
  const text = say.value.trim();
  if (!text) return;
  const chat = S.room === WINDOW_ROOM ? "" : S.room;
  send.disabled = true;
  try {
    const data = await post("/api/say", chat ? { text, chat } : { text });
    say.value = "";
    say.style.height = "auto";
    if (chat) toast(`Ушло в «${S.roomName}»: ход пойдёт там`);
    else if (!data.midturn) toast("Ушло запиской: агент прочитает на следующем ходу");
    talk.afterSend();
  } catch (e) {
    toast("Не ушло: " + (e as Error).message);
  }
  send.disabled = false;
}

// ---------------------------------------------------------------- события

onConnection((ok) => {
  S.connected = ok;
  if (ok) {
    void refreshState();
    void loadRooms().then(() => {
      if (S.view === "talk") void show("talk");
    });
    void refreshPulse();
  } else {
    renderState(S.agentState, false);
  }
});

onEvent((ev) => {
  if (ev.t === "health") void refreshState();
  if (ev.t === "llm") void refreshPulse();
  if (ev.t === "run") {
    void loadRooms();
    talk.onRunEvent(String(ev.run_id ?? ""));
    void refreshState();
  }
  if (ev.t === "skips" && S.view === "journal") void show("journal");
});

// ---------------------------------------------------------------- старт

S.agent = (cfg.agent || "").trim() || "Агент";
q<HTMLElement>("#rail-agent").textContent = S.agent;
syncComposer();
addEventListener("frame-room", syncComposer);
addEventListener("frame-go", (e) => void show((e as CustomEvent<View>).detail));
connect();
void refreshState();
loadRooms()
  .then(() => show("talk"))
  .catch(() => {
    view.innerHTML = `<div class="empty"><b>${esc(S.agent)} сейчас не на связи</b>Окно продолжит попытки само. Можно оставить его открытым.</div>`;
    renderState(null, false);
  });
void refreshPulse();
setInterval(() => void refreshState(), 8000);
setInterval(() => void refreshPulse(), 20000);
