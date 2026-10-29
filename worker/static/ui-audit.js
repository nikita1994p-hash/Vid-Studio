// Checks the current Studio page against the [A] rules in DESIGN.md. Developer tool only:
//   (await import("/static/ui-audit.js")).audit()     -> problems on this page
//   (await import("/static/ui-audit.js")).auditAll()  -> {route: problems} for every page
const ROUTES = ["#/", "#/content", "#/topics", "#/ab", "#/earnings", "#/accounts", "#/settings",
  "#/channels", "#/account", "#/users", "#/stats"];

const visible = (e) => e.offsetParent !== null && e.getBoundingClientRect().width > 0;
const name = (e) => (e.id ? "#" + e.id : e.tagName.toLowerCase() + (e.className && typeof e.className === "string"
  ? "." + e.className.trim().split(/\s+/).join(".") : "")) + (e.textContent ? ` “${e.textContent.trim().slice(0, 24)}”` : "");
const inScroller = (e, stop) => {
  for (let p = e.parentElement; p && p !== stop.parentElement; p = p.parentElement) {   // the card itself may scroll
    if (/(auto|scroll|hidden)/.test(getComputedStyle(p).overflowX)) return true;
  }
  return false;
};

export function audit() {
  const out = [];
  const add = (rule, el) => out.push(`${rule}: ${el ? name(el) : ""}`);
  const view = document.getElementById("view") || document.body;
  document.querySelectorAll("#view details").forEach((d) => { d.open = true; });

  // no sideways scrolling
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) {
    add(`page scrolls sideways by ${document.documentElement.scrollWidth - document.documentElement.clientWidth}px`);
  }
  // stacked blocks keep at least 16 px apart
  const kids = [...view.children].filter(visible);
  for (let i = 1; i < kids.length; i++) {
    const gap = kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().bottom;
    const boxy = (e) => e.matches(".card, .grid, .acct-grid, form, .error-box, .ok-box");
    if (boxy(kids[i]) && boxy(kids[i - 1]) && gap < 16) add(`blocks only ${Math.round(gap)}px apart`, kids[i]);
  }
  // grid rows start at the same height
  for (const g of view.querySelectorAll(".grid, .acct-grid")) {
    const rows = {};
    for (const c of [...g.children].filter(visible)) {
      const r = c.getBoundingClientRect();
      const key = Math.round(r.top / 40);
      (rows[key] = rows[key] || []).push(r.top);
    }
    for (const tops of Object.values(rows)) {
      if (Math.max(...tops) - Math.min(...tops) > 2) add("cards in one row start at different heights", g);
    }
  }
  // nothing sticks out of its card; fields never overlap
  for (const card of view.querySelectorAll(".card")) {
    const cr = card.getBoundingClientRect();
    for (const k of card.querySelectorAll("*")) {
      if (!visible(k) || inScroller(k, card)) continue;
      const r = k.getBoundingClientRect();
      if (r.right > cr.right + 2 || r.left < cr.left - 2) { add("sticks out of its card", k); break; }
    }
  }
  const fields = [...view.querySelectorAll(".setting, .platform, .topic")].filter(visible).map((e) => [e, e.getBoundingClientRect()]);
  for (let i = 0; i < fields.length; i++) {
    for (let j = i + 1; j < fields.length; j++) {
      const [ea, a] = fields[i], [, b] = fields[j];
      if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) add("fields overlap", ea);
    }
  }
  // buttons: label centred, clickable things at least 24 px high
  for (const b of document.querySelectorAll(".btn, .tab, button, a.btn, select")) {
    if (!visible(b) || b.closest(".menu")) continue;
    const r = b.getBoundingClientRect();
    if (r.height < 24 && !b.classList.contains("icon-btn")) add(`only ${Math.round(r.height)}px high`, b);
    if (!b.matches(".btn, .tab")) continue;
    const range = document.createRange();
    range.selectNodeContents(b);
    const t = range.getBoundingClientRect();
    if (!t.width) continue;
    const dx = Math.abs((t.left + t.right) / 2 - (r.left + r.right) / 2);
    const dy = Math.abs((t.top + t.bottom) / 2 - (r.top + r.bottom) / 2);
    if (dx > 3 || dy > 3) add(`label off centre by ${dx.toFixed(0)}/${dy.toFixed(0)}px`, b);
  }
  // switches and text boxes
  for (const c of document.querySelectorAll("input[type=checkbox]")) {
    if (getComputedStyle(c).appearance !== "none") add("tick box instead of a switch", c);
  }
  for (const t of view.querySelectorAll("textarea")) {
    if (!visible(t)) continue;
    if (!["vertical", "none"].includes(getComputedStyle(t).resize)) add("text box can be stretched sideways", t);
  }
  // no tiny text
  for (const e of view.querySelectorAll("*")) {
    if (!visible(e) || !e.childNodes.length) continue;
    const own = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (own && parseFloat(getComputedStyle(e).fontSize) < 11) { add("text smaller than 11px", e); break; }
  }
  return [...new Set(out)];
}

export async function auditAll(routes = ROUTES, wait = 1500) {
  const result = {};
  for (const r of routes) {
    location.hash = r;
    await new Promise((ok) => setTimeout(ok, wait));
    result[r] = audit();
  }
  return result;
}
