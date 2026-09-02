// Настройки экраном, а не файлом: имена, модель с живой проверкой, подписка
// ChatGPT с состоянием входа, Telegram, служба с квитанцией, автозапуск, тема,
// папка данных. Пишет vera.json через оболочку; применяется перезапуском.
import { api, cfg, inTauri, post, shell } from "../api";
import QRCode from "qrcode";
import { el, esc, q, toast } from "../lib";
import { S } from "../state";

interface Config {
  agent?: { name?: string };
  phone?: { enabled?: boolean };
  update?: { url?: string };
  owner?: { name?: string; room?: string };
  model?: { framework?: string; base_url?: string; model?: string; key?: string; max_tokens?: number };
  relay?: { enabled?: boolean; port?: number };
  telegram?: { bot_token?: string; owner_id?: number | string; mode?: string; api_id?: string | number; api_hash?: string; phone?: string };
  sandbox?: { enabled?: boolean; network?: boolean };
  [k: string]: unknown;
}

interface Loaded {
  config: Config;
  path: string;
  tree: string;
  exe_dir: string;
}

type Provider = "api" | "chatgpt" | "local";

function providerOf(c: Config): Provider {
  if (c.relay?.enabled) return "chatgpt";
  const url = c.model?.base_url || "";
  if (/127\.0\.0\.1|localhost/.test(url) && !c.relay?.enabled) return "local";
  return "api";
}

function field(label: string, value: string, onInput: (v: string) => void, opts: { type?: string; mono?: boolean; placeholder?: string; hint?: string } = {}): HTMLElement {
  const wrap = el("label", "field");
  wrap.append(el("span", "field-label", label));
  const input = el("input", "field-input" + (opts.mono ? " mono" : ""));
  input.type = opts.type || "text";
  input.value = value;
  input.placeholder = opts.placeholder || "";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.addEventListener("input", () => onInput(input.value));
  wrap.append(input);
  if (opts.hint) wrap.append(el("span", "field-hint", opts.hint));
  return wrap;
}

function toggle(label: string, value: boolean, onChange: (v: boolean) => void): HTMLButtonElement {
  const b = el("button", "switch");
  b.type = "button";
  b.setAttribute("role", "switch");
  b.setAttribute("aria-checked", String(value));
  b.append(el("span", "switch-knob"), el("span", "switch-label", label));
  b.addEventListener("click", () => {
    const next = b.getAttribute("aria-checked") !== "true";
    b.setAttribute("aria-checked", String(next));
    onChange(next);
  });
  return b;
}

