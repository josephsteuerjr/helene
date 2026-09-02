// Сцена «Установить»: сводка решений, кнопка, ход установки с расписками и
// кнопка «Открыть Frame». Установка начинается только нажатием кнопки.
import { FormScene } from "./base";
import { button, el } from "./form";
import { openFrame, runInstall, setup, type Receipt } from "../setup";

export class InstallScene extends FormScene {
  private summary: HTMLElement;
  private actions: HTMLElement;
  private progress: HTMLElement;
  private result: HTMLElement;
  private started = false;
  private receipt: Receipt | null = null;

  constructor(root: HTMLElement) {
    super(root);
    const head = el("h2", "form-head");
    head.append(el("span", "line", "Готово к установке"));
    this.summary = el("dl", "summary");
    this.actions = el("div", "install-actions");
    this.actions.append(button("Установить", "primary", () => void this.run()));
    this.progress = el("ol", "progress");
    this.progress.hidden = true;
    this.result = el("div", "install-result");
    this.result.hidden = true;
    this.mount(head, this.summary, this.actions, this.progress, this.result);
  }

  get locked(): boolean {
    return this.started;
  }

  private row(term: string, value: string) {
    const dt = el("dt", "", term);
    const dd = el("dd", "", value);
    this.summary.append(dt, dd);
  }

  protected beforeEnter() {
    if (this.started) return;
    this.summary.replaceChildren();
    const model =
      setup.provider === "api"
        ? `${setup.api.model} · ${setup.api.base_url}`
        : setup.provider === "chatgpt"
          ? "подписка ChatGPT через встроенное реле"
          : `${setup.local.model} · ${setup.local.base_url}`;
    this.row("Агент", setup.agent.trim());
    this.row("Владелец", setup.owner.trim());
    this.row("Модель", model);
    this.row("Telegram", setup.telegram.bot_token.trim() ? "бот подключён" : "только окно");
    this.row("Служба Windows", setup.service ? "поставить" : "не ставить");
    this.row("Папка", setup.dir);
  }

  private async run() {
    if (this.started) return;
    this.started = true;
    this.actions.hidden = true;
    this.progress.hidden = false;
    this.progress.replaceChildren();
    let current: HTMLElement | null = null;
    try {
      this.receipt = await runInstall((p) => {
        if (current) current.classList.add("done");
        current = el("li", "step", p.label);
        this.progress.append(current);
      });
      if (current) (current as HTMLElement).classList.add("done");
      this.showResult(this.receipt);
    } catch (err) {
      if (current) (current as HTMLElement).classList.add("failed");
      const fail = el("li", "step failed", String(err));
      this.progress.append(fail);
      this.started = false;
      this.actions.hidden = false;
      this.actions.replaceChildren(button("Повторить", "primary", () => void this.run()));
    }
  }

  private showResult(r: Receipt) {
    this.result.hidden = false;
    this.result.replaceChildren();
    const title = el("h3", "", "Установлено");
    const lines = el("p", "");
    const service =
      r.service === "running"
        ? "Служба работает."
        : r.service === "stopped"
          ? "Служба поставлена, но не запустилась: подробности в журнале службы."
          : r.service === "absent"
            ? "Служба не поставилась: проверь, было ли дано разрешение администратора."
            : "";
    lines.textContent = `Агент живёт в ${r.dir}. ${service}`.trim();
    const open = button("Открыть", "primary", () => void openFrame(r.exe));
    this.result.append(title, lines, open);
  }
}
