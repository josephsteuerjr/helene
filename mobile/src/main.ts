// Телефон: разговор с агентом с того же origin, что и труба. Ключ устройства
// приходит по QR (/m/?pair=<токен>) и живёт в localStorage; на iPhone страница
// в Safari и приложение на экране «Домой» — разные хранилища, поэтому токен
// пары годится дважды, а манифест несёт его в start_url.
import "./styles.css";

type Level = "ok" | "live" | "warn" | "error" | "off";

interface AgentState {
  agent: string;
  level: Level;
  phrase: string;
}

interface Msg {
  timestamp?: string;
  outgoing?: boolean;
  text?: string;
  sender_name?: string;
}

interface Room {
  key: string;
  name: string;
}

const q = <E extends Element = HTMLElement>(sel: string) => document.querySelector<E>(sel)!;
const esc = (s: unknown) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

const params = new URLSearchParams(location.search);
const standalone =
  matchMedia("(display-mode: standalone)").matches || (navigator as Navigator & { standalone?: boolean }).standalone === true;
const isApple = /iPhone|iPad|iPod/.test(navigator.userAgent);

let key = "";
try {
  key = localStorage.getItem("frame.device") || "";
} catch {
  key = "";
}

let agent = "Агент";
let room = "window";
let roomName = "Окно";
let rooms: Room[] = [];

// Манифест с токеном: чтобы установленное приложение открылось с ним же.
const pairToken = params.get("pair") || "";
const manifest = document.createElement("link");
manifest.rel = "manifest";
manifest.href = "/m/manifest.webmanifest" + (pairToken ? "?pair=" + encodeURIComponent(pairToken) : "");
document.head.append(manifest);

