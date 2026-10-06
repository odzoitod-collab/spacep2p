/* Strait Pay mini app. No build step: one file, the Telegram SDK and our JSON API (/app/api, bot/api/webapp*.py).
   Sign-in is Telegram's initData sent with every request; the bot checks it and applies its own rules.
   Every screen renders with a token: a slow answer for a screen the user already left is thrown away, and every
   request has a timeout — a screen never hangs on its skeleton. Errors of the page itself go to the bot's log. */
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const $navIn = $nav.querySelector(".in") || $nav;
const $toast = document.getElementById("toast");
const S = { me: null, seq: 0, timers: [], drafts: {}, stack: [], ava: null, sheet: null, keep: false, chatSeen: {}, tab: {}, reported: 0 };

/* ---------- small helpers ---------- */

const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n = (v) => Number(v || 0);
const NF = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const rub = (v) => `${NF.format(n(v))} ₽`;
const usdt = (v) => { const x = n(v); return x !== 0 && Math.abs(x) < 0.01 ? x.toFixed(6).replace(/0+$/, "") : NF.format(x); };
const signed = (v) => (n(v) > 0 ? "+" : n(v) < 0 ? "−" : "") + usdt(Math.abs(n(v)));
const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `сегодня, ${hm}`;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `вчера, ${hm}`;
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? " " + d.getFullYear() : ""}, ${hm}`;
}
const left = (iso) => Math.max(0, Math.floor((new Date(iso) - Date.now()) / 1000));
const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
const haptic = (kind) => {
  try {
    if (kind === "tap") tg.HapticFeedback.impactOccurred("light");
    else if (kind === "select") tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.notificationOccurred(kind);
  } catch (e) { /* outside Telegram */ }
};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) => (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16)));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : "");
const amountOf = (v) => String(v || "").replace(/\s/g, "").replace(",", ".");

function toast(text, bad) {
  $toast.textContent = text;
  $toast.className = "toast" + (bad ? " bad" : "");
  $toast.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { $toast.hidden = true; }, bad ? 3600 : 2400);
  haptic(bad ? "error" : "success");
}

function confirmBox(text) {
  return new Promise((ok) => {
    try {
      if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) { tg.showConfirm(text, (r) => ok(!!r)); return; }
    } catch (e) { /* a popup is already open: fall back */ }
    ok(window.confirm(text));
  });
}

function openUrl(url) {
  if (!url) return;
  try {
    if (tg && /^https:\/\/t\.me\//.test(url) && tg.openTelegramLink) { tg.openTelegramLink(url); return; }
    if (tg && tg.openLink) { tg.openLink(url); return; }
  } catch (e) { /* fall through */ }
  window.open(url, "_blank");
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const t = document.createElement("textarea");
    t.value = text; t.setAttribute("readonly", ""); t.style.position = "fixed"; t.style.opacity = "0";
    document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); } catch (e2) { /* nothing more to try */ }
    t.remove();
  }
  toast(`${what || "Текст"} скопирован${what && /а$/.test(what) ? "а" : ""}`);
}

function every(ms, fn) { S.timers.push(setInterval(fn, ms)); }
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }

/* ---------- the API: a timeout on every request, one retry for reads ---------- */

class ApiError extends Error { constructor(msg, code, status) { super(msg); this.code = code; this.status = status; } }
const GATES = { unauthorized: 1, expired: 1, start_bot: 1, banned: 1, signup: 1, join: 1 };

async function api(path, opts = {}) {
  const headers = { "X-Telegram-Init-Data": (tg && tg.initData) || "" };
  let body = opts.body;
  const form = body instanceof FormData;
  if (body && !form) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
  const method = opts.method || (body ? "POST" : "GET");
  for (let attempt = 0; ; attempt++) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), form ? 90000 : 20000);
    let r;
    try {
      r = await fetch("/app/api/" + path, { method, headers, body, signal: ctl.signal, cache: "no-store" });
    } catch (e) {
      clearTimeout(timer);
      if (method === "GET" && attempt === 0) { await new Promise((ok) => setTimeout(ok, 700)); continue; }
      throw new ApiError(e && e.name === "AbortError" ? "Сервер долго не отвечает — попробуйте ещё раз" : "Нет связи — проверьте интернет и попробуйте ещё раз", "network", 0);
    }
    clearTimeout(timer);
    if (opts.raw) {
      if (!r.ok) { let d = null; try { d = await r.json(); } catch (e) { /* binary */ } throw new ApiError((d && d.error && d.error.message) || "Файл недоступен", d && d.error && d.error.code, r.status); }
      return r.blob();
    }
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      if (r.status >= 500 && method === "GET" && attempt === 0) { await new Promise((ok) => setTimeout(ok, 700)); continue; }
      const err = (data && data.error) || {};
      throw new ApiError(err.message || "Что-то пошло не так. Попробуйте ещё раз", err.code || "http", r.status);
    }
    return data;
  }
}

function report(err, where) {
  if (S.reported >= 5 || !tg || !tg.initData) return;
  S.reported += 1;
  const e = err || {};
  api("log", { body: { message: String(e.message || e).slice(0, 300), stack: String(e.stack || "").slice(0, 1500), path: where || path(), ua: navigator.userAgent } }).catch(() => {});
}

/* ---------- icons (24px, stroke) ---------- */

const P = {
  home: '<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1v-8.5Z"/>',
  swap: '<path d="M7 7h12l-3.5-3.5M17 17H5l3.5 3.5"/>',
  deals: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01" stroke-width="2.6"/>',
  work: '<rect x="3.5" y="7" width="17" height="12.5" rx="2"/><path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7M3.5 12.5h17"/>',
  user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c1.3-3.4 4-5 7-5s5.7 1.6 7 5"/>',
  down: '<path d="M12 4.5v14M6.5 13 12 18.5 17.5 13"/>',
  up: '<path d="M12 19.5v-14M6.5 11 12 5.5 17.5 11"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  wallet: '<path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H18v3"/><rect x="4" y="8" width="16" height="11" rx="2"/><path d="M16 13.5h.01" stroke-width="2.6"/>',
  card: '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="M3 10h18M7 15h4"/>',
  headset: '<path d="M4.5 14v-2a7.5 7.5 0 0 1 15 0v2"/><rect x="3.5" y="13" width="4" height="6" rx="1.5"/><rect x="16.5" y="13" width="4" height="6" rx="1.5"/>',
  chat: '<path d="M5 18.5V6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v7a2.5 2.5 0 0 1-2.5 2.5H8.5L5 18.5Z"/>',
  copy: '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V6.5A1.5 1.5 0 0 1 6.5 5H15"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  clip: '<path d="m19 11.5-6.8 6.8a4.5 4.5 0 0 1-6.4-6.4l7.4-7.4a3 3 0 0 1 4.3 4.3l-7.3 7.3a1.5 1.5 0 0 1-2.2-2.2l6.6-6.6"/>',
  shield: '<path d="M12 3.5 5 6.5v5c0 4.3 3 7.6 7 9 4-1.4 7-4.7 7-9v-5l-7-3Z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  book: '<path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H19v15H7.5A2.5 2.5 0 0 0 5 20.5v-15Z"/><path d="M5 20.5A2.5 2.5 0 0 1 7.5 18H19v3H7.5"/>',
  chart: '<path d="M4 20h16"/><rect x="6" y="11" width="3" height="6" rx="1"/><rect x="11" y="7" width="3" height="10" rx="1"/><rect x="16" y="13" width="3" height="4" rx="1"/>',
  bell: '<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 2H5l1.5-2Z"/><path d="M10 20.5a2.2 2.2 0 0 0 4 0"/>',
  people: '<circle cx="9" cy="9" r="3.3"/><path d="M3 19c1-3 3.4-4.6 6-4.6s5 1.6 6 4.6"/><circle cx="17" cy="8" r="2.6"/><path d="M16.5 13.6c2.3.2 3.8 1.6 4.5 4.2"/>',
  send: '<path d="M4.5 12 19.5 5l-4 15-3.6-6.4L4.5 12Z"/><path d="m11.9 13.6 7.6-8.6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  flag: '<path d="M5.5 21V4.5M5.5 5h11l-2 4 2 4h-11"/>',
  bot: '<rect x="4.5" y="7.5" width="15" height="11" rx="3"/><path d="M12 4v3.5M9 12.5h.01M15 12.5h.01M9.5 15.5h5"/>',
  power: '<path d="M12 3.5V11"/><path d="M7 6.5a7 7 0 1 0 10 0"/>',
  star: '<path d="m12 4 2.4 5 5.4.7-4 3.7 1 5.4L12 16.2 7.2 18.8l1-5.4-4-3.7 5.4-.7L12 4Z"/>',
  refresh: '<path d="M19 12a7 7 0 1 1-2.1-5"/><path d="M19 4.5V9h-4.5"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8h.01"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  file: '<path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20V3.5Z"/><path d="M14 3.5V8h4"/>',
  video: '<rect x="3.5" y="6" width="12" height="12" rx="2"/><path d="m15.5 10.5 5-3v9l-5-3"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m20.5 16-5-5L7 19.5"/>',
  scale: '<path d="M12 4v16M7 20h10M5 7h14"/><path d="m5 7-2.5 6a2.5 2.5 0 0 0 5 0L5 7ZM19 7l-2.5 6a2.5 2.5 0 0 0 5 0L19 7Z"/>',
  alert: '<path d="M12 4 2.8 19.5h18.4L12 4Z"/><path d="M12 10v4.5M12 17h.01"/>',
};
const ic = (name, cls) => `<svg${cls ? ` class="${cls}"` : ""} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;
const chev = () => ic("chev", "chev");

/* ---------- pieces of screens ---------- */

const ROLE = { buyer: ["Покупка", "down", "sea"], seller: ["Продажа", "up", "red"], operator: ["Ордер", "shield", "blue"], admin: ["Сделка", "deals", ""], offer: ["Заявка", "bell", "amber"] };
const TONE = { searching: "blue", assigned: "blue", checking: "blue", waiting_payment: "amber", paid: "blue", dispute: "red", completed: "sea", cancelled: "", expired: "", void: "" };
const PROGRESS = { searching: 15, assigned: 30, checking: 45, waiting_payment: 55, paid: 80, dispute: 80, completed: 100 };

function tgUser() { return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || {}; }

function avatar(cls) {
  const u = tgUser(), me = S.me ? S.me.user : {};
  const name = u.first_name || me.name || "S";
  const init = esc((name.trim()[0] || "S").toUpperCase() + ((u.last_name || "").trim()[0] || "").toUpperCase());
  const src = S.ava || u.photo_url;
  return `<div class="ava ${cls || ""}" data-me>${src ? `<img src="${esc(src)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}${src ? "" : init}</div>`;
}

async function loadAvatar() {
  const u = tgUser();
  const key = "ava:" + (u.id || "");
  try { const cached = localStorage.getItem(key); if (cached) S.ava = cached; } catch (e) { /* storage off */ }
  try {
    const r = await api("avatar");
    if (!r.url) return;
    S.ava = r.url;
    try { localStorage.setItem(key, r.url); } catch (e) { /* storage full or off */ }
    document.querySelectorAll(".ava[data-me]").forEach((a) => { a.innerHTML = `<img src="${esc(r.url)}" alt="">`; });
  } catch (e) { /* the initials stay */ }
}

function dealRow(d, quiet) {
  const [label, icon, tone] = ROLE[d.role] || ROLE.admin;
  const p = PROGRESS[d.status];
  return `<button class="row" data-go="${d.role === "offer" ? "request" : "deal"}/${d.id}">
    <span class="ic ${d.action ? "amber" : tone}">${ic(icon)}</span>
    <span class="mid"><b>${label} #${d.id}${d.bybit ? " · Bybit" : ""}</b><small>${d.action && quiet !== true ? "<span style='color:var(--amber)'>Нужно действие · </span>" : ""}${esc(d.status_text)} · ${when(d.created_at)}</small></span>
    <span class="end"><b>${rub(d.amount_rub)}</b><small>${usdt(d.debit || d.usdt)} USDT</small></span>
    ${p != null && p < 100 ? `<span class="strait"><i style="width:${p}%"></i></span>` : ""}
  </button>`;
}

