/* Strait Pay mini app. No build step: one file, the Telegram SDK and our JSON API (/app/api, bot/api/webapp.py).
   Sign-in is Telegram's initData sent with every request; the bot checks it and applies its own rules. */
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const $toast = document.getElementById("toast");
const S = { me: null, timers: [], drafts: {} };

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
const haptic = (kind) => { try { kind === "tap" ? tg.HapticFeedback.impactOccurred("light") : tg.HapticFeedback.notificationOccurred(kind); } catch (e) { /* outside Telegram */ } };
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) => (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16)));

function toast(text, bad) {
  $toast.textContent = text;
  $toast.className = "toast" + (bad ? " bad" : "");
  $toast.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { $toast.hidden = true; }, 2600);
  haptic(bad ? "error" : "success");
}

function confirmBox(text) {
  return new Promise((ok) => {
    if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm(text, ok);
    else ok(window.confirm(text));
  });
}

function openUrl(url) {
  if (!url) return;
  if (tg && /^https:\/\/t\.me\//.test(url) && tg.openTelegramLink) tg.openTelegramLink(url);
  else if (tg && tg.openLink) tg.openLink(url);
  else window.open(url, "_blank");
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const t = document.createElement("textarea");
    t.value = text; document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); } catch (e2) { /* nothing more to try */ }
    t.remove();
  }
  toast(`${what || "Текст"} скопирован${what && /а$/.test(what) ? "а" : ""}`);
}

function every(ms, fn) { S.timers.push(setInterval(fn, ms)); }
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ---------- the API ---------- */

class ApiError extends Error { constructor(msg, code, status) { super(msg); this.code = code; this.status = status; } }

async function api(path, opts = {}) {
  const headers = { "X-Telegram-Init-Data": (tg && tg.initData) || "" };
  let body = opts.body;
  if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
  let r;
  try { r = await fetch("/app/api/" + path, { method: opts.method || (body ? "POST" : "GET"), headers, body }); }
  catch (e) { throw new ApiError("Нет связи — проверьте интернет и попробуйте ещё раз", "network", 0); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(err.message || "Что-то пошло не так. Попробуйте ещё раз", err.code || "http", r.status);
  }
  return data;
}

const GATES = { unauthorized: 1, expired: 1, start_bot: 1, banned: 1, signup: 1, join: 1 };

/* ---------- icons (24px, stroke) ---------- */

const P = {
  down: '<path d="M12 4v15M6 13l6 6 6-6"/>',
  up: '<path d="M12 20V5M6 11l6-6 6 6"/>',
  swap: '<path d="M7 7h11l-3-3M17 17H6l3 3"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  wallet: '<path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H18v3"/><rect x="4" y="8" width="16" height="11" rx="2.5"/><circle cx="16" cy="13.5" r="1.3" fill="currentColor"/>',
  deals: '<path d="M8 6h12M8 12h12M8 18h12"/><circle cx="4" cy="6" r="1" fill="currentColor"/><circle cx="4" cy="12" r="1" fill="currentColor"/><circle cx="4" cy="18" r="1" fill="currentColor"/>',
  card: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3 10h18M7 15h4"/>',
  user: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c1.4-3.6 4.2-5.2 7.5-5.2s6.1 1.6 7.5 5.2"/>',
  headset: '<path d="M4.5 14v-2a7.5 7.5 0 0 1 15 0v2"/><rect x="3.5" y="13" width="4" height="6" rx="1.6"/><rect x="16.5" y="13" width="4" height="6" rx="1.6"/><path d="M18.5 19c0 1.2-1.6 2-4 2"/>',
  chat: '<path d="M5 18.5V6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v7a2.5 2.5 0 0 1-2.5 2.5H8.5L5 18.5Z"/>',
  copy: '<rect x="8" y="8" width="11" height="11" rx="2.5"/><path d="M5 15V6.5A1.5 1.5 0 0 1 6.5 5H15"/>',
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
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
};
const ic = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;

/* ---------- pieces of screens ---------- */

const STATUS_TONE = { searching: "blue", assigned: "blue", checking: "blue", waiting_payment: "amber", paid: "blue", dispute: "red", completed: "green", cancelled: "gray", expired: "gray", void: "gray" };
const ROLE = { buyer: ["Покупка", "down"], seller: ["Продажа", "up"], operator: ["Ордер · оператор", "shield"], admin: ["Сделка", "deals"] };

function tgUser() { return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || {}; }

function avatar(cls) {
  const u = tgUser(), me = S.me ? S.me.user : {};
  const name = u.first_name || me.name || "S";
  const init = (name.trim()[0] || "S").toUpperCase() + ((u.last_name || "").trim()[0] || "").toUpperCase();
  return `<div class="ava ${cls || ""}">${u.photo_url ? `<img src="${esc(u.photo_url)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}${u.photo_url ? "" : esc(init)}</div>`;
}

function dealRow(d) {
  const [label, icon] = ROLE[d.role] || ROLE.admin;
  const tone = d.action ? "amber" : { completed: "green", dispute: "red", cancelled: "gray", expired: "gray", void: "gray" }[d.status] || "";
  return `<button class="row" data-go="deal/${d.id}">
    <span class="ic ${tone}">${ic(icon)}</span>
    <span class="mid"><b>${label} #${d.id}</b><small>${d.action ? "<b style='display:inline;color:#c27a00'>Нужно действие · </b>" : ""}${esc(d.status_text)} · ${when(d.created_at)}</small></span>
    <span class="end"><b>${rub(d.amount_rub)}</b><small>${usdt(d.usdt)} USDT</small></span>
  </button>`;
}

function empty(icon, text, action) {
  return `<div class="empty">${ic(icon)}${esc(text)}${action || ""}</div>`;
}

function skeleton() {
  return `<div class="screen"><div class="sk" style="height:46px;width:60%;margin:8px 0 24px"></div>
    <div class="sk" style="height:110px;margin-bottom:14px"></div><div class="sk" style="height:180px"></div></div>`;
}

function view(html, opts = {}) {
  $app.innerHTML = `<div class="screen">${html}</div>`;
  if (!opts.keepScroll) window.scrollTo(0, 0);
}

function head(title, sub) {
  return `<div class="head"><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ""}</div>`;
}

function brand() {
  return `<div class="brand"><img src="/docs/static/logo.png" alt="">Strait Pay · P2P-обмен USDT ⇄ RUB с защитой сделки</div>`;
}

function failView(e) {
  if (GATES[e.code]) return gate(e.message);
  view(`${head("Не получилось")}<div class="card pad"><p style="margin:0">${esc(e.message)}</p>
    <button class="btn" data-act="retry">${ic("refresh")}Повторить</button></div>`);
  $app.querySelector("[data-act=retry]").onclick = render;
}

function gate(text) {
  $nav.hidden = true;
  view(`<div class="gate"><img src="/docs/static/logo.png" alt="Strait Pay"><h1>Strait Pay</h1><p>${esc(text)}</p>
    <button class="btn" data-act="close">${ic("bot")}Вернуться в бота</button></div>`);
  $app.querySelector("[data-act=close]").onclick = () => (tg && tg.close ? tg.close() : null);
}

async function me(force) {
  if (!S.me || force) S.me = await api("me");
  return S.me;
}

/* ---------- screens ---------- */