function withKey(path: string): string {
  if (!key) return path;
  return path + (path.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(key);
}

async function api<T = any>(path: string): Promise<T> {
  const r = await fetch(withKey(path));
  if (r.status === 403) throw new Error("нет ключа");
  if (!r.ok) throw new Error(path + ": " + r.status);
  return r.json();
}

async function post<T = any>(path: string, body: unknown): Promise<T> {
  const r = await fetch(withKey(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.status === 403) throw new Error("нет ключа");
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ---------------------------------------------------------------- спаривание

function showPair(title: string, text: string, action?: { label: string; onClick: () => void }) {
  const box = q("#pair");
  box.hidden = false;
  box.innerHTML = `<h1>${esc(title)}</h1><p>${esc(text)}</p>`;
  if (action) {
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = action.label;
    b.addEventListener("click", action.onClick);
    box.append(b);
  }
}

async function redeem(token: string): Promise<boolean> {
  try {
    const r = await fetch("/pair/redeem?token=" + encodeURIComponent(token));
    if (!r.ok) {
      showPair("Код устарел", "Ссылка из QR живёт десять минут и годится дважды. Покажи QR на компьютере заново и открой его снова.");
      return false;
    }
    const d = (await r.json()) as { key: string; agent: string };
    key = d.key;
    agent = d.agent || agent;
    try {
      localStorage.setItem("frame.device", key);
    } catch {
      // без хранилища ключ живёт до перезагрузки страницы
    }
    return true;
  } catch {
    showPair("Нет связи", "Компьютер с агентом не отвечает. Телефон должен быть в той же Wi-Fi, что и компьютер, или подключён к Tailscale.");
    return false;
  }
}

// ---------------------------------------------------------------- экран

const md = (s: string) =>
  esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .split(/\n{2,}/)
    .map((p) => "<p>" + p.replace(/\n/g, "<br>") + "</p>")
    .join("");

function fmtTime(iso?: string) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function fmtDay(iso?: string) {
  const d = new Date(iso ?? "");
  if (isNaN(d.getTime())) return "";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const that = new Date(d);
  that.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - that.getTime()) / 86400000);
  if (diff === 0) return "Сегодня";
  if (diff === 1) return "Вчера";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

async function renderState() {
  try {
    const s = await api<AgentState>("/api/state");
    agent = s.agent || agent;
    q("#top-name").textContent = agent;
    document.title = agent;
    q("#top-state").dataset.level = s.level;
    q("#top-phrase").textContent = s.phrase;
  } catch (e) {
    q("#top-state").dataset.level = "off";
    q("#top-phrase").textContent = (e as Error).message === "нет ключа" ? "Нет ключа" : "Нет связи";
  }
}

async function loadRooms() {
  const chats = await api<Array<{ peer_id: string; title?: string }>>("/api/chats").catch(() => []);
  rooms = [{ key: "window", name: "Окно" }];
  for (const c of chats) {
    const k = String(c.peer_id);
    if (k === "window" || k === "pult") continue;
    rooms.push({ key: k, name: c.title || "чат " + k });
  }
  const box = q("#rooms");
  box.replaceChildren();
  if (rooms.length < 2) return;
  for (const r of rooms) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "room";
    b.textContent = r.name;
    b.setAttribute("aria-current", String(r.key === room));
    b.addEventListener("click", () => {
      room = r.key;
      roomName = r.name;
      void loadRooms();
      void renderFeed();
    });
    box.append(b);
  }
}

async function renderFeed() {
  const feed = q("#feed");
  const rows = await api<Msg[]>("/api/chat/" + encodeURIComponent(room) + "?n=120").catch(() => [] as Msg[]);
  const html: string[] = [];
  let day = "";
  for (const m of rows) {
    const d = fmtDay(m.timestamp);
    if (d && d !== day) {
      html.push(`<div class="day">${esc(d)}</div>`);
      day = d;
    }
    const system = !m.outgoing && m.sender_name === "Vera";
    const own = !m.outgoing && !system;
    html.push(`<div class="msg ${own ? "own" : ""} ${system ? "system" : ""}">
      <div class="msg-time">${fmtTime(m.timestamp)}</div>
      <div class="msg-body">${md(m.text || "")}</div></div>`);
  }
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200;
  feed.innerHTML = html.join("") || '<div class="empty">Здесь пока тихо</div>';
  if (nearBottom || !feed.dataset.ready) feed.scrollTop = feed.scrollHeight;
  feed.dataset.ready = "1";
  q("#send").setAttribute("title", roomName);
}

const say = q<HTMLTextAreaElement>("#say");
const send = q<HTMLButtonElement>("#send");
say.addEventListener("input", () => {
  say.style.height = "auto";
  say.style.height = Math.min(say.scrollHeight, 140) + "px";
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
  send.disabled = true;
  try {
    await post("/api/say", room === "window" ? { text } : { text, chat: room });
    say.value = "";
    say.style.height = "auto";
    setTimeout(() => void renderFeed(), 1200);
  } catch (e) {
    notice("Не ушло: " + (e as Error).message);
  }
  send.disabled = false;
}

function notice(text: string, action?: { label: string; onClick: () => void }) {
  const box = q("#notice");
  box.hidden = false;
  box.innerHTML = esc(text);
  if (action) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = action.label;
    b.addEventListener("click", () => {
      box.hidden = true;
      action.onClick();
    });
    box.append(document.createElement("br"), b);
  }
}

// ---------------------------------------------------------------- старт

async function start() {
  if (pairToken) {
    const ok = await redeem(pairToken);
    if (!ok) return;
    // Токен из адреса убираем только вне установленного приложения: там он и
    // так остаётся в start_url манифеста, но уже потрачен и больше не нужен.
    history.replaceState(null, "", "/m/");
  }
  if (!key) {
    showPair("Нужен ключ", "Открой на компьютере Настройки → Телефон → «Показать QR» и наведи камеру. Ссылка подключит этот телефон.");
    return;
  }
  q("#pair").hidden = true;
  await renderState();
  if (q("#top-state").dataset.level === "off" && q("#top-phrase").textContent === "Нет ключа") {
    key = "";
    try {
      localStorage.removeItem("frame.device");
    } catch {
      // нечего убирать
    }
    showPair("Ключ отозван", "Этот телефон больше не подключён. Покажи QR на компьютере заново.");
    return;
  }
  if (isApple && !standalone) {
    notice("Чтобы открывать как приложение: «Поделиться» → «На экран „Домой“». Первое открытие из значка допишет ключ само.", {
      label: "Понятно",
      onClick: () => {},
    });
  }
  await loadRooms();
  await renderFeed();
  setInterval(() => void renderState(), 8000);
  setInterval(() => void renderFeed(), 6000);
}

void start();