function empty(icon, text, action) { return `<div class="empty">${ic(icon)}${esc(text)}${action || ""}</div>`; }
const sec = (title, extra) => `<div class="sec"><h2>${title}</h2>${extra || ""}</div>`;
const tag = (text, tone, icon) => `<span class="tag ${tone || ""}">${icon ? ic(icon) : ""}${esc(text)}</span>`;
const kv = (k, v) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`;
const titleBlock = (title, sub) => `<div class="title"><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ""}</div>`;

const SK = {
  home: () => `<div class="sk" style="height:44px;width:58%;margin:6px 0 12px"></div><div class="sk" style="height:132px"></div><div class="sk" style="height:62px;margin-top:10px"></div>${SK.rows(3)}`,
  rows: (k = 4) => `<div class="sk" style="height:14px;width:34%;margin:22px 2px 10px"></div>${Array.from({ length: k }, () => `<div class="sk" style="height:56px;margin-top:6px"></div>`).join("")}`,
  page: () => `<div class="sk" style="height:28px;width:46%;margin:8px 0 14px"></div><div class="sk" style="height:120px"></div>${SK.rows(3)}`,
  deal: () => `<div class="sk" style="height:24px;width:40%;margin:8px 0 14px"></div><div class="sk" style="height:38px;width:62%"></div><div class="sk" style="height:6px;margin:16px 0"></div><div class="sk" style="height:150px"></div><div class="sk" style="height:44px;margin-top:12px"></div>`,
};

function view(c, html, opts = {}) {
  if (!c.alive()) return false;
  $app.innerHTML = `<div class="screen">${html}</div>`;
  if (!opts.keepScroll) window.scrollTo(0, 0);
  return true;
}

function on(sel, fn, root) {
  (root || $app).querySelectorAll(sel).forEach((el) => { el.onclick = (e) => { e.preventDefault(); fn(el, e); }; });
}

async function busy(btn, fn) {
  if (!btn || btn.disabled) return;
  btn.classList.add("busy"); btn.disabled = true;
  try { await fn(); }
  finally { if (btn.isConnected) { btn.classList.remove("busy"); btn.disabled = false; } }
}

function failView(c, e) {
  if (GATES[e.code]) return gate(e.message);
  if (!(e instanceof ApiError)) report(e);
  if (!view(c, `${titleBlock("Не получилось")}<div class="box pad"><p style="margin:0">${esc(e.message || "Ошибка страницы")}</p>
    <button class="btn" data-act="retry">${ic("refresh")}Повторить</button></div>`)) return;
  on("[data-act=retry]", () => render());
}

function gate(text) {
  $nav.hidden = true;
  $app.innerHTML = `<div class="screen gate"><img src="/docs/static/logo.png" alt="Strait Pay"><h1>Strait Pay</h1><p>${esc(text)}</p>
    <button class="btn" data-act="close">${ic("bot")}Вернуться в бота</button></div>`;
  on("[data-act=close]", () => (tg && tg.close ? tg.close() : null));
}

async function me(force) {
  if (!S.me || force) S.me = await api("me");
  return S.me;
}

/* ---------- bottom sheet ---------- */

function sheet(html) {
  closeSheet(false);
  const veil = document.createElement("div");
  veil.className = "veil";
  const sh = document.createElement("div");
  sh.className = "sheet";
  sh.innerHTML = `<div class="in"><div class="grip"></div>${html}</div>`;
  veil.onclick = () => closeSheet();
  document.body.append(veil, sh);
  S.sheet = { veil, sh };
  syncBack();
  return sh;
}

function closeSheet(animate = true) {
  const cur = S.sheet;
  if (!cur) return;
  S.sheet = null;
  if (!animate) { cur.veil.remove(); cur.sh.remove(); syncBack(); return; }
  cur.veil.classList.add("closing"); cur.sh.classList.add("closing");
  setTimeout(() => { cur.veil.remove(); cur.sh.remove(); }, 200);
  syncBack();
}

/* ---------- screens: home ---------- */

async function home(c) {
  const [m, list] = await Promise.all([me(true), api("deals?scope=active")]);
  const u = tgUser(), b = m.balance, w = m.work || {};
  const locked = n(b.withdrawable) < n(b.available);
  const action = list.deals.filter((d) => d.action);
  const rest = list.deals.filter((d) => !d.action).slice(0, 5);
  const work = [
    m.roles.admin && w.disputes ? `<button class="row" data-go="admin"><span class="ic red">${ic("scale")}</span><span class="mid"><b>Споры ждут решения</b><small>Администрирование</small></span><span class="end"><b>${w.disputes}</b></span></button>` : "",
    m.roles.operator && w.free_orders ? `<button class="row" data-go="operator"><span class="ic blue">${ic("shield")}</span><span class="mid"><b>Bybit-ордера ждут оператора</b><small>Кабинет оператора</small></span><span class="end"><b>${w.free_orders}</b></span></button>` : "",
    m.roles.merchant === "approved" && w.offers ? `<button class="row" data-go="orders"><span class="ic amber">${ic("bell")}</span><span class="mid"><b>Свободные заявки</b><small>Ордерные реквизиты</small></span><span class="end"><b>${w.offers}</b></span></button>` : "",
  ].join("");
  if (!view(c, `
    <div class="bar">
      <button class="who" data-go="profile">${avatar()}<div><b>${esc(u.first_name || m.user.name || "Профиль")}</b><small>${m.user.username ? "@" + esc(m.user.username) : "ID " + m.user.id}</small></div></button>
      ${m.links.manager ? `<button class="ibtn" data-open="${esc(m.links.manager)}" aria-label="Менеджер">${ic("headset")}</button>` : ""}
    </div>
    <section class="balance">
      <div class="lbl"><span>Баланс</span><span class="num">1 USDT = ${NF.format(n(m.rate.rate))} ₽</span></div>
      <div class="sum">${usdt(b.available)}<small>USDT</small></div>
      <div class="sub">≈ ${rub(n(b.available) * n(m.rate.rate))}${m.rate.own ? " · ваши условия" : ""}</div>
      ${n(b.frozen) || locked || b.debt || n(b.team) ? `<div class="facts">
        ${n(b.frozen) ? tag(`В сделках ${usdt(b.frozen)}`, "", "lock") : ""}
        ${locked ? tag(`Можно вывести ${usdt(b.withdrawable)}`, "blue") : ""}
        ${b.debt ? tag(`Долг оператора ${usdt(b.debt)}`, "red") : ""}
        ${n(b.team) ? tag(`Командный ${usdt(b.team)}`, "sea") : ""}</div>` : ""}
    </section>
    <div class="quick">
      <button data-go="deposit">${ic("down")}Пополнить</button>
      <button data-go="withdraw">${ic("up")}Вывести</button>
      <button data-go="buy">${ic("swap")}Купить</button>
      <button data-go="history">${ic("clock")}История</button>
    </div>
    ${work ? `${sec("Работа")}<div class="list">${work}</div>` : ""}
    ${action.length ? `${sec("Нужно ваше действие")}<div class="list">${action.map((d) => dealRow(d, true)).join("")}</div>` : ""}
    ${sec("Активные сделки", list.deals.length ? `<button data-go="deals">Все</button>` : "")}
    <div class="list">${rest.length ? rest.map(dealRow).join("") : action.length ? empty("check", "Остальные сделки закрыты") :
      empty("swap", "Открытых сделок нет", `<button class="btn sm" data-go="buy" style="margin:12px auto 0">${ic("swap")}Купить USDT</button>`)}</div>
    <div class="foot">Strait Pay · P2P-обмен USDT ⇄ RUB</div>`)) return;
  setBadge(m.counts.action);
  every(15000, async () => {
    try { const f = await api("deals?scope=active"); if (c.alive() && JSON.stringify(f.deals) !== JSON.stringify(list.deals)) { S.keep = true; render(); } } catch (e) { /* next tick */ }
  });
}

/* ---------- exchange: buy for rubles, or how to sell ---------- */

async function exchange(c, side) {
  const m = await me();
  const sell = side === "sell";
  if (!view(c, `${titleBlock("Обмен")}
    <div class="box pad" style="display:flex;justify-content:space-between;align-items:baseline">
      <span style="color:var(--muted);font-size:13px">Курс сервиса</span>
      <span class="num" style="font-size:18px;font-weight:600">1 USDT = ${NF.format(n(m.rate.rate))} ₽</span>
    </div>
    <div class="seg"><button data-side="buy" class="${sell ? "" : "on buy"}">Купить USDT</button><button data-side="sell" class="${sell ? "on sell" : ""}">Продать USDT</button></div>
    ${sell ? sellPane(m) : `
      <label class="field"><span>Сколько рублей переведёте</span><div class="inp big"><input id="amt" inputmode="decimal" placeholder="10 000" autocomplete="off"><span class="unit">₽</span></div></label>
      <div class="chips" style="margin-top:8px">${[3000, 5000, 10000, 20000, 50000].map((v) => `<button class="chip" data-v="${v}">${NF.format(v)}</button>`).join("")}</div>
      <div id="quote"><p class="hint">Комиссия ${esc(m.rate.pct)}%${m.rate.own ? " — ваши личные условия" : ""}. Реквизиты продавца появятся после создания сделки.</p></div>`}`)) return;
  on("[data-side]", (b) => { haptic("select"); go(b.dataset.side, { replace: true }); });
  if (sell) return;
  const input = $app.querySelector("#amt");
  const out = $app.querySelector("#quote");
  let q = null;
  const quote = debounce(async () => {
    const v = amountOf(input.value);
    q = null;
    if (!v) { out.innerHTML = `<p class="hint">Комиссия ${esc(m.rate.pct)}%. Реквизиты продавца появятся после создания сделки.</p>`; return; }
    try {
      const r = await api(`buy/quote?amount=${encodeURIComponent(v)}`);
      if (!c.alive() || amountOf(input.value) !== v) return;
      q = r;
      out.innerHTML = `<div class="box pad" style="margin-top:12px">
          ${kv(`По курсу ${NF.format(n(q.rate))} ₽`, `<span class="num">${usdt(q.usdt)} USDT</span>`)}
          ${kv(`Комиссия ${esc(q.pct)}%`, `<span class="num">−${usdt(q.fee)} USDT</span>`)}
          <div class="kv total"><span>Вы получите</span><b>${usdt(q.credit)} USDT</b></div></div>
        <div class="box pad" style="margin-top:8px;display:flex;gap:10px;align-items:flex-start">
          <span class="ic" style="width:32px;height:32px;border-radius:8px;display:grid;place-items:center;background:var(--fill);flex:none">${ic(q.mode === "card" ? "card" : "search")}</span>
          <div style="font-size:14px">${q.mode === "card" ? `<b style="font-weight:500">Готовая карта продавца · ${esc(q.bank)}</b><div class="hint" style="margin:2px 0 0">${q.type === "sbp" ? "СБП" : "Перевод на карту"}. На перевод и чек — ${q.minutes} мин.</div>`
            : `<b style="font-weight:500">Реквизиты под вашу сумму</b><div class="hint" style="margin:2px 0 0">Подберём мерчанта до ${q.search_minutes} мин — придёт уведомление. На оплату — от ${q.minutes} мин.</div>`}</div></div>
        <button class="btn" id="go">${q.mode === "card" ? "Создать сделку" : "Найти реквизиты"}</button>
        <p class="hint">Переводите только после создания и только на показанные реквизиты, одним платежом.</p>`;
      on("#go", (btn) => busy(btn, create));
    } catch (e) { if (c.alive()) out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }, 300);
  const create = async () => {
    if (!q) return;
    try {
      const res = await api("buy", { body: { amount_rub: q.amount_rub, credit: q.credit, card_id: q.card_id } });
      toast(q.mode === "card" ? "Сделка создана" : "Заявка создана — ищем реквизиты");
      go(`deal/${res.deal.id}`, { replace: true });
    } catch (e) { toast(e.message, true); quote(); }
  };
  input.oninput = quote;
  on("[data-v]", (b) => { input.value = NF.format(n(b.dataset.v)); haptic("select"); quote(); });
}

function sellPane(m) {
  const r = m.roles;
  return `<div class="list">
      <button class="row" data-go="cards"><span class="ic sea">${ic("card")}</span><span class="mid"><b>Своя карта или СБП</b><small>Покупатели переводят рубли на вашу карту · доход ${esc(m.terms.seller_pct)}%</small></span>${chev()}</button>
      <button class="row" data-go="orders"><span class="ic amber">${ic("bell")}</span><span class="mid"><b>Реквизиты под сумму</b><small>Заявки покупателей · курс ${NF.format(n(m.terms.order_rate))} ₽ за USDT</small></span>${chev()}</button>
    </div>
    <p class="hint">${r.seller ? "Ваши карты — во вкладке «Работа»." : "Добавьте карту — и выходите на смену: сделки придут уведомлением, USDT спишутся с баланса только после вашего подтверждения."}</p>`;
}

/* ---------- deals ---------- */

async function dealsScreen(c) {
  const scope = S.dealScope || "active";
  const filter = S.dealFilter || "all";
  const list = await api(`deals?scope=${scope}`);
  const shown = (ds) => ds.filter((d) => filter === "all" || (filter === "buy" ? d.role === "buyer" : filter === "sell" ? d.role === "seller" : d.role === "operator"));
  if (!view(c, `${titleBlock("Сделки")}
    <div class="seg"><button data-scope="active" class="${scope === "active" ? "on" : ""}">Активные</button><button data-scope="history" class="${scope === "history" ? "on" : ""}">Завершённые</button></div>
    <div class="chips">${[["all", "Все"], ["buy", "Покупки"], ["sell", "Продажи"], ["op", "Ордера"]].map(([k, t]) => `<button class="chip ${k === filter ? "on" : ""}" data-f="${k}">${t}</button>`).join("")}</div>
    <div class="list" id="dl" style="margin-top:10px">${shown(list.deals).length ? shown(list.deals).map(dealRow).join("") : empty("deals", scope === "active" ? "Открытых сделок нет" : "Завершённых сделок пока нет", scope === "active" ? `<button class="btn sm" data-go="buy" style="margin:12px auto 0">${ic("swap")}Купить USDT</button>` : "")}</div>
    ${list.deals.length >= 30 ? `<button class="btn line" id="more">Показать ещё</button>` : ""}`)) return;
  on("[data-scope]", (b) => { S.dealScope = b.dataset.scope; haptic("select"); render(); });
  on("[data-f]", (b) => { S.dealFilter = b.dataset.f; haptic("select"); render(); });
  on("#more", (more) => busy(more, async () => {
    const next = await api(`deals?scope=${scope}&before=${list.deals[list.deals.length - 1].id}`);
    list.deals.push(...next.deals);
    $app.querySelector("#dl").insertAdjacentHTML("beforeend", shown(next.deals).map(dealRow).join(""));
    if (next.deals.length < 30) more.remove();
  }));
}

/* ---------- a deal: money, the strait, actions, chat ---------- */

const BANKS = ["Сбербанк", "Т-Банк", "Альфа-Банк", "ВТБ", "Райффайзен", "Озон Банк", "Газпромбанк", "МТС Банк"];
const QUICK = {
  buyer: ["Перевёл, чек прикрепил", "Реквизиты не проходят", "Подождите пару минут"],
  seller: ["Проверяю поступление", "Деньги пришли, подтверждаю", "Перевод не вижу — пришлите чек"],
  operator: ["Реквизиты выданы", "Проверяю оплату", "Оплата пришла"],
  admin: ["Администрация проверяет сделку", "Пришлите, пожалуйста, выписку"],
};

function requisites(d) {
  const r = d.requisites;
  const label = r.type === "sbp" ? "Телефон СБП" : "Номер карты";
  const exact = n(d.amount_rub).toFixed(2).replace(/\.00$/, "");
  return `${sec(d.role === "buyer" ? "Куда переводить" : "Реквизиты")}<div class="box pad">
    ${kv("Банк", `${esc(r.bank)} · ${r.type === "sbp" ? "СБП" : "карта"}`)}
    ${r.holder ? kv("Получатель", esc(r.holder)) : ""}
    <button class="req" data-copy="${esc(r.number)}" data-what="${label}"><span class="v"><small>${label} — нажмите, чтобы скопировать</small><span class="mono">${esc(r.type === "card" ? r.number.replace(/(\d{4})(?=\d)/g, "$1 ") : r.number)}</span></span>${ic("copy")}</button>
    <button class="req" data-copy="${exact}" data-what="Сумма"><span class="v"><small>Ровно эта сумма, одним переводом</small><span class="mono">${rub(d.amount_rub)}</span></span>${ic("copy")}</button>
  </div>`;
}

function amountLine(d) {
  if (d.role === "buyer") return `вы получите <b>${usdt(d.usdt)} USDT</b> · курс ${NF.format(n(d.rate))} ₽`;
  if (d.role === "operator") return `ордер на <b>${usdt(d.seller_debit || d.usdt)} USDT</b> по ${NF.format(n(d.merchant_rate || d.rate))} ₽`;
  if (d.role === "offer") return `${d.take ? `<b>${usdt(d.take.debit)} USDT</b> по ${NF.format(n(d.take.rate))} ₽` : ""}`;
  if (d.role === "seller") return `спишется <b>${usdt(d.usdt)} USDT</b>${d.income ? ` · ваш доход <b class="plus">+${usdt(d.income)}</b>` : ""}`;
  return `покупателю <b>${usdt(d.usdt)} USDT</b>${d.seller_debit ? ` · мерчант отдаёт ${usdt(d.seller_debit)}` : ""}`;
}

function straitBar(d) {
  const now = d.steps.find((s) => s.state === "now");
  return `<div class="strait-bar">${d.steps.map((s) => `<i class="${s.state === "done" ? "done" : s.state === "now" ? "now" : ""}"></i>`).join("")}</div>
    <div class="strait-cap"><span>${now ? `<b>${esc(cap(now.hint || now.title))}</b>` : `<b>${esc(cap(d.close_reason || d.status_text))}</b>`}</span><span class="num">${d.steps.filter((s) => s.state === "done").length} из ${d.steps.length}</span></div>`;
}

function stepsList(d) {
  return `<ol class="steps">${d.steps.map((s) => `<li class="${s.state}"><span class="d">${s.state === "done" ? ic("check") : ""}</span><span>${esc(s.title)}${s.hint ? `<small>${esc(s.hint)}</small>` : ""}</span></li>`).join("")}</ol>`;
}

function dealActions(d) {
  const has = (a) => d.actions.includes(a);
  const out = [];
  if (has("receipt") || has("late_receipt")) out.push(`<label class="btn" style="cursor:pointer">${ic("clip")}${has("late_receipt") ? "Я перевёл — прикрепить чек" : "Прикрепить чек (PDF)"}<input type="file" id="file" accept="application/pdf,image/jpeg,image/png" hidden></label>`);
  if (has("take")) out.push(`<button class="btn" data-a="take">${ic("check")}Взять заявку</button>`);
  if (has("accept")) out.push(`<button class="btn" data-a="accept">${ic("check")}Принять ордер</button>`);
  if (has("link")) out.push(`<button class="btn" data-a="link">${ic("link")}Отправить ссылку на ордер Bybit</button>`);
  if (has("give")) out.push(`<button class="btn" data-a="give">${ic("send")}Выдать реквизиты покупателю</button>`);
  if (has("confirm")) out.push(`<button class="btn" data-a="confirm">${ic("check")}${d.bybit ? "Оплата пришла — подтвердить" : "Деньги пришли — подтвердить"}</button>`);
  if (has("resolve")) out.push(`<button class="btn ink" data-a="resolve">${ic("scale")}Решение по сделке</button>`);
  const second = [];
  if (has("receipt_view")) second.push(`<button class="btn line" data-a="receipt">${ic("file")}Чек</button>`);
  if (has("dispute")) second.push(`<button class="btn line red" data-a="dispute">${ic("flag")}Спор</button>`);
  if (has("evidence")) second.push(`<button class="btn line" data-a="evidence">${ic("clip")}Доказательства</button>`);
  if (has("pass_on")) second.push(`<button class="btn line" data-a="pass_on">${ic("search")}Другой мерчант</button>`);
  if (has("recreate")) second.push(`<button class="btn line" data-a="recreate">${ic("refresh")}Пересоздать</button>`);
  if (has("no_requisites")) second.push(`<button class="btn line red" data-a="no_requisites">${ic("x")}Нет реквизитов</button>`);
  if (has("drop")) second.push(`<button class="btn line red" data-a="drop">${ic("x")}Отказаться</button>`);
  if (has("close")) second.push(`<button class="btn line red" data-a="close">${ic("x")}Закрыть сделку</button>`);
  if (has("cancel") || has("cancel_request")) second.push(`<button class="btn line red" data-a="cancel">${ic("x")}Отменить</button>`);
  if (!out.length && !second.length) return "";
  return `<div class="dock">${out.join("")}${second.length ? `<div class="btns">${second.join("")}</div>` : ""}</div>`;
}

async function dealScreen(c, id, tab, offer) {
  const { deal: d } = await api(offer ? `requests/${id}` : `deals/${id}`);
  S.dealSeen = JSON.stringify(d);
  const key = `${offer ? "r" : "d"}${id}`;
  tab = tab || S.tab[key] || "deal";
  S.tab[key] = tab;
  const chat = d.actions.includes("chat");
  const [label] = ROLE[d.role] || ROLE.admin;
  const timer = d.expires_at && ["waiting_payment", "searching", "assigned", "checking"].includes(d.status);
  const what = d.status === "waiting_payment" ? (d.role === "buyer" ? "Оплатите за" : "Покупатель оплачивает") :
    d.status === "searching" ? "Поиск реквизитов" : d.status === "assigned" ? (d.role === "seller" ? "Ответьте за" : "Мерчант готовит реквизиты") : "Проверка ордера";
  const html = `
    <div class="deal-head"><h1>${label} #${d.id}${d.bybit ? " · Bybit" : ""}</h1>${tag(d.status_text, TONE[d.status])}</div>
    <div class="amount"><div class="rub">${rub(d.amount_rub)}</div><div class="to">${amountLine(d)}</div></div>
    ${straitBar(d)}
    ${timer ? `<div class="timer-line" id="tl"><span>${what}</span><b id="timer">${mmss(left(d.expires_at))}</b></div>` : ""}
    ${d.held && d.role !== "buyer" ? `<p class="hint">${ic("lock").replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} Срока нет: сделку ведёт и закрывает оператор.</p>` : ""}
    ${chat ? `<div class="tabs"><button data-tab="deal" class="${tab === "deal" ? "on" : ""}">Сделка</button><button data-tab="chat" class="${tab === "chat" ? "on" : ""}">Чат<span class="badge" id="unread" hidden></span></button></div>` : ""}
    <div id="pane">${tab === "chat" && chat ? `<div class="chat" id="chat">${SK.rows(2)}</div>` : dealPane(d)}</div>
    ${tab === "chat" && chat ? "" : dealActions(d)}`;
  if (!view(c, html, { keepScroll: S.keep })) return;
  if (timer) every(1000, () => { const t = $app.querySelector("#timer"); if (!t) return; const sLeft = left(d.expires_at); t.textContent = mmss(sLeft); $app.querySelector("#tl").classList.toggle("low", sLeft < 180); });
  on("[data-tab]", (b) => { S.tab[key] = b.dataset.tab; haptic("select"); S.keep = true; render(); });
  bindDeal(c, d, offer);
  if (chat) await chatPane(c, d, tab === "chat");
  if (!offer && ["searching", "assigned", "checking", "waiting_payment", "paid", "dispute"].includes(d.status)) {
    every(5000, async () => {
      try {
        const fresh = await api(`deals/${id}`);
        if (!c.alive() || JSON.stringify(fresh.deal) === S.dealSeen) return;
        const typing = document.activeElement && /INPUT|TEXTAREA/.test(document.activeElement.tagName);
        if (!typing && !S.sheet) { S.keep = true; render(); }
      } catch (e) { /* the next tick tries again */ }
    });
  }
}

function dealPane(d) {
  const parts = [];
  if (d.requisites) parts.push(requisites(d));
  if (d.role === "buyer" && d.status === "waiting_payment") parts.push(`<p class="hint">Переведите ровно эту сумму одним платежом, без комментария. Затем скачайте в банке PDF-чек и прикрепите его.</p>`);
  if (d.bybit_url && ["operator", "admin", "offer"].includes(d.role)) parts.push(`<button class="btn line" data-open="${esc(d.bybit_url)}">${ic("link")}Открыть ордер Bybit</button>`);
  if (d.take) parts.push(`${sec("Как взять")}<div class="box pad">
      ${kv("Bybit-ордер", d.take.bybit_problem ? `<span class="minus">${esc(d.take.bybit_problem)}</span>` : `ссылка за ${d.take.link_minutes} мин`)}
      ${kv("С баланса", d.take.balance_problem ? `<span class="minus">${esc(d.take.balance_problem)}</span>` : `заморозим ${usdt(d.take.debit)} USDT`)}
      ${d.sender_bank ? kv("Банк покупателя", esc(d.sender_bank)) : ""}</div>`);
  if (d.rating_id) parts.push(`${sec("Оцените мерчанта")}<div class="box pad"><p class="hint" style="margin:0 0 4px">Готовность карты, скорость ссылки, реквизиты в ордере. 1 — плохо, 10 — отлично.</p>
      <div class="opts">${Array.from({ length: 10 }, (_, i) => `<button class="opt" data-rate="${i + 1}">${i + 1}</button>`).join("")}</div></div>`);
  if (d.dispute) parts.push(`${sec("Спор")}<div class="box pad">
      ${kv("Причина", esc(d.dispute.reason))}
      ${d.dispute.amount_rub ? kv("Фактически пришло", `<span class="num">${rub(d.dispute.amount_rub)}</span>`) : ""}
      ${kv("Доказательств", `${d.dispute.files}${d.actions.includes("evidence") ? ` · ваших ${d.dispute.mine} из ${d.dispute.max}` : ""}`)}
      ${d.resolution ? `<p class="hint" style="margin-top:8px">Решение: ${esc(d.resolution)}</p>` : ""}</div>`);
  else if (d.resolution) parts.push(`<div class="box pad" style="margin-top:12px"><p class="hint" style="margin:0">Решение: ${esc(d.resolution)}</p></div>`);
  if (d.parties) parts.push(`${sec("Участники")}<div class="list">${d.parties.map((p) => `<div class="row"><span class="ic">${ic("user")}</span><span class="mid"><b>${esc(p.name || "—")}</b><small>${esc(p.role)} · ${p.username ? "@" + esc(p.username) + " · " : ""}ID ${p.id}</small></span><button class="link-btn" data-copy="${p.id}" data-what="ID">ID</button></div>`).join("")}</div>`);
  if (d.files && d.files.length) parts.push(`${sec("Материалы")}<div class="list">${d.files.map((f) => `<button class="row" data-file="${f.n}" data-kind="${f.kind}"><span class="ic">${ic({ photo: "image", video: "video", document: "file", text: "chat" }[f.kind] || "file")}</span><span class="mid"><b>${esc(f.title)}</b><small>${f.kind === "text" ? esc(f.text) : f.kind === "document" ? "откроется в чате с ботом" : "посмотреть"}</small></span>${chev()}</button>`).join("")}</div>`);
  parts.push(`${sec("Детали")}<div class="box pad">
      ${kv("Курс", `<span class="num">${NF.format(n(d.rate))} ₽</span>`)}
      ${d.sender_bank && !d.take ? kv("Банк покупателя", esc(d.sender_bank)) : ""}
      ${kv("Создана", when(d.created_at))}
      ${d.paid_at ? kv("Чек прикреплён", when(d.paid_at)) : ""}
      ${d.closed_at ? kv("Закрыта", when(d.closed_at)) : ""}
      ${d.close_reason ? kv("Итог", esc(d.close_reason)) : ""}
    </div>${sec("Ход сделки")}<div class="box pad">${stepsList(d)}</div>`);
  return parts.join("");
}

function bindDeal(c, d, offer) {
  const id = d.id;
  const refresh = () => { S.keep = true; render(); };
  const file = $app.querySelector("#file");
  if (file) file.onchange = async () => {
    const f = file.files[0];
    if (!f) return;
    const form = new FormData();
    form.append("file", f);
    toast("Отправляем чек…");
    try { await api(`deals/${id}/receipt`, { body: form }); toast("Чек отправлен — ждём подтверждения"); refresh(); }
    catch (e) { toast(e.message, true); }
    file.value = "";
  };
  on("[data-rate]", (b) => busy(b, async () => {
    try { const r = await api(`ratings/${d.rating_id}`, { body: { score: n(b.dataset.rate) } }); toast(r.message); refresh(); } catch (e) { toast(e.message, true); }
  }));
  on("[data-file]", (b) => openFile(d, b.dataset.file, b.dataset.kind));
  const ASK = {
    no_requisites: "В ордере нет реквизитов? Мерчанту засчитается пропуск, заявка уйдёт другим.",
    pass_on: "Передать заявку другому мерчанту? Без пропуска для этого мерчанта.",
    recreate: "Попросить мерчанта пересоздать ордер? Выданные реквизиты отзовутся.",
    close: "Закрыть сделку без перевода? Если покупатель уже перевёл — не закрывайте.",
    accept: "Принять этот Bybit-ордер? Его USDT придут вам на Bybit — это станет вашим долгом.",
    drop: "Отказаться от заявки? Она уйдёт другим мерчантам" + (d.bybit ? "." : ", заморозка снимется."),
  };
  const DONE = { accept: "Ордер ваш — выдайте реквизиты", recreate: "Мерчант пришлёт новый ордер", close: "Сделка закрыта",
    pass_on: "Ищем другого мерчанта", no_requisites: "Пропуск засчитан, ищем другого мерчанта", drop: "Вы отказались от заявки" };
  on("[data-a]", async (b) => {
    const a = b.dataset.a;
    if (a === "take") return takeSheet(d);
    if (a === "give") return giveSheet(d);
    if (a === "link") return linkSheet(d);
    if (a === "dispute") return disputeSheet(d);
    if (a === "evidence") return evidenceSheet(d);
    if (a === "resolve") return resolveSheet(d);
    if (a === "receipt") return openFile(d, "r", d.receipt_kind === "photo" ? "photo" : "document");
    if (a === "confirm") {
      if (!(await confirmBox(`На ваш счёт пришло ${rub(d.amount_rub)}? Покупателю уйдёт ${usdt(d.role === "seller" ? d.usdt : d.usdt)} USDT. Отменить подтверждение нельзя.`))) return;
      return busy(b, async () => { try { await api(`deals/${id}/confirm`, { method: "POST" }); haptic("success"); toast("Сделка завершена"); refresh(); } catch (e) { toast(e.message, true); } });
    }
    if (a === "cancel") {
      const text = d.status === "waiting_payment" ? `Отменить сделку #${id}? Если уже перевели ${rub(d.amount_rub)} — не отменяйте, прикрепите чек.` : `Отменить заявку #${id}?`;
      if (!(await confirmBox(text))) return;
      return busy(b, async () => { try { await api(`deals/${id}/cancel`, { method: "POST" }); toast("Отменено"); refresh(); } catch (e) { toast(e.message, true); } });
    }
    if (ASK[a] && !(await confirmBox(ASK[a]))) return;
    busy(b, async () => {
      try {
        await api(`deals/${id}/${a}`, { method: "POST" });
        toast(DONE[a] || "Готово");
        if (a === "pass_on" || a === "no_requisites") go("operator", { replace: true });
        else if (a === "drop") go("orders", { replace: true });
        else if (offer) go(`deal/${id}`, { replace: true });
        else refresh();
      } catch (e) { toast(e.message, true); }
    });
  });
}

