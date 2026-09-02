// Мелкие общие вещи: экранирование, мини-маркдаун, форматы, DOM.

export const esc = (s: unknown): string =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string,
  );

export function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

export function fmtTimeSec(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDay(iso?: string | null): string {
  const d = new Date(iso ?? "");
  if (isNaN(d.getTime())) return "…";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const that = new Date(d);
  that.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - that.getTime()) / 86400000);
  if (diff === 0) return "Сегодня";
  if (diff === 1) return "Вчера";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

export const fmtN = (n: unknown): string =>
  n == null || n === "" ? "" : Number(n).toLocaleString("ru-RU");

export const fmtTs = (ts?: number | string | null): string =>
  ts ? new Date(Number(ts) * 1000).toLocaleTimeString("ru-RU") : "";

export function fmtAge(mtime: number): string {
  const s = Date.now() / 1000 - mtime;
  if (s < 3600) return Math.round(s / 60) + " мин";
  if (s < 86400) return Math.round(s / 3600) + " ч";
  return Math.round(s / 86400) + " дн";
}

export function plural(n: number, a: string, b: string, c: string): string {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return a;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return b;
  return c;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className = "",
  text = "",
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function q<E extends Element = HTMLElement>(sel: string, root: ParentNode = document): E {
  const node = root.querySelector<E>(sel);
  if (!node) throw new Error(`нет элемента ${sel}`);
  return node;
}

/** Мини-маркдаун её файлов и реплик: заголовки, списки, код, таблицы, цитаты. */
export function md(src: unknown): string {
  const lines = String(src || "").split("\n");
  const out: string[] = [];
  let inCode = false;
  let codeBuf: string[] = [];
  let listStack: "ul" | "ol" | null = null;
  let inQuote = false;
  let tableBuf: string[] = [];
  const inline = (s: string) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const flushList = () => {
    if (listStack) {
      out.push(listStack === "ul" ? "</ul>" : "</ol>");
      listStack = null;
    }
  };
  const flushQuote = () => {
    if (inQuote) {
      out.push("</blockquote>");
      inQuote = false;
    }
  };
  const flushTable = () => {
    if (!tableBuf.length) return;
    const rows = tableBuf.filter((r) => !/^\s*\|[\s\-:|]+\|\s*$/.test(r));
    out.push("<table>");
    rows.forEach((row, i) => {
      const cells = row.replace(/^\s*\||\|\s*$/g, "").split("|");
      out.push(
        "<tr>" +
          cells.map((c) => `<${i ? "td" : "th"}>${inline(c.trim())}</${i ? "td" : "th"}>`).join("") +
          "</tr>",
      );
    });
    out.push("</table>");
    tableBuf = [];
  };
  for (const raw of lines) {
    if (raw.startsWith("```")) {
      flushList();
      flushQuote();
      flushTable();
      if (inCode) {
        out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>");
        codeBuf = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      codeBuf.push(raw);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(raw)) {
      flushList();
      flushQuote();
      tableBuf.push(raw);
      continue;
    }
    flushTable();
    const h = raw.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      flushList();
      flushQuote();
      out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
      continue;
    }
    if (/^\s*([-*_]){3,}\s*$/.test(raw)) {
      flushList();
      flushQuote();
      out.push("<hr>");
      continue;
    }
    const qm = raw.match(/^>\s?(.*)/);
    if (qm) {
      flushList();
      if (!inQuote) {
        out.push("<blockquote>");
        inQuote = true;
      }
      out.push(inline(qm[1]) + "<br>");
      continue;
    }
    flushQuote();
    const li = raw.match(/^\s*([-*•]|\d+[.)])\s+(.*)/);
    if (li) {
      const kind: "ul" | "ol" = /^[-*•]/.test(li[1]) ? "ul" : "ol";
      if (listStack !== kind) {
        flushList();
        out.push(kind === "ul" ? "<ul>" : "<ol>");
        listStack = kind;
      }
      out.push("<li>" + inline(li[2]) + "</li>");
      continue;
    }
    flushList();
    if (!raw.trim()) {
      out.push("");
      continue;
    }
    out.push("<p>" + inline(raw) + "</p>");
  }
  if (inCode) out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>");
  flushList();
  flushQuote();
  flushTable();
  return out.join("\n");
}

let toastTimer = 0;
export function toast(text: string) {
  const box = document.getElementById("toast");
  if (!box) return;
  box.textContent = text;
  box.hidden = false;
  box.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    box.classList.remove("show");
    window.setTimeout(() => (box.hidden = true), 300);
  }, 2800);
}
