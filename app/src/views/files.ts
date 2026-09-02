// Файлы: память агента как она есть — маркдауны по группам, чтение справа.
import { api, post } from "../api";
import { esc, fmtAge, md, q, toast } from "../lib";
import { S } from "../state";

interface Group {
  group: string;
  root: string;
  files: Array<{ path: string; name: string; mtime: number }>;
}

export async function render(container: HTMLElement): Promise<void> {
  const groups = await api<Group[]>("/api/md-tree");
  if (!groups.length) {
    container.innerHTML = '<div class="empty"><b>Файлов пока нет</b>Память появится после первых ходов.</div>';
    return;
  }
  const cols = groups
    .map(
      (g, i) => `<details class="fold" ${i === 0 || (S.mdSel || "").startsWith(g.root) ? "open" : ""}>
      <summary><b>${esc(g.group)}</b> <span class="muted">${g.files.length}</span></summary>
      <div class="fold-body" style="padding:0 6px 8px">${g.files
        .map(
          (f) => `<div class="item" role="button" aria-current="${f.path === S.mdSel}" data-path="${esc(f.path)}">
          <span>${esc(f.name)}</span><span class="n">${fmtAge(f.mtime)}</span></div>`,
        )
        .join("")}</div>
    </details>`,
    )
    .join("");
  container.innerHTML = `<div class="split wide">
    <div class="list">${cols}</div>
    <div class="reading" id="md-main"><div class="empty">Выбери файл слева</div></div>
  </div>`;
  for (const el of container.querySelectorAll<HTMLElement>(".item[data-path]")) {
    el.addEventListener("click", () => void open(container, el.dataset.path!));
  }
  if (S.mdSel) await open(container, S.mdSel);
}

async function open(container: HTMLElement, path: string) {
  S.mdSel = path;
  for (const el of container.querySelectorAll<HTMLElement>(".item[data-path]")) {
    el.setAttribute("aria-current", String(el.dataset.path === path));
  }
  const main = q<HTMLElement>("#md-main", container);
  main.innerHTML = '<div class="empty">читаю…</div>';
  const doc = await api<{ error?: string; text?: string }>("/api/md?path=" + encodeURIComponent(path));
  if (doc.error) {
    main.innerHTML = `<div class="empty">${esc(doc.error)}</div>`;
    return;
  }
  const text = doc.text || "";
  main.innerHTML = `<div class="tools-row"><span class="muted mono">${esc(path)}</span>
      <button class="btn btn-quiet" id="md-edit" type="button">Править</button></div>
    <div class="card md" id="md-view">${md(text)}</div>
    <div id="md-editor" hidden>
      <textarea class="md-editor" spellcheck="false"></textarea>
      <div class="actions" style="margin-top:12px">
        <button class="btn btn-primary" id="md-save" type="button">Сохранить</button>
        <button class="btn btn-quiet" id="md-cancel" type="button">Отмена</button>
        <span class="receipt" id="md-receipt"></span>
      </div>
    </div>`;
  const editor = q<HTMLTextAreaElement>(".md-editor", main);
  const viewBox = q<HTMLElement>("#md-view", main);
  const editBox = q<HTMLElement>("#md-editor", main);
  const receipt = q<HTMLElement>("#md-receipt", main);
  editor.value = text;
  q("#md-edit", main).addEventListener("click", () => {
    viewBox.hidden = true;
    editBox.hidden = false;
    editor.focus();
  });
  q("#md-cancel", main).addEventListener("click", () => {
    editor.value = text;
    editBox.hidden = true;
    viewBox.hidden = false;
  });
  q("#md-save", main).addEventListener("click", async () => {
    receipt.className = "receipt";
    receipt.textContent = "пишу…";
    try {
      await post("/api/md", { path, text: editor.value });
      toast("Сохранено: агент увидит это в следующем ходу");
      await open(container, path);
    } catch (e) {
      receipt.className = "receipt err";
      receipt.textContent = "Не сохранилось: " + (e as Error).message;
    }
  });
}