async function openFile(d, nKey, kind) {
  if (kind === "text") { const f = (d.files || []).find((x) => String(x.n) === String(nKey)); if (f) sheet(`<h3>${esc(f.title)}</h3><p style="white-space:pre-wrap">${esc(f.text)}</p>`); return; }
  if (kind === "photo" || kind === "video") {
    const sh = sheet(`<h3>${nKey === "r" ? "Чек покупателя" : "Материал спора"}</h3><div class="sk" style="height:220px;margin-top:8px" id="media"></div>
      <button class="btn line" id="tochat">${ic("send")}Отправить в чат с ботом</button>`);
    on("#tochat", (b) => busy(b, () => sendFile(d, nKey)), sh);
    try {
      const blob = await api(`deals/${d.id}/files/${nKey}`, { raw: true });
      const url = URL.createObjectURL(blob);
      const slot = sh.querySelector("#media");
      if (slot) slot.outerHTML = kind === "photo" ? `<img class="media" src="${url}" alt="">` : `<video class="media" src="${url}" controls playsinline></video>`;
    } catch (e) { const slot = sh.querySelector("#media"); if (slot) slot.outerHTML = `<p class="err">${esc(e.message)}</p>`; }
    return;
  }
  sendFile(d, nKey);
}

async function sendFile(d, nKey) {
  try { await api(`deals/${d.id}/files/${nKey}/send`, { method: "POST" }); toast("Файл отправлен в чат с ботом"); }
  catch (e) { toast(e.message, true); }
}

