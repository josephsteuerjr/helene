// Установщик Frame — сцена первого запуска.
//
// Кадр 1920×1080 масштабируется пропорционально до порога, ниже — обрезается.
// Навигация — невидимые четверти экрана слева и справа; явные контролы
// (кнопки окна, тема, поля ввода) четвертям не отдаются. Тема следует системе,
// ручное переопределение — кнопкой в верхней полосе.
import "./styles.css";
import { animate } from "motion";
import { COPY, MIN_SCALE, PRODUCT_NAME, STAGE } from "./config";
import { AboutScene } from "./scenes/about";
import { ConstitutionScene } from "./scenes/constitution";
import { InstallScene } from "./scenes/install";
import { KeysScene } from "./scenes/keys";
import { NameScene } from "./scenes/name";
import { ServiceScene } from "./scenes/service";
import { TypewriterScene } from "./scenes/typewriter";
import { WordmarkScene } from "./scenes/wordmark";
import { loadDefaults, setup } from "./setup";
import { T, sleep, type Dir } from "./wind";

function q<E extends Element>(sel: string): E {
  const el = document.querySelector<E>(sel);
  if (!el) throw new Error(`нет элемента ${sel}`);
  return el;
}

const viewport = q<HTMLElement>("#viewport");
const stage = q<HTMLElement>("#stage");
const gust = q<HTMLElement>(".gust");
const hint = q<HTMLElement>("#hint");
const params = new URLSearchParams(location.search);
const inTauri = "__TAURI_INTERNALS__" in window;

// ---------------------------------------------------------------- кадр

function fit() {
  const s = Math.max(Math.min(innerWidth / STAGE.w, innerHeight / STAGE.h), MIN_SCALE);
  stage.style.setProperty("--scale", s.toFixed(4));
}
addEventListener("resize", fit);
fit();

// ---------------------------------------------------------------- тема

type Mode = keyof typeof COPY.theme;
const MODES: Mode[] = ["system", "light", "dark"];
const ICONS: Record<Mode, string> = {
  system: '<circle cx="8" cy="8" r="6"/><path class="fill" d="M8 2a6 6 0 0 1 0 12z"/>',
  light:
    '<circle cx="8" cy="8" r="3.1"/><path d="M8 1.6v1.9M8 12.5v1.9M1.6 8h1.9M12.5 8h1.9M3.5 3.5l1.3 1.3M11.2 11.2l1.3 1.3M3.5 12.5l1.3-1.3M11.2 4.8l1.3-1.3"/>',
  dark: '<path d="M13.6 9.6A6 6 0 0 1 6.4 2.4a6 6 0 1 0 7.2 7.2z"/>',
};
const themeBtn = q<HTMLButtonElement>(".theme");
const themeIco = q<SVGElement>(".theme-ico");
const themeLabel = q<HTMLElement>(".theme-label");

function storage(get: string): string | null;
function storage(get: string, set: string): void;
function storage(key: string, value?: string): string | null | void {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch {
    return null;
  }
}

let mode: Mode = (() => {
  const raw = params.get("theme") ?? storage("frame.setup.theme");
  return MODES.includes(raw as Mode) ? (raw as Mode) : "system";
})();

