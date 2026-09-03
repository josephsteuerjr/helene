// Состояние установки и мост к оболочке.
//
// Всё, что человек вводит по сценам, живёт здесь одним объектом; установка в
// конце отдаёт его оболочке целиком. В браузерном превью оболочки нет —
// команды отвечают заглушками, чтобы сцены можно было смотреть и править.
import soulCanon from "../../../resources/SOUL.md?raw";

export type Provider = "api" | "anthropic" | "chatgpt" | "local";
export type Effort = "" | "low" | "medium" | "high" | "xhigh";

export interface Setup {
  agent: string;
  owner: string;
  constitution: string;
  accepted: boolean;
  provider: Provider;
  chatgpt_model: string;
  reasoning_effort: Effort;
  api: { base_url: string; model: string; key: string };
  anthropic: { base_url: string; model: string; key: string };
  local: { base_url: string; model: string };
  telegram: { bot_token: string; owner_id: string };
  service: boolean;
  dir: string;
}

export interface Defaults {
  dir: string;
  payload: string | null; // папка поставки рядом с установщиком, если она есть
  version: string;
}

export interface Receipt {
  dir: string;
  exe: string;
  service: "running" | "stopped" | "absent" | "skipped";
  steps: Array<{ label: string; ok: boolean; note?: string }>;
}

export interface Progress {
  step: number;
  total: number;
  label: string;
}

export const setup: Setup = {
  agent: "",
  owner: "",
  constitution: "",
  accepted: false,
  provider: "api",
  chatgpt_model: "gpt-5.6-sol",
  reasoning_effort: "",
  api: { base_url: "https://api.openai.com/v1", model: "gpt-5.4", key: "" },
  anthropic: { base_url: "https://api.z.ai/api/anthropic", model: "glm-5.3", key: "" },
  local: { base_url: "http://127.0.0.1:11434/v1", model: "" },
  telegram: { bot_token: "", owner_id: "" },
  service: false,
  dir: "",
};

/** Каноническая конституция с подставленными именами (тот же текст, что читает boot.py). */
export function constitutionFor(agent: string, owner: string): string {
  return soulCanon
    .replace(/\{\{agent\}\}/g, agent.trim() || "Агент")
    .replace(/\{\{owner\}\}/g, owner.trim() || "владелец");
}

const inTauri = "__TAURI_INTERNALS__" in window;

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(cmd, args);
}

export async function loadDefaults(): Promise<Defaults> {
  if (!inTauri) {
    return { dir: "C:\\Users\\…\\AppData\\Local\\Programs\\Hélène", payload: null, version: "превью" };
  }
  return invoke<Defaults>("defaults");
}

/** Установка: оболочка копирует поставку, пишет конфиг и конституцию, ставит ярлыки. */
export async function runInstall(onProgress: (p: Progress) => void): Promise<Receipt> {
  if (!inTauri) {
    const labels = [
      "Копирую файлы программы",
      "Записываю настройки и конституцию",
      "Создаю ярлыки",
      "Регистрирую удаление",
      "Готово",
    ];
    for (const [i, label] of labels.entries()) {
      onProgress({ step: i + 1, total: labels.length, label });
      await new Promise((r) => setTimeout(r, 650));
    }
    return {
      dir: setup.dir,
      exe: setup.dir + "\\helene.exe",
      service: setup.service ? "running" : "skipped",
      steps: labels.map((label) => ({ label, ok: true })),
    };
  }
  const { listen } = await import("@tauri-apps/api/event");
  const stop = await listen<Progress>("install-progress", (e) => onProgress(e.payload));
  try {
    return await invoke<Receipt>("install", { setup });
  } finally {
    stop();
  }
}

/** Живая проверка адреса и ключа через оболочку; в превью — заглушка. */
export async function probeModel(baseUrl: string, key: string, framework = "openai"): Promise<{ ok: boolean; note: string; models?: string[] }> {
  if (!inTauri) return { ok: true, note: "Превью: проверка доступна в установщике", models: ["пример-1", "пример-2"] };
  return invoke("probe_model", { baseUrl, key, framework });
}

/** Список моделей самого реле ChatGPT: реле поднимается на миг из поставки. */
export async function relayModels(): Promise<string[]> {
  if (!inTauri) return ["gpt-5.6-sol", "gpt-5.5"];
  return invoke("relay_models");
}

export async function relayLogin(): Promise<string> {
  if (!inTauri) return "Превью: вход доступен в установщике";
  return invoke("relay_login");
}

export async function relayStatus(): Promise<"authorized" | "pending" | "no-auth"> {
  if (!inTauri) return "no-auth";
  return invoke("relay_status");
}

/** Снятие из визарда; в превью — заглушка. */
export async function runUninstall(purge: boolean): Promise<string> {
  if (!inTauri) return "Превью: снятие доступно в установщике";
  return invoke("uninstall_run", { purge });
}

export async function openFrame(exe: string): Promise<void> {
  if (!inTauri) return;
  await invoke("open_frame", { exe });
}