async function home() {
  const [m, list] = await Promise.all([me(true), api("deals?scope=active")]);
  const u = tgUser();
  const b = m.balance;
  const locked = n(b.withdrawable) < n(b.available);
  const action = list.deals.filter((d) => d.action);
  const rest = list.deals.filter((d) => !d.action).slice(0, 4);
  view(`
    <div class="top">
      <div class="who" data-go="profile">${avatar()}<div><b>${esc(u.first_name || m.user.name || "Профиль")}</b><small>${m.user.username ? "@" + esc(m.user.username) : "ID " + m.user.id}</small></div></div>
      ${m.links.manager ? `<button class="ibtn" data-open="${esc(m.links.manager)}" aria-label="Менеджер">${ic("headset")}</button>` : ""}
    </div>
    <section class="hero">
      <div class="lbl">Баланс</div>
      <div class="sum">${usdt(b.available)}<small>USDT</small></div>
      <div class="sub">≈ ${rub(n(b.available) * n(m.rate.rate))} · курс ${NF.format(n(m.rate.rate))} ₽${m.rate.own ? " · ваши условия" : ""}</div>
      <div class="chips">
        ${n(b.frozen) ? `<span class="chip">${ic("lock")}В сделках ${usdt(b.frozen)}</span>` : ""}
        ${locked ? `<span class="chip blue">Можно вывести ${usdt(b.withdrawable)}</span>` : ""}
        ${b.debt ? `<span class="chip red">Долг оператора ${usdt(b.debt)}</span>` : ""}
        ${n(b.team) ? `<span class="chip green">Командный ${usdt(b.team)}</span>` : ""}
      </div>
    </section>
    <div class="acts">
      <button class="act" data-go="deposit"><i>${ic("down")}</i>Пополнить</button>
      <button class="act" data-go="withdraw"><i>${ic("up")}</i>Вывести</button>
      <button class="act" data-go="buy"><i>${ic("swap")}</i>Купить</button>
      <button class="act" data-go="history"><i>${ic("clock")}</i>История</button>
    </div>
    ${action.length ? `<div class="sec"><h2>Нужно ваше действие</h2></div><div class="list">${action.map(dealRow).join("")}</div>` : ""}
    ${rest.length || !action.length ? `<div class="sec"><h2>Активные сделки</h2>${list.deals.length ? `<a href="#/deals">Все</a>` : ""}</div>
    <div class="list">${rest.length ? rest.map(dealRow).join("") :
      empty("swap", "Открытых сделок нет", `<button class="btn soft" data-go="buy">${ic("swap")}Купить USDT за рубли</button>`)}</div>` : ""}
    ${m.roles.operator ? `<div class="sec"><h2>Оператор</h2></div>
      <button class="row list" data-go="operator"><span class="ic">${ic("shield")}</span>
      <span class="mid"><b>Кабинет оператора</b><small>Принятые ордера, свободные ордера, долг</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
    ${m.roles.seller || m.roles.merchant === "approved" ? `<div class="sec"><h2>Мерчант</h2><a href="#/cards">Карты</a></div>
      <button class="row list" data-go="cards"><span class="ic ${m.user.online ? "green" : "gray"}">${ic("power")}</span>
      <span class="mid"><b>${m.user.online ? "Вы на смене" : "Вы не на смене"}</b><small>Карты в потоке, лимиты и статистика</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
    <button class="promo" data-go="guides"><span style="flex:1"><b>Как это работает</b><span>Покупка, продажа, кошелёк и споры — по шагам</span></span><img src="/docs/static/flow.jpg" alt=""></button>
    ${brand()}
  `);
  setBadge(m.counts.action);
}

async function dealsScreen(scope) {
  scope = scope || S.dealScope || "active";
  S.dealScope = scope;
  const list = await api(`deals?scope=${scope}`);
  view(`${head("Сделки")}
    <div class="seg"><button data-scope="active" class="${scope === "active" ? "on" : ""}">Активные</button><button data-scope="history" class="${scope === "history" ? "on" : ""}">Завершённые</button></div>
    <div class="list" id="dl">${list.deals.length ? list.deals.map(dealRow).join("") : empty("deals", scope === "active" ? "Открытых сделок нет" : "Завершённых сделок пока нет")}</div>
    ${list.deals.length >= 30 ? `<button class="btn ghost" id="more">Показать ещё</button>` : ""}
    <button class="btn" data-go="buy">${ic("swap")}Новая покупка</button>`);
  $app.querySelectorAll("[data-scope]").forEach((b) => (b.onclick = () => { haptic("tap"); dealsScreen(b.dataset.scope); }));
  const more = $app.querySelector("#more");
  if (more) more.onclick = async () => {
    const last = list.deals[list.deals.length - 1].id;
    const next = await api(`deals?scope=${scope}&before=${last}`);
    list.deals.push(...next.deals);
    $app.querySelector("#dl").insertAdjacentHTML("beforeend", next.deals.map(dealRow).join(""));
    if (next.deals.length < 30) more.remove();
  };
}

function way(steps) {
  return `<ol class="way">${steps.map((s) => `<li class="${s.state}"><span class="dot">${s.state === "done" ? ic("check") : ""}</span>
    <b>${esc(s.title)}</b>${s.hint ? `<small>${esc(s.hint)}</small>` : ""}</li>`).join("")}</ol>`;
}

function requisites(d) {
  const r = d.requisites;
  const label = r.type === "sbp" ? "Телефон СБП" : "Номер карты";
  const exact = n(d.amount_rub).toFixed(2).replace(/\.00$/, "");
  return `<div class="card pad">
    <div class="kv" style="padding-top:0"><span>Банк</span><b>${esc(r.bank)} · ${r.type === "sbp" ? "СБП" : "карта"}</b></div>
    <button class="req" data-copy="${esc(r.number)}" data-what="${label}"><span class="v"><small>${label} — нажмите, чтобы скопировать</small><span class="mono">${esc(r.number)}</span></span>${ic("copy")}</button>
    ${r.holder ? `<div class="kv" style="margin-top:6px"><span>Получатель</span><b>${esc(r.holder)}</b></div>` : ""}
    <button class="req" data-copy="${exact}" data-what="Сумма"><span class="v"><small>Ровно — одним переводом</small><span class="mono">${rub(d.amount_rub)}</span></span>${ic("copy")}</button>
  </div>`;
}

async function dealScreen(id) {
  const { deal: d } = await api(`deals/${id}`);
  S.dealSeen = JSON.stringify(d);
  const buyer = d.role === "buyer";
  const tone = STATUS_TONE[d.status] || "gray";
  const has = (a) => d.actions.includes(a);
  const timer = d.expires_at && ["waiting_payment", "searching", "assigned", "checking"].includes(d.status);
  const what = d.status === "waiting_payment" ? (buyer ? "Оплатите за" : "Покупатель оплачивает") :
    d.status === "searching" ? "Ищем реквизиты" : d.status === "assigned" ? "Мерчант готовит реквизиты" : "Проверка ордера";
  view(`
    <div class="head" style="display:flex;justify-content:space-between;align-items:center;gap:10px">
      <h1>${esc((ROLE[d.role] || ROLE.admin)[0])} #${d.id}</h1><span class="chip ${tone}">${esc(d.status_text)}</span>
    </div>
    <div class="card pad" style="margin-top:12px">
      ${buyer || d.role === "admin" ? `
        <div class="kv" style="padding-top:0"><span>Вы переводите</span><b>${rub(d.amount_rub)}</b></div>
        <div class="kv total"><span>Получите</span><b>${usdt(d.usdt)} USDT</b></div>` : `
        <div class="kv" style="padding-top:0"><span>Покупатель переводит</span><b>${rub(d.amount_rub)}</b></div>
        <div class="kv total"><span>${d.bybit && d.role === "operator" ? "Зайти на" : "Спишется"}</span><b>${usdt(d.usdt)} USDT</b></div>
        ${d.income ? `<div class="kv"><span>Ваш доход</span><b class="plus">+${usdt(d.income)} USDT</b></div>` : ""}`}
      <div class="kv"><span>Курс</span><b>${NF.format(n(d.rate))} ₽</b></div>
      ${d.sender_bank ? `<div class="kv"><span>Банк покупателя</span><b>${esc(d.sender_bank)}</b></div>` : ""}
      <div class="kv"><span>Создана</span><b>${when(d.created_at)}</b></div>
      ${d.close_reason ? `<div class="kv"><span>Итог</span><b>${esc(d.close_reason)}</b></div>` : ""}
    </div>
    ${timer ? `<div class="card pad" style="margin-top:10px;display:flex;align-items:center;justify-content:space-between">
      <span style="color:var(--slate);font-weight:700">${what}</span><span class="timer" id="timer">${mmss(left(d.expires_at))}</span></div>` : ""}
    ${d.requisites ? `<div class="sec"><h2>${buyer ? "Реквизиты для перевода" : "Реквизиты"}</h2></div>${requisites(d)}` : ""}
    ${buyer && d.status === "waiting_payment" ? `<p class="hint">Переведите ровно эту сумму одним платежом, без комментария. Затем скачайте в банке PDF-чек и прикрепите его.</p>` : ""}
    ${has("receipt") || has("late_receipt") ? `<label class="btn green" style="cursor:pointer">${ic("clip")}${has("late_receipt") ? "Я перевёл — прикрепить чек" : "Прикрепить PDF-чек"}<input type="file" id="file" accept="application/pdf,image/jpeg,image/png" hidden></label>` : ""}
    ${d.bybit_url && d.role === "operator" ? `<button class="btn ghost" data-open="${esc(d.bybit_url)}">${ic("swap")}Открыть ордер Bybit</button>` : ""}
    ${has("accept") ? `<button class="btn green" data-op="accept">${ic("check")}Принять ордер</button>` : ""}
    ${has("give") ? `<div class="card pad" style="margin-top:12px"><b>Реквизиты клиенту</b>
      <p class="hint" style="margin-top:2px">Одним сообщением: номер карты или телефон СБП и банк. ФИО — по желанию, второй строкой.</p>
      <div class="inp" style="margin-top:8px"><textarea id="req" rows="2" placeholder="2200 7001 2345 6781 Сбербанк"></textarea></div>
      <button class="btn green" data-op="requisites">${ic("send")}Выдать реквизиты клиенту</button></div>` : ""}
    ${has("pass_on") || has("recreate") ? `<div class="btns">
      ${has("pass_on") ? `<button class="btn ghost" data-op="pass_on">${ic("search")}Другой мерчант</button>` : ""}
      ${has("recreate") ? `<button class="btn ghost" data-op="recreate">${ic("refresh")}Пересоздать ордер</button>` : ""}</div>` : ""}
    ${has("no_requisites") ? `<button class="btn danger" data-op="no_requisites">${ic("x")}В ордере нет реквизитов</button>` : ""}
    ${has("close") ? `<button class="btn danger" data-op="close">${ic("x")}Закрыть сделку</button>` : ""}
    ${has("confirm") ? `<button class="btn green" id="confirm">${ic("check")}${d.bybit ? "Подтвердить оплату" : "Деньги пришли"}</button>
      <p class="hint">Подтверждайте, только когда видите поступление ${rub(d.amount_rub)} в банке. Отменить подтверждение нельзя.</p>` : ""}
    <div class="sec"><h2>Ход сделки</h2></div>
    <div class="card pad">${way(d.steps)}</div>
    <div class="btns">
      ${has("chat") ? `<button class="btn ghost" data-go="deal/${d.id}/chat">${ic("chat")}Чат</button>` : ""}
      ${has("give_bot") || has("link_bot") || has("dispute_bot") || (d.receipt && !buyer) ? `<button class="btn ghost" data-act="bot">${ic("bot")}${has("give_bot") ? "Выдать в боте" : has("link_bot") ? "Ссылка в боте" : has("dispute_bot") ? "Спор в боте" : "Чек в боте"}</button>` : ""}
      ${has("cancel") || has("cancel_request") ? `<button class="btn danger" id="cancel">${ic("x")}Отменить</button>` : ""}
    </div>
  `, { keepScroll: S.keepScroll });
  S.keepScroll = false;
  if (timer) every(1000, () => { const t = $app.querySelector("#timer"); if (!t) return; const s = left(d.expires_at); t.textContent = mmss(s); t.classList.toggle("low", s < 180); });
  if (["searching", "assigned", "checking", "waiting_payment", "paid", "dispute"].includes(d.status)) {
    every(5000, async () => {
      try {
        const fresh = await api(`deals/${id}`);
        if (JSON.stringify(fresh.deal) !== S.dealSeen && path() === `deal/${id}`) { S.keepScroll = true; render(); }
      } catch (e) { /* the next tick tries again */ }
    });
  }
  const file = $app.querySelector("#file");
  if (file) file.onchange = async () => {
    if (!file.files[0]) return;
    const form = new FormData();
    form.append("file", file.files[0]);
    toast("Отправляем чек…");
    try { await api(`deals/${id}/receipt`, { body: form }); toast("Чек отправлен — ждём подтверждения"); render(); }
    catch (e) { toast(e.message, true); }
  };
  const confirmBtn = $app.querySelector("#confirm");
  if (confirmBtn) confirmBtn.onclick = async () => {
    if (!(await confirmBox(`Деньги пришли? На ваш счёт поступило ${rub(d.amount_rub)}. Покупателю уйдёт ${usdt(d.usdt)} USDT. Отменить будет нельзя.`))) return;
    try { await api(`deals/${id}/confirm`, { method: "POST" }); toast("Сделка завершена"); render(); } catch (e) { toast(e.message, true); }
  };
  const cancel = $app.querySelector("#cancel");
  if (cancel) cancel.onclick = async () => {
    const text = d.status === "waiting_payment" ? `Отменить сделку #${d.id}? Если уже перевели ${rub(d.amount_rub)} — не отменяйте, прикрепите чек.` : `Отменить заявку #${d.id}?`;
    if (!(await confirmBox(text))) return;
    try { await api(`deals/${id}/cancel`, { method: "POST" }); toast("Отменено"); render(); } catch (e) { toast(e.message, true); }
  };
  const ASK = {
    no_requisites: "В ордере нет реквизитов? Мерчанту засчитается пропуск, заявка уйдёт другим.",
    pass_on: "Передать заявку другому мерчанту? Этот мерчант её больше не возьмёт.",
    recreate: "Попросить мерчанта пересоздать ордер? Выданные реквизиты отзовутся.",
    close: "Закрыть сделку без перевода? Покупатель уже перевёл — не закрывайте.",
  };
  const DONE = { accept: "Ордер ваш", requisites: "Реквизиты выданы клиенту", recreate: "Мерчант пришлёт новый ордер",
    close: "Сделка закрыта", pass_on: "Ищем другого мерчанта", no_requisites: "Пропуск засчитан, ищем другого мерчанта" };
  $app.querySelectorAll("[data-op]").forEach((b) => (b.onclick = async () => {
    const act = b.dataset.op;
    if (ASK[act] && !(await confirmBox(ASK[act]))) return;
    const body = act === "requisites" ? { text: $app.querySelector("#req").value } : undefined;
    b.disabled = true;
    try {
      await api(`deals/${id}/${act}`, body ? { body } : { method: "POST" });
      toast(DONE[act]);
      if (act === "pass_on" || act === "no_requisites") go("operator", true); else render();
    } catch (e) { toast(e.message, true); b.disabled = false; }
  }));
  const bot = $app.querySelector("[data-act=bot]");
  if (bot) bot.onclick = () => (S.me && S.me.links.bot ? openUrl(S.me.links.bot) : tg && tg.close());
}

function msgHtml(m) {
  return `<div class="msg ${m.mine ? "me" : ""}" data-id="${m.id}"><div class="who">${esc(m.mine ? "Вы" : m.role)}</div><div class="t">${esc(m.text)}</div><time>${when(m.at)}</time></div>`;
}

async function chatScreen(id) {
  const data = await api(`deals/${id}/chat`);
  $nav.hidden = true;
  view(`${head(`Чат сделки #${id}`)}
    <div class="members">${data.members.map((x) => `<span class="chip gray">${esc(x)}</span>`).join("")}</div>
    <p class="hint">${ic("shield").replace("<svg", '<svg style="width:14px;height:14px;vertical-align:-2px"')} Ссылки и @юзернеймы запрещены — общайтесь только здесь.</p>
    <div class="chat" id="chat">${data.messages.length ? data.messages.map(msgHtml).join("") : `<div class="empty" id="none">${ic("chat")}Сообщений пока нет — напишите первым</div>`}</div>
    ${data.open ? `<div class="compose"><div class="in"><div class="inp"><textarea id="text" rows="1" maxlength="1000" placeholder="Сообщение">${esc(S.drafts[id] || "")}</textarea></div>
      <button class="send" id="send" aria-label="Отправить">${ic("send")}</button></div></div>` : `<p class="hint">Сделка закрыта — чат только для чтения.</p>`}`);
  const box = $app.querySelector("#chat");
  let last = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
  const add = (msgs) => {
    if (!msgs.length) return;
    const none = $app.querySelector("#none");
    if (none) none.remove();
    msgs.filter((m) => !box.querySelector(`[data-id="${m.id}"]`)).forEach((m) => box.insertAdjacentHTML("beforeend", msgHtml(m)));
    last = msgs[msgs.length - 1].id;
    window.scrollTo(0, document.body.scrollHeight);
  };
  window.scrollTo(0, document.body.scrollHeight);
  every(3000, async () => { try { add((await api(`deals/${id}/chat?after=${last}`)).messages); } catch (e) { /* retry */ } });
  const text = $app.querySelector("#text");
  const send = $app.querySelector("#send");
  if (!text) return;
  const grow = () => { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 120) + "px"; };
  text.oninput = () => { S.drafts[id] = text.value; grow(); };
  grow();
  send.onclick = async () => {
    const v = text.value.trim();
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
}

const OP_GROUPS = [["paid", "Проверьте оплату", "red"], ["checking", "Выдайте реквизиты", "blue"],
  ["waiting_payment", "Ждём перевод покупателя", "amber"], ["assigned", "Мерчант пересоздаёт ордер", "gray"], ["dispute", "Спор", "red"]];

async function operatorScreen() {
  const data = await api("operator");
  const groups = OP_GROUPS.map(([st, label, tone]) => [label, tone, data.working.filter((d) => d.status === st)]).filter((g) => g[2].length);
  view(`${head("Оператор", n(data.debt) ? `Долг перед площадкой: <b>${usdt(data.debt)} USDT</b>` : "Долга нет")}
    ${groups.length ? groups.map(([label, tone, list]) => `<div class="sec"><h2>${esc(label)} <span class="chip ${tone}">${list.length}</span></h2></div>
      <div class="list">${list.map(dealRow).join("")}</div>`).join("") : `<div class="list" style="margin-top:14px">${empty("shield", "Принятых ордеров нет")}</div>`}
    <div class="sec"><h2>Свободные ордера</h2><button id="refresh">${ic("refresh").replace("<svg", '<svg style="width:18px;height:18px;vertical-align:-4px"')}</button></div>
    <div class="list">${data.free.length ? data.free.map(dealRow).join("") : empty("bell", "Новых ордеров нет — придут уведомлением")}</div>
    <p class="hint">Долг погашается в боте: «Оператор» → «Погасить».</p>`);
  $app.querySelector("#refresh").onclick = () => operatorScreen();
  every(10000, async () => { if (path() === "operator") { try { const f = await api("operator"); if (JSON.stringify(f) !== JSON.stringify(data)) operatorScreen(); } catch (e) { /* retry */ } } });
}

async function buyScreen() {
  const m = await me();
  view(`${head("Купить USDT", `Курс ${NF.format(n(m.rate.rate))} ₽ · комиссия ${esc(m.rate.pct)}%${m.rate.own ? " · ваши условия" : ""}`)}
    <label class="field"><span>Сколько рублей переведёте</span><div class="inp big"><input id="amt" inputmode="decimal" placeholder="10 000" autocomplete="off"><span class="unit">₽</span></div></label>
    <div class="chips" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${[5000, 10000, 20000, 50000].map((v) => `<button class="chip" data-v="${v}">${NF.format(v)} ₽</button>`).join("")}</div>
    <div id="quote"></div>`);
  const input = $app.querySelector("#amt");
  const out = $app.querySelector("#quote");
  let q = null;
  const quote = debounce(async () => {
    const v = input.value.replace(/\s/g, "").replace(",", ".");
    q = null;
    if (!v) { out.innerHTML = ""; return; }
    try {
      q = await api(`buy/quote?amount=${encodeURIComponent(v)}`);
      out.innerHTML = `<div class="card pad" style="margin-top:16px">
        <div class="kv" style="padding-top:0"><span>По курсу ${NF.format(n(q.rate))} ₽</span><b>${usdt(q.usdt)} USDT</b></div>
        <div class="kv"><span>Комиссия ${esc(q.pct)}%</span><b>−${usdt(q.fee)} USDT</b></div>
        <div class="kv total"><span>Вы получите</span><b>${usdt(q.credit)} USDT</b></div></div>
        <div class="card pad" style="margin-top:10px;display:flex;gap:12px;align-items:flex-start">
          <span class="ic" style="width:40px;height:40px;border-radius:50%;display:grid;place-items:center;background:var(--soft);color:var(--deep);flex:none">${ic(q.mode === "card" ? "card" : "search")}</span>
          <div>${q.mode === "card" ? `<b>Готовая карта продавца</b><div class="hint" style="margin:2px 0 0">${esc(q.bank)} · ${q.type === "sbp" ? "СБП" : "карта"}. Реквизиты появятся после создания, на перевод и чек — ${q.minutes} мин.</div>`
            : `<b>Реквизиты под вашу сумму</b><div class="hint" style="margin:2px 0 0">Выдаст ордерный мерчант, ищем до ${q.search_minutes} мин — придёт уведомление. На оплату — от ${q.minutes} мин.</div>`}</div></div>
        <button class="btn" id="go">${ic("check")}${q.mode === "card" ? "Создать сделку" : "Создать заявку"}</button>
        <p class="hint">Переводите только после создания и только на показанные реквизиты.</p>`;
      out.querySelector("#go").onclick = create;
    } catch (e) { out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }, 350);
  const create = async () => {
    if (!q) return;
    const btn = out.querySelector("#go");
    btn.disabled = true;
    try {
      const res = await api("buy", { body: { amount_rub: q.amount_rub, credit: q.credit, card_id: q.card_id } });
      toast(q.mode === "card" ? "Сделка создана" : "Заявка создана — ищем реквизиты");
      go(`deal/${res.deal.id}`, true);
    } catch (e) { toast(e.message, true); btn.disabled = false; quote(); }
  };
  input.oninput = quote;
  $app.querySelectorAll("[data-v]").forEach((b) => (b.onclick = () => { input.value = NF.format(n(b.dataset.v)); quote(); }));
  input.focus();
}

async function depositScreen(tab) {
  tab = tab || "net";
  const w = await api("wallet");
  view(`${head("Пополнить", `Только USDT · комиссия ${esc(w.deposit.fee)} · от ${esc(w.deposit.min)} USDT`)}
    <div class="seg"><button data-tab="net" class="${tab === "net" ? "on" : ""}">Адрес в сети</button><button data-tab="inv" class="${tab === "inv" ? "on" : ""}">Счёт xRocket</button></div>
    ${tab === "net" ? `<div class="card pad"><b>Выберите сеть</b><p class="hint" style="margin-top:2px">Бот выдаст адрес, зачислим автоматически после подтверждения сети.</p>
      <div class="nets">${w.networks.map((x) => `<button class="net" data-net="${esc(x.code)}">${esc(x.name)}</button>`).join("") || `<p class="hint">Сети сейчас недоступны</p>`}</div></div>
      <p class="hint">${ic("info").replace("<svg", '<svg style="width:14px;height:14px;vertical-align:-2px"')} Отправляйте только USDT и только в выбранной сети — иначе деньги не зачислятся.</p>`
    : `<label class="field"><span>Сумма пополнения</span><div class="inp big"><input id="amt" inputmode="decimal" placeholder="100"><span class="unit">USDT</span></div></label>
      <p class="hint">Оплата в @xRocket в два нажатия. Минимум ${esc(w.deposit.min)} USDT.</p>
      <button class="btn" id="inv">${ic("wallet")}Создать счёт</button>`}
    ${w.pending_deposits.length ? `<div class="sec"><h2>Ожидают оплаты</h2></div><div class="list">${w.pending_deposits.map((d) => `<button class="row" data-go="deposit/${d.id}">
      <span class="ic amber">${ic("clock")}</span><span class="mid"><b>${d.network ? "Адрес · " + esc(d.network_name) : "Счёт xRocket"}</b><small>#${d.id} · ${when(d.created_at)}</small></span>
      <span class="end"><b>${d.amount ? usdt(d.amount) + " USDT" : "любая сумма"}</b></span></button>`).join("")}</div>` : ""}`);
  $app.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => depositScreen(b.dataset.tab)));
  $app.querySelectorAll("[data-net]").forEach((b) => (b.onclick = async () => {
    b.classList.add("on");
    try { const r = await api("deposits", { body: { network: b.dataset.net } }); go(`deposit/${r.deposit.id}`); }
    catch (e) { toast(e.message, true); b.classList.remove("on"); }
  }));
  const inv = $app.querySelector("#inv");
  if (inv) inv.onclick = async () => {
    inv.disabled = true;
    try {
      const r = await api("deposits", { body: { amount: $app.querySelector("#amt").value.replace(/\s/g, "").replace(",", ".") } });
      go(`deposit/${r.deposit.id}`);
    } catch (e) { toast(e.message, true); inv.disabled = false; }
  };
}

async function depositView(id, check) {
  const r = await api(`deposits/${id}${check ? "?check=1" : ""}`);
  const d = r.deposit;
  const paid = d.status === "paid";
  view(`${head(d.network ? `Пополнение · ${d.network_name}` : `Счёт #${d.id}`)}
    ${paid ? `<div class="card pad" style="text-align:center"><div class="ic green" style="width:64px;height:64px;margin:6px auto 10px;border-radius:50%;display:grid;place-items:center;background:#e3f8ef;color:#0a9c6a">${ic("check")}</div>
      <b style="font:700 20px Unbounded,sans-serif">+${usdt(d.credit)} USDT</b><p class="hint">Зачислено на баланс</p></div>
      <button class="btn" data-go="">${ic("wallet")}В кошелёк</button>` :
    d.status === "active" && d.address ? `<div class="card pad">
      <div class="kv" style="padding-top:0"><span>Сеть</span><b>${esc(d.network_name)} · только USDT</b></div>
      <button class="req" data-copy="${esc(d.address)}" data-what="Адрес"><span class="v"><small>Адрес — нажмите, чтобы скопировать</small><span class="mono">${esc(d.address)}</span></span>${ic("copy")}</button>
      ${d.expires_at ? `<div class="kv" style="margin-top:6px"><span>Действует до</span><b>${when(d.expires_at)}</b></div>` : ""}</div>
      <button class="btn" data-copy="${esc(d.address)}" data-what="Адрес">${ic("copy")}Скопировать адрес</button>
      <p class="hint">Зачислим автоматически после подтверждения сети — придёт уведомление. Другая монета или сеть — деньги не вернуть.</p>` :
    d.status === "active" ? `<div class="card pad">
      <div class="kv" style="padding-top:0"><span>К оплате</span><b>${usdt(d.amount)} USDT</b></div>
      <div class="kv total"><span>Зачислим</span><b>${usdt(d.credit)} USDT</b></div></div>
      ${d.link ? `<button class="btn" data-open="${esc(d.link)}">${ic("wallet")}Оплатить в xRocket</button>` : ""}` :
    `<div class="card pad"><b>${d.status === "cancelled" ? "Пополнение отменено" : d.status === "expired" ? "Время оплаты вышло" : "Пополнение не создано"}</b>
      <p class="hint">${d.status === "cancelled" ? "Если вы всё же оплатили — зачислим автоматически." : "Создайте новое пополнение."}</p></div>
      <button class="btn" data-go="deposit">${ic("plus")}Новое пополнение</button>`}
    ${d.status === "active" ? `<div class="btns"><button class="btn ghost" id="check">${ic("refresh")}Проверить</button><button class="btn danger" id="cancel">${ic("x")}Отменить</button></div>` : ""}`);
  if (d.status === "active") {
    every(10000, async () => {
      try { const f = await api(`deposits/${id}`); if (f.deposit.status !== "active" && path() === `deposit/${id}`) depositView(id); } catch (e) { /* retry */ }
    });
    $app.querySelector("#check").onclick = async () => {
      try { await depositView(id, true); if (S.lastDeposit === "active") toast("Оплата ещё не поступила — проверим автоматически"); } catch (e) { toast(e.message, true); }
    };
    $app.querySelector("#cancel").onclick = async () => {
      if (!(await confirmBox("Отменить пополнение? Если уже отправили USDT — не отменяйте, зачислим сами."))) return;
      try { await api(`deposits/${id}/cancel`, { method: "POST" }); toast("Пополнение отменено"); depositView(id); } catch (e) { toast(e.message, true); }
    };
  }
  S.lastDeposit = d.status;
  if (paid && check) haptic("success");
}

async function withdrawScreen(tab) {
  tab = tab || S.wdTab || "xrocket";
  S.wdTab = tab;
  const w = await api("wallet");
  const req = uuid();
  let net = tab === "chain" ? (S.wdNet && w.networks.find((x) => x.code === S.wdNet) ? S.wdNet : (w.networks[0] || {}).code) : null;
  view(`${head("Вывести", `Можно вывести <b>${usdt(w.balance.withdrawable)} USDT</b>${n(w.balance.withdrawable) < n(w.balance.available) ? ` из ${usdt(w.balance.available)}` : ""}`)}
    ${w.lock_note ? `<div class="card pad" style="display:flex;gap:10px"><span style="color:var(--deep);flex:none">${ic("info")}</span><span class="hint" style="margin:0">${esc(w.lock_note)}</span></div>` : ""}
    <div class="seg"><button data-tab="xrocket" class="${tab === "xrocket" ? "on" : ""}">Чек xRocket</button><button data-tab="chain" class="${tab === "chain" ? "on" : ""}">На кошелёк</button></div>
    ${tab === "chain" ? `<div class="nets" id="nets">${w.networks.map((x) => `<button class="net ${x.code === net ? "on" : ""}" data-net="${esc(x.code)}">${esc(x.name)}</button>`).join("")}</div>
      <label class="field"><span>Адрес кошелька USDT</span><div class="inp"><input id="addr" class="mono" autocomplete="off" spellcheck="false" placeholder="Вставьте адрес"></div></label>
      <label class="field" id="memoBox" hidden><span>Memo (если выводите на биржу)</span><div class="inp"><input id="memo" autocomplete="off" placeholder="Необязательно"></div></label>` :
      `<p class="hint">Чек придёт в чат с ботом — активирует его только ваш аккаунт, USDT придут в @xRocket. Мгновенно.</p>`}
    <label class="field"><span>Сумма списания</span><div class="inp big"><input id="amt" inputmode="decimal" placeholder="0"><button class="max" id="max">Макс</button></div></label>
    <div id="q" class="hint">${esc(tab === "chain" ? w.withdraw.chain_terms : w.withdraw.cheque_terms)} · минимум ${esc(tab === "chain" ? w.withdraw.chain_min : w.withdraw.min)} USDT</div>
    <button class="btn" id="go" disabled>${ic("up")}Вывести</button>
    ${w.withdrawals.length ? `<div class="sec"><h2>В пути</h2></div><div class="list">${w.withdrawals.map((x) => `<div class="row">
      <span class="ic ${x.status === "queued" ? "amber" : ""}">${ic(x.status === "queued" ? "clock" : "up")}</span>
      <span class="mid"><b>${usdt(x.receive)} USDT · ${x.method === "chain" ? esc(x.network) : "чек"}</b><small>#${x.id} · ${esc(x.status_text)}</small></span>
      ${x.status === "queued" ? `<button class="chip red" data-cancel="${x.id}">Отменить</button>` : ""}</div>`).join("")}</div>` : ""}`);
  const amt = $app.querySelector("#amt");
  const qbox = $app.querySelector("#q");
  const goBtn = $app.querySelector("#go");
  const addr = $app.querySelector("#addr");
  const memoBox = $app.querySelector("#memoBox");
  let q = null;
  const setNet = (code) => {
    net = code; S.wdNet = code;
    $app.querySelectorAll("[data-net]").forEach((b) => b.classList.toggle("on", b.dataset.net === code));
    const last = (w.networks.find((x) => x.code === code) || {}).last_address;
    if (addr && !addr.value && last) addr.value = last;
    if (memoBox) memoBox.hidden = code !== "TON";
    quote();
  };
  const quote = debounce(async () => {
    const v = amt.value.replace(/\s/g, "").replace(",", ".");
    q = null; goBtn.disabled = true;
    if (!v) return;
    try {
      q = await api(`withdraw/quote?method=${tab}${tab === "chain" ? "&network=" + net : ""}&amount=${encodeURIComponent(v)}`);
      if (q.error) { qbox.innerHTML = `<span class="err" style="margin:0">${esc(q.error)}</span>`; return; }
      qbox.innerHTML = `Комиссия ${esc(q.terms)}: −${usdt(q.fee)} USDT · <b style="color:var(--ink)">придёт ${usdt(q.receive)} USDT</b>`;
      q.amount = v;
      goBtn.disabled = tab === "chain" && !(addr.value.trim());
    } catch (e) { qbox.innerHTML = `<span class="err" style="margin:0">${esc(e.message)}</span>`; }
  }, 350);
  amt.oninput = quote;
  if (addr) addr.oninput = () => { goBtn.disabled = !(q && !q.error && addr.value.trim()); };
  $app.querySelector("#max").onclick = () => { amt.value = w.balance.withdrawable; quote(); };
  $app.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => withdrawScreen(b.dataset.tab)));
  $app.querySelectorAll("[data-net]").forEach((b) => (b.onclick = () => setNet(b.dataset.net)));
  if (net) setNet(net);
  $app.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmBox("Отменить вывод из очереди? USDT вернутся на баланс."))) return;
    try { await api(`withdrawals/${b.dataset.cancel}/cancel`, { method: "POST" }); toast("Вывод отменён, USDT на балансе"); withdrawScreen(tab); } catch (e) { toast(e.message, true); }
  }));
  goBtn.onclick = async () => {
    if (!q || q.error) return;
    const where = tab === "chain" ? `в сети ${(w.networks.find((x) => x.code === net) || {}).name} на ${addr.value.trim().slice(0, 6)}…${addr.value.trim().slice(-4)}` : "чеком xRocket";
    if (!(await confirmBox(`Вывести ${usdt(q.amount)} USDT ${where}? Придёт ${usdt(q.receive)} USDT.${tab === "chain" ? " Перевод в блокчейне не отменить." : ""}`))) return;
    goBtn.disabled = true;
    try {
      const body = { method: tab, amount: q.amount, fee: q.fee, request_id: req };
      if (tab === "chain") Object.assign(body, { network: net, address: addr.value.trim(), memo: ($app.querySelector("#memo") || {}).value || null });
      const r = await api("withdraw", { body });
      toast(r.message, !r.ok);
      if (r.ok) go("", true); else withdrawScreen(tab);
    } catch (e) { toast(e.message, true); goBtn.disabled = false; }
  };
}

const KIND_ICON = { deposit: ["down", "green"], ton_deposit: ["down", "green"], withdraw: ["up", ""], withdraw_refund: ["refresh", "green"],
  deal_buy: ["swap", "green"], deal_sell: ["swap", ""], admin: ["shield", "gray"], team_fee: ["people", "green"], team_income: ["people", "green"],
  team_out: ["people", ""], team_in: ["people", "green"], debt_repay: ["shield", ""], freeze: ["lock", "gray"], unfreeze: ["lock", "gray"] };

function historyRow(r) {
  const [icon, tone] = KIND_ICON[r.kind] || ["clock", "gray"];
  const amount = n(r.delta) ? `<b class="${n(r.delta) > 0 ? "plus" : "minus"}">${signed(r.delta)}</b>` : `<b class="minus">${signed(r.frozen)}</b><small>заморозка</small>`;
  const deal = /^deal:(\d+)$/.exec(r.ref || "");
  return `<${deal ? `button data-go="deal/${deal[1]}"` : "div"} class="row"><span class="ic ${tone}">${ic(icon)}</span>
    <span class="mid"><b>${esc(r.title)}</b><small>${when(r.at)}${r.note ? " · " + esc(r.note) : ""}</small></span><span class="end">${amount}</span></${deal ? "button" : "div"}>`;
}

async function historyScreen() {
  const data = await api("history");
  view(`${head("История операций")}
    <div class="list" id="hl" style="margin-top:14px">${data.items.length ? data.items.map(historyRow).join("") : empty("clock", "Операций пока нет")}</div>
    ${data.items.length >= 40 ? `<button class="btn ghost" id="more">Показать ещё</button>` : ""}`);
  const more = $app.querySelector("#more");
  if (more) more.onclick = async () => {
    const next = await api(`history?before=${data.items[data.items.length - 1].id}`);
    data.items.push(...next.items);
    $app.querySelector("#hl").insertAdjacentHTML("beforeend", next.items.map(historyRow).join(""));
    if (next.items.length < 40) more.remove();
  };
}

function cardRow(c) {
  return `<div class="row"><button style="display:flex;align-items:center;gap:12px;flex:1;min-width:0;text-align:left" data-go="card/${c.id}">
    <span class="ic ${c.banned ? "red" : c.visible ? "green" : "gray"}">${ic("card")}</span>
    <span class="mid"><b>${esc(c.bank)} ${esc(c.mask)}</b><small>${c.banned ? "заблокирована администрацией" : c.visible ? `в потоке · ${rub(c.min)} – ${rub(c.max)}` : esc(c.why)}</small></span></button>
    ${c.banned ? "" : `<button class="sw ${c.active ? "on" : ""}" data-toggle="${c.id}" aria-label="В потоке"></button>`}</div>`;
}

async function cardsScreen() {
  const data = await api("cards");
  const visible = data.cards.filter((c) => c.visible).length;
  view(`${head("Карты", `Доход ${esc(data.pct)}% с каждой сделки · сделка до ${rub(data.cap_rub)}`)}
    <div class="card pad" style="margin-top:14px;display:flex;align-items:center;gap:12px">
      <span class="ic ${data.online ? "green" : "gray"}" style="width:44px;height:44px;border-radius:50%;display:grid;place-items:center;background:${data.online ? "#e3f8ef" : "#edf2f7"};color:${data.online ? "#0a9c6a" : "var(--slate)"};flex:none">${ic("power")}</span>
      <div style="flex:1"><b>${data.online ? "На смене" : "Не на смене"}</b><div class="hint" style="margin:2px 0 0">${data.online ? `Покупатели видят карт: ${visible} из ${data.cards.length}` : "Покупатели ваши карты не видят"}${data.online && data.auto_off ? ` · смена закончится сама через ${data.auto_off} мин без действий` : ""}</div></div>
      <button class="sw ${data.online ? "on" : ""}" id="shift" aria-label="Смена"></button>
    </div>
    <div class="sec"><h2>Карты в потоке</h2><a href="#/stats">Статистика</a></div>
    <div class="list">${data.cards.length ? data.cards.map(cardRow).join("") : empty("card", "Карт пока нет — добавьте карту или СБП, и покупатели увидят её")}</div>
    <button class="btn" data-go="card/new">${ic("plus")}Добавить карту</button>`);
  $app.querySelector("#shift").onclick = async () => {
    try { await api("shift", { body: { online: !data.online } }); haptic("tap"); cardsScreen(); } catch (e) { toast(e.message, true); }
  };
  $app.querySelectorAll("[data-toggle]").forEach((b) => (b.onclick = async () => {
    const c = data.cards.find((x) => String(x.id) === b.dataset.toggle);
    try { await api(`cards/${c.id}`, { body: { active: !c.active } }); haptic("tap"); cardsScreen(); } catch (e) { toast(e.message, true); }
  }));
}

async function cardScreen(id) {
  const data = await api("cards");
  const c = data.cards.find((x) => String(x.id) === String(id));
  if (!c) { go("cards", true); return; }
  const spaced = c.type === "card" ? c.number.replace(/(\d{4})(?=\d)/g, "$1 ") : c.number;
  view(`${head(`${c.bank} ${c.mask}`)}
    <div class="bank ${c.visible ? "" : "off"}" style="margin-top:14px">
      <div class="t"><span>${esc(c.bank)}</span><span>${c.type === "sbp" ? "СБП" : "карта"}</span></div>
      <div class="chipcard"></div>
      <div class="n">${esc(spaced)}</div>
      <div class="h"><span>${esc(c.holder)}</span><span>${c.visible ? "в потоке" : "не видна"}</span></div>
    </div>
    <div class="card pad" style="margin-top:12px">
      <div class="kv" style="padding-top:0;align-items:center"><span><b style="color:var(--ink)">В потоке</b><br><small>${c.visible ? "покупатели видят карту" : esc(c.why)}</small></span>
        ${c.banned ? `<span class="chip red">заблокирована</span>` : `<button class="sw ${c.active ? "on" : ""}" id="active" aria-label="В потоке"></button>`}</div>
      ${c.busy_deal ? `<div class="kv"><span>Идёт сделка</span><b><a href="#/deal/${c.busy_deal}">#${c.busy_deal}</a></b></div>` : ""}
      <div class="kv"><span>Принято сегодня</span><b>${rub(c.used_today)}${c.daily ? ` из ${rub(c.daily)}` : ""}</b></div>
      <div class="kv"><span>Всего сделок</span><b>${c.deals} · ${rub(c.turnover)}</b></div>
    </div>
    ${c.banned ? "" : `<div class="sec"><h2>Лимиты</h2></div>
    <div class="card pad">
      <label class="field" style="margin-top:0"><span>Минимум одной сделки</span><div class="inp"><input id="min" inputmode="decimal" value="${esc(c.min)}"><span class="unit">₽</span></div></label>
      <label class="field"><span>Максимум одной сделки</span><div class="inp"><input id="max" inputmode="decimal" value="${esc(c.max)}"><span class="unit">₽</span></div></label>
      <label class="field"><span>Лимит банка на входящие в день (0 — без лимита)</span><div class="inp"><input id="daily" inputmode="decimal" value="${esc(c.daily || 0)}"><span class="unit">₽</span></div></label>
      <button class="btn" id="save">${ic("check")}Сохранить</button>
    </div>
    ${data.cards.filter((x) => x.active).length > 1 || !c.active ? `<button class="btn ghost" id="solo">${ic("star")}Работать только с этой картой</button>` : ""}
    <p class="hint">Банк, номер и получателя меняйте в боте: «USDT ⇄ RUB» → карта. Открытые сделки изменения не затрагивают.</p>`}`);
  const act = $app.querySelector("#active");
  if (act) act.onclick = async () => {
    try { await api(`cards/${c.id}`, { body: { active: !c.active } }); haptic("tap"); cardScreen(id); } catch (e) { toast(e.message, true); }
  };
  const solo = $app.querySelector("#solo");
  if (solo) solo.onclick = async () => {
    try { await api(`cards/${c.id}`, { body: { solo: true } }); toast("В потоке только эта карта"); cardScreen(id); } catch (e) { toast(e.message, true); }
  };
  const save = $app.querySelector("#save");
  if (save) save.onclick = async () => {
    const val = (k) => $app.querySelector("#" + k).value.replace(/\s/g, "").replace(",", ".");
    try {
      await api(`cards/${c.id}`, { body: { min: val("min"), max: val("max"), daily: val("daily") } });
      toast("Лимиты сохранены"); cardScreen(id);
    } catch (e) { toast(e.message, true); }
  };
}

const BANKS = ["Сбербанк", "Т-Банк", "Альфа-Банк", "ВТБ", "Райффайзен", "Озон Банк"];

async function cardNew() {
  view(`${head("Новая карта", "Карта или СБП — покупатели увидят её сразу после сохранения")}
    <label class="field"><span>Номер карты или телефон СБП</span><div class="inp"><input id="num" class="mono" inputmode="numeric" autocomplete="off" placeholder="2200 7001 2345 6781 или +7 900…"></div></label>
    <label class="field"><span>Банк</span><div class="inp"><input id="bank" autocomplete="off" placeholder="Сбербанк"></div></label>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${BANKS.map((b) => `<button class="chip" data-bank="${esc(b)}">${esc(b)}</button>`).join("")}</div>
    <label class="field"><span>Получатель — как его видит отправитель</span><div class="inp"><input id="holder" autocomplete="off" placeholder="Иван Иванович И."></div></label>
    <div class="btns"><label class="field"><span>Минимум, ₽</span><div class="inp"><input id="min" inputmode="decimal" placeholder="1 000"></div></label>
      <label class="field"><span>Максимум, ₽</span><div class="inp"><input id="max" inputmode="decimal" placeholder="50 000"></div></label></div>
    <p class="hint">${ic("info").replace("<svg", '<svg style="width:14px;height:14px;vertical-align:-2px"')} Ошибка в цифрах — деньги покупателя уйдут чужому человеку. Проверьте номер.</p>
    <button class="btn" id="save">${ic("check")}Сохранить и включить</button>`);
  $app.querySelectorAll("[data-bank]").forEach((b) => (b.onclick = () => { $app.querySelector("#bank").value = b.dataset.bank; }));
  $app.querySelector("#save").onclick = async () => {
    const v = (k) => $app.querySelector("#" + k).value.trim();
    try {
      const r = await api("cards", { body: { number: v("num"), bank: v("bank"), holder: v("holder"), min: v("min").replace(/\s/g, ""), max: v("max").replace(/\s/g, "") } });
      toast(r.notice, /не в потоке/.test(r.notice)); go("cards", true);
    } catch (e) { toast(e.message, true); }
  };
}

const ROLE_CHIPS = (r) => [
  r.admin && ["Администратор", "red"], r.operator && ["Оператор", "blue"], r.merchant === "approved" && ["Ордерный мерчант", "blue"],
  r.seller && ["Продавец", "green"], r.team && [r.team.leader ? `Тимлид · ${r.team.name}` : `Команда · ${r.team.name}`, "amber"],
].filter(Boolean);

async function profile() {
  const [m, st] = await Promise.all([me(true), api("stats")]);
  const u = tgUser();
  const chips = ROLE_CHIPS(m.roles);
  view(`<div style="text-align:center;padding:18px 0 4px">${avatar("xl").replace('class="ava xl"', 'class="ava xl" style="margin:0 auto"')}
      <h1 style="font:700 21px Unbounded,sans-serif;margin:14px 0 2px">${esc([u.first_name, u.last_name].filter(Boolean).join(" ") || m.user.name)}</h1>
      <div class="hint" style="margin:0">${m.user.username ? "@" + esc(m.user.username) + " · " : ""}<button data-copy="${m.user.id}" data-what="ID" style="color:var(--slate);font-weight:700">ID ${m.user.id}</button></div>
      ${chips.length ? `<div class="chips" style="display:flex;justify-content:center;flex-wrap:wrap;gap:6px;margin-top:12px">${chips.map(([t, tone]) => `<span class="chip ${tone}">${esc(t)}</span>`).join("")}</div>` : ""}
    </div>
    <div class="tiles" style="margin-top:16px">
      <div class="tile"><small>Куплено, USDT</small><b>${usdt(st.buyer.usdt)}</b><em>${st.buyer.n} сделок · ${rub(st.buyer.rub)}</em></div>
      <div class="tile"><small>Продано</small><b>${rub(st.seller[3].rub)}</b><em>${st.seller[3].n} сделок${n(st.seller[3].income) ? " · +" + usdt(st.seller[3].income) + " USDT" : ""}</em></div>
    </div>
    <div class="sec"><h2>Аккаунт</h2></div>
    <div class="list">
      <button class="row" data-go="stats"><span class="ic">${ic("chart")}</span><span class="mid"><b>Статистика и аналитика</b><small>Оборот, доход по дням, успешность</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>
      ${m.roles.seller || m.roles.merchant ? `<button class="row" data-go="cards"><span class="ic">${ic("card")}</span><span class="mid"><b>Карты и смена</b><small>В потоке, лимиты</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
      ${m.roles.operator ? `<button class="row" data-go="operator"><span class="ic">${ic("shield")}</span><span class="mid"><b>Кабинет оператора</b><small>Ордера в работе и свободные</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
      <button class="row" data-go="history"><span class="ic">${ic("clock")}</span><span class="mid"><b>История операций</b><small>Пополнения, выводы, сделки</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>
      <div class="row"><span class="ic">${ic("bell")}</span><span class="mid"><b>Уведомления без звука</b><small>О сделках — тихо</small></span><button class="sw ${m.user.quiet ? "on" : ""}" id="quiet" aria-label="Без звука"></button></div>
    </div>
    <div class="sec"><h2>Помощь</h2></div>
    <div class="list">
      ${m.links.manager ? `<button class="row" data-open="${esc(m.links.manager)}"><span class="ic">${ic("headset")}</span><span class="mid"><b>Менеджер</b><small>${m.links.manager_nick ? "@" + esc(m.links.manager_nick) + " · " : ""}вопросы по сделкам и условиям</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
      <button class="row" data-go="guides"><span class="ic">${ic("book")}</span><span class="mid"><b>Инструкции</b><small>Как купить, продать, вывести</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>
      ${m.links.chat ? `<button class="row" id="chat"><span class="ic">${ic("people")}</span><span class="mid"><b>Чат Strait Pay</b><small>Курс, новости, заявки</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
      ${m.links.channel ? `<button class="row" data-open="${esc(m.links.channel)}"><span class="ic">${ic("bell")}</span><span class="mid"><b>Инфо-канал</b><small>Правила и новости</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
      ${m.links.bot ? `<button class="row" data-open="${esc(m.links.bot)}"><span class="ic">${ic("bot")}</span><span class="mid"><b>Открыть бота</b><small>Споры, анкеты, админка</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>` : ""}
    </div>
    <p class="hint" style="text-align:center;margin-top:16px">В Strait Pay с ${when(m.user.since).replace(/, \d\d:\d\d$/, "")}</p>
    ${brand()}`);
  $app.querySelector("#quiet").onclick = async (e) => {
    try { const r = await api("settings", { body: { quiet: !m.user.quiet } }); m.user.quiet = r.quiet; e.currentTarget.classList.toggle("on", r.quiet); haptic("tap"); } catch (err) { toast(err.message, true); }
  };
  const chat = $app.querySelector("#chat");
  if (chat) chat.onclick = async () => {
    try { const r = await api("chat-invite", { method: "POST" }); openUrl(r.chat); } catch (e) { toast(e.message, true); }
  };
  setBadge(m.counts.action);
}

async function statsScreen(period) {
  period = period || "week";
  const st = await api("stats");
  const p = st.seller.find((x) => x.key === period);
  const max = Math.max(...st.days.map((d) => n(d.income)), 0.000001);
  view(`${head("Статистика", "Продажи на ваших картах и ордерах")}
    <div class="seg">${[["today", "Сегодня"], ["week", "7 дней"], ["month", "30 дней"], ["all", "Всё"]].map(([k, t]) => `<button data-p="${k}" class="${k === period ? "on" : ""}">${t}</button>`).join("")}</div>
    <div class="tiles">
      <div class="tile"><small>Сделок</small><b>${p.n}</b></div>
      <div class="tile"><small>Доход</small><b>${usdt(p.income)}</b><em>USDT</em></div>
      <div class="tile"><small>Оборот</small><b>${rub(p.rub)}</b></div>
      <div class="tile"><small>Средний чек</small><b>${rub(p.avg)}</b></div>
      <div class="tile"><small>Успешных</small><b>${p.success == null ? "—" : p.success + "%"}</b></div>
      <div class="tile"><small>Подтверждаете за</small><b>${p.confirm_min == null ? "—" : p.confirm_min + " мин"}</b>${p.disputes ? `<em style="color:var(--coral)">споров: ${p.disputes}</em>` : ""}</div>
    </div>
    <div class="sec"><h2>Доход по дням</h2><span class="hint" style="margin:0">14 дней, USDT</span></div>
    <div class="card pad"><div class="bars">${st.days.map((d) => `<div title="${esc(d.day)}: ${d.n} сд., +${usdt(d.income)} USDT"><i class="${n(d.income) ? "" : "zero"}" style="height:${Math.round((n(d.income) / max) * 100)}%"></i><span>${new Date(d.day).getDate()}</span></div>`).join("")}</div></div>
    <div class="sec"><h2>Покупки</h2></div>
    <div class="card pad"><div class="kv" style="padding-top:0"><span>Сделок</span><b>${st.buyer.n}</b></div><div class="kv"><span>Переведено</span><b>${rub(st.buyer.rub)}</b></div>
      <div class="kv total"><span>Получено</span><b>${usdt(st.buyer.usdt)} USDT</b></div></div>
    <p class="hint">Успешные — завершённые из всех закрытых. Быстрое подтверждение — меньше споров и больше сделок.</p>`);
  $app.querySelectorAll("[data-p]").forEach((b) => (b.onclick = () => { haptic("tap"); statsScreen(b.dataset.p); }));
}

async function guidesScreen() {
  const data = await api("guides");
  view(`${head("Инструкции", "Путь от первой покупки до вывода — по шагам")}
    <button class="promo" style="margin-top:14px" data-go="guide/${data.guides[0] ? data.guides[0].slug : "start"}"><span style="flex:1"><b>С чего начать</b><span>Роли, главное меню, первая сделка</span></span><img src="/docs/static/flow.jpg" alt=""></button>
    <div class="list" style="margin-top:14px">${data.guides.map((g, i) => `<button class="row" data-go="guide/${esc(g.slug)}"><span class="ic" style="font:600 13px 'JetBrains Mono',monospace">${i + 1}</span>
      <span class="mid"><b>${esc(g.title)}</b><small>${esc(g.about)}</small></span>${ic("chev").replace("<svg", '<svg class="chev"')}</button>`).join("")}</div>
    ${S.me && S.me.links.manager ? `<button class="btn ghost" data-open="${esc(S.me.links.manager)}">${ic("headset")}Спросить менеджера</button>` : ""}`);
}

async function guideScreen(slug) {
  const g = await api(`guides/${slug}`);
  view(`<article class="article" style="margin-top:8px">${g.html}</article>`);
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

/* ---------- routing ---------- */

const ROUTES = [
  [/^$/, home], [/^deals$/, () => dealsScreen()], [/^deal\/(\d+)$/, dealScreen], [/^deal\/(\d+)\/chat$/, chatScreen],
  [/^buy$/, buyScreen], [/^deposit$/, () => depositScreen()], [/^deposit\/(\d+)$/, (id) => depositView(id)],
  [/^withdraw$/, () => withdrawScreen()], [/^history$/, historyScreen], [/^cards$/, cardsScreen], [/^card\/new$/, cardNew],
  [/^card\/(\d+)$/, cardScreen], [/^profile$/, profile], [/^stats$/, () => statsScreen()], [/^guides$/, guidesScreen],
  [/^guide\/([a-z]+)$/, guideScreen], [/^operator$/, operatorScreen],
];
const TABS = [["", "Кошелёк", "wallet"], ["deals", "Сделки", "deals"], ["cards", "Карты", "card"], ["profile", "Профиль", "user"]];

function parent(p) {
  if (/^deal\/\d+\/chat$/.test(p)) return p.replace(/\/chat$/, "");
  if (/^deal\//.test(p)) return "deals";
  if (/^card\//.test(p)) return "cards";
  if (/^guide\//.test(p)) return "guides";
  if (p === "stats" || p === "guides" || p === "operator") return "profile";
  return "";
}

const path = () => (location.hash.startsWith("#/") ? decodeURIComponent(location.hash.slice(2)) : "");
function go(p, replace) {
  if (replace) { history.replaceState(null, "", "#/" + p); render(); }
  else if (path() === p) render();
  else location.hash = "#/" + p;
}

function setBadge(count) {
  const b = $nav.querySelector("[data-tab='deals'] .badge");
  if (b) { b.textContent = count; b.hidden = !count; }
}

function drawNav(p) {
  const root = TABS.some(([t]) => t === p);
  const tab = root ? p : ({ deals: "deals", cards: "cards", profile: "profile" })[parent(p)] || (/^deal/.test(p) ? "deals" : /^card/.test(p) ? "cards" : /^(stats|guide)/.test(p) ? "profile" : "");
  $nav.innerHTML = TABS.map(([t, label, icon]) => `<a href="#/${t}" data-tab="${t}" class="${t === tab ? "on" : ""}">${ic(icon)}${label}${t === "deals" ? `<span class="badge" hidden></span>` : ""}</a>`).join("");
  $nav.hidden = /\/chat$/.test(p);
  if (tg && tg.BackButton) { if (root) tg.BackButton.hide(); else tg.BackButton.show(); }
  if (S.me) setBadge(S.me.counts.action);
}

async function render() {
  clearTimers();
  const p = path();
  drawNav(p);
  const route = ROUTES.find(([re]) => re.test(p));
  if (!route) { go("", true); return; }
  if (!$app.firstElementChild || !S.keepScroll) $app.innerHTML = skeleton();
  try { await route[1](...(p.match(route[0]).slice(1))); }
  catch (e) { failView(e); }
}

/* ---------- start ---------- */

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-go],[data-copy],[data-open]");
  if (!t) return;
  if (t.dataset.copy !== undefined) { e.preventDefault(); copy(t.dataset.copy, t.dataset.what); return; }
  if (t.dataset.open) { e.preventDefault(); openUrl(t.dataset.open); return; }
  e.preventDefault();
  haptic("tap");
  go(t.dataset.go);
});
$nav.addEventListener("click", () => haptic("tap"));
window.addEventListener("hashchange", render);

(function start() {
  if (!tg || !tg.initData) { gate("Откройте Strait Pay в Telegram — кнопкой «Приложение» в боте."); return; }
  tg.ready();
  tg.expand();
  try {
    tg.setHeaderColor("#f2f7fd");
    tg.setBackgroundColor("#f2f7fd");
    if (tg.isVersionAtLeast("7.10")) tg.setBottomBarColor("#f2f7fd");
    if (tg.isVersionAtLeast("7.7")) tg.disableVerticalSwipes();
  } catch (e) { /* older clients */ }
  if (tg.BackButton) tg.BackButton.onClick(() => go(parent(path()), true));
  // the start page: ?p=deal/5 from the bot's buttons, startapp=deal-5 from t.me links
  const fromQuery = new URLSearchParams(location.search).get("p");
  const fromLink = (tg.initDataUnsafe && tg.initDataUnsafe.start_param || "").replace(/-/g, "/");
  const first = [fromQuery, fromLink].find((x) => x && ROUTES.some(([re]) => re.test(x))) || "";
  history.replaceState(null, "", location.pathname + "#/" + first);
  render();
})();