function takeSheet(d) {
  const t = d.take;
  const sh = sheet(`<h3>Взять заявку #${d.id}</h3><p class="hint" style="margin-top:0">${rub(d.amount_rub)} · ${usdt(t.debit)} USDT по ${NF.format(n(t.rate))} ₽</p>
    <div class="list" style="margin-top:12px">
      <button class="row" data-mode="bybit" ${t.bybit_problem ? "disabled style='opacity:.5'" : ""}><span class="ic blue">${ic("link")}</span><span class="mid"><b>Через Bybit-ордер</b><small>${t.bybit_problem ? esc(t.bybit_problem) : `Баланс не нужен · ссылка на ордер за ${t.link_minutes} мин`}</small></span>${chev()}</button>
      <button class="row" data-mode="balance" ${t.balance_problem ? "disabled style='opacity:.5'" : ""}><span class="ic sea">${ic("wallet")}</span><span class="mid"><b>С баланса</b><small>${t.balance_problem ? esc(t.balance_problem) : `Заморозим ${usdt(t.debit)} USDT · реквизиты выдаёте сами за ${t.take_minutes} мин`}</small></span>${chev()}</button>
    </div>
    <p class="hint">Bybit-ордер берите, только когда карта под ордер уже готова: не успели со ссылкой — заявка уйдёт другим, а вам — срыв в репутацию.</p>`);
  on("[data-mode]", (b) => { if (b.disabled) return; busy(b, async () => {
    try { const r = await api(`deals/${d.id}/take`, { body: { mode: b.dataset.mode } }); closeSheet(); toast(b.dataset.mode === "bybit" ? "Заявка ваша — пришлите ссылку на ордер" : "Заявка ваша — выдайте реквизиты"); go(`deal/${r.deal.id}`, { replace: true }); }
    catch (e) { toast(e.message, true); }
  }); }, sh);
}

function giveSheet(d) {
  const g = d.give || { choices: [] };
  let kind = "card", minutes = g.default;
  const draft = S.drafts["give" + d.id] || {};
  const sh = sheet(`<h3>Реквизиты для покупателя</h3><p class="hint" style="margin-top:0">${rub(d.amount_rub)}${d.sender_bank ? ` · перевод из ${esc(d.sender_bank)}` : ""}. Покупатель увидит их сразу.</p>
    <div class="seg" style="margin-top:10px"><button data-k="card" class="on">Карта</button><button data-k="sbp">СБП</button></div>
    <label class="field" style="margin-top:0"><span id="numlbl">Номер карты</span><div class="inp"><input id="gnum" class="mono" inputmode="numeric" autocomplete="off" placeholder="2200 7001 2345 6781" value="${esc(draft.num || "")}"></div></label>
    <label class="field"><span>Банк</span><div class="inp"><input id="gbank" autocomplete="off" placeholder="Сбербанк" value="${esc(draft.bank || "")}"></div></label>
    <div class="chips" style="margin-top:6px">${BANKS.map((b) => `<button class="chip" data-bank="${esc(b)}">${esc(b)}</button>`).join("")}</div>
    <label class="field"><span>Получатель (по желанию)</span><div class="inp"><input id="gholder" autocomplete="off" placeholder="Иван Иванович И." value="${esc(draft.holder || "")}"></div></label>
    ${g.choices.length ? `<div class="field"><span>Время на оплату</span><div class="opts">${g.choices.map((m) => `<button class="opt ${m === minutes ? "on" : ""}" data-min="${m}">${m} мин</button>`).join("")}</div></div>` : ""}
    <p class="err" id="gerr" hidden></p>
    <button class="btn" id="gsend">${ic("send")}Выдать реквизиты</button>`);
  const num = sh.querySelector("#gnum"), bank = sh.querySelector("#gbank"), holder = sh.querySelector("#gholder");
  const save = () => { S.drafts["give" + d.id] = { num: num.value, bank: bank.value, holder: holder.value }; };
  [num, bank, holder].forEach((x) => { x.oninput = save; });
  on("[data-k]", (b) => {
    kind = b.dataset.k;
    sh.querySelectorAll("[data-k]").forEach((x) => x.classList.toggle("on", x === b));
    sh.querySelector("#numlbl").textContent = kind === "sbp" ? "Телефон СБП" : "Номер карты";
    num.placeholder = kind === "sbp" ? "+7 900 123-45-67" : "2200 7001 2345 6781";
    num.inputMode = kind === "sbp" ? "tel" : "numeric";
  }, sh);
  on("[data-bank]", (b) => { bank.value = b.dataset.bank; save(); haptic("select"); }, sh);
  on("[data-min]", (b) => { minutes = n(b.dataset.min); sh.querySelectorAll("[data-min]").forEach((x) => x.classList.toggle("on", x === b)); }, sh);
  on("#gsend", (b) => {
    const err = sh.querySelector("#gerr");
    err.hidden = true;
    if (!num.value.trim() || !bank.value.trim()) { err.textContent = "Укажите номер и банк"; err.hidden = false; return; }
    busy(b, async () => {
      if (!(await confirmBox(`Выдать покупателю: ${bank.value.trim()} · ${num.value.trim()}${holder.value.trim() ? " · " + holder.value.trim() : ""}${minutes ? ` · ${minutes} мин на оплату` : ""}? Проверьте цифры — покупатель переведёт именно сюда.`))) return;
      try {
        await api(`deals/${d.id}/requisites`, { body: { number: num.value.trim(), bank: bank.value.trim(), holder: holder.value.trim(), minutes } });
        delete S.drafts["give" + d.id];
        closeSheet(); toast("Реквизиты выданы покупателю"); S.keep = true; render();
      } catch (e) { err.textContent = e.message; err.hidden = false; haptic("error"); }
    });
  }, sh);
}

function linkSheet(d) {
  const sh = sheet(`<h3>Ссылка на Bybit-ордер</h3>
    <p class="hint" style="margin-top:0">Создайте на Bybit P2P ордер на продажу: <b class="num">${rub(d.amount_rub)}</b> = <b class="num">${usdt(d.seller_debit || d.usdt)} USDT</b> по ${NF.format(n(d.merchant_rate || d.rate))} ₽, с вашими реквизитами. Пришлите ссылку — оператор выдаст покупателю реквизиты из ордера.</p>
    <label class="field"><span>Ссылка</span><div class="inp"><input id="lurl" autocomplete="off" placeholder="https://www.bybit.com/…"></div></label>
    <p class="err" id="lerr" hidden></p>
    <button class="btn" id="lsend">${ic("send")}Отправить оператору</button>`);
  on("#lsend", (b) => busy(b, async () => {
    const err = sh.querySelector("#lerr");
    try { await api(`deals/${d.id}/link`, { body: { url: sh.querySelector("#lurl").value.trim() } }); closeSheet(); toast("Ссылка ушла операторам"); S.keep = true; render(); }
    catch (e) { err.textContent = e.message; err.hidden = false; haptic("error"); }
  }), sh);
}

function filesField(label) {
  return `<label class="field"><span>${label}</span><div class="inp" style="padding:0"><label class="btn line" style="margin:0;border:0;cursor:pointer">${ic("clip")}<span id="fname">Выбрать файлы</span><input type="file" id="ffiles" accept="video/*,image/jpeg,image/png,application/pdf" multiple hidden></label></div></label>`;
}

function bindFiles(sh) {
  const input = sh.querySelector("#ffiles");
  input.onchange = () => { const k = input.files.length; sh.querySelector("#fname").textContent = k ? `Выбрано файлов: ${k}` : "Выбрать файлы"; };
  return input;
}

function disputeSheet(d) {
  const buyer = d.role === "buyer";
  let reason = "not_received";
  const sh = sheet(`<h3>Открыть спор · #${d.id}</h3>
    ${buyer ? `<p class="hint" style="margin-top:0">Администрация сверит ваш чек с данными продавца. USDT продавца останутся заморожены до решения.</p>`
      : `<p class="hint" style="margin-top:0">${d.bybit ? "Вы оператор: доказательство из банка или Bybit решит спор сразу." : `До решения ${usdt(d.usdt)} USDT останутся заморожены.`}</p>
      <div class="seg"><button data-r="not_received" class="on">Деньги не пришли</button><button data-r="wrong_amount">Другая сумма</button></div>
      <label class="field" id="amtbox" hidden style="margin-top:0"><span>Сколько фактически пришло</span><div class="inp"><input id="damt" inputmode="decimal" placeholder="4 500"><span class="unit">₽</span></div></label>`}
    ${filesField(buyer ? "Видео или выписка о списании" : "Видео из банка и/или выписка")}
    <label class="field"><span>Пояснение</span><div class="inp"><textarea id="dtext" rows="3" maxlength="1000" placeholder="${buyer ? "Когда и откуда переводили" : "Что видите в банке"}"></textarea></div></label>
    <p class="err" id="derr" hidden></p>
    <button class="btn red" id="dsend">${ic("flag")}Открыть спор</button>`);
  const input = bindFiles(sh);
  on("[data-r]", (b) => { reason = b.dataset.r; sh.querySelectorAll("[data-r]").forEach((x) => x.classList.toggle("on", x === b)); sh.querySelector("#amtbox").hidden = reason !== "wrong_amount"; }, sh);
  on("#dsend", (b) => busy(b, async () => {
    const err = sh.querySelector("#derr");
    err.hidden = true;
    const form = new FormData();
    if (!buyer) { form.append("reason", reason); if (reason === "wrong_amount") form.append("amount", amountOf(sh.querySelector("#damt").value)); }
    form.append("text", sh.querySelector("#dtext").value);
    for (const f of input.files) form.append("file", f);
    if (!buyer && !input.files.length) { err.textContent = "Приложите видео из банка или выписку"; err.hidden = false; return; }
    if (!(await confirmBox("Открыть спор? Сделку проверит администрация."))) return;
    try { await api(`deals/${d.id}/dispute`, { body: form }); closeSheet(); toast("Спор открыт"); S.keep = true; render(); }
    catch (e) { err.textContent = e.message; err.hidden = false; haptic("error"); }
  }), sh);
}