function button(text: string, kind: "primary" | "quiet", onClick: () => void): HTMLButtonElement {
  const b = el("button", `btn btn-${kind}`, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}


// Список моделей после проверки адреса: выбор одним нажатием вместо имени вслепую.
function setInput(wrap: HTMLElement, value: string) {
  const input = wrap.querySelector("input");
  if (input) input.value = value;
}

function renderModels(box: HTMLElement, models: string[], current: string, pick: (id: string) => void) {
  box.replaceChildren();
  if (!models.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const known = models.includes(current.trim());
  const label = el("span", "models-label", known ? "Доступные модели" : `Модели «${current.trim() || "…"}» в списке нет. Доступные:`);
  box.append(label);
  for (const id of models.slice(0, 40)) {
    const chip = el("button", "model-chip", id) ;
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(id === current.trim()));
    chip.addEventListener("click", () => {
      pick(id);
      for (const other of box.querySelectorAll(".model-chip")) other.setAttribute("aria-pressed", String(other === chip));
      label.textContent = "Доступные модели";
    });
    box.append(chip);
  }
}

function card(title: string, ...children: Array<HTMLElement | string>): HTMLElement {
  const c = el("section", "card");
  c.append(el("h3", "", title));
  for (const ch of children) c.append(typeof ch === "string" ? el("p", "field-hint", ch) : ch);
  return c;
}

export async function render(container: HTMLElement): Promise<void> {
  if (!inTauri) {
    const center = el("div", "center");
    center.append(themeCard(), phoneCard({}), el("div", "card muted", "Остальные настройки доступны в приложении: здесь окно смотрит на удалённый харнесс."));
    container.replaceChildren(center);
    bindTheme(container);
    return;
  }
  let loaded: Loaded;
  try {
    loaded = await shell<Loaded>("config_load");
  } catch (e) {
    container.innerHTML = `<div class="empty"><b>Настройки не прочитались</b>${esc((e as Error).message)}</div>`;
    return;
  }
  const c = loaded.config;
  const draft: Config = JSON.parse(JSON.stringify(c));
  draft.agent = draft.agent || {};
  draft.owner = draft.owner || {};
  draft.model = draft.model || {};
  draft.telegram = draft.telegram || {};
  let provider = providerOf(draft);
  const center = el("div", "center");

  // --- имена
  const names = el("div", "form-grid two");
  names.append(
    field("Имя агента", String(draft.agent.name || ""), (v) => (draft.agent!.name = v)),
    field("Твоё имя", String(draft.owner.name || ""), (v) => (draft.owner!.name = v)),
  );
  center.append(card("Имена", names, "Имя агента войдёт в подписи и в кадр; имя владельца нужно агенту, чтобы знать, чьё слово решает."));

  // --- модель
  const model = el("div");
  const pick = el("div", "choice");
  pick.setAttribute("role", "radiogroup");
  const panes: Record<Provider, HTMLElement> = { api: el("div"), chatgpt: el("div"), local: el("div") };
  const items: Array<[Provider, string, string]> = [
    ["api", "Ключ API", "OpenAI и любой совместимый адрес"],
    ["chatgpt", "Подписка ChatGPT", "вход в аккаунт через встроенное реле"],
    ["local", "Локальная модель", "Ollama или LM Studio на этом ПК"],
  ];
  const syncPick = () => {
    for (const b of pick.querySelectorAll<HTMLButtonElement>(".choice-item")) b.setAttribute("aria-checked", String(b.dataset.value === provider));
    for (const [k, pane] of Object.entries(panes)) pane.hidden = k !== provider;
  };
  for (const [value, title, text] of items) {
    const b = el("button", "choice-item");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.dataset.value = value;
    b.append(el("span", "choice-title", title), el("span", "choice-text", text));
    b.addEventListener("click", () => {
      provider = value;
      syncPick();
    });
    pick.append(b);
  }
  const apiGrid = el("div", "form-grid three");
  apiGrid.style.marginTop = "14px";
  const apiDraft = { base_url: provider === "api" ? String(draft.model.base_url || "") : "https://api.openai.com/v1", model: provider === "api" ? String(draft.model.model || "") : "gpt-5.2", key: provider === "api" ? String(draft.model.key || "") : "" };
  const apiModelField = field("Модель", apiDraft.model, (v) => (apiDraft.model = v), { mono: true });
  apiGrid.append(
    field("Адрес", apiDraft.base_url, (v) => (apiDraft.base_url = v), { mono: true }),
    apiModelField,
    field("Ключ", apiDraft.key, (v) => (apiDraft.key = v), { type: "password", mono: true }),
  );
  const apiModels = el("div", "models");
  apiModels.hidden = true;
  const probeRow = el("div", "actions");
  probeRow.style.marginTop = "12px";
  const probeOut = el("span", "receipt");
  probeRow.append(
    button("Проверить", "quiet", async () => {
      probeOut.className = "receipt";
      probeOut.textContent = "проверяю…";
      try {
        const r = await shell<{ ok: boolean; note: string; models?: string[] }>("probe_model", { baseUrl: apiDraft.base_url, key: apiDraft.key });
        probeOut.className = "receipt " + (r.ok ? "ok" : "err");
        probeOut.textContent = r.note;
        renderModels(apiModels, r.models || [], apiDraft.model, (id) => {
          apiDraft.model = id;
          setInput(apiModelField, id);
        });
      } catch (e) {
        probeOut.className = "receipt err";
        probeOut.textContent = String(e);
      }
    }),
    probeOut,
  );
  panes.api.append(apiGrid, probeRow, apiModels);

  const relayRow = el("div", "actions");
  relayRow.style.marginTop = "14px";
  const relayOut = el("span", "receipt");
  const relayRefresh = async () => {
    try {
      const st = await shell<string>("relay_status");
      relayOut.className = "receipt " + (st === "authorized" ? "ok" : "");
      relayOut.textContent =
        st === "authorized" ? "Вход выполнен" : st === "pending" ? "Ждём вход в браузере. Повторное нажатие отменит прежнюю попытку." : "Вход ещё не выполнен";
      loginBtn.textContent = st === "pending" ? "Начать вход заново" : "Войти в ChatGPT";
    } catch (e) {
      relayOut.textContent = String(e);
    }
  };
  let relayPoll = 0;
  const loginBtn = button("Войти в ChatGPT", "quiet", async () => {
    try {
      toast(await shell<string>("relay_login"));
      window.clearInterval(relayPoll);
      let tries = 0;
      relayPoll = window.setInterval(() => {
        void relayRefresh();
        if (++tries > 100) window.clearInterval(relayPoll);
      }, 3000);
    } catch (e) {
      toast(String(e));
    }
  });
  relayRow.append(loginBtn, relayOut);
  const chatgptDraft = { model: provider === "chatgpt" ? String(draft.model.model || "gpt-5.2") : "gpt-5.2" };
  const chatgptGrid = el("div", "form-grid two");
  chatgptGrid.style.marginTop = "14px";
  const chatgptModelField = field("Модель", chatgptDraft.model, (v) => (chatgptDraft.model = v), { mono: true });
  chatgptGrid.append(chatgptModelField);
  const chatgptModels = el("div", "models");
  chatgptModels.hidden = true;
  const chatgptProbeRow = el("div", "actions");
  const chatgptProbeOut = el("span", "receipt");
  chatgptProbeRow.append(
    button("Показать модели подписки", "quiet", async () => {
      chatgptProbeOut.className = "receipt";
      chatgptProbeOut.textContent = "спрашиваю реле…";
      try {
        const r = await shell<{ ok: boolean; note: string; models?: string[] }>("probe_model", { baseUrl: "http://127.0.0.1:5011/v1", key: String(draft.model?.key || "") });
        chatgptProbeOut.className = "receipt " + (r.ok ? "ok" : "err");
        chatgptProbeOut.textContent = r.ok ? r.note : `${r.note}. Реле поднимается вместе с программой после сохранения и перезапуска.`;
        renderModels(chatgptModels, r.models || [], chatgptDraft.model, (id) => {
          chatgptDraft.model = id;
          setInput(chatgptModelField, id);
        });
      } catch (e) {
        chatgptProbeOut.className = "receipt err";
        chatgptProbeOut.textContent = String(e);
      }
    }),
    chatgptProbeOut,
  );
  panes.chatgpt.append(el("p", "field-hint", "Реле поднимается вместе с программой и ходит в ChatGPT по подписке. Вход открывает браузер; после входа ключ не нужен."), relayRow, chatgptGrid, chatgptProbeRow, chatgptModels);
  void relayRefresh();

  const localGrid = el("div", "form-grid two");
  localGrid.style.marginTop = "14px";
  const localDraft = { base_url: provider === "local" ? String(draft.model.base_url || "") : "http://127.0.0.1:11434/v1", model: provider === "local" ? String(draft.model.model || "") : "" };
  const localModelField = field("Модель", localDraft.model, (v) => (localDraft.model = v), { mono: true, placeholder: "например, qwen3:14b" });
  localGrid.append(field("Адрес", localDraft.base_url, (v) => (localDraft.base_url = v), { mono: true }), localModelField);
  const localModels = el("div", "models");
  localModels.hidden = true;
  const localProbeRow = el("div", "actions");
  localProbeRow.style.marginTop = "12px";
  const localProbeOut = el("span", "receipt");
  localProbeRow.append(
    button("Показать модели", "quiet", async () => {
      localProbeOut.className = "receipt";
      localProbeOut.textContent = "спрашиваю…";
      try {
        const r = await shell<{ ok: boolean; note: string; models?: string[] }>("probe_model", { baseUrl: localDraft.base_url, key: "" });
        localProbeOut.className = "receipt " + (r.ok ? "ok" : "err");
        localProbeOut.textContent = r.note;
        renderModels(localModels, r.models || [], localDraft.model, (id) => {
          localDraft.model = id;
          setInput(localModelField, id);
        });
      } catch (e) {
        localProbeOut.className = "receipt err";
        localProbeOut.textContent = String(e);
      }
    }),
    localProbeOut,
  );
  panes.local.append(localGrid, localProbeRow, localModels);
  model.append(pick, panes.api, panes.chatgpt, panes.local);
  syncPick();
  center.append(card("Модель", model));

  // --- Telegram: бот или свой аккаунт агента
  const tgBox = el("div");
  let tgMode: "bot" | "account" = draft.telegram.mode === "account" ? "account" : "bot";
  const tgPick = el("div", "choice");
  tgPick.setAttribute("role", "radiogroup");
  const tgPanes = { bot: el("div"), account: el("div") };
  const syncTg = () => {
    for (const b of tgPick.querySelectorAll<HTMLButtonElement>(".choice-item")) b.setAttribute("aria-checked", String(b.dataset.value === tgMode));
    tgPanes.bot.hidden = tgMode !== "bot";
    tgPanes.account.hidden = tgMode !== "account";
  };
  for (const [value, title, text] of [
    ["bot", "Бот", "токен от @BotFather, самый простой путь"],
    ["account", "Свой аккаунт", "отдельный номер для агента, MTProto"],
  ] as Array<["bot" | "account", string, string]>) {
    const b = el("button", "choice-item");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.dataset.value = value;
    b.append(el("span", "choice-title", title), el("span", "choice-text", text));
    b.addEventListener("click", () => {
      tgMode = value;
      draft.telegram!.mode = value;
      syncTg();
    });
    tgPick.append(b);
  }
  const ownerField = field("Твой Telegram id", String(draft.telegram.owner_id || "") === "0" ? "" : String(draft.telegram.owner_id || ""), (v) => (draft.telegram!.owner_id = v), { mono: true, placeholder: "число, узнать у @userinfobot" });
  const tg = el("div", "form-grid two");
  tg.style.marginTop = "14px";
  tg.append(field("Токен бота", String(draft.telegram.bot_token || ""), (v) => (draft.telegram!.bot_token = v), { type: "password", mono: true, placeholder: "от @BotFather" }));
  tgPanes.bot.append(tg, el("p", "field-hint", "Бот — вторая дверь к агенту. Без токена агент живёт только в окне."));
  const acc = el("div", "form-grid three");
  acc.style.marginTop = "14px";
  acc.append(
    field("api_id", String(draft.telegram.api_id || ""), (v) => (draft.telegram!.api_id = v), { mono: true, placeholder: "с my.telegram.org" }),
    field("api_hash", String(draft.telegram.api_hash || ""), (v) => (draft.telegram!.api_hash = v), { type: "password", mono: true }),
    field("Телефон агента", String(draft.telegram.phone || ""), (v) => (draft.telegram!.phone = v), { mono: true, placeholder: "+7…" }),
  );
  const accRow = el("div", "actions");
  accRow.style.marginTop = "12px";
  const accOut = el("span", "receipt");
  const codeField = field("Код из Telegram", "", (v) => (accCode = v), { mono: true, placeholder: "12345" });
  const passField = field("Облачный пароль, если есть", "", (v) => (accPass = v), { type: "password", mono: true });
  let accCode = "";
  let accPass = "";
  const codeGrid = el("div", "form-grid two");
  codeGrid.style.marginTop = "12px";
  codeGrid.append(codeField, passField);
  codeGrid.hidden = true;
  const accCall = async (step: string) => {
    accOut.className = "receipt";
    accOut.textContent = "спрашиваю Telegram…";
    try {
      const r = await shell<{ ok: boolean; state?: string; username?: string; name?: string; error?: string }>("telegram_account", {
        step,
        apiId: String(draft.telegram?.api_id || ""),
        apiHash: String(draft.telegram?.api_hash || ""),
        phone: String(draft.telegram?.phone || ""),
        code: accCode,
        password: accPass,
      });
      if (r.state === "authorized") {
        accOut.className = "receipt ok";
        accOut.textContent = `Вошла: ${r.name || ""} ${r.username ? "@" + r.username : ""}`.trim();
        codeGrid.hidden = true;
      } else if (r.state === "code_sent" || r.state === "password_needed") {
        accOut.className = "receipt " + (r.ok ? "" : "err");
        accOut.textContent = r.error || "Код отправлен в Telegram агента — введи его ниже";
        codeGrid.hidden = false;
      } else if (r.ok) {
        accOut.textContent = "Вход ещё не выполнен";
      } else {
        accOut.className = "receipt err";
        accOut.textContent = r.error || "не вышло";
      }
    } catch (e) {
      accOut.className = "receipt err";
      accOut.textContent = String(e);
    }
  };
  accRow.append(
    button("Получить код", "quiet", () => void accCall("send")),
    button("Войти с кодом", "primary", () => void accCall("code")),
    button("Выйти", "quiet", () => void accCall("logout")),
    accOut,
  );
  tgPanes.account.append(
    acc,
    accRow,
    codeGrid,
    el("p", "field-hint", "Агент говорит из своего аккаунта Telegram, как человек: нужен отдельный номер и ключи приложения с my.telegram.org. Вход один раз; сессия лежит в data/telegram. Применяется перезапуском."),
  );
  tgBox.append(tgPick, tgPanes.bot, tgPanes.account, ownerField);
  syncTg();
  if (tgMode === "account" && draft.telegram.api_id) void accCall("status");
  center.append(card("Telegram", tgBox));

  // --- песочница
  draft.sandbox = draft.sandbox || {};
  const sb = el("div");
  const sbToggle = toggle("Песочница для рук", draft.sandbox.enabled !== false, (v) => (draft.sandbox!.enabled = v));
  const sbNet = toggle("Сеть из shell", draft.sandbox.network !== false, (v) => (draft.sandbox!.network = v));
  sb.append(sbToggle, sbNet);
  center.append(card("Песочница", sb, "Shell агента работает в контейнере Windows без доступа к файлам вне папки Vera; файловые руки — только внутри неё. Остальные руки (компьютер, проекты) ограда не трогает. Состояние видно на экране «Устройство». Применяется перезапуском."));

  // --- служба
  const svc = el("div", "actions");
  const svcOut = el("span", "receipt");
  const svcRefresh = async () => {
    try {
      const st = await shell<string>("service_state");
      svcOut.className = "receipt " + (st === "running" ? "ok" : "");
      svcOut.textContent = st === "running" ? "Служба работает" : st === "stopped" ? "Служба поставлена, но не запущена" : "Службы нет";
      installBtn.hidden = st !== "absent";
      removeBtn.hidden = st === "absent";
    } catch (e) {
      svcOut.textContent = String(e);
    }
  };
  const afterService = () => {
    let tries = 0;
    const poll = window.setInterval(() => {
      void svcRefresh();
      if (++tries > 10) clearInterval(poll);
    }, 2500);
  };
  const installBtn = button("Поставить службу", "quiet", async () => {
    try {
      toast(await shell<string>("install_service"));
      afterService();
    } catch (e) {
      toast(String(e));
    }
  });
  const removeBtn = button("Снять службу", "quiet", async () => {
    try {
      toast(await shell<string>("remove_service"));
      afterService();
    } catch (e) {
      toast(String(e));
    }
  });
  svc.append(installBtn, removeBtn, svcOut);
  void svcRefresh();
  center.append(card("Служба Windows", "Служба запускает агента до входа в систему и держит его, пока компьютер включён. Windows один раз попросит права администратора.", svc));

  center.append(phoneCard(draft));

  // --- автозапуск
  const auto = el("div");
  const autoToggle = toggle("Запускать при входе в Windows", false, async (v) => {
    try {
      await shell("autostart_set", { on: v });
      toast(v ? "Автозапуск включён" : "Автозапуск выключен");
    } catch (e) {
      toast(String(e));
    }
  });
  shell<boolean>("autostart_get").then((v) => autoToggle.setAttribute("aria-checked", String(v))).catch(() => {});
  auto.append(autoToggle);
  center.append(card("Автозапуск", auto));

  // --- тема
  center.append(themeCard());

  // --- данные
  const data = el("div", "actions");
  data.append(
    el("span", "mono", loaded.tree),
    button("Открыть папку", "quiet", () => void shell("open_path", { path: loaded.tree }).catch((e) => toast(String(e)))),
  );
  center.append(card("Данные агента", data, "Память, дневник, конституция и настройки лежат здесь. Перенос агента на другую машину — перенос этой папки вместе с программой."));

  // --- о программе
  draft.update = draft.update || {};
  const about = el("div");
  const aboutRow = el("div", "actions");
  const ver = el("span", "mono", "версия …");
  let aboutInfo: { version: string; exe_dir: string; log: string } | null = null;
  shell<{ version: string; exe_dir: string; log: string }>("app_info")
    .then((i) => {
      aboutInfo = i;
      ver.textContent = `версия ${i.version}`;
    })
    .catch(() => (ver.textContent = "версия видна в окне программы"));
  const updOut = el("span", "receipt");
  aboutRow.append(
    ver,
    button("Проверить обновления", "quiet", async () => {
      updOut.className = "receipt";
      updOut.textContent = "спрашиваю…";
      try {
        const r = await shell<{ current: string; latest: string; newer: boolean; url: string; notes: string }>("update_check", {
          url: String(draft.update?.url || ""),
        });
        if (r.newer) {
          updOut.className = "receipt ok";
          updOut.textContent = `Есть версия ${r.latest}. ${r.notes || ""}`.trim();
          if (r.url) aboutRow.append(button("Скачать", "primary", () => void shell("open_path", { path: r.url }).catch((e) => toast(String(e)))));
        } else {
          updOut.textContent = `Это последняя версия (${r.current}).`;
        }
      } catch (e) {
        updOut.className = "receipt err";
        updOut.textContent = String(e);
      }
    }),
    updOut,
  );
  const logsRow = el("div", "actions");
  logsRow.append(
    button("Собрать логи для поддержки", "quiet", async () => {
      try {
        const p = await shell<string>("logs_bundle");
        toast("Логи собраны: " + p);
        await shell("reveal_path", { path: p });
      } catch (e) {
        toast(String(e));
      }
    }),
    button("Открыть vera.log", "quiet", () => {
      if (aboutInfo) void shell("open_path", { path: aboutInfo.log }).catch((e) => toast(String(e)));
    }),
  );
  about.append(
    aboutRow,
    field("Адрес обновлений", String(draft.update?.url || ""), (v) => (draft.update!.url = v), {
      mono: true,
      placeholder: "https://api.github.com/repos/<владелец>/vera/releases/latest",
      hint: "Адрес выпусков на GitHub (…/releases/latest) или свой JSON с полями version, url и notes. Программа только сообщает о новой версии и даёт ссылку, сама ничего не подменяет.",
    }),
    logsRow,
  );
  center.append(card("О программе", about));

  // --- сохранить
  const save = el("div", "actions");
  const saveOut = el("span", "receipt");
  save.append(
    button("Сохранить", "primary", async () => {
      const out: Config = JSON.parse(JSON.stringify(draft));
      out.model = { ...(out.model || {}), framework: "openai" };
      delete out.relay;
      if (provider === "api") {
        out.model.base_url = apiDraft.base_url.trim();
        out.model.model = apiDraft.model.trim();
        out.model.key = apiDraft.key.trim();
      } else if (provider === "local") {
        out.model.base_url = localDraft.base_url.trim();
        out.model.model = localDraft.model.trim();
        out.model.key = "";
      } else {
        out.model.base_url = "http://127.0.0.1:5011/v1";
        out.model.model = chatgptDraft.model.trim() || "gpt-5.2";
        if (!String(out.model.key || "").startsWith("sk-frame-")) out.model.key = "sk-frame-" + Math.random().toString(16).slice(2) + Math.random().toString(16).slice(2);
        out.relay = { enabled: true, port: 5011 };
      }
      out.telegram = { ...(out.telegram || {}), owner_id: Number(String(out.telegram?.owner_id || "0").trim()) || 0, mode: tgMode };
      out.sandbox = { enabled: draft.sandbox?.enabled !== false, network: draft.sandbox?.network !== false };
      out.phone = { enabled: !!draft.phone?.enabled };
      out.setup_complete = true;
      try {
        await shell("config_save", { config: JSON.stringify(out) });
        saveOut.className = "receipt ok";
        saveOut.textContent = "Сохранено. Чтобы применить, перезапусти программу.";
        restartBtn.hidden = false;
        S.agent = String(out.agent?.name || S.agent);
      } catch (e) {
        saveOut.className = "receipt err";
        saveOut.textContent = String(e);
      }
    }),
    saveOut,
  );
  const restartBtn = button("Перезапустить сейчас", "quiet", () => void shell("restart_self").catch((e) => toast(String(e))));
  restartBtn.hidden = true;
  save.append(restartBtn);
  const saveCard = el("section", "card");
  saveCard.append(save, el("p", "field-hint", `Файл настроек: ${loaded.path}`));
  center.append(saveCard);

  container.replaceChildren(center);
  bindTheme(container);
}

function phoneCard(draft: Config): HTMLElement {
  draft.phone = draft.phone || {};
  const phone = el("div");
  const phoneToggle = toggle("Разрешить подключение телефона по сети", !!draft.phone.enabled, (v) => (draft.phone!.enabled = v));
  const phoneHint = el("p", "field-hint", "Труба начнёт слушать сеть, а не только эту машину; доступ с других устройств — только по ключу из QR. В той же Wi-Fi телефон достучится сразу. Из любой другой сети — через Tailscale: поставь его на компьютер и телефон, войди в один аккаунт, и QR возьмёт его адрес. Включение применяется перезапуском.");
  const qrRow = el("div", "actions");
  qrRow.style.marginTop = "12px";
  const qrOut = el("div", "qr-out");
  qrOut.hidden = true;
  const devicesBox = el("div", "devices");
  const drawDevices = async () => {
    let rows: Array<{ id: string; name: string; created: string }> = [];
    try {
      rows = await api("/pair/devices");
    } catch {
      rows = [];
    }
    devicesBox.replaceChildren();
    if (!rows.length) return;
    devicesBox.append(el("p", "field-label", "Подключённые устройства"));
    for (const d of rows) {
      const row = el("div", "device-row");
      row.append(el("span", "", `${d.name} · ${new Date(d.created).toLocaleDateString("ru-RU")}`));
      row.append(button("Отвязать", "quiet", async () => {
        await post("/pair/revoke", { id: d.id }).catch((e) => toast(String(e)));
        void drawDevices();
      }));
      devicesBox.append(row);
    }
  };
  qrRow.append(
    button("Показать QR", "quiet", async () => {
      try {
        const pair = await post<{ path: string; expires_in: number; uses: number }>("/pair/new", {});
        let host = location.host;
        let via = "";
        if (inTauri) {
          const port = new URL(cfg.base || "http://127.0.0.1:8094").port || "8094";
          const ts = await shell<string | null>("tailscale_ip").catch(() => null);
          const ip = ts || (await shell<string | null>("lan_ip").catch(() => null));
          if (ip) host = `${ip}:${port}`;
          via = ts
            ? "Адрес из Tailscale: телефон с Tailscale в том же аккаунте достучится из любой сети."
            : "Адрес в этой Wi-Fi: телефон должен быть в той же сети. Чтобы доставать отовсюду, поставь Tailscale на оба устройства.";
        }
        const url = `http://${host}${pair.path}`;
        const svg = await QRCode.toString(url, { type: "svg", margin: 1, width: 240, color: { dark: "#262320", light: "#00000000" } });
        qrOut.hidden = false;
        qrOut.innerHTML = `<div class="qr">${svg}</div>
          <div class="qr-text">
            <p>Открой камеру телефона и наведи на код. Ссылка живёт десять минут и годится дважды.</p>
            <p><b>iPhone:</b> страница откроется в Safari; нажми «Поделиться» → «На экран „Домой“». Второе открытие из значка допишет ключ, поэтому код и двухразовый.</p>
            ${via ? `<p>${esc(via)}</p>` : ""}
            <p class="mono qr-url">${esc(url)}</p>
          </div>`;
        if (inTauri) {
          const fw = await shell<string>("firewall_allow", { port: Number(new URL(cfg.base || "http://127.0.0.1:8094").port || 8094) }).catch((e) => String(e));
          toast(fw);
        }
      } catch (e) {
        toast("QR не собрался: " + String(e));
      }
    }),
  );
  phone.append(phoneToggle, phoneHint, qrRow, qrOut, devicesBox);
  void drawDevices();
  return card("Телефон", phone);

}

function themeCard(): HTMLElement {
  const c = el("section", "card");
  c.append(el("h3", "", "Тема"));
  const row = el("div", "choice");
  row.id = "theme-choice";
  for (const [value, title, text] of [
    ["system", "Как в Windows", "днём светлая, ночью тёмная"],
    ["light", "Светлая", "всегда, самая светлая"],
    ["dark", "Тёмная", "всегда, тёплый уголь"],
  ]) {
    const b = el("button", "choice-item");
    b.type = "button";
    b.dataset.value = value;
    b.append(el("span", "choice-title", title), el("span", "choice-text", text));
    row.append(b);
  }
  c.append(row);
  return c;
}

function bindTheme(container: HTMLElement) {
  const row = container.querySelector<HTMLElement>("#theme-choice");
  if (!row) return;
  const current = (() => {
    try {
      return localStorage.getItem("frame.theme") || "system";
    } catch {
      return "system";
    }
  })();
  const sync = (v: string) => {
    for (const b of row.querySelectorAll<HTMLButtonElement>(".choice-item")) b.setAttribute("aria-checked", String(b.dataset.value === v));
  };
  sync(current);
  for (const b of row.querySelectorAll<HTMLButtonElement>(".choice-item")) {
    b.addEventListener("click", () => {
      try {
        localStorage.setItem("frame.theme", b.dataset.value!);
      } catch {
        // без хранилища тема живёт до перезапуска
      }
      sync(b.dataset.value!);
      dispatchEvent(new Event("frame-theme"));
    });
  }
  q("#theme-choice", container);
}
