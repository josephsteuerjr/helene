// Сцена «Ключи»: откуда приходит модель и нужен ли Telegram. Рядом с каждым
// полем — зачем оно, человеческими словами.
import { FormScene } from "./base";
import { button, choice, el, explain, field } from "./form";
import { probeModel, relayLogin, relayModels, relayStatus, setup, type Effort, type Provider } from "../setup";

export class KeysScene extends FormScene {
  private panes: Record<Provider, HTMLElement>;

  constructor(root: HTMLElement) {
    super(root);
    const head = el("h2", "form-head");
    head.append(el("span", "line", "Откуда приходит модель"));
    const lead = el("p", "form-lead",
      "Модель — единственное, что агент берёт извне. Всё остальное остаётся на этом компьютере.");

    const picker = choice<Provider>({
      value: setup.provider,
      items: [
        { value: "api", title: "Ключ API", text: "OpenAI и любой совместимый адрес" },
        { value: "anthropic", title: "Anthropic API", text: "Anthropic, Z.ai и совместимые" },
        { value: "chatgpt", title: "Подписка ChatGPT", text: "вход в аккаунт, без ключа" },
        { value: "local", title: "Локальная модель", text: "Ollama или LM Studio на этом ПК" },
      ],
      onChange: (v) => {
        setup.provider = v;
        this.showPane(v);
      },
    });

    const api = el("div", "pane");
    const apiGrid = el("div", "form-grid three");
    const apiModelField = field({ label: "Модель", value: setup.api.model, mono: true, onInput: (v) => (setup.api.model = v) });
    apiGrid.append(
      field({ label: "Адрес", value: setup.api.base_url, mono: true, onInput: (v) => (setup.api.base_url = v) }),
      apiModelField,
      field({ label: "Ключ", value: setup.api.key, type: "password", mono: true, placeholder: "sk-…", onInput: (v) => (setup.api.key = v) }),
    );
    const apiModels = el("div", "models");
    apiModels.hidden = true;
    const probeRow = el("div", "actions");
    const probeOut = el("span", "receipt");
    probeRow.append(
      button("Проверить ключ", "quiet", async () => {
        probeOut.className = "receipt";
        probeOut.textContent = "проверяю…";
        const r = await probeModel(setup.api.base_url, setup.api.key).catch((e) => ({ ok: false, note: String(e), models: [] as string[] }));
        probeOut.className = "receipt " + (r.ok ? "ok" : "err");
        probeOut.textContent = r.note;
        renderModels(apiModels, r.models || [], setup.api.model, (id) => {
          setup.api.model = id;
          setInput(apiModelField, id);
        });
      }),
      probeOut,
    );
    api.append(apiGrid, probeRow, apiModels, explain("Зачем ключ",
      "Ключ даёт агенту доступ к модели напрямую. Он лежит только в файле настроек на этом диске и никуда больше не уходит. Адрес и модель — любые OpenAI-совместимые."));

    // Anthropic-совместимые: Anthropic и Z.ai (тот же протокол, другой адрес).
    const anthropic = el("div", "pane");
    const anthGrid = el("div", "form-grid three");
    const anthBase = field({ label: "Адрес", value: setup.anthropic.base_url, mono: true, onInput: (v) => (setup.anthropic.base_url = v) });
    const anthModel = field({ label: "Модель", value: setup.anthropic.model, mono: true, onInput: (v) => (setup.anthropic.model = v) });
    anthGrid.append(
      anthBase,
      anthModel,
      field({ label: "Ключ", value: setup.anthropic.key, type: "password", mono: true, placeholder: "sk-ant-…", onInput: (v) => (setup.anthropic.key = v) }),
    );
    const anthPresets = el("div", "actions");
    for (const [label, url] of [["Z.ai (GLM)", "https://api.z.ai/api/anthropic"], ["Anthropic", "https://api.anthropic.com"]] as Array<[string, string]>) {
      anthPresets.append(button(label, "quiet", () => {
        setup.anthropic.base_url = url;
        setInput(anthBase, url);
      }));
    }
    const anthModels = el("div", "models");
    anthModels.hidden = true;
    const anthProbeRow = el("div", "actions");
    const anthOut = el("span", "receipt");
    anthProbeRow.append(
      button("Проверить ключ", "quiet", async () => {
        anthOut.className = "receipt";
        anthOut.textContent = "проверяю…";
        const r = await probeModel(setup.anthropic.base_url, setup.anthropic.key, "anthropic").catch((e) => ({ ok: false, note: String(e), models: [] as string[] }));
        anthOut.className = "receipt " + (r.ok ? "ok" : "err");
        anthOut.textContent = r.note;
        renderModels(anthModels, r.models || [], setup.anthropic.model, (id) => {
          setup.anthropic.model = id;
          setInput(anthModel, id);
        });
      }),
      anthOut,
    );
    anthropic.append(anthGrid, anthPresets, anthProbeRow, anthModels, explain("Тот же ключ, другой протокол",
      "Anthropic и Z.ai говорят на протоколе Anthropic Messages. Адрес и модель — из их документации; проверка спросит список моделей, если сервер его отдаёт."));

    const chatgpt = el("div", "pane");
    const relayRow = el("div", "actions");
    const relayOut = el("span", "receipt");
    const relayRefresh = async () => {
      const st = await relayStatus().catch(() => "no-auth" as const);
      relayOut.className = "receipt " + (st === "authorized" ? "ok" : "");
      relayOut.textContent =
        st === "authorized"
          ? "Вход выполнен, он переедет вместе с установкой"
          : st === "pending"
            ? "Ждём вход в браузере. Если вкладка закрылась, нажми ещё раз: прежняя попытка отменится."
            : "Вход ещё не выполнен";
      loginBtn.textContent = st === "pending" ? "Начать вход заново" : "Войти в ChatGPT";
    };
    let poll = 0;
    const loginBtn = button("Войти в ChatGPT", "quiet", async () => {
      relayOut.className = "receipt";
      relayOut.textContent = await relayLogin().catch((e) => String(e));
      window.clearInterval(poll);
      let tries = 0;
      poll = window.setInterval(() => {
        void relayRefresh();
        if (++tries > 100) window.clearInterval(poll);
      }, 3000);
    });
    relayRow.append(loginBtn, relayOut);
    void relayRefresh();
    const chatgptGrid = el("div", "form-grid two");
    const chatgptModel = field({ label: "Модель", value: setup.chatgpt_model, mono: true, onInput: (v) => (setup.chatgpt_model = v) });
    chatgptGrid.append(chatgptModel);
    const relayModelsBox = el("div", "models");
    relayModelsBox.hidden = true;
    const relayModelsRow = el("div", "actions");
    const relayModelsOut = el("span", "receipt");
    relayModelsRow.append(
      button("Показать модели реле", "quiet", async () => {
        relayModelsOut.className = "receipt";
        relayModelsOut.textContent = "спрашиваю реле…";
        try {
          const ids = await relayModels();
          relayModelsOut.className = "receipt ok";
          relayModelsOut.textContent = `Реле знает моделей: ${ids.length}`;
          renderModels(relayModelsBox, ids, setup.chatgpt_model, (id) => {
            setup.chatgpt_model = id;
            setInput(chatgptModel, id);
          });
        } catch (e) {
          relayModelsOut.className = "receipt err";
          relayModelsOut.textContent = String(e);
        }
      }),
      relayModelsOut,
    );
    chatgpt.append(chatgptGrid, relayModelsRow, relayModelsBox, relayRow, explain("Как это работает",
      "Встроенное реле ходит в ChatGPT по твоей подписке. Вход откроет браузер; учётные данные переедут в установленную программу. Ключ не нужен. Войти можно и позже, в настройках; там же после входа виден список моделей подписки, и модель можно сменить."));

    const local = el("div", "pane");
    const localGrid = el("div", "form-grid two");
    const localModelField = field({ label: "Модель", value: setup.local.model, mono: true, placeholder: "например, qwen3:14b", onInput: (v) => (setup.local.model = v) });
    localGrid.append(
      field({ label: "Адрес", value: setup.local.base_url, mono: true, onInput: (v) => (setup.local.base_url = v) }),
      localModelField,
    );
    const localModels = el("div", "models");
    localModels.hidden = true;
    const localProbeRow = el("div", "actions");
    const localProbeOut = el("span", "receipt");
    localProbeRow.append(
      button("Показать модели", "quiet", async () => {
        localProbeOut.className = "receipt";
        localProbeOut.textContent = "спрашиваю…";
        const r = await probeModel(setup.local.base_url, "").catch((e) => ({ ok: false, note: String(e), models: [] as string[] }));
        localProbeOut.className = "receipt " + (r.ok ? "ok" : "err");
        localProbeOut.textContent = r.note;
        renderModels(localModels, r.models || [], setup.local.model, (id) => {
          setup.local.model = id;
          setInput(localModelField, id);
        });
      }),
      localProbeOut,
    );
    local.append(localGrid, localProbeRow, localModels, explain("Всё остаётся здесь",
      "Ollama или LM Studio на этом компьютере. Ключа нет, в сеть ничего не уходит. Модель должна быть уже скачана."));

    this.panes = { api, anthropic, chatgpt, local };
    const paneBox = el("div", "panes");
    paneBox.append(api, anthropic, chatgpt, local);

    // Усилие рассуждения — отдельный селектор, начальное значение (слово владельца).
    const effortBox = el("div", "effort");
    effortBox.append(el("h3", "form-sub", "Усилие рассуждения"));
    const effortRow = el("div", "models");
    const efforts: Array<[Effort, string]> = [["", "по умолчанию модели"], ["low", "low"], ["medium", "medium"], ["high", "high"], ["xhigh", "xhigh"]];
    for (const [value, label] of efforts) {
      const chip = el("button", "model-chip", label) as HTMLButtonElement;
      chip.type = "button";
      chip.setAttribute("aria-pressed", String(setup.reasoning_effort === value));
      chip.addEventListener("click", () => {
        setup.reasoning_effort = value;
        for (const other of effortRow.querySelectorAll(".model-chip")) other.setAttribute("aria-pressed", String(other === chip));
      });
      effortRow.append(chip);
    }
    effortBox.append(effortRow, explain("",
      "Сколько модель думает перед ответом. Не у всех моделей есть эта ручка; где нет — значение просто не уйдёт. Менять можно в настройках."));

    const tg = el("div", "tg");
    tg.append(el("h3", "form-sub", "Telegram, если нужен"));
    const tgGrid = el("div", "form-grid two");
    tgGrid.append(
      field({ label: "Токен бота", value: setup.telegram.bot_token, type: "password", mono: true, placeholder: "от @BotFather", onInput: (v) => (setup.telegram.bot_token = v) }),
      field({ label: "Твой Telegram id", value: setup.telegram.owner_id, mono: true, placeholder: "число, узнать у @userinfobot", onInput: (v) => (setup.telegram.owner_id = v) }),
    );
    tg.append(tgGrid, explain("",
      "Бот — вторая дверь к агенту, кроме окна. Токен выдаёт @BotFather, свой id подскажет @userinfobot: без него агент не поймёт, кто из пишущих его владелец. Можно оставить пустым. Свой аккаунт Telegram для агента (не бот) подключается после установки, в настройках."));

    this.mount(head, lead, picker, paneBox, effortBox, tg);
    this.showPane(setup.provider);
  }

  private showPane(p: Provider) {
    for (const [k, pane] of Object.entries(this.panes)) pane.hidden = k !== p;
  }

  validate(): string | null {
    if (setup.provider === "api") {
      if (!setup.api.base_url.trim()) return "Нужен адрес модели";
      if (!setup.api.model.trim()) return "Назови модель";
      if (!setup.api.key.trim()) return "Нужен ключ API";
    }
    if (setup.provider === "local") {
      if (!setup.local.base_url.trim()) return "Нужен адрес локальной модели";
      if (!setup.local.model.trim()) return "Назови локальную модель";
    }
    const tg = setup.telegram;
    if (tg.bot_token.trim() && !/^\d+$/.test(tg.owner_id.trim())) {
      return "Для Telegram нужен и твой id: число";
    }
    return null;
  }
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
    const chip = el("button", "model-chip", id) as HTMLButtonElement;
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