function evidenceSheet(d) {
  const sh = sheet(`<h3>Доказательства · #${d.id}</h3><p class="hint" style="margin-top:0">Видео, фото, PDF до 10 МБ или пояснение. Уже ваших: ${d.dispute ? d.dispute.mine : 0} из ${d.dispute ? d.dispute.max : 15}.</p>
    ${filesField("Файлы")}
    <label class="field"><span>Пояснение</span><div class="inp"><textarea id="etext" rows="3" maxlength="1000"></textarea></div></label>
    <p class="err" id="eerr" hidden></p>
    <button class="btn" id="esend">${ic("send")}Добавить</button>`);
  const input = bindFiles(sh);
  on("#esend", (b) => busy(b, async () => {
    const err = sh.querySelector("#eerr");
    const form = new FormData();
    form.append("text", sh.querySelector("#etext").value);
    for (const f of input.files) form.append("file", f);
    try { await api(`deals/${d.id}/evidence`, { body: form }); closeSheet(); toast("Добавлено"); S.keep = true; render(); }
    catch (e) { err.textContent = e.message; err.hidden = false; haptic("error"); }
  }), sh);
}

function resolveSheet(d) {
  let verdict = null;
  const sh = sheet(`<h3>Решение по сделке #${d.id}</h3><p class="hint" style="margin-top:0">Необратимо. Обе стороны получат уведомление с решением и комментарием.</p>
    <div class="list" style="margin-top:10px">${d.verdicts.map((v) => `<button class="row" data-v="${v.code}"><span class="ic">${ic("scale")}</span><span class="mid"><b>${esc(v.title)}</b><small style="white-space:normal">${v.effects.map(esc).join(" · ")}</small></span></button>`).join("")}</div>
    <label class="field"><span>Комментарий сторонам (по желанию)</span><div class="inp"><textarea id="vcomment" rows="2" maxlength="500" placeholder="Например: перевод найден в выписке"></textarea></div></label>
    <p class="err" id="verr" hidden></p>
    <button class="btn ink" id="vsend" disabled>Выберите решение</button>`);
  const send = sh.querySelector("#vsend");
  on("[data-v]", (b) => {
    verdict = b.dataset.v;
    sh.querySelectorAll("[data-v]").forEach((x) => x.querySelector(".ic").className = "ic" + (x === b ? " sea" : ""));
    send.disabled = false; send.textContent = "Провести: " + d.verdicts.find((v) => v.code === verdict).title;
    haptic("select");
  }, sh);
  on("#vsend", (b) => busy(b, async () => {
    if (!verdict || !(await confirmBox(`Провести решение «${d.verdicts.find((v) => v.code === verdict).title}»? Это необратимо.`))) return;
    try { await api(`deals/${d.id}/resolve`, { body: { verdict, comment: sh.querySelector("#vcomment").value } }); closeSheet(); toast("Решение проведено"); S.keep = true; render(); }
    catch (e) { const err = sh.querySelector("#verr"); err.textContent = e.message; err.hidden = false; haptic("error"); }
  }), sh);
}

/* chat inside the deal: the buyer, the merchant, the operator and the administration */

function msgHtml(m) {
  return `<div class="msg ${m.mine ? "me" : /админ/i.test(m.role) ? "adm" : ""}" data-id="${m.id}"><div class="who">${esc(m.mine ? "Вы" : m.role)}</div><div class="t">${esc(m.text)}</div><time>${when(m.at)}</time></div>`;
}

async function chatPane(c, d, visible) {
  const id = d.id;
  let data;
  try { data = await api(`deals/${id}/chat`); } catch (e) { if (visible && c.alive()) $app.querySelector("#pane").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  if (!c.alive()) return;
  let last = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
  const badge = $app.querySelector("#unread");
  const seen = () => { S.chatSeen[id] = last; if (badge) badge.hidden = true; };
  const showUnread = () => {
    const k = data.messages.filter((m) => m.id > (S.chatSeen[id] || 0) && !m.mine).length;
    if (badge) { badge.textContent = k; badge.hidden = !k || visible; }
  };
  if (!visible) {
    showUnread();
    every(6000, async () => { try { const r = await api(`deals/${id}/chat?after=${last}`); if (r.messages.length) { data.messages.push(...r.messages); last = r.messages[r.messages.length - 1].id; showUnread(); } } catch (e) { /* retry */ } });
    return;
  }
  const quick = QUICK[d.role] || QUICK.admin;
  $app.querySelector("#pane").innerHTML = `
    <p class="hint" style="margin-top:0">${esc(data.members.join(" · "))}. Ссылки и @юзернеймы не проходят — общайтесь только здесь.</p>
    <div class="chat" id="chat">${data.messages.length ? data.messages.map(msgHtml).join("") : empty("chat", "Сообщений пока нет — напишите первым")}</div>
    ${data.open ? `<div class="compose"><div class="quick-replies">${quick.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <div class="in"><div class="inp"><textarea id="text" rows="1" maxlength="1000" placeholder="Сообщение">${esc(S.drafts[id] || "")}</textarea></div>
      <button class="send" id="send" aria-label="Отправить">${ic("send")}</button></div></div>` : `<p class="hint">Сделка закрыта — чат только для чтения.</p>`}`;
  seen();
  const box = $app.querySelector("#chat");
  const bottom = () => window.scrollTo(0, document.body.scrollHeight);
  const add = (msgs) => {
    const fresh = msgs.filter((m) => !box.querySelector(`[data-id="${m.id}"]`));
    if (!fresh.length) return;
    const none = box.querySelector(".empty");
    if (none) none.remove();
    fresh.forEach((m) => box.insertAdjacentHTML("beforeend", msgHtml(m)));
    last = Math.max(last, msgs[msgs.length - 1].id);
    seen(); bottom();
  };
  bottom();
  every(3000, async () => { try { add((await api(`deals/${id}/chat?after=${last}`)).messages); } catch (e) { /* retry */ } });
  const text = $app.querySelector("#text");
  const send = $app.querySelector("#send");
  if (!text) return;
  const grow = () => { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 110) + "px"; };
  text.oninput = () => { S.drafts[id] = text.value; grow(); };
  grow();
  const post = async (v) => {
    if (!v) return;
    send.disabled = true;
    try {
      const res = await api(`deals/${id}/chat`, { body: { text: v } });
      text.value = ""; S.drafts[id] = ""; grow();
      add(res.messages.filter((m) => m.id > last));
      haptic("tap");
    } catch (e) { toast(e.message, true); }
    send.disabled = false;
  };
  send.onclick = () => post(text.value.trim());
  text.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !/Mobi/.test(navigator.userAgent)) { e.preventDefault(); post(text.value.trim()); } };
  on("[data-q]", (b) => post(b.dataset.q));
}

/* ---------- wallet ---------- */

async function depositScreen(c) {
  const [w, d] = await Promise.all([api("wallet"), api("deposit")]);
  if (!view(c, `${titleBlock("Пополнить", `USDT в сети TON · комиссия ${esc(w.deposit.fee)} · от ${esc(w.deposit.min)} USDT`)}
    <div class="box pad" style="margin-top:10px">
      ${kv("Сеть и монета", "TON · только USDT")}
      <button class="req" data-copy="${esc(d.address)}" data-what="Адрес"><span class="v"><small>Ваш личный адрес — нажмите, чтобы скопировать</small><span class="mono">${esc(d.address)}</span></span>${ic("copy")}</button>
    </div>
    <button class="btn" data-copy="${esc(d.address)}" data-what="Адрес">${ic("copy")}Скопировать адрес</button>
    <p class="hint">Адрес постоянный и только ваш, memo не нужен. Зачислим автоматически через 1–2 минуты после подтверждения в сети. Меньше ${esc(w.deposit.min)} USDT, другая монета или сеть — не зачислятся.</p>
    <button class="btn line" id="check">${ic("refresh")}Проверить поступление</button>
    ${w.deposits.length ? `${sec("Последние поступления")}<div class="list">${w.deposits.map((x) => `<button class="row" ${x.link ? `data-open="${esc(x.link)}"` : ""}>
      <span class="ic ${x.status === "paid" ? "sea" : "amber"}">${ic(x.status === "paid" ? "down" : "info")}</span><span class="mid"><b>${usdt(x.amount)} USDT</b><small>#${x.id} · ${when(x.created_at)}</small></span>
      <span class="end"><b class="${x.status === "paid" ? "plus" : ""}">${x.status === "paid" ? "+" + usdt(x.credit) : "не зачислено"}</b></span></button>`).join("")}</div>` : ""}`)) return;
  on("#check", (b) => busy(b, async () => {
    try {
      const r = await api("deposit?check=1");
      if (r.checked === null) toast("Проверка уже идёт — зачислим автоматически");
      else if (!r.checked.length) toast("Новых поступлений пока нет — зачислим автоматически");
      else { toast(`Зачислено: ${r.checked.map((x) => usdt(x.credit)).join(", ")} USDT`); S.keep = true; render(); }
    } catch (e) { toast(e.message, true); }
  }));
  every(15000, async () => {
    try { const f = await api("wallet"); if (c.alive() && (f.deposits[0] || {}).id !== (w.deposits[0] || {}).id) { S.keep = true; render(); } } catch (e) { /* retry */ }
  });
}

async function withdrawScreen(c) {
  const w = await api("wallet");
  const req = uuid();
  if (!view(c, `${titleBlock("Вывести", `Можно вывести <b class="num">${usdt(w.balance.withdrawable)} USDT</b>${n(w.balance.withdrawable) < n(w.balance.available) ? ` из ${usdt(w.balance.available)}` : ""}`)}
    ${w.lock_note ? `<div class="box pad" style="display:flex;gap:10px;margin-top:10px"><span style="color:var(--blue);flex:none">${ic("info")}</span><span class="hint" style="margin:0">${esc(w.lock_note)}</span></div>` : ""}
    <label class="field"><span>Адрес кошелька USDT в сети TON</span><div class="inp"><input id="addr" class="mono" autocomplete="off" spellcheck="false" placeholder="UQ… или EQ…" value="${esc(w.withdraw.last_address || "")}"></div></label>
    <label class="field"><span>Memo — если выводите на биржу</span><div class="inp"><input id="memo" autocomplete="off" placeholder="Необязательно"></div></label>
    <label class="field"><span>Сумма списания</span><div class="inp big"><input id="amt" inputmode="decimal" placeholder="0"><button class="max" id="max">Макс</button></div></label>
    <div id="q" class="hint">Комиссия ${esc(w.withdraw.terms)} · минимум ${esc(w.withdraw.min)} USDT</div>
    <button class="btn" id="go" disabled>${ic("up")}Вывести</button>
    <p class="hint">Отправляем автоматически, обычно за 1–2 минуты. Если у сервиса не хватит USDT — вывод подождёт в очереди и уйдёт сам.</p>
    ${w.withdrawals.length ? `${sec("В пути")}<div class="list">${w.withdrawals.map((x) => `<div class="row">
      <span class="ic ${x.status === "queued" ? "amber" : "blue"}">${ic(x.status === "queued" ? "clock" : "up")}</span>
      <span class="mid"><b class="num">${usdt(x.receive)} USDT</b><small>#${x.id} · ${esc(x.status_text)}</small></span>
      ${x.cancellable ? `<button class="btn line red sm" data-cancel="${x.id}">Отменить</button>` : ""}</div>`).join("")}</div>` : ""}`)) return;
  const amt = $app.querySelector("#amt"), qbox = $app.querySelector("#q"), goBtn = $app.querySelector("#go"), addr = $app.querySelector("#addr");
  let q = null;
  const ready = () => { goBtn.disabled = !(q && !q.error && addr.value.trim()); };
  const quote = debounce(async () => {
    const v = amountOf(amt.value);
    q = null; ready();
    if (!v) { qbox.textContent = `Комиссия ${w.withdraw.terms} · минимум ${w.withdraw.min} USDT`; return; }
    try {
      const r = await api(`withdraw/quote?amount=${encodeURIComponent(v)}`);
      if (!c.alive() || amountOf(amt.value) !== v) return;
      q = r;
      if (q.error) { qbox.innerHTML = `<span class="err" style="margin:0">${esc(q.error)}</span>`; return; }
      qbox.innerHTML = `Комиссия ${esc(q.terms)}: −<span class="num">${usdt(q.fee)}</span> USDT · <b style="color:var(--ink);font-weight:500">придёт <span class="num">${usdt(q.receive)}</span> USDT</b>`;
      q.amount = v;
      ready();
    } catch (e) { qbox.innerHTML = `<span class="err" style="margin:0">${esc(e.message)}</span>`; }
  }, 300);
  amt.oninput = quote;
  addr.oninput = ready;
  on("#max", () => { amt.value = w.balance.withdrawable; quote(); });
  on("[data-cancel]", async (b) => {
    if (!(await confirmBox("Отменить вывод из очереди? USDT вернутся на баланс."))) return;
    busy(b, async () => { try { await api(`withdrawals/${b.dataset.cancel}/cancel`, { method: "POST" }); toast("Вывод отменён, USDT на балансе"); S.keep = true; render(); } catch (e) { toast(e.message, true); } });
  });
  on("#go", async (b) => {
    if (!q || q.error) return;
    const a = addr.value.trim();
    if (!(await confirmBox(`Вывести ${usdt(q.amount)} USDT в сети TON на ${a.slice(0, 6)}…${a.slice(-4)}? Придёт ${usdt(q.receive)} USDT. Перевод в блокчейне не отменить.`))) return;
    busy(b, async () => {
      try {
        const r = await api("withdraw", { body: { amount: q.amount, fee: q.fee, request_id: req, address: a, memo: $app.querySelector("#memo").value.trim() || null } });
        toast(r.message, !r.ok);
        if (r.ok) go("", { replace: true });
      } catch (e) { toast(e.message, true); }
    });
  });
}

