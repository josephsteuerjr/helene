// Сцена «Служба»: жить ли агенту без окна и до входа в систему. Честно про
// Session 0: служба не видит рабочий стол, столом занимается спутник в сессии.
import { FormScene } from "./base";
import { el, explain, toggle } from "./form";
import { setup } from "../setup";

export class ServiceScene extends FormScene {
  constructor(root: HTMLElement) {
    super(root);
    const head = el("h2", "form-head");
    head.append(el("span", "line", "Жить и без окна?"));
    const row = el("div", "explain-row");
    row.append(
      explain(
        "Что даёт служба",
        "Служба Windows запускает агента ещё до входа в систему и держит его живым, пока компьютер включён. Без неё он живёт, пока открыто окно или значок у часов.",
      ),
      explain(
        "Честно про рабочий стол",
        "Служба работает в отдельной сессии Windows и стола не видит. Окнами, приложениями и всем, что на экране, занимается спутник в твоей сессии; они говорят по защищённому каналу.",
      ),
      explain(
        "Один раз спросит права",
        "Служба ставится в Program Files, поэтому Windows один раз попросит права администратора. Сама программа и все данные остаются в твоей папке.",
      ),
    );
    const control = el("div", "service-control");
    control.append(
      toggle({
        label: "Поставить службу Windows",
        value: setup.service,
        onChange: (v) => (setup.service = v),
      }),
      el("p", "field-hint", "Можно решить и позже, в настройках."),
    );
    this.mount(head, row, control);
  }
}
