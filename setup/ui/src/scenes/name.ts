// Сцена «Имя»: как назвать агента и как зовут владельца.
import { FormScene } from "./base";
import { el, explain, field } from "./form";
import { setup } from "../setup";

export class NameScene extends FormScene {
  private agentInput!: HTMLInputElement;

  constructor(root: HTMLElement) {
    super(root);
    const head = el("h2", "form-head");
    head.append(el("span", "line", "Как назвать агента?"));
    const grid = el("div", "form-grid two");
    const agentField = field({
      label: "Имя агента",
      value: setup.agent,
      placeholder: "например, Вера или Марк",
      onInput: (v) => (setup.agent = v),
    });
    const ownerField = field({
      label: "Твоё имя",
      value: setup.owner,
      placeholder: "как к тебе обращаться",
      onInput: (v) => (setup.owner = v),
    });
    grid.append(agentField, ownerField);
    this.agentInput = agentField.querySelector("input")!;
    const notes = el("div", "explain-row");
    notes.append(
      explain(
        "Имя войдёт в конституцию",
        "Оно станет частью того, кем агент себя считает, и подписью его слов в окне и в Telegram. Поменять потом можно в настройках.",
      ),
      explain(
        "Своё имя нужно агенту",
        "Чтобы знать, чьё слово решает при любом конфликте указаний, и обращаться к тебе по-человечески.",
      ),
    );
    this.mount(head, grid, notes);
  }

  protected onEntered() {
    if (!setup.agent) this.agentInput.focus({ preventScroll: true });
  }

  validate(): string | null {
    if (!setup.agent.trim()) return "Сначала назови агента";
    if (!setup.owner.trim()) return "И скажи, как зовут тебя";
    return null;
  }
}