const KIND_ICON = { deposit: ["down", "sea"], ton_deposit: ["down", "sea"], withdraw: ["up", ""], withdraw_refund: ["refresh", "sea"],
  deal_buy: ["swap", "sea"], deal_sell: ["swap", ""], admin: ["shield", ""], team_fee: ["people", "sea"], team_income: ["people", "sea"],
  team_out: ["people", ""], team_in: ["people", "sea"], debt_repay: ["shield", ""], freeze: ["lock", ""], unfreeze: ["lock", ""] };

function historyRow(r) {
  const [icon, tone] = KIND_ICON[r.kind] || ["clock", ""];
  const delta = n(r.delta) ? signed(r.delta) : r.frozen ? `${signed(r.frozen)} заморозка` : "0";
  const link = (r.ref || "").match(/^deal:(\d+)$/);
  return `<${link ? `button data-go="deal/${link[1]}"` : "div"} class="row"><span class="ic ${tone}">${ic(icon)}</span>
    <span class="mid"><b>${esc(r.title)}</b><small>${when(r.at)}${r.note ? " · " + esc(r.note) : ""}</small></span>
    <span class="end"><b class="${n(r.delta) > 0 ? "plus" : ""}">${delta}</b></span></${link ? "button" : "div"}>`;
}

async function historyScreen(c) {
  const data = await api("history");
  if (!view(c, `${titleBlock("История операций")}
    <div class="list" id="hl" style="margin-top:12px">${data.items.length ? data.items.map(historyRow).join("") : empty("clock", "Операций пока нет")}</div>
    ${data.items.length >= 40 ? `<button class="btn line" id="more">Показать ещё</button>` : ""}`)) return;
  on("#more", (more) => busy(more, async () => {
    const next = await api(`history?before=${data.items[data.items.length - 1].id}`);
    data.items.push(...next.items);
    $app.querySelector("#hl").insertAdjacentHTML("beforeend", next.items.map(historyRow).join(""));
    if (next.items.length < 40) more.remove();
  }));
}

/* ---------- work: cards, order requests, operator, admin ---------- */

async function workScreen(c) {
  const m = await me(true);
  const r = m.roles, w = m.work || {};
  const rows = [
    `<button class="row" data-go="cards"><span class="ic sea">${ic("card")}</span><span class="mid"><b>Карты и смена</b><small>${r.seller ? (m.user.online ? `На смене · в потоке карт: ${w.cards_on || 0}` : "Не на смене") : "Продавайте USDT на свою карту или СБП"}</small></span>${chev()}</button>`,
    `<button class="row" data-go="orders"><span class="ic amber">${ic("bell")}</span><span class="mid"><b>Ордерные реквизиты</b><small>${r.merchant === "approved" ? `Свободных заявок: ${w.offers || 0}` : r.merchant === "pending" ? "Анкета на рассмотрении" : r.merchant === "suspended" ? "Доступ приостановлен" : "Заявки покупателей под точную сумму"}</small></span>${chev()}</button>`,
    r.operator || n(m.balance.debt) ? `<button class="row" data-go="operator"><span class="ic blue">${ic("shield")}</span><span class="mid"><b>Оператор</b><small>${w.free_orders ? `Ордеров ждут оператора: ${w.free_orders}` : "Bybit-ордера, реквизиты, подтверждение"}</small></span>${chev()}</button>` : "",
    r.admin ? `<button class="row" data-go="admin"><span class="ic red">${ic("scale")}</span><span class="mid"><b>Администрирование</b><small>${w.disputes ? `Споров: ${w.disputes}` : "Споры, сделки, деньги"}</small></span>${chev()}</button>` : "",
    `<button class="row" data-go="stats"><span class="ic">${ic("chart")}</span><span class="mid"><b>Статистика</b><small>Оборот, доход по дням, успешность</small></span>${chev()}</button>`,
  ].join("");
  view(c, `${titleBlock("Работа", "Продажа, заявки, операторская и администрирование — всё, что приносит доход")}<div class="list" style="margin-top:12px">${rows}</div>`);
}

function cardRow(c) {
  return `<div class="row"><button style="display:flex;align-items:center;gap:11px;flex:1;min-width:0;text-align:left" data-go="card/${c.id}">
    <span class="ic ${c.banned ? "red" : c.visible ? "sea" : ""}">${ic("card")}</span>
    <span class="mid"><b>${esc(c.bank)} ${esc(c.mask)}</b><small>${c.banned ? "заблокирована администрацией" : c.visible ? `в потоке · ${rub(c.min)} – ${rub(c.max)}` : esc(c.why)}</small></span></button>
    ${c.banned ? "" : `<button class="sw ${c.active ? "on" : ""}" data-toggle="${c.id}" aria-label="В потоке"></button>`}</div>`;
}