function applyTheme() {
  if (mode === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  themeIco.innerHTML = ICONS[mode];
  themeLabel.textContent = COPY.theme[mode];
}
themeBtn.addEventListener("click", () => {
  mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
  storage("frame.setup.theme", mode);
  applyTheme();
});
applyTheme();

// ---------------------------------------------------------------- окно

const win = inTauri ? (await import("@tauri-apps/api/window")).getCurrentWindow() : null;
for (const btn of document.querySelectorAll<HTMLButtonElement>(".win")) {
  if (!win) {
    btn.disabled = true;
    continue;
  }
  const action = btn.dataset.win;
  btn.addEventListener("click", () => {
    if (action === "minimize") void win.minimize();
    else if (action === "maximize") void win.toggleMaximize();
    else void win.close();
  });
}

// ---------------------------------------------------------------- сцены

const wordmark = new WordmarkScene(q<HTMLElement>(".scene-wordmark"), PRODUCT_NAME);
const about = new AboutScene(q<HTMLElement>(".scene-about"), COPY.about);
const typewriter = new TypewriterScene(q<HTMLElement>(".scene-typewriter"), COPY.typewriter);
const name = new NameScene(q<HTMLElement>(".scene-name"));
const constitution = new ConstitutionScene(q<HTMLElement>(".scene-constitution"));
const keys = new KeysScene(q<HTMLElement>(".scene-keys"));
const service = new ServiceScene(q<HTMLElement>(".scene-service"));
const install = new InstallScene(q<HTMLElement>(".scene-install"));
type Scene = WordmarkScene | AboutScene | TypewriterScene | NameScene | ConstitutionScene | KeysScene | ServiceScene | InstallScene;
const scenes: Scene[] = [wordmark, about, typewriter, name, constitution, keys, service, install];
const byName: Record<string, number> = { about: 1, typewriter: 2, name: 3, constitution: 4, keys: 5, service: 6, install: 7 };
const nextLabel = q<HTMLElement>(".edge-next-label");
let index = 0;
let busy = false;
let touched = false;
let hintTimer = 0;

function showHint(text: string, delayMs: number) {
  clearTimeout(hintTimer);
  hintTimer = window.setTimeout(() => {
    hint.textContent = text;
    hint.classList.add("show");
  }, delayMs);
}

function hideHint() {
  clearTimeout(hintTimer);
  hint.classList.remove("show");
}

function canGo(dir: Dir): boolean {
  const n = index + dir;
  return n >= 0 && n < scenes.length;
}

/** Порыв: широкая мягкая полоса проходит через окно вместе с ветром. */
function blowGust(dir: Dir) {
  const w = innerWidth;
  const from = dir > 0 ? -0.55 * w : 1.2 * w;
  const to = dir > 0 ? 1.2 * w : -0.55 * w;
  void animate(
    gust,
    { x: [from, to], opacity: [0, 1, 1, 0] },
    { duration: 1.9 * T, ease: [0.45, 0, 0.55, 1], times: [0, 0.2, 0.7, 1] },
  );
}

async function go(dir: Dir): Promise<void> {
  touched = true;
  hideHint();
  if (busy) return;
  // Сцена может забрать шаг вперёд себе: машинка сначала дописывает текст.
  if (dir > 0 && scenes[index] === typewriter && typewriter.finishNow()) {
    refreshEdge();
    return;
  }
  // Сцена с вводом не отпускает вперёд, пока не заполнена; установка началась —
  // назад дороги нет, всё уже пишется на диск.
  const current = scenes[index];
  if (dir < 0 && current === install && install.locked) return;
  const reason = dir > 0 && "validate" in current ? current.validate() : null;
  if (reason || !canGo(dir)) {
    busy = true;
    if (reason) showHint(reason, 0);
    if ("nudge" in current) await current.nudge();
    busy = false;
    if (reason) hintTimer = window.setTimeout(hideHint, 3200);
    return;
  }
  busy = true;
  const from = scenes[index];
  const to = scenes[index + dir];
  index += dir;
  refreshEdge();
  const leaving = from.leave(dir);
  if (from === wordmark) blowGust(dir);
  // Следующее приходит, пока прошлое ещё уносит: склейки нет.
  await sleep((from === wordmark ? 1.2 : 0.35) * T * 1000);
  await Promise.all([leaving, to.enter(dir)]);
  busy = false;
  refreshEdge();
  if (to === about && !edgeSeen) showHint(COPY.hint, 1800);
}

// ---------------------------------------------------------------- навигация

let lastX = -1;
let edgeSeen = false;

function isControl(target: EventTarget | null): boolean {
  return target instanceof Element
    ? !!target.closest("[data-control], button, input, textarea, select, a, [contenteditable='true']")
    : false;
}

function refreshEdge(overControl = false) {
  let edge = "";
  if (lastX >= 0 && !overControl && !busy) {
    const x = lastX / innerWidth;
    if (x < 0.25 && canGo(-1)) edge = "left";
    else if (x > 0.75 && canGo(1)) edge = "right";
  }
  if (edge) {
    edgeSeen = true;
    hideHint();
  }
  nextLabel.textContent = scenes[index] === typewriter ? typewriter.nextLabel() : "Далее";
  viewport.dataset.edge = edge;
}

viewport.addEventListener("mousemove", (e) => {
  lastX = e.clientX;
  refreshEdge(isControl(e.target));
});
viewport.addEventListener("mouseleave", () => {
  lastX = -1;
  refreshEdge();
});
viewport.addEventListener("click", (e) => {
  if (isControl(e.target)) return;
  const x = e.clientX / innerWidth;
  if (x < 0.25) void go(-1);
  else if (x > 0.75) void go(1);
});
addEventListener("keydown", (e) => {
  if (isControl(e.target) && !(e.target instanceof HTMLButtonElement)) return;
  if (["ArrowRight", " ", "Enter", "PageDown"].includes(e.key)) {
    e.preventDefault();
    void go(1);
  } else if (["ArrowLeft", "Backspace", "PageUp"].includes(e.key)) {
    e.preventDefault();
    void go(-1);
  }
});

// ---------------------------------------------------------------- старт

async function start() {
  try {
    await Promise.all([
      document.fonts.load('400 100px "Source Serif 4"'),
      document.fonts.load('400 16px "Golos Text"'),
      document.fonts.load('500 16px "Golos Text"'),
    ]);
  } catch {
    // без шрифтов сцена всё равно идёт — на запасных
  }
  if (win) {
    // Окно родилось невидимым: показать после первого кадра, без белой вспышки.
    await new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())));
    await win.show();
    await win.setFocus();
  }
  const freeze = Number(params.get("freeze"));
  if (freeze > 0) {
    setTimeout(() => document.getAnimations().forEach((a) => a.pause()), freeze);
  }
  try {
    const d = await loadDefaults();
    setup.dir = d.dir;
  } catch {
    // без оболочки папка останется примером
  }
  const jump = params.get("scene");
  if (jump && jump in byName) {
    index = byName[jump];
    wordmark.root.hidden = true;
    const target = scenes[index] as Exclude<Scene, WordmarkScene>;
    if (params.has("static")) target.setStatic();
    else await target.enter(1);
    refreshEdge();
    return;
  }
  if (params.has("static")) {
    wordmark.root.hidden = false;
    wordmark.settle();
    wordmark.state = "settled";
    return;
  }
  const settled = await wordmark.appear();
  if (!settled) return; // человек уже пошёл дальше сам
  await sleep(1400 * T);
  if (wordmark.state === "settled" && !touched) void go(1);
}

declare global {
  interface Window {
    __frame?: {
      go: typeof go;
      index: () => number;
      busy: () => boolean;
      about: AboutScene;
      animate: typeof animate;
    };
  }
}
// Отладочная ручка для проверки из встроенного браузера; в продукте безвредна.
window.__frame = { go, index: () => index, busy: () => busy, about, animate };

void start();