async function cardsScreen(c) {
  const data = await api("cards");
  const visible = data.cards.filter((x) => x.visible).length;
  if (!view(c, `${titleBlock("Карты", `Доход ${esc(data.pct)}% с каждой сделки · сделка до ${rub(data.cap_rub)}`)}
    <div class="box pad" style="margin-top:10px;display:flex;align-items:center;gap:12px">
      <span class="ic" style="width:36px;height:36px;border-radius:9px;display:grid;place-items:center;flex:none;background:${data.online ? "var(--sea-soft)" : "var(--fill)"};color:${data.online ? "var(--sea)" : "var(--muted)"}">${ic("power")}</span>
      <div style="flex:1"><b style="font-weight:500">${data.online ? "На смене" : "Не на смене"}</b><div class="hint" style="margin:1px 0 0">${data.online ? `Покупатели видят карт: ${visible} из ${data.cards.length}` : "Покупатели ваши карты не видят"}${data.online && data.auto_off ? ` · смена закончится сама через ${data.auto_off} мин без действий` : ""}</div></div>
      <button class="sw ${data.online ? "on" : ""}" id="shift" aria-label="Смена"></button>
    </div>
    ${sec("Карты в потоке", `<button data-go="stats">Статистика</button>`)}
    <div class="list">${data.cards.length ? data.cards.map(cardRow).join("") : empty("card", "Карт пока нет — добавьте карту или СБП, и покупатели увидят её")}</div>
    <button class="btn" data-go="card/new">${ic("plus")}Добавить карту</button>`)) return;
  on("#shift", (b) => busy(b, async () => { try { await api("shift", { body: { online: !data.online } }); haptic("tap"); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
  on("[data-toggle]", (b) => busy(b, async () => {
    const card = data.cards.find((x) => String(x.id) === b.dataset.toggle);
    try { await api(`cards/${card.id}`, { body: { active: !card.active } }); haptic("tap"); S.keep = true; render(); } catch (e) { toast(e.message, true); }
  }));
}

async function cardScreen(c, id) {
  const data = await api("cards");
  const card = data.cards.find((x) => String(x.id) === String(id));
  if (!card) { go("cards", { replace: true }); return; }
  const spaced = card.type === "card" ? card.number.replace(/(\d{4})(?=\d)/g, "$1 ") : card.number;
  if (!view(c, `${titleBlock(`${card.bank} ${card.mask}`)}
    <div class="bankcard ${card.visible ? "" : "off"}" style="margin-top:10px">
      <div class="t"><span>${esc(card.bank)}</span><span>${card.type === "sbp" ? "СБП" : "карта"}</span></div>
      <div class="n">${esc(spaced)}</div>
      <div class="h"><span>${esc(card.holder)}</span><span>${card.visible ? "в потоке" : "не видна"}</span></div>
    </div>
    <div class="box pad" style="margin-top:10px">
      <div class="kv" style="align-items:center"><span><b style="color:var(--ink);font-weight:500">В потоке</b><br><small>${card.visible ? "покупатели видят карту" : esc(card.why)}</small></span>
        ${card.banned ? tag("заблокирована", "red") : `<button class="sw ${card.active ? "on" : ""}" id="active" aria-label="В потоке"></button>`}</div>
      ${card.busy_deal ? kv("Идёт сделка", `<button class="link-btn" data-go="deal/${card.busy_deal}">#${card.busy_deal}</button>`) : ""}
      ${kv("Принято сегодня", `<span class="num">${rub(card.used_today)}${card.daily ? ` из ${rub(card.daily)}` : ""}</span>`)}
      ${kv("Всего сделок", `<span class="num">${card.deals} · ${rub(card.turnover)}</span>`)}
    </div>
    ${card.banned ? "" : `${sec("Лимиты")}
    <div class="box pad">
      <label class="field" style="margin-top:0"><span>Минимум одной сделки</span><div class="inp"><input id="min" inputmode="decimal" value="${esc(card.min)}"><span class="unit">₽</span></div></label>
      <label class="field"><span>Максимум одной сделки</span><div class="inp"><input id="max" inputmode="decimal" value="${esc(card.max)}"><span class="unit">₽</span></div></label>
      <label class="field"><span>Лимит банка на входящие в день (0 — без лимита)</span><div class="inp"><input id="daily" inputmode="decimal" value="${esc(card.daily || 0)}"><span class="unit">₽</span></div></label>
      <button class="btn" id="save">Сохранить</button>
    </div>
    ${data.cards.filter((x) => x.active).length > 1 || !card.active ? `<button class="btn line" id="solo">${ic("star")}Работать только с этой картой</button>` : ""}
    <p class="hint">Банк, номер и получателя меняйте в боте: «USDT ⇄ RUB» → карта. Открытые сделки изменения не затрагивают.</p>`}`)) return;
  on("#active", (b) => busy(b, async () => { try { await api(`cards/${card.id}`, { body: { active: !card.active } }); haptic("tap"); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
  on("#solo", (b) => busy(b, async () => { try { await api(`cards/${card.id}`, { body: { solo: true } }); toast("В потоке только эта карта"); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
  on("#save", (b) => busy(b, async () => {
    const val = (k) => amountOf($app.querySelector("#" + k).value);
    try { await api(`cards/${card.id}`, { body: { min: val("min"), max: val("max"), daily: val("daily") } }); toast("Лимиты сохранены"); S.keep = true; render(); } catch (e) { toast(e.message, true); }
  }));
}

async function cardNew(c) {
  if (!view(c, `${titleBlock("Новая карта", "Карта или СБП — покупатели увидят её сразу после сохранения")}
    <label class="field"><span>Номер карты или телефон СБП</span><div class="inp"><input id="num" class="mono" inputmode="numeric" autocomplete="off" placeholder="2200 7001 2345 6781 или +7 900…"></div></label>
    <label class="field"><span>Банк</span><div class="inp"><input id="bank" autocomplete="off" placeholder="Сбербанк"></div></label>
    <div class="chips" style="margin-top:6px">${BANKS.map((b) => `<button class="chip" data-bank="${esc(b)}">${esc(b)}</button>`).join("")}</div>
    <label class="field"><span>Получатель — как его видит отправитель</span><div class="inp"><input id="holder" autocomplete="off" placeholder="Иван Иванович И."></div></label>
    <div class="btns"><label class="field"><span>Минимум, ₽</span><div class="inp"><input id="min" inputmode="decimal" placeholder="1 000"></div></label>
      <label class="field"><span>Максимум, ₽</span><div class="inp"><input id="max" inputmode="decimal" placeholder="50 000"></div></label></div>
    <p class="hint">Ошибка в цифрах — деньги покупателя уйдут чужому человеку. Проверьте номер.</p>
    <button class="btn" id="save">Сохранить и включить</button>`)) return;
  on("[data-bank]", (b) => { $app.querySelector("#bank").value = b.dataset.bank; haptic("select"); });
  on("#save", (b) => busy(b, async () => {
    const v = (k) => $app.querySelector("#" + k).value.trim();
    try {
      const r = await api("cards", { body: { number: v("num"), bank: v("bank"), holder: v("holder"), min: amountOf(v("min")), max: amountOf(v("max")) } });
      toast(r.notice, /не в потоке/.test(r.notice)); go("cards", { replace: true });
    } catch (e) { toast(e.message, true); }
  }));
}

async function ordersScreen(c) {
  const [data, m] = await Promise.all([api("merchant"), me()]);
  if (!data.status || data.status === "rejected" || data.status === "pending") {
    view(c, `${titleBlock("Ордерные реквизиты", "Берите заявки покупателей под точную сумму и зарабатывайте на курсе")}
      <div class="box pad" style="margin-top:10px">
        ${kv("Курс мерчанта", `<span class="num">${NF.format(n(data.terms.rate))} ₽</span> за USDT`)}
        ${kv("Bybit-ордер", `без баланса · ссылка за ${data.terms.link_minutes} мин`)}
        ${kv("С баланса", `реквизиты выдаёте сами за ${data.terms.take_minutes} мин`)}
      </div>
      ${data.status === "pending" ? `<p class="hint">${ic("clock").replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} Анкета на рассмотрении — ответ придёт в бот.</p>` :
        `<p class="hint">${data.status === "rejected" && data.reason ? `Прошлая анкета отклонена: ${esc(data.reason)}. ` : ""}Анкета — 4 коротких шага в боте, ответ обычно в течение суток.</p>
        ${m.links.bot ? `<button class="btn" data-open="${esc(m.links.bot)}">${ic("bot")}Заполнить анкету в боте</button>` : ""}`}`);
    return;
  }
  const st = data.stats;
  if (!view(c, `${titleBlock("Ордерные реквизиты")}
    <div class="box pad" style="margin-top:8px;display:flex;align-items:center;gap:10px">
      ${data.status !== "approved" ? tag("Приостановлено администрацией", "red") : data.asleep ? tag(`Пауза до ${when(data.sleep_until)}`, "amber") : tag("На линии · заявки приходят все", "sea")}
    </div>
    <div class="tiles" style="margin-top:8px">
      <div class="tile"><small>Сегодня</small><b>+${usdt(st.today.income)}</b><em>${st.today.n} · ${rub(st.today.rub)}</em></div>
      <div class="tile"><small>Репутация</small><b>${data.reputation.score == null ? "—" : data.reputation.score}</b><em>${esc(data.reputation.line)}</em></div>
      <div class="tile"><small>Курс</small><b>${NF.format(n(data.terms.rate))} ₽</b><em>за USDT, без процента</em></div>
      <div class="tile"><small>С баланса до</small><b>${rub(data.cover_rub)}</b><em>свободно ${usdt(data.balance)} USDT</em></div>
    </div>
    ${data.strikes && !data.asleep ? `<p class="hint">Пропусков реквизитов подряд: ${data.strikes} из ${data.terms.strike_limit} — потом пауза.</p>` : ""}
    ${sec("В работе")}<div class="list">${data.working.length ? data.working.map(dealRow).join("") : empty("deals", "Взятых заявок нет")}</div>
    ${sec("Свободные заявки", `<button id="refresh">${ic("refresh").replace("<svg", '<svg style="width:15px;height:15px;vertical-align:-3px"')}</button>`)}
    <div class="list">${data.offers.length ? data.offers.map((d) => dealRow({ ...d, role: "offer" })).join("") : empty("bell", data.status === "approved" && !data.asleep ? "Свободных заявок нет — новые придут уведомлением" : "Заявки не приходят, пока доступ на паузе")}</div>
    ${sec("Время на оплату по умолчанию")}
    <div class="opts">${data.pay_choices.map((x) => `<button class="opt ${x === data.pay_minutes ? "on" : ""}" data-pm="${x}">${x} мин</button>`).join("")}</div>
    <div class="foot">7 дней: ${st.week.n} · +${usdt(st.week.income)} USDT · всего ${st.all.n}${st.all.success != null ? ` · успешных ${st.all.success}%` : ""}</div>`)) return;
  on("#refresh", () => { S.keep = true; render(); });
  on("[data-pm]", (b) => busy(b, async () => { try { await api("merchant", { body: { pay_minutes: n(b.dataset.pm) } }); toast(`По умолчанию — ${b.dataset.pm} мин на оплату`); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
  every(8000, async () => { try { const f = await api("merchant"); if (c.alive() && JSON.stringify([f.offers, f.working]) !== JSON.stringify([data.offers, data.working])) { S.keep = true; render(); } } catch (e) { /* retry */ } });
}

const OP_GROUPS = [["paid", "Проверьте оплату", "red"], ["checking", "Выдайте реквизиты", "blue"],
  ["waiting_payment", "Ждём перевод покупателя", "amber"], ["assigned", "Мерчант пересоздаёт ордер", ""], ["dispute", "Спор", "red"]];

async function operatorScreen(c) {
  const data = await api("operator");
  const groups = OP_GROUPS.map(([st, label, tone]) => [label, tone, data.working.filter((d) => d.status === st)]).filter((g) => g[2].length);
  if (!view(c, `${titleBlock("Оператор")}
    <div class="tiles" style="margin-top:8px">
      <div class="tile ${n(data.debt) ? "alert" : ""}"><small>Долг перед площадкой</small><b>${usdt(data.debt)}</b><em>USDT</em></div>
      <div class="tile"><small>В работе</small><b>${data.working.length}</b><em>свободных ${data.free.length}</em></div>
    </div>
    ${data.ratings && data.ratings.length ? `${sec("Оцените мерчантов")}<div class="list">${data.ratings.map((r) => `<button class="row" data-go="deal/${r.deal_id}"><span class="ic amber">${ic("star")}</span><span class="mid"><b>Сделка #${r.deal_id}</b><small>${r.gave ? "Как сработал мерчант" : "Мерчант не дал реквизиты"}</small></span>${chev()}</button>`).join("")}</div>` : ""}
    ${groups.map(([label, tone, list]) => `${sec(`${esc(label)} ${tag(list.length, tone)}`)}<div class="list">${list.map(dealRow).join("")}</div>`).join("")}
    ${sec("Свободные ордера", `<button id="refresh">${ic("refresh").replace("<svg", '<svg style="width:15px;height:15px;vertical-align:-3px"')}</button>`)}
    <div class="list">${data.free.length ? data.free.map(dealRow).join("") : empty("bell", "Новых ордеров нет — придут уведомлением")}</div>
    ${n(data.debt) ? `${sec("Погасить долг")}
      ${n(data.balance) ? `<button class="btn line" id="repay">${ic("wallet")}С баланса · ${usdt(Math.min(n(data.debt), n(data.balance)))} USDT</button>` : ""}
      ${data.debt_address ? `<div class="box pad" style="margin-top:8px">${kv("Сеть и монета", "TON · только USDT")}
        <button class="req" data-copy="${esc(data.debt_address)}" data-what="Адрес"><span class="v"><small>Ваш адрес погашения долга</small><span class="mono">${esc(data.debt_address)}</span></span>${ic("copy")}</button></div>
        <p class="hint">Всё, что придёт на этот адрес, уменьшит долг автоматически, без комиссии; сверх долга — на баланс.</p>` : ""}` : ""}`)) return;
  on("#refresh", () => { S.keep = true; render(); });
  on("#repay", async (b) => {
    if (!(await confirmBox(`Погасить ${usdt(Math.min(n(data.debt), n(data.balance)))} USDT долга с баланса?`))) return;
    busy(b, async () => { try { const r = await api("operator/repay", { method: "POST" }); toast(`Погашено ${usdt(r.repaid)} USDT`); S.keep = true; render(); } catch (e) { toast(e.message, true); } });
  });
  every(8000, async () => { try { const f = await api("operator"); if (c.alive() && JSON.stringify([f.free, f.working]) !== JSON.stringify([data.free, data.working])) { S.keep = true; render(); } } catch (e) { /* retry */ } });
}

async function adminScreen(c) {
  const a = await api("admin");
  const k = a.counts, mo = a.money;
  const tile = (go, label, value, alert, sub) => `<button class="tile ${alert ? "alert" : ""}" ${go ? `data-go="${go}"` : ""}><small>${label}</small><b>${value}</b>${sub ? `<em>${sub}</em>` : ""}</button>`;
  if (!view(c, `${titleBlock("Администрирование")}
    <div class="tiles" style="margin-top:8px">
      ${tile("admin/deals?dispute", "Споры", k.disputes, k.disputes)}
      ${tile("admin/deals?paid", "Ждут подтверждения", k.slow, k.slow, "дольше срока")}
      ${tile("admin/deals?open", "Открытых сделок", k.open, false, `ищут реквизиты: ${k.searching}`)}
      ${tile("", "Выводы на проверке", k.unknown_wd, k.unknown_wd, `в очереди ${k.queued_wd}`)}
    </div>
    ${sec("Деньги")}<div class="box pad">
      ${kv("Балансы пользователей", `<span class="num">${usdt(mo.users)} USDT</span>`)}
      ${mo.hot ? (mo.hot.error ? kv("Горячий кошелёк", "нет ответа сети") : kv("Горячий кошелёк", `<span class="num">${usdt(mo.hot.usdt)} USDT · <span class="${mo.hot.low ? "minus" : ""}">${mo.hot.ton} TON</span></span>`)) : kv("Горячий кошелёк", "выключен")}
      ${n(mo.unswept) ? kv("Не собрано с адресов", `<span class="num">${usdt(mo.unswept)} USDT</span>`) : ""}
      ${n(mo.op_debt) ? kv("Долг операторов", `<span class="num">${usdt(mo.op_debt)} USDT</span>`) : ""}
      ${kv("За 24 часа", `<span class="num">${a.day.deals} · ${rub(a.day.rub)} · +${usdt(a.day.income)} USDT</span>`)}
    </div>
    ${mo.hot && mo.hot.address ? `<button class="btn line" data-copy="${esc(mo.hot.address)}" data-what="Адрес">${ic("copy")}Адрес горячего кошелька (газ и выплаты)</button>` : ""}
    ${a.disputes.length ? `${sec("Споры")}<div class="list">${a.disputes.map(dealRow).join("")}</div>` : ""}
    ${a.slow.length ? `${sec("Продавец молчит")}<div class="list">${a.slow.map(dealRow).join("")}</div>` : ""}
    ${a.withdrawals.length ? `${sec("Выводы на проверке")}<div class="list">${a.withdrawals.map((w) => `<div class="row"><span class="ic red">${ic("up")}</span><span class="mid"><b class="num">#${w.id} · ${usdt(w.amount)} USDT</b><small class="mono">${esc((w.address || "").slice(0, 8))}…${esc((w.address || "").slice(-6))} · ID ${w.user_id}</small></span></div>`).join("")}</div><p class="hint">Решение по выводу — в боте: «Админ-панель → TON-кошелёк».</p>` : ""}
    ${k.signups || k.tickets || k.merchants || k.adjustments ? `${sec("В боте")}<div class="box pad">
      ${k.signups ? kv("Заявок на вход", k.signups) : ""}${k.merchants ? kv("Анкет мерчантов", k.merchants) : ""}
      ${k.tickets ? kv("Обращений", k.tickets) : ""}${k.adjustments ? kv("Корректировок ждут второго админа", k.adjustments) : ""}</div>` : ""}
    <button class="btn line" data-go="admin/deals?all">${ic("search")}Все сделки</button>`)) return;
  every(10000, async () => { try { const f = await api("admin"); if (c.alive() && JSON.stringify([f.counts, f.disputes.map((x) => x.id)]) !== JSON.stringify([a.counts, a.disputes.map((x) => x.id)])) { S.keep = true; render(); } } catch (e) { /* retry */ } });
}

async function adminDeals(c, filter) {
  filter = filter || S.adminFilter || "dispute";
  S.adminFilter = filter;
  const q = S.adminQ || "";
  const list = await api(`admin/deals?filter=${filter}${q ? "&q=" + encodeURIComponent(q) : ""}`);
  if (!view(c, `${titleBlock("Сделки площадки")}
    <label class="field"><div class="inp">${ic("search").replace("<svg", '<svg style="width:17px;height:17px;color:var(--muted)"')}<input id="q" inputmode="numeric" placeholder="Номер сделки или ID пользователя" value="${esc(q)}"></div></label>
    <div class="chips" style="margin-top:10px">${[["dispute", "Споры"], ["paid", "Ждут подтверждения"], ["open", "Открытые"], ["closed", "Закрытые"], ["all", "Все"]].map(([k, t]) => `<button class="chip ${k === filter ? "on" : ""}" data-f="${k}">${t}</button>`).join("")}</div>
    <div class="list" style="margin-top:10px">${list.deals.length ? list.deals.map(dealRow).join("") : empty("deals", "Сделок нет")}</div>`)) return;
  on("[data-f]", (b) => { S.adminQ = ""; go(`admin/deals?${b.dataset.f}`, { replace: true }); });
  const input = $app.querySelector("#q");
  input.oninput = debounce(() => { S.adminQ = input.value.trim(); S.keep = true; render(); }, 450);
}

/* ---------- profile ---------- */

const ROLE_TAGS = (r) => [
  r.admin && ["Администратор", "red"], r.operator && ["Оператор", "blue"], r.merchant === "approved" && ["Ордерный мерчант", "amber"],
  r.seller && ["Продавец", "sea"], r.team && [r.team.leader ? `Тимлид · ${r.team.name}` : `Команда · ${r.team.name}`, ""],
].filter(Boolean);

async function profile(c) {
  const [m, st] = await Promise.all([me(true), api("stats")]);
  const u = tgUser();
  const tags = ROLE_TAGS(m.roles);
  if (!view(c, `<div style="text-align:center;padding:14px 0 2px">${avatar("xl").replace('class="ava xl"', 'class="ava xl" style="margin:0 auto"')}
      <h1 style="font-size:20px;font-weight:600;margin:12px 0 2px">${esc([u.first_name, u.last_name].filter(Boolean).join(" ") || m.user.name)}</h1>
      <div class="hint" style="margin:0">${m.user.username ? "@" + esc(m.user.username) + " · " : ""}<button data-copy="${m.user.id}" data-what="ID" class="num" style="color:var(--muted)">ID ${m.user.id}</button></div>
      ${tags.length ? `<div style="display:flex;justify-content:center;flex-wrap:wrap;gap:6px;margin-top:10px">${tags.map(([t, tone]) => tag(t, tone)).join("")}</div>` : ""}
    </div>
    <div class="tiles" style="margin-top:14px">
      <div class="tile"><small>Куплено</small><b>${usdt(st.buyer.usdt)}</b><em>USDT · ${st.buyer.n} сделок</em></div>
      <div class="tile"><small>Продано</small><b>${rub(st.seller[3].rub)}</b><em>${st.seller[3].n} сделок${n(st.seller[3].income) ? " · +" + usdt(st.seller[3].income) + " USDT" : ""}</em></div>
    </div>
    ${sec("Аккаунт")}<div class="list">
      <button class="row" data-go="stats"><span class="ic">${ic("chart")}</span><span class="mid"><b>Статистика</b><small>Оборот, доход по дням, успешность</small></span>${chev()}</button>
      <button class="row" data-go="history"><span class="ic">${ic("clock")}</span><span class="mid"><b>История операций</b><small>Пополнения, выводы, сделки</small></span>${chev()}</button>
      <div class="row"><span class="ic">${ic("bell")}</span><span class="mid"><b>Уведомления без звука</b><small>О сделках — тихо</small></span><button class="sw ${m.user.quiet ? "on" : ""}" id="quiet" aria-label="Без звука"></button></div>
    </div>
    ${sec("Помощь")}<div class="list">
      ${m.links.manager ? `<button class="row" data-open="${esc(m.links.manager)}"><span class="ic">${ic("headset")}</span><span class="mid"><b>Менеджер</b><small>${m.links.manager_nick ? "@" + esc(m.links.manager_nick) + " · " : ""}вопросы по сделкам и условиям</small></span>${chev()}</button>` : ""}
      <button class="row" data-go="guides"><span class="ic">${ic("book")}</span><span class="mid"><b>Инструкции</b><small>Как купить, продать, вывести</small></span>${chev()}</button>
      ${m.links.chat ? `<button class="row" id="chat"><span class="ic">${ic("people")}</span><span class="mid"><b>Чат Strait Pay</b><small>Курс, новости, заявки</small></span>${chev()}</button>` : ""}
      ${m.links.channel ? `<button class="row" data-open="${esc(m.links.channel)}"><span class="ic">${ic("bell")}</span><span class="mid"><b>Инфо-канал</b><small>Правила и новости</small></span>${chev()}</button>` : ""}
    </div>
    ${m.links.bot ? `<button class="btn line" data-open="${esc(m.links.bot)}">${ic("bot")}Открыть бота</button>` : ""}
    <div class="foot">В Strait Pay с ${when(m.user.since).replace(/, \d\d:\d\d$/, "")}</div>`)) return;
  on("#quiet", (sw) => busy(sw, async () => {
    try { const r = await api("settings", { body: { quiet: !m.user.quiet } }); m.user.quiet = r.quiet; sw.classList.toggle("on", r.quiet); haptic("tap"); }
    catch (err) { toast(err.message, true); }
  }));
  on("#chat", (b) => busy(b, async () => { try { const r = await api("chat-invite", { method: "POST" }); openUrl(r.chat); } catch (e) { toast(e.message, true); } }));
  setBadge(m.counts.action);
}

async function statsScreen(c) {
  const period = S.period || "week";
  const st = await api("stats");
  const p = st.seller.find((x) => x.key === period);
  const max = Math.max(...st.days.map((d) => n(d.income)), 0.000001);
  if (!view(c, `${titleBlock("Статистика", "Продажи на ваших картах и ордерах")}
    <div class="seg">${[["today", "Сегодня"], ["week", "7 дней"], ["month", "30 дней"], ["all", "Всё"]].map(([k, t]) => `<button data-p="${k}" class="${k === period ? "on" : ""}">${t}</button>`).join("")}</div>
    <div class="tiles">
      <div class="tile"><small>Сделок</small><b>${p.n}</b></div>
      <div class="tile"><small>Доход</small><b>${usdt(p.income)}</b><em>USDT</em></div>
      <div class="tile"><small>Оборот</small><b>${rub(p.rub)}</b></div>
      <div class="tile"><small>Средний чек</small><b>${rub(p.avg)}</b></div>
      <div class="tile"><small>Успешных</small><b>${p.success == null ? "—" : p.success + "%"}</b></div>
      <div class="tile"><small>Подтверждаете за</small><b>${p.confirm_min == null ? "—" : p.confirm_min + " мин"}</b>${p.disputes ? `<em style="color:var(--red)">споров: ${p.disputes}</em>` : ""}</div>
    </div>
    ${sec("Доход по дням", `<span class="hint" style="margin:0">14 дней, USDT</span>`)}
    <div class="box pad"><div class="bars">${st.days.map((d) => `<div title="${esc(d.day)}: ${d.n} сд., +${usdt(d.income)} USDT"><i class="${n(d.income) ? "" : "zero"}" style="height:${Math.round((n(d.income) / max) * 100)}%"></i><span>${new Date(d.day).getDate()}</span></div>`).join("")}</div></div>
    ${sec("Покупки")}
    <div class="box pad">${kv("Сделок", st.buyer.n)}${kv("Переведено", `<span class="num">${rub(st.buyer.rub)}</span>`)}<div class="kv total"><span>Получено</span><b>${usdt(st.buyer.usdt)} USDT</b></div></div>`)) return;
  on("[data-p]", (b) => { S.period = b.dataset.p; haptic("select"); S.keep = true; render(); });
}

async function guidesScreen(c) {
  const data = await api("guides");
  view(c, `${titleBlock("Инструкции", "От первой покупки до вывода — по шагам")}
    <div class="list" style="margin-top:12px">${data.guides.map((g) => `<button class="row" data-go="guide/${esc(g.slug)}"><span class="ic">${ic("book")}</span>
      <span class="mid"><b>${esc(g.title)}</b><small>${esc(g.about)}</small></span>${chev()}</button>`).join("")}</div>
    ${S.me && S.me.links.manager ? `<button class="btn line" data-open="${esc(S.me.links.manager)}">${ic("headset")}Спросить менеджера</button>` : ""}`);
}

async function guideScreen(c, slug) {
  const g = await api(`guides/${slug}`);
  if (!view(c, `<article class="article" style="margin-top:6px">${g.html}</article>`)) return;
  $app.querySelectorAll(".article a[href]").forEach((a) => {
    const href = a.getAttribute("href");
    const local = /^\/docs\/([a-z]+)$/.exec(href);
    a.onclick = (e) => {
      e.preventDefault();
      if (local && local[1] !== "help") go(`guide/${local[1]}`);
      else if (href.startsWith("#")) { const t = document.getElementById(href.slice(1)); if (t) t.scrollIntoView({ behavior: "smooth" }); }
      else openUrl(new URL(href, location.origin).href);
    };
  });
}

/* ---------- routing: an in-memory stack, the hash only mirrors the current page ---------- */

const ROUTES = [
  [/^$/, home, "home"], [/^buy$/, (c) => exchange(c, "buy"), "page"], [/^sell$/, (c) => exchange(c, "sell"), "page"],
  [/^deals$/, dealsScreen, "page"], [/^deal\/(\d+)$/, (c, id) => dealScreen(c, id), "deal"],
  [/^deal\/(\d+)\/chat$/, (c, id) => dealScreen(c, id, "chat"), "deal"], [/^request\/(\d+)$/, (c, id) => dealScreen(c, id, "deal", true), "deal"],
  [/^deposit$/, depositScreen, "page"], [/^withdraw$/, withdrawScreen, "page"], [/^history$/, historyScreen, "page"],
  [/^work$/, workScreen, "page"], [/^cards$/, cardsScreen, "page"], [/^card\/new$/, cardNew, "page"], [/^card\/(\d+)$/, cardScreen, "page"],
  [/^orders$/, ordersScreen, "page"], [/^operator$/, operatorScreen, "page"], [/^admin$/, adminScreen, "page"],
  [/^admin\/deals(?:\?(\w+))?$/, adminDeals, "page"],
  [/^profile$/, profile, "page"], [/^stats$/, statsScreen, "page"], [/^guides$/, guidesScreen, "page"], [/^guide\/([a-z]+)$/, guideScreen, "page"],
];
const TABS = [["", "Главная", "home"], ["buy", "Обмен", "swap"], ["deals", "Сделки", "deals"], ["work", "Работа", "work"], ["profile", "Профиль", "user"]];
const TAB_OF = (p) => (/^(buy|sell)$/.test(p) ? "buy" : /^(deals|deal\/|request\/)/.test(p) ? "deals" :
  /^(work|cards|card\/|orders|operator|admin|stats)/.test(p) ? "work" : /^(profile|guides|guide\/)/.test(p) ? "profile" : "");

function parent(p) {
  if (/^deal\/\d+\/chat$/.test(p)) return p.replace(/\/chat$/, "");
  if (/^(deal|request)\//.test(p)) return "deals";
  if (/^card\//.test(p)) return "cards";
  if (/^admin\/deals/.test(p)) return "admin";
  if (/^(cards|orders|operator|admin|stats)$/.test(p)) return "work";
  if (/^guide\//.test(p)) return "guides";
  if (p === "guides") return "profile";
  if (p === "sell") return "buy";
  return "";
}

const path = () => (location.hash.startsWith("#/") ? decodeURIComponent(location.hash.slice(2)) : "");
const isRoot = (p) => TABS.some(([t]) => t === p);

function go(p, opts = {}) {
  const cur = path();
  if (!opts.replace && cur !== p) {
    if (isRoot(p)) S.stack = [];
    else S.stack.push(cur);
    if (S.stack.length > 30) S.stack.shift();
  }
  history.replaceState(null, "", location.pathname + location.search + "#/" + p);
  render();
}

function back() {
  if (S.sheet) { closeSheet(); return; }
  const p = path();
  const prev = S.stack.length ? S.stack.pop() : parent(p);
  history.replaceState(null, "", location.pathname + location.search + "#/" + prev);
  render();
}

function syncBack() {
  if (!tg || !tg.BackButton) return;
  try { if (S.sheet || !isRoot(path())) tg.BackButton.show(); else tg.BackButton.hide(); } catch (e) { /* old client */ }
}

function setBadge(count) {
  const b = $navIn.querySelector("[data-tab='deals'] .badge");
  if (b) { b.textContent = count; b.hidden = !count; }
}

function drawNav(p) {
  const tab = TAB_OF(p);
  $navIn.innerHTML = TABS.map(([t, label, icon]) => `<button data-tab="${t}" class="${t === tab ? "on" : ""}">${ic(icon)}${label}${t === "deals" ? `<span class="badge" hidden></span>` : ""}</button>`).join("");
  $nav.hidden = false;
  $navIn.querySelectorAll("[data-tab]").forEach((b) => { b.onclick = () => { haptic("select"); if (b.dataset.tab === tab && isRoot(p)) { window.scrollTo({ top: 0, behavior: "smooth" }); return; } go(b.dataset.tab); }; });
  if (S.me) setBadge(S.me.counts.action);
  syncBack();
}

async function render() {
  clearTimers();
  closeSheet(false);
  const t = ++S.seq;
  const p = path();
  drawNav(p);
  const route = ROUTES.find(([re]) => re.test(p));
  if (!route) { go("", { replace: true }); return; }
  const c = { t, alive: () => t === S.seq && path() === p };
  if (!S.keep) { $app.innerHTML = `<div class="screen">${SK[route[2]]()}</div>`; window.scrollTo(0, 0); }
  try { await route[1](c, ...p.match(route[0]).slice(1)); }
  catch (e) { if (c.alive()) failView(c, e); }
  finally { if (t === S.seq) S.keep = false; }
}

/* ---------- start ---------- */

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-go],[data-copy],[data-open]");
  if (!t || t.disabled) return;
  if (t.dataset.copy !== undefined) { e.preventDefault(); copy(t.dataset.copy, t.dataset.what); return; }
  if (t.dataset.open) { e.preventDefault(); openUrl(t.dataset.open); return; }
  e.preventDefault();
  haptic("tap");
  closeSheet(false);
  go(t.dataset.go);
});
window.addEventListener("hashchange", () => render());  // a link to #/… inside a page (our own moves do not fire it)
window.addEventListener("error", (e) => report(e.error || e.message, "window"));
window.addEventListener("unhandledrejection", (e) => { if (!(e.reason instanceof ApiError)) report(e.reason, "promise"); });

(function start() {
  if (!tg || !tg.initData) { gate("Откройте Strait Pay в Telegram — кнопкой «Открыть приложение» в боте."); return; }
  try { tg.ready(); tg.expand(); } catch (e) { /* old client */ }
  try {
    tg.setHeaderColor("#f4f6f7");
    tg.setBackgroundColor("#f4f6f7");
    if (tg.isVersionAtLeast("7.10")) tg.setBottomBarColor("#ffffff");
    if (tg.isVersionAtLeast("7.7")) tg.disableVerticalSwipes();
  } catch (e) { /* older clients */ }
  try { if (tg.BackButton) tg.BackButton.onClick(back); } catch (e) { /* old client */ }
  // the start page: ?p=deal/5 from the bot's buttons, startapp=deal-5 from t.me links; a hash survives a reload
  const fromQuery = new URLSearchParams(location.search).get("p");
  const fromLink = ((tg.initDataUnsafe && tg.initDataUnsafe.start_param) || "").replace(/-/g, "/");
  const fromHash = path();
  const first = [fromHash, fromQuery, fromLink].find((x) => x && ROUTES.some(([re]) => re.test(x))) || "";
  if (first && !isRoot(first)) S.stack = [parent(first)];
  history.replaceState(null, "", location.pathname + location.search + "#/" + first);
  render();
  loadAvatar();
})();
