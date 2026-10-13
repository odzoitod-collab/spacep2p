/* Strait Pay mini app. No build step: one file, the Telegram SDK and our JSON API (/app/api, bot/api/webapp*.py).
   Sign-in is Telegram's initData sent with every request; the bot checks it and applies its own rules.
   Every screen renders with a token: a slow answer for a screen the user already left is thrown away, and every
   request has a timeout — a screen never hangs on its skeleton. Errors of the page itself go to the bot's log.
   Look: light / dark theme (auto from Telegram), a bottom bar on the phone, a sidebar from 960px; the deal chat and
   files open full screen with a clear close button. */
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const $navIn = $nav.querySelector(".in") || $nav;
const $toast = document.getElementById("toast");
const $side = document.getElementById("side");
const S = { me: null, seq: 0, timers: [], drafts: {}, stack: [], ava: null, sheet: null, keep: false, chatSeen: {}, tab: {}, reported: 0, viewer: null };
const store = {
  get(k, d) { try { const v = localStorage.getItem("sp:" + k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("sp:" + k, v); } catch (e) { /* storage off */ } },
};

/* ---------- theme: auto (Telegram / system), light or dark ---------- */

function themeMode() { return store.get("theme", "auto"); }
function applyTheme() {
  const mode = themeMode();
  const sys = (tg && tg.colorScheme) || (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const dark = mode === "dark" || (mode === "auto" && sys === "dark");
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const paper = dark ? "#0a1120" : "#f1f5fb";
  try {
    if (tg && tg.setHeaderColor) { tg.setHeaderColor(paper); tg.setBackgroundColor(paper); }
    if (tg && tg.isVersionAtLeast && tg.isVersionAtLeast("7.10")) tg.setBottomBarColor(dark ? "#0e1626" : "#ffffff");
  } catch (e) { /* older clients */ }
}
function setTheme(mode) { store.set("theme", mode); applyTheme(); if (typeof MB !== "undefined") MB.show(); }

/* ---------- the visible height: the chat composer stays above the keyboard ---------- */

function syncHeight() {
  const h = (window.visualViewport && window.visualViewport.height) || (tg && tg.viewportHeight) || window.innerHeight;
  document.documentElement.style.setProperty("--app-h", Math.round(h) + "px");
}

/* ---------- full screen: Telegram 8.0+, a browser's own otherwise; always a clear way out ---------- */

const fs = {
  can() { return !!((tg && tg.requestFullscreen && tg.isVersionAtLeast && tg.isVersionAtLeast("8.0")) || document.documentElement.requestFullscreen); },
  on() { return !!((tg && tg.isFullscreen) || document.fullscreenElement); },
  toggle() {
    try {
      if (tg && tg.requestFullscreen && tg.isVersionAtLeast("8.0")) { if (tg.isFullscreen) tg.exitFullscreen(); else tg.requestFullscreen(); return; }
    } catch (e) { /* fall back to the browser */ }
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => toast("Полный экран недоступен", true));
  },
  sync() {
    let pill = document.getElementById("fsx");
    if (fs.on() && !pill) {
      pill = document.createElement("button");
      pill.id = "fsx"; pill.className = "fs-exit";
      pill.innerHTML = `${ic("x")}Свернуть полный экран`;
      pill.onclick = () => fs.toggle();
      document.body.appendChild(pill);
    } else if (!fs.on() && pill) pill.remove();
    document.querySelectorAll("[data-fs]").forEach((b) => { b.innerHTML = ic(fs.on() ? "shrink" : "expand"); b.setAttribute("aria-label", fs.on() ? "Свернуть" : "Во весь экран"); });
    syncHeight();
  },
};
/* ---------- Telegram's own bottom button: the screen's main action under the thumb ---------- */

const MB = {
  fn: null, cfg: null,
  ok() { return !!(tg && tg.initData && tg.MainButton && tg.isVersionAtLeast && tg.isVersionAtLeast("6.1") && tg.platform !== "unknown"); },
  set(text, fn, opts = {}) {
    if (!MB.ok()) return false;
    MB.cfg = { text, active: opts.active !== false, red: !!opts.red, shine: !!opts.shine };
    MB.fn = fn;
    MB.show();
    return true;
  },
  active(on, text) { if (!MB.cfg) return; MB.cfg.active = on; if (text) MB.cfg.text = text; MB.show(); },
  show() {
    if (!MB.cfg || !MB.ok()) return;
    try {
      if (S.sheet || S.viewer) { tg.MainButton.hide(); return; }
      const dark = document.documentElement.dataset.theme === "dark";
      const color = MB.cfg.red ? (dark ? "#ff6b62" : "#d83b31") : (dark ? "#4b90ff" : "#1b6fe8");
      tg.MainButton.setParams({ text: MB.cfg.text.slice(0, 60), color: MB.cfg.active ? color : (dark ? "#24324c" : "#c7d3e6"),
        text_color: "#ffffff", is_active: MB.cfg.active, is_visible: true, has_shine_effect: MB.cfg.shine && MB.cfg.active });
    } catch (e) { /* an old client */ }
  },
  clear() { MB.cfg = null; MB.fn = null; try { if (MB.ok()) { tg.MainButton.hideProgress(); tg.MainButton.hide(); } } catch (e) { /* old client */ } },
  async click() {
    if (!MB.fn || !MB.cfg || !MB.cfg.active) return;
    haptic("tap");
    try { await MB.fn(); } catch (e) { report(e, "main-button"); }
  },
  /* the page's own button moves to Telegram's: hidden here, clicked from there */
  take(btn, opts = {}) {
    if (!btn || !MB.ok()) return false;
    const label = btn.textContent.trim();
    const input = btn.tagName === "LABEL" ? btn.querySelector("input[type=file]") : null;
    if (input && tg.platform === "ios") return false;  // iOS opens a file picker only from a tap inside the page
    MB.set(label, () => (input ? input.click() : btn.click()), opts);
    btn.hidden = true;
    return true;
  },
};

const fsBtn = () => (fs.can() ? `<button class="ibtn" data-fs aria-label="Во весь экран">${ic(fs.on() ? "shrink" : "expand")}</button>` : "");

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
function dayLabel(iso) {
  const d = new Date(iso), now = new Date(), y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "Сегодня";
  if (d.toDateString() === y.toDateString()) return "Вчера";
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? " " + d.getFullYear() : ""}`;
}
const hm = (iso) => new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
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

function every(ms, fn) { S.timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); }  // asleep in the background
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
  expand: '<path d="M4.5 9V4.5H9M15 4.5h4.5V9M19.5 15v4.5H15M9 19.5H4.5V15"/>',
  shrink: '<path d="M9 4.5V9H4.5M19.5 9H15V4.5M15 19.5V15h4.5M4.5 15H9v4.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  moon: '<path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10Z"/>',
  auto: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17A8.5 8.5 0 0 0 12 3.5Z" fill="currentColor"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 8.5-8.5M16 7l2.5 2.5M14 9l2 2"/>',
  ban: '<circle cx="12" cy="12" r="8.5"/><path d="m6 6 12 12"/>',
  coin: '<ellipse cx="12" cy="7" rx="7" ry="3"/><path d="M5 7v5c0 1.7 3.1 3 7 3s7-1.3 7-3V7M5 12v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 19.5h14"/>',
  zoomin: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4M8.5 11h5M11 8.5v5"/>',
  zoomout: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4M8.5 11h5"/>',
  home2: '<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-8.5Z"/>',
  back: '<path d="M15 5.5 8.5 12l6.5 6.5"/>',
  orders: '<path d="M6 4.5h12a1 1 0 0 1 1 1V20l-3-2-3 2-3-2-3 2-2-1.3V5.5a1 1 0 0 1 1-1Z"/><path d="M9 9h6M9 12.5h6"/>',
};
/* the logo: the strait between two shores */
const LOGO = '<svg viewBox="0 0 36 36" aria-hidden="true"><rect width="36" height="36" rx="11" fill="var(--accent)"/><path d="M7 14c3.5-3 7.5-3 11 0s7.5 3 11 0M7 22c3.5-3 7.5-3 11 0s7.5 3 11 0" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>';
/* the empty states: a small animated picture instead of a gray icon */
const ART = {
  deals: '<path class="ring" d="M44 6a26 26 0 1 1-.1 0" fill="none" stroke="currentColor" stroke-opacity=".18" stroke-width="2" stroke-dasharray="4 6"/><g class="float"><rect x="26" y="18" width="36" height="28" rx="7" fill="var(--accent-soft)" stroke="currentColor" stroke-width="2"/><path d="M33 28h22M33 35h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></g>',
  bell: '<path class="ring" d="M44 6a26 26 0 1 1-.1 0" fill="none" stroke="currentColor" stroke-opacity=".18" stroke-width="2" stroke-dasharray="4 6"/><g class="float"><path d="M34 40V31a10 10 0 0 1 20 0v9l3 4H31l3-4Z" fill="var(--accent-soft)" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M41 48a3.5 3.5 0 0 0 6 0" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></g>',
  ok: '<path class="ring" d="M44 6a26 26 0 1 1-.1 0" fill="none" stroke="currentColor" stroke-opacity=".18" stroke-width="2" stroke-dasharray="4 6"/><g class="float"><circle cx="44" cy="32" r="15" fill="var(--accent-soft)" stroke="currentColor" stroke-width="2"/><path d="m37 32 5 5 9-10" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></g>',
};
const art = (name) => `<svg class="art" viewBox="0 0 88 64" aria-hidden="true">${ART[name] || ART.deals}</svg>`;
const WAVES = '<svg class="waves" viewBox="0 0 400 70" preserveAspectRatio="none" aria-hidden="true"><path d="M0 40c50-22 100-22 150 0s100 22 150 0 70-18 100-6v36H0Z" fill="rgba(255,255,255,.22)"/><path d="M0 50c60-18 110-18 160 0s110 18 160 0 60-12 80-6v26H0Z" fill="rgba(255,255,255,.18)"/></svg>';
const LOADER = '<svg class="loader" viewBox="0 0 50 50" aria-hidden="true"><circle cx="25" cy="25" r="20" fill="none" stroke="currentColor" stroke-width="4"/></svg>';
const ic = (name, cls) => `<svg${cls ? ` class="${cls}"` : ""} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;
const chev = () => ic("chev", "chev");

/* ---------- pieces of screens ---------- */

const ROLE = { buyer: ["Покупка", "down", "sea"], seller: ["Продажа", "up", "red"], operator: ["Ордер", "shield", "blue"], admin: ["Сделка", "deals", ""], offer: ["Заявка", "bell", "amber"] };
const TONE = { searching: "blue", assigned: "blue", checking: "blue", waiting_payment: "amber", paid: "accent", dispute: "red", completed: "green", cancelled: "", expired: "", void: "" };
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
    <span class="end"><b>${rub(d.amount_rub)}</b><small data-usdt="${usdt(d.debit || d.usdt)} USDT">${unreadOf(d) ? `<span class="unread">${ic("chat")}${unreadOf(d)}</span>` : `${usdt(d.debit || d.usdt)} USDT`}</small></span>
    ${p != null && p < 100 ? `<span class="strait"><i style="width:${p}%"></i></span>` : ""}
  </button>`;
}

/* unread chat messages: the server's count, refreshed in the background */
const unreadOf = (d) => (S.unreadBy && S.unreadBy[d.id] != null ? S.unreadBy[d.id] : d.unread || 0);

async function refreshUnread() {
  if (!tg || !tg.initData || document.hidden) return;
  try {
    const r = await api("chat/unread");
    const before = S.unreadTotal;
    S.unreadBy = Object.fromEntries(Object.entries(r.deals).map(([k, v]) => [Number(k), v]));
    S.unreadTotal = r.total;
    if (S.me) S.me.counts.unread = r.total;
    drawBadges();
    $app.querySelectorAll('.row[data-go^="deal/"]').forEach((row) => {  // rows already on the screen
      const k = S.unreadBy[Number(row.dataset.go.split("/")[1])] || 0, slot = row.querySelector(".end small");
      const chip = slot && slot.querySelector(".unread");
      if (!slot || (!k && !chip)) return;
      if (k) slot.innerHTML = `<span class="unread">${ic("chat")}${k}</span>`;
      else { const d = slot.dataset.usdt; if (d) slot.textContent = d; }
    });
    if (before != null && r.total > before) haptic("tap");
  } catch (e) { /* the next round */ }
}

function drawBadges() {
  const k = counts();
  [["deals", k.deals], ["orders", k.orders], ["admin", k.admin]].forEach(([t, v]) => {
    document.querySelectorAll(`[data-tab='${t}'] .badge, [data-side='${t}'] .badge`).forEach((b) => { b.textContent = v; b.hidden = !v; });
  });
}

function empty(icon, text, action) {
  const pic = { bell: "bell", check: "ok", ok: "ok" }[icon] || "deals";
  return `<div class="empty">${art(pic)}${esc(text)}${action || ""}</div>`;
}
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

function topBar(p) {
  if (p === "") return "";
  const root = isRoot(p);
  return `<div class="top">${root ? "" : `<button class="ibtn" data-back aria-label="Назад">${ic("back")}</button><button class="ibtn" data-go="" aria-label="Главная">${ic("home2")}</button>`}<span class="sp"></span>${fsBtn()}</div>`;
}

function view(c, html, opts = {}) {
  if (!c.alive()) return false;
  $app.innerHTML = `<div class="screen">${topBar(path())}${html}</div>`;
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
  $side.innerHTML = "";
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
  sh.innerHTML = `<div class="in"><div class="grip"></div><button class="x" data-x aria-label="Закрыть">${ic("x")}</button>${html}</div>`;
  sh.querySelector("[data-x]").onclick = () => closeSheet();
  veil.onclick = () => closeSheet();
  document.body.append(veil, sh);
  S.sheet = { veil, sh };
  syncBack();
  MB.show();
  return sh;
}

function closeSheet(animate = true) {
  const cur = S.sheet;
  if (!cur) return;
  S.sheet = null;
  if (!animate) { cur.veil.remove(); cur.sh.remove(); syncBack(); MB.show(); return; }
  cur.veil.classList.add("closing"); cur.sh.classList.add("closing");
  setTimeout(() => { cur.veil.remove(); cur.sh.remove(); }, 200);
  syncBack();
  MB.show();
}

/* ---------- screens: home ---------- */

async function home(c) {
  const [m, list, hist] = await Promise.all([me(true), api("deals?scope=active"), api("history").catch(() => ({ items: [] }))]);
  const u = tgUser(), b = m.balance, w = m.work || {}, r = m.roles;
  const locked = n(b.withdrawable) < n(b.available);
  const action = list.deals.filter((d) => d.action);
  const rest = list.deals.filter((d) => !d.action).slice(0, 6);
  const work = [
    r.admin && w.disputes ? `<button class="row" data-go="admin"><span class="ic red">${ic("scale")}</span><span class="mid"><b>Споры ждут решения</b><small>Администрирование</small></span><span class="end"><b>${w.disputes}</b></span></button>` : "",
    r.operator && w.free_orders ? `<button class="row" data-go="operator"><span class="ic blue">${ic("shield")}</span><span class="mid"><b>Bybit-ордера ждут оператора</b><small>Кабинет оператора</small></span><span class="end"><b>${w.free_orders}</b></span></button>` : "",
    r.merchant === "approved" && w.offers && w.line ? `<button class="row" data-go="orders"><span class="ic amber">${ic("bell")}</span><span class="mid"><b>Свободные заявки</b><small>Нажмите, чтобы взять</small></span><span class="end"><b>${w.offers}</b></span></button>` : "",
  ].join("");
  const line = r.merchant === "approved" ? `<div class="line"><span class="dot ${w.line ? "on" : ""}"></span><div class="mid"><b>${w.line ? "На линии" : "Не на линии"}</b><small>${w.line ? "Заявки покупателей приходят вам" : "Заявки не приходят — включите, когда готовы"}</small></div><button class="sw green ${w.line ? "on" : ""}" id="line" aria-label="На линии"></button></div>`
    : r.seller ? `<div class="line"><span class="dot ${m.user.online ? "on" : ""}"></span><div class="mid"><b>${m.user.online ? "На смене" : "Не на смене"}</b><small>${m.user.online ? `Карт в потоке: ${w.cards_on || 0}` : "Покупатели не видят ваши карты"}</small></div><button class="sw green ${m.user.online ? "on" : ""}" id="shift" aria-label="Смена"></button></div>` : "";
  const left = `
    <section class="balance">${WAVES}
      <div class="lbl"><span>Баланс</span><span class="num">1 USDT = ${NF.format(n(m.rate.rate))} ₽</span></div>
      <div class="sum">${usdt(b.available)}<small>USDT</small></div>
      <div class="sub">≈ ${rub(n(b.available) * n(m.rate.rate))}${m.rate.own ? " · ваши условия" : ""}</div>
      ${n(b.frozen) || locked || b.debt || n(b.team) ? `<div class="facts">
        ${n(b.frozen) ? tag(`В сделках ${usdt(b.frozen)}`, "", "lock") : ""}
        ${locked ? tag(`Можно вывести ${usdt(b.withdrawable)}`) : ""}
        ${b.debt ? tag(`Долг оператора ${usdt(b.debt)}`) : ""}
        ${n(b.team) ? tag(`Командный ${usdt(b.team)}`) : ""}</div>` : ""}
    </section>
    <div class="quick">
      <button data-go="deposit"><span>${ic("down")}</span>Пополнить</button>
      <button data-go="withdraw"><span>${ic("up")}</span>Вывести</button>
      <button data-go="buy"><span>${ic("swap")}</span>Купить</button>
      <button data-go="${r.merchant === "approved" ? "orders" : "sell"}"><span>${ic(r.merchant === "approved" ? "orders" : "card")}</span>${r.merchant === "approved" ? "Заявки" : "Продать"}</button>
    </div>
    ${line}
    ${work ? `${sec("Работа")}<div class="list">${work}</div>` : ""}`;
  const right = `
    ${action.length ? `${sec("Нужно ваше действие")}<div class="list">${action.map((d) => dealRow(d, true)).join("")}</div>` : ""}
    ${sec("Активные сделки", list.deals.length ? `<button data-go="deals">Все</button>` : "")}
    <div class="list">${rest.length ? rest.map(dealRow).join("") : action.length ? empty("check", "Остальные сделки закрыты") :
      empty("swap", "Открытых сделок нет", `<button class="btn sm" data-go="buy" style="margin:12px auto 0">${ic("swap")}Купить USDT</button>`)}</div>
    ${sec("История", hist.items.length ? `<button data-go="history">Вся история</button>` : "")}
    <div class="list">${hist.items.length ? hist.items.slice(0, 6).map(historyRow).join("") : empty("clock", "Операций пока нет")}</div>`;
  if (!view(c, `
    <div class="bar">
      <button class="who" data-go="profile">${avatar()}<div><b>${esc(u.first_name || m.user.name || "Профиль")}</b><small>${m.user.username ? "@" + esc(m.user.username) : "ID " + m.user.id}</small></div></button>
      ${m.links.manager ? `<button class="ibtn" data-open="${esc(m.links.manager)}" aria-label="Менеджер">${ic("headset")}</button>` : ""}
      ${fsBtn()}
    </div>
    <div class="grid2"><div class="col">${left}</div><div class="col">${right}</div></div>
    <div class="foot">Strait Pay · P2P-обмен USDT ⇄ RUB</div>`)) return;
  setBadge(m.counts.action);
  drawNav(path());
  on("#line", (sw) => busy(sw, async () => { try { await api("merchant", { body: { online: !w.line } }); haptic("success"); toast(w.line ? "Вы не на линии — заявки не приходят" : "Вы на линии — заявки приходят"); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
  on("#shift", (sw) => busy(sw, async () => { try { await api("shift", { body: { online: !m.user.online } }); haptic("success"); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
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
    if (!v) { MB.clear(); out.innerHTML = `<p class="hint">Комиссия ${esc(m.rate.pct)}%. Реквизиты продавца появятся после создания сделки.</p>`; return; }
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
      MB.take(out.querySelector("#go"), { shine: true });
    } catch (e) { MB.clear(); if (c.alive()) out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
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
    <label class="field" style="margin-top:10px"><div class="inp">${ic("search").replace("<svg", '<svg style="width:18px;height:18px;color:var(--muted)"')}<input id="ds" inputmode="numeric" placeholder="Номер сделки или сумма" autocomplete="off"></div></label>
    <div class="list" id="dl" style="margin-top:10px">${shown(list.deals).length ? shown(list.deals).map(dealRow).join("") : empty("deals", scope === "active" ? "Открытых сделок нет" : "Завершённых сделок пока нет", scope === "active" ? `<button class="btn sm" data-go="buy" style="margin:12px auto 0">${ic("swap")}Купить USDT</button>` : "")}</div>
    ${list.deals.length >= 30 ? `<button class="btn line" id="more">Показать ещё</button>` : ""}`)) return;
  const ds = $app.querySelector("#ds");
  ds.oninput = () => {
    const q = ds.value.replace(/[\s#₽]/g, "");
    $app.querySelectorAll("#dl > .row").forEach((row) => {
      const id = (row.dataset.go || "").split("/")[1] || "", amount = row.querySelector(".end b") ? row.querySelector(".end b").textContent.replace(/\D/g, "") : "";
      row.hidden = !!q && !id.startsWith(q) && !amount.includes(q);
    });
  };
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
    <button class="btn soft" data-copy="${esc(`${r.bank} · ${r.number}${r.holder ? " · " + r.holder : ""} · ${exact} ₽`)}" data-what="Реквизиты">${ic("copy")}Скопировать всё одной строкой</button>
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
  if (has("receipt") || has("late_receipt")) out.push(`<label class="btn" style="cursor:pointer">${ic("clip")}${has("late_receipt") ? "Я перевёл — прикрепить чек" : "Прикрепить чек (PDF)"}<input type="file" id="file" accept="application/pdf" hidden></label>`);
  if (has("take")) out.push(`<button class="btn" data-a="take">${ic("check")}Взять заявку</button>`);
  if (has("accept")) out.push(`<button class="btn" data-a="accept">${ic("check")}Принять ордер</button>`);
  if (has("link")) out.push(`<button class="btn" data-a="link">${ic("link")}Отправить ссылку на ордер Bybit</button>`);
  if (has("give")) out.push(`<button class="btn" data-a="give">${ic("send")}Выдать реквизиты покупателю</button>`);
  if (has("confirm")) out.push(`<button class="btn" data-a="confirm">${ic("check")}${d.bybit ? "Оплата пришла — подтвердить" : "Деньги пришли — подтвердить"}</button>`);
  if (has("resolve")) out.push(`<button class="btn ink" data-a="resolve">${ic("scale")}Решение по сделке</button>`);
  const second = [];
  if (has("receipt_view")) second.push(`<button class="btn line" data-a="receipt">${ic("file")}Смотреть чек</button>`);
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
  const chat = d.actions.includes("chat");
  const [label] = ROLE[d.role] || ROLE.admin;
  const timer = d.expires_at && ["waiting_payment", "searching", "assigned", "checking"].includes(d.status);
  const what = d.status === "waiting_payment" ? (d.role === "buyer" ? "Оплатите за" : "Покупатель оплачивает") :
    d.status === "searching" ? "Поиск реквизитов" : d.status === "assigned" ? (d.role === "seller" ? "Ответьте за" : "Мерчант готовит реквизиты") : "Проверка ордера";
  const html = `
    <div class="deal-head"><h1>${label} #${d.id}${d.bybit ? " · Bybit" : ""}</h1>${tag(d.status_text, TONE[d.status])}</div>
    <div class="amount"><div class="rub">${rub(d.amount_rub)}</div><div class="to">${amountLine(d)}</div></div>
    ${straitBar(d)}
    ${d.status === "completed" ? `<div class="done-mark"><svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><circle class="c" cx="18" cy="18" r="16"/><path class="k" d="m11 18.5 5 5 9-10"/></svg>Сделка завершена</div>` : ""}
    ${timer ? `<div class="timer-line" id="tl"><span>${what}</span><b id="timer">${mmss(left(d.expires_at))}</b></div>` : ""}
    ${d.held && d.role !== "buyer" ? `<p class="hint">${ic("lock").replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} Срока нет: сделку ведёт и закрывает оператор.</p>` : ""}
    ${chat ? `<button class="chat-open" data-go="deal/${d.id}/chat">${ic("chat")}<span class="sp">Чат сделки<small id="chatline">Покупатель, продавец${d.bybit ? ", оператор" : ""} и администрация</small></span><span class="badge" id="unread" hidden></span>${chev()}</button>` : ""}
    <div id="pane">${dealPane(d)}</div>
    ${dealActions(d)}`;
  if (!view(c, html, { keepScroll: S.keep })) return;
  if (timer) every(1000, () => { const t = $app.querySelector("#timer"); if (!t) return; const sLeft = left(d.expires_at); t.textContent = mmss(sLeft); $app.querySelector("#tl").classList.toggle("low", sLeft < 180); });
  bindDeal(c, d, offer);
  MB.take($app.querySelector(".dock > .btn"), { shine: true });
  if (chat) chatUnread(c, d);
  if (!offer && ["searching", "assigned", "checking", "waiting_payment", "paid", "dispute"].includes(d.status)) {
    every(5000, async () => {
      try {
        const fresh = await api(`deals/${id}`);
        if (!c.alive() || JSON.stringify(fresh.deal) === S.dealSeen) return;
        if (fresh.deal.status !== d.status) { haptic("success"); toast(`Сделка #${id}: ${fresh.deal.status_text}`); }
        const typing = document.activeElement && /INPUT|TEXTAREA/.test(document.activeElement.tagName);
        if (!typing && !S.sheet && !S.viewer) { S.keep = true; render(); }
      } catch (e) { /* the next tick tries again */ }
    });
  }
}

function withDays(msgs) {
  let day = "";
  return msgs.map((m) => { const d = dayLabel(m.at); const sep = d !== day ? `<div class="day">${d}</div>` : ""; day = d; return sep + msgHtml(m); }).join("");
}

/* the unread counter of the deal's chat on the deal screen */
async function chatUnread(c, d) {
  let data;
  try { [data] = await Promise.all([api(`deals/${d.id}/chat`), refreshUnread()]); } catch (e) { return; }
  if (!c.alive()) return;
  const show = () => {
    const k = unreadOf(d);
    const badge = $app.querySelector("#unread"), line = $app.querySelector("#chatline");
    if (badge) { badge.textContent = k; badge.hidden = !k; }
    const lastMsg = data.messages[data.messages.length - 1];
    if (line && lastMsg) line.textContent = `${lastMsg.mine ? "Вы" : lastMsg.role}: ${lastMsg.text}`.slice(0, 80);
  };
  show();
  let last = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
  every(6000, async () => { try { const r = await api(`deals/${d.id}/chat?after=${last}`); if (r.messages.length) { data.messages.push(...r.messages); last = r.messages[r.messages.length - 1].id; await refreshUnread(); show(); } } catch (e) { /* retry */ } });
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
  if (d.files && d.files.length) parts.push(`${sec("Материалы")}<div class="list">${d.files.map((f) => `<button class="row" data-file="${f.n}" data-kind="${f.kind}"><span class="ic">${ic({ photo: "image", video: "video", document: "file", text: "chat" }[f.kind] || "file")}</span><span class="mid"><b>${esc(f.title)}</b><small>${f.kind === "text" ? esc(f.text) : "посмотреть"}</small></span>${chev()}</button>`).join("")}</div>`);
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

/* ---------- the viewer: a receipt or evidence full screen, PDFs drawn in the page (pdf.js) ---------- */

function closeViewer(animate = true) {
  const v = S.viewer;
  if (!v) return;
  S.viewer = null;
  v.urls.forEach((u) => URL.revokeObjectURL(u));
  if (animate) { v.el.style.opacity = "0"; v.el.style.transition = "opacity .15s"; setTimeout(() => v.el.remove(), 150); } else v.el.remove();
  syncBack();
  MB.show();
}

function openViewer(title, d, nKey) {
  closeViewer(false);
  const el = document.createElement("div");
  el.className = "viewer";
  el.innerHTML = `<div class="vbar"><b>${esc(title)}</b>
      <button class="vbtn" data-z="-1" aria-label="Уменьшить" hidden>${ic("zoomout")}</button><button class="vbtn" data-z="1" aria-label="Увеличить" hidden>${ic("zoomin")}</button>
      <button class="vbtn" data-send aria-label="В чат с ботом">${ic("send")}</button>
      <button class="vbtn close" data-close>${ic("x")}Закрыть</button></div>
    <div class="vbody">${LOADER}</div>`;
  document.body.appendChild(el);
  const v = { el, body: el.querySelector(".vbody"), urls: [], zoom: 1, doc: null };
  el.querySelector("[data-close]").onclick = () => closeViewer();
  el.querySelector("[data-send]").onclick = () => sendFile(d, nKey);
  el.querySelectorAll("[data-z]").forEach((b) => { b.onclick = () => { v.zoom = Math.min(3, Math.max(0.6, v.zoom + n(b.dataset.z) * 0.35)); drawPdf(v); }; });
  S.viewer = v;
  syncBack();
  MB.show();
  return v;
}

const loaded = {};
function loadScript(name, global, fail) {
  if (window[global]) return Promise.resolve(window[global]);
  loaded[name] = loaded[name] || new Promise((ok, bad) => {
    const sc = document.createElement("script");
    sc.src = "/app/vendor/" + name;
    sc.onload = () => ok(window[global]);
    sc.onerror = () => { delete loaded[name]; bad(new Error(fail)); };
    document.head.appendChild(sc);
  });
  return loaded[name];
}

async function loadPdfJs() {
  const lib = await loadScript("pdf.min.js", "pdfjsLib", "Просмотр PDF не загрузился — отправьте файл в чат с ботом");
  lib.GlobalWorkerOptions.workerSrc = "/app/vendor/pdf.worker.min.js";
  return lib;
}

async function drawPdf(v) {
  if (!v.doc || S.viewer !== v) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  const width = Math.min(v.body.clientWidth - 24, 980) * v.zoom;
  const pages = [];
  for (let i = 1; i <= Math.min(v.doc.numPages, 20); i++) {
    const page = await v.doc.getPage(i);
    const base = page.getViewport({ scale: 1 });
    const vp = page.getViewport({ scale: (width / base.width) * dpr });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width); canvas.height = Math.floor(vp.height);
    canvas.style.width = Math.floor(vp.width / dpr) + "px";
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
    pages.push(canvas);
  }
  if (S.viewer !== v) return;
  v.body.innerHTML = "";
  pages.forEach((cv) => v.body.appendChild(cv));
  if (v.doc.numPages > 20) v.body.insertAdjacentHTML("beforeend", `<div class="vmsg">Показаны 20 страниц из ${v.doc.numPages} — весь файл: «В чат с ботом»</div>`);
}

async function openFile(d, nKey, kind) {
  if (kind === "text") { const f = (d.files || []).find((x) => String(x.n) === String(nKey)); if (f) sheet(`<h3>${esc(f.title)}</h3><p style="white-space:pre-wrap">${esc(f.text)}</p>`); return; }
  const v = openViewer(nKey === "r" ? `Чек · сделка #${d.id}` : `Материал спора · #${d.id}`, d, nKey);
  try {
    const blob = await api(`deals/${d.id}/files/${nKey}`, { raw: true });
    if (S.viewer !== v) return;
    if (blob.type === "application/pdf") {
      const lib = await loadPdfJs();
      v.doc = await lib.getDocument({ data: new Uint8Array(await blob.arrayBuffer()), isEvalSupported: false }).promise;
      v.el.querySelectorAll("[data-z]").forEach((b) => { b.hidden = false; });
      await drawPdf(v);
      return;
    }
    const url = URL.createObjectURL(blob);
    v.urls.push(url);
    v.body.innerHTML = blob.type.startsWith("video") ? `<video src="${url}" controls playsinline autoplay></video>` : `<img src="${url}" alt="">`;
  } catch (e) {
    if (S.viewer === v) v.body.innerHTML = `<div class="vmsg">${esc(e.message)}<br><br>Нажмите ${ic("send").replace("<svg", '<svg style="width:15px;height:15px;vertical-align:-3px"')} — файл придёт в чат с ботом.</div>`;
  }
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
    <div class="hint" id="gnumhint"></div>
    <label class="field"><span>Банк</span><div class="inp"><input id="gbank" autocomplete="off" placeholder="Сбербанк" value="${esc(draft.bank || "")}"></div></label>
    <div class="chips" style="margin-top:6px">${BANKS.map((b) => `<button class="chip" data-bank="${esc(b)}">${esc(b)}</button>`).join("")}</div>
    <label class="field"><span>Получатель (по желанию)</span><div class="inp"><input id="gholder" autocomplete="off" placeholder="Иван Иванович И." value="${esc(draft.holder || "")}"></div></label>
    ${g.choices.length ? `<div class="field"><span>Время на оплату</span><div class="opts">${g.choices.map((m) => `<button class="opt ${m === minutes ? "on" : ""}" data-min="${m}">${m} мин</button>`).join("")}</div></div>` : ""}
    <p class="err" id="gerr" hidden></p>
    <button class="btn" id="gsend">${ic("send")}Выдать реквизиты</button>`);
  const num = sh.querySelector("#gnum"), bank = sh.querySelector("#gbank"), holder = sh.querySelector("#gholder");
  const save = () => { S.drafts["give" + d.id] = { num: num.value, bank: bank.value, holder: holder.value }; };
  bindNumber(num, sh.querySelector("#gnumhint"), () => kind === "sbp");
  [num, bank, holder].forEach((x) => { x.oninput = save; });
  on("[data-k]", (b) => {
    kind = b.dataset.k;
    sh.querySelectorAll("[data-k]").forEach((x) => x.classList.toggle("on", x === b));
    sh.querySelector("#numlbl").textContent = kind === "sbp" ? "Телефон СБП" : "Номер карты";
    num.placeholder = kind === "sbp" ? "+7 900 123-45-67" : "2200 7001 2345 6781";
    num.inputMode = kind === "sbp" ? "tel" : "numeric";
    num.value = ""; num.dispatchEvent(new Event("input"));
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

/* the deal's chat: the buyer, the merchant, the operator and the administration — a full-height view: the feed
   scrolls on its own, the composer stays at the bottom, above the keyboard */

function msgHtml(m) {
  return `<div class="msg ${m.mine ? "me" : /админ/i.test(m.role) ? "adm" : ""}" data-id="${m.id}">${m.mine ? "" : `<div class="who">${esc(m.role)}</div>`}<div class="t">${esc(m.text)}</div><time>${hm(m.at)}</time></div>`;
}

async function chatScreen(c, id) {
  $app.innerHTML = `<div class="chatview"><div class="top"><button class="ibtn" data-back aria-label="Назад">${ic("back")}</button><h2>Чат · сделка #${id}</h2>${fsBtn()}</div><div class="feed">${LOADER}</div></div>`;
  const [{ deal: d }, data] = await Promise.all([api(`deals/${id}`), api(`deals/${id}/chat?read=1`)]);
  if (S.unreadBy) { S.unreadTotal = Math.max(0, (S.unreadTotal || 0) - (S.unreadBy[id] || 0)); S.unreadBy[id] = 0; drawBadges(); }
  if (!c.alive()) return;
  let last = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
  const quick = QUICK[d.role] || QUICK.admin;
  $app.innerHTML = `<div class="chatview">
    <div class="top"><button class="ibtn" data-back aria-label="Назад">${ic("back")}</button>
      <h2>Чат · сделка #${d.id}<small>${esc(data.members.join(" · "))}</small></h2>${fsBtn()}</div>
    <div class="feed" id="feed"><div class="note">Ссылки и @юзернеймы не проходят — общайтесь только здесь. ${rub(d.amount_rub)} · ${esc(d.status_text)}</div>
      ${withDays(data.messages)}${data.messages.length ? "" : `<div class="note" id="none">Сообщений пока нет — напишите первым</div>`}</div>
    <button class="jump" id="jump" hidden>${ic("down")}<span>Новые сообщения</span></button>
    ${data.open ? `<div class="bottom"><div class="quick-replies">${quick.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <div class="compose"><div class="inp"><textarea id="text" rows="1" maxlength="1000" placeholder="Сообщение" enterkeyhint="send">${esc(S.drafts[id] || "")}</textarea></div>
      <button class="send" id="send" aria-label="Отправить">${ic("send")}</button></div></div>` : `<div class="bottom"><p class="hint" style="margin:0;text-align:center">Сделка закрыта — чат только для чтения</p></div>`}
  </div>`;
  const feed = $app.querySelector("#feed"), jump = $app.querySelector("#jump");
  const bottom = () => { feed.scrollTop = feed.scrollHeight; jump.hidden = true; };
  jump.onclick = () => { feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" }); jump.hidden = true; };
  feed.onscroll = () => { if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60) jump.hidden = true; };
  const seen = () => { S.chatSeen[id] = last; };
  const add = (msgs) => {
    const fresh = msgs.filter((m) => !feed.querySelector(`[data-id="${m.id}"]`));
    if (!fresh.length) return;
    const none = feed.querySelector("#none");
    if (none) none.remove();
    const near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
    fresh.forEach((m) => {
      const lastDay = feed.dataset.day;
      if (dayLabel(m.at) !== lastDay) { feed.insertAdjacentHTML("beforeend", `<div class="day">${dayLabel(m.at)}</div>`); feed.dataset.day = dayLabel(m.at); }
      feed.insertAdjacentHTML("beforeend", msgHtml(m));
    });
    last = Math.max(last, msgs[msgs.length - 1].id);
    seen();
    if (near || fresh.some((m) => m.mine)) bottom();
    else { jump.hidden = false; haptic("tap"); }
  };
  feed.dataset.day = data.messages.length ? dayLabel(data.messages[data.messages.length - 1].at) : "";
  seen(); bottom();
  every(2500, async () => { try { add((await api(`deals/${id}/chat?after=${last}&read=1`)).messages); } catch (e) { /* retry */ } });
  const text = $app.querySelector("#text");
  const send = $app.querySelector("#send");
  if (!text) return;
  const grow = () => { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 120) + "px"; };
  text.oninput = () => { S.drafts[id] = text.value; grow(); };
  text.onfocus = () => setTimeout(() => { syncHeight(); bottom(); }, 250);  // the keyboard is opening
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
    text.focus();
  };
  send.onclick = () => post(text.value.trim());
  text.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !/Mobi/.test(navigator.userAgent)) { e.preventDefault(); post(text.value.trim()); } };
  on("[data-q]", (b) => post(b.dataset.q));
}

/* ---------- wallet ---------- */

async function depositScreen(c) {
  const [w, d] = await Promise.all([api("wallet"), api("deposit")]);
  const parts = d.address.slice(2).match(/.{1,4}/g) || [];
  if (!view(c, `${titleBlock("Пополнить", "USDT в сети BEP-20 (BSC) · без комиссии · зачисление за секунды")}
    <div class="grid2"><div class="col">
      <div class="box pad qrbox" style="margin-top:10px">
        <div class="qr" id="qr">${LOADER}</div>
        <div class="netline">${tag("USDT", "accent")}${tag("BNB Smart Chain · BEP-20", "blue")}</div>
        <button class="req addr" data-copy="${esc(d.address)}" data-what="Адрес"><span class="v"><small>Ваш личный адрес — нажмите, чтобы скопировать</small>
          <span class="mono"><b>0x</b>${parts.map((x, i) => (i === 0 || i === parts.length - 1 ? `<b>${esc(x)}</b>` : esc(x))).join(" ")}</span></span>${ic("copy")}</button>
        <button class="btn" data-copy="${esc(d.address)}" data-what="Адрес">${ic("copy")}Скопировать адрес</button>
      </div>
    </div><div class="col">
      ${sec("Как пополнить")}<div class="box pad"><ol class="steps">
        <li class="done"><span class="d">1</span><span>На бирже или в кошельке выберите <b>USDT</b> и сеть <b>BSC (BEP20)</b></span></li>
        <li class="done"><span class="d">2</span><span>Вставьте адрес или отсканируйте QR. Сверьте начало и конец адреса</span></li>
        <li class="done"><span class="d">3</span><span>Зачислим автоматически и пришлём уведомление — обычно через несколько секунд</span></li></ol>
        <p class="hint" style="margin-top:4px">Адрес постоянный и только ваш. Другая монета или другая сеть не зачислятся.</p></div>
      <button class="btn line" id="check">${ic("refresh")}Проверить поступление</button>
      ${w.deposits.length ? `${sec("Последние поступления")}<div class="list">${w.deposits.map((x) => `<button class="row" ${x.link ? `data-open="${esc(x.link)}"` : ""}>
        <span class="ic ${x.status === "paid" ? "green" : "amber"}">${ic(x.status === "paid" ? "down" : "info")}</span><span class="mid"><b>${usdt(x.amount)} USDT</b><small>#${x.id} · ${when(x.created_at)}</small></span>
        <span class="end"><b class="${x.status === "paid" ? "plus" : ""}">${x.status === "paid" ? "+" + usdt(x.credit) : "не зачислено"}</b></span></button>`).join("")}</div>` : ""}
    </div></div>`)) return;
  loadScript("qrcode.min.js", "qrcode", "QR не загрузился").then((qrcode) => {
    const box = $app.querySelector("#qr");
    if (!box || !c.alive()) return;
    const q = qrcode(0, "M");
    q.addData(d.address);
    q.make();
    box.innerHTML = q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }) + `<span class="qrlogo">${LOGO}</span>`;
  }).catch(() => { const box = $app.querySelector("#qr"); if (box) box.remove(); });
  on("#check", (b) => busy(b, async () => {
    try {
      const r = await api("deposit?check=1");
      if (r.checked === null) toast("Проверка уже идёт — зачислим автоматически");
      else if (!r.checked.length) toast("За последние 10 минут поступлений нет — зачислим автоматически");
      else { toast(`Зачислено: ${r.checked.map((x) => usdt(x.credit)).join(", ")} USDT`); S.keep = true; render(); }
    } catch (e) { toast(e.message, true); }
  }));
  every(10000, async () => {
    try {
      const f = await api("wallet");
      if (c.alive() && (f.deposits[0] || {}).id !== (w.deposits[0] || {}).id) { haptic("success"); toast(`Пришло ${usdt(f.deposits[0].credit)} USDT`); S.keep = true; render(); }
    } catch (e) { /* retry */ }
  });
}

const ADDR = /^0x[0-9a-fA-F]{40}$/;

/* a card number in groups of four with a Luhn check; a phone (СБП) as +7 900 123-45-67 */
function luhn(digits) {
  let sum = 0;
  for (let i = 0; i < digits.length; i++) { let x = n(digits[digits.length - 1 - i]); if (i % 2) { x *= 2; if (x > 9) x -= 9; } sum += x; }
  return digits.length >= 13 && sum % 10 === 0;
}
function bindNumber(input, hint, sbp) {
  const fmt = () => {
    const raw = input.value;
    const phone = (sbp && sbp()) || /^\s*(\+|8\s?9|7\s?9)/.test(raw);
    const d = raw.replace(/\D/g, "");
    if (phone) {
      const p = (d.startsWith("8") ? "7" + d.slice(1) : d).slice(0, 11);
      input.value = p ? "+" + [p.slice(0, 1), p.slice(1, 4), p.slice(4, 7), [p.slice(7, 9), p.slice(9, 11)].filter(Boolean).join("-")].filter(Boolean).join(" ") : "";
      if (hint) hint.innerHTML = p.length === 11 ? `<span class="plus">Телефон СБП</span>` : "";
    } else {
      input.value = d.slice(0, 19).replace(/(\d{4})(?=\d)/g, "$1 ");
      if (hint) hint.innerHTML = d.length < 16 ? "" : luhn(d) ? `<span class="plus">${ic("check").replace("<svg", '<svg style="width:14px;height:14px;vertical-align:-2px"')} Номер карты корректный</span>`
        : `<span class="minus">Номер не проходит проверку — сверьте цифры</span>`;
    }
  };
  input.addEventListener("input", fmt);
  fmt();
}

async function pasteInto(input, after) {
  try {
    const text = (await navigator.clipboard.readText()).trim();
    if (!text) throw new Error("empty");
    input.value = text; after(); haptic("select");
  } catch (e) { input.focus(); toast("Вставьте адрес вручную: долгое нажатие → «Вставить»"); }
}

async function withdrawScreen(c) {
  const w = await api("wallet");
  const req = uuid();
  const max = n(w.withdraw.max);
  if (!view(c, `${titleBlock("Вывести", `USDT в сети BEP-20 · комиссия ${esc(w.withdraw.terms)} · минимум ${esc(w.withdraw.min)} USDT`)}
    <div class="tiles" style="margin-top:10px"><div class="tile accent"><small>Можно вывести</small><b>${usdt(w.withdraw.max)}</b><em>USDT</em></div>
      <div class="tile"><small>Баланс</small><b>${usdt(w.balance.available)}</b><em>${n(w.balance.frozen) ? `в сделках ${usdt(w.balance.frozen)}` : "USDT"}</em></div></div>
    ${w.lock_note ? `<div class="note-box">${ic("info")}<span>${esc(w.lock_note)}</span></div>` : ""}
    <label class="field"><span>Адрес кошелька USDT · сеть BEP-20 (BSC)</span><div class="inp" id="addrbox"><input id="addr" class="mono" autocomplete="off" spellcheck="false" placeholder="0x… — 42 символа" value="${esc(w.withdraw.last_address || "")}"><button class="max" id="paste" type="button">Вставить</button></div></label>
    <div id="addrhint" class="hint"></div>
    <label class="field"><span>Сумма списания</span><div class="inp big"><input id="amt" inputmode="decimal" placeholder="0" autocomplete="off"><span class="unit">USDT</span></div></label>
    <div class="chips" style="margin-top:8px">${[["25%", 0.25], ["50%", 0.5], ["75%", 0.75], ["Всё", 1]].map(([l, k]) => `<button class="chip" data-part="${k}">${l}</button>`).join("")}</div>
    <div class="box pad" id="sum" style="margin-top:12px" hidden></div>
    <p class="err" id="qerr" hidden></p>
    <button class="btn" id="go" disabled>${ic("up")}Вывести</button>
    <p class="hint">Отправляем автоматически, обычно за минуту. Не хватит USDT у сервиса — вывод подождёт в очереди и уйдёт сам.</p>
    ${w.withdrawals.length ? `${sec("В пути")}<div class="list">${w.withdrawals.map((x) => `<div class="row">
      <span class="ic ${x.status === "queued" ? "amber" : "accent"}">${ic(x.status === "queued" ? "clock" : "up")}</span>
      <span class="mid"><b class="num">${usdt(x.receive)} USDT</b><small>#${x.id} · ${esc(x.status_text)}</small></span>
      ${x.cancellable ? `<button class="btn line red sm" data-cancel="${x.id}">Отменить</button>` : ""}</div>`).join("")}</div>` : ""}`)) return;
  const amt = $app.querySelector("#amt"), sum = $app.querySelector("#sum"), qerr = $app.querySelector("#qerr"), goBtn = $app.querySelector("#go"), addr = $app.querySelector("#addr");
  const addrHint = $app.querySelector("#addrhint"), addrBox = $app.querySelector("#addrbox");
  let q = null;
  const addrOk = () => ADDR.test(addr.value.trim());
  const checkAddr = () => {
    const v = addr.value.trim();
    addrBox.classList.toggle("bad", !!v && !addrOk());
    addrBox.classList.toggle("good", addrOk());
    addrHint.innerHTML = !v ? "" : addrOk() ? `<span class="plus">${ic("check").replace("<svg", '<svg style="width:14px;height:14px;vertical-align:-2px"')} Адрес BEP-20 · сверьте: <b class="mono">${esc(v.slice(0, 6))}…${esc(v.slice(-4))}</b></span>`
      : `<span class="minus">Адрес BEP-20 — 0x и 40 символов (сейчас ${v.length})</span>`;
    ready();
  };
  const ready = () => {
    goBtn.disabled = !(q && !q.error && addrOk());
    MB.active(!goBtn.disabled, q && !q.error ? `Вывести ${usdt(q.amount)} USDT` : "Вывести");
  };
  const quote = debounce(async () => {
    const v = amountOf(amt.value);
    q = null; ready(); qerr.hidden = true;
    if (!v) { sum.hidden = true; return; }
    try {
      const r = await api(`withdraw/quote?amount=${encodeURIComponent(v)}`);
      if (!c.alive() || amountOf(amt.value) !== v) return;
      q = r;
      if (q.error) { sum.hidden = true; qerr.textContent = q.error; qerr.hidden = false; return; }
      sum.innerHTML = `${kv("Спишется", `<span class="num">${usdt(v)} USDT</span>`)}${kv(`Комиссия ${esc(q.terms)}`, `<span class="num">−${usdt(q.fee)}</span>`)}<div class="kv total"><span>Придёт</span><b>${usdt(q.receive)} USDT</b></div>`;
      sum.hidden = false;
      q.amount = v;
      ready();
    } catch (e) { qerr.textContent = e.message; qerr.hidden = false; }
  }, 300);
  amt.oninput = quote;
  addr.oninput = checkAddr;
  MB.take(goBtn, { active: false });
  checkAddr();
  on("#paste", () => pasteInto(addr, checkAddr));
  on("[data-part]", (b) => { amt.value = String(Math.floor(max * n(b.dataset.part) * 100) / 100); haptic("select"); quote(); });
  on("[data-cancel]", async (b) => {
    if (!(await confirmBox("Отменить вывод из очереди? USDT вернутся на баланс."))) return;
    busy(b, async () => { try { await api(`withdrawals/${b.dataset.cancel}/cancel`, { method: "POST" }); toast("Вывод отменён, USDT на балансе"); S.keep = true; render(); } catch (e) { toast(e.message, true); } });
  });
  on("#go", async (b) => {
    if (!q || q.error) return;
    const a = addr.value.trim();
    if (!(await confirmBox(`Вывести ${usdt(q.amount)} USDT в сети BEP-20 на ${a.slice(0, 6)}…${a.slice(-4)}? Придёт ${usdt(q.receive)} USDT. Перевод в блокчейне не отменить.`))) return;
    busy(b, async () => {
      try {
        const r = await api("withdraw", { body: { amount: q.amount, fee: q.fee, request_id: req, address: a } });
        toast(r.message, !r.ok);
        if (r.ok) { haptic("success"); go("", { replace: true }); }
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
    `<button class="row" data-go="orders"><span class="ic amber">${ic("orders")}</span><span class="mid"><b>Заявки (реквизиты под сумму)</b><small>${r.merchant === "approved" ? `Свободных заявок: ${w.offers || 0}` : r.merchant === "pending" ? "Анкета на рассмотрении" : r.merchant === "suspended" ? "Доступ приостановлен" : "Заявки покупателей под точную сумму"}</small></span>${chev()}</button>`,
    r.operator || n(m.balance.debt) ? `<button class="row" data-go="operator"><span class="ic blue">${ic("shield")}</span><span class="mid"><b>Оператор</b><small>${w.free_orders ? `Ордеров ждут оператора: ${w.free_orders}` : "Bybit-ордера, реквизиты, подтверждение"}</small></span>${chev()}</button>` : "",
    r.team ? `<button class="row" data-go="team"><span class="ic accent">${ic("people")}</span><span class="mid"><b>${r.team.leader ? "Моя команда" : "Команда"} · ${esc(r.team.name)}</b><small>${r.team.leader ? "Участники, ссылка, доход тимлида" : "Ваша команда и тимлид"}</small></span>${chev()}</button>` : "",
    r.admin ? `<button class="row" data-go="admin"><span class="ic red">${ic("scale")}</span><span class="mid"><b>Администрирование</b><small>${w.disputes ? `Споров: ${w.disputes}` : "Споры, пользователи, касса, финансы"}</small></span>${chev()}</button>` : "",
    `<button class="row" data-go="stats"><span class="ic">${ic("chart")}</span><span class="mid"><b>Статистика</b><small>Оборот, доход по дням, успешность</small></span>${chev()}</button>`,
  ].join("");
  view(c, `${titleBlock("Работа", "Продажа, заявки, команда, операторская и администрирование")}<div class="list" style="margin-top:12px">${rows}</div>`);
}

/* ---------- a team: the leader's cabinet ---------- */

async function teamScreen(c) {
  const t = await api("team");
  const st = (k) => t[k] || { n: 0, rub: 0, income: 0 };
  if (!t.leader) {
    view(c, `${titleBlock(`Команда «${t.name}»`, "Вы участник команды")}
      <div class="box pad" style="margin-top:10px">${kv("Тимлид", esc(t.leader_name || "—"))}${kv("Участников", t.members)}${kv("Статус", t.status === "approved" ? "работает" : "приостановлена")}</div>
      <p class="hint">Тимлид получает ${esc(t.pct)}% от ваших сделок — из дохода площадки, с вашей суммы ничего не удерживается.</p>`);
    return;
  }
  const share = `https://t.me/share/url?url=${encodeURIComponent(t.link)}&text=${encodeURIComponent("Работаю в Strait Pay — заходи в мою команду")}`;
  if (!view(c, `${titleBlock(`Команда «${t.name}»`, t.status === "approved" ? `Вы тимлид · ${esc(t.pct)}% от сделок участников` : "Команда приостановлена администрацией")}
    <section class="balance" style="margin-top:12px">${WAVES}
      <div class="lbl"><span>Командный баланс</span><span>${t.members} в команде</span></div>
      <div class="sum">${usdt(t.balance)}<small>USDT</small></div>
      <div class="sub">Сегодня +${usdt(st("today").income)} · 7 дней +${usdt(st("week").income)} USDT</div>
    </section>
    ${n(t.balance) ? `<button class="btn" id="out">${ic("wallet")}Перевести ${usdt(t.balance)} USDT на основной баланс</button>` : ""}
    <div class="grid2"><div class="col">
      ${sec("Реферальная ссылка")}
      <div class="box pad"><button class="req" data-copy="${esc(t.link)}" data-what="Ссылка" style="margin-top:0"><span class="v"><small>Кто запустит бота по ссылке — попадёт в команду</small><span class="mono" style="font-size:14px">${esc(t.link)}</span></span>${ic("copy")}</button>
        <div class="btns"><button class="btn soft" data-copy="${esc(t.link)}" data-what="Ссылка">${ic("copy")}Скопировать</button><button class="btn soft" data-open="${esc(share)}">${ic("send")}Поделиться</button></div>
        ${t.chat ? "" : `<p class="hint">Чат команды не подключён: добавьте бота админом в группу и отправьте там /team.</p>`}</div>
      ${sec("Доход тимлида")}
      <div class="tiles">
        <div class="tile accent"><small>Сегодня</small><b>+${usdt(st("today").income)}</b><em>${st("today").n} сделок · ${rub(st("today").rub)}</em></div>
        <div class="tile"><small>7 дней</small><b>+${usdt(st("week").income)}</b><em>${st("week").n} · ${rub(st("week").rub)}</em></div>
        <div class="tile"><small>Всего</small><b>+${usdt(st("all").income)}</b><em>${st("all").n} сделок</em></div>
        <div class="tile"><small>Процент</small><b>${esc(t.pct)}%</b><em>от каждой сделки</em></div>
      </div></div>
    <div class="col">${sec(`Участники · ${t.members}`)}
      <div class="list">${t.list.length ? t.list.map((u) => `<div class="row"><span class="ic ${u.online ? "green" : ""}">${ic("user")}</span><span class="mid"><b>${esc(u.name || "—")}${u.username ? ` <small style="display:inline">@${esc(u.username)}</small>` : ""}</b><small>с ${when(u.since).replace(/, \d\d:\d\d$/, "")}${u.online ? " · на смене" : ""}</small></span><span class="end"><b>${u.deals}</b><small>сделок</small></span></div>`).join("") : empty("people", "Пока никого — поделитесь ссылкой")}</div></div></div>`)) return;
  on("#out", (b) => busy(b, async () => { try { const r = await api("team/out", { method: "POST" }); toast(`${usdt(r.moved)} USDT на основном балансе`); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
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
    <label class="field"><span>Номер карты или телефон СБП</span><div class="inp"><input id="num" class="mono" inputmode="tel" autocomplete="off" placeholder="2200 7001 2345 6781 или +7 900…"></div></label>
    <div class="hint" id="numhint"></div>
    <label class="field"><span>Банк</span><div class="inp"><input id="bank" autocomplete="off" placeholder="Сбербанк"></div></label>
    <div class="chips" style="margin-top:6px">${BANKS.map((b) => `<button class="chip" data-bank="${esc(b)}">${esc(b)}</button>`).join("")}</div>
    <label class="field"><span>Получатель — как его видит отправитель</span><div class="inp"><input id="holder" autocomplete="off" placeholder="Иван Иванович И."></div></label>
    <div class="btns"><label class="field"><span>Минимум, ₽</span><div class="inp"><input id="min" inputmode="decimal" placeholder="1 000"></div></label>
      <label class="field"><span>Максимум, ₽</span><div class="inp"><input id="max" inputmode="decimal" placeholder="50 000"></div></label></div>
    <p class="hint">Ошибка в цифрах — деньги покупателя уйдут чужому человеку. Проверьте номер.</p>
    <button class="btn" id="save">Сохранить и включить</button>`)) return;
  bindNumber($app.querySelector("#num"), $app.querySelector("#numhint"));
  on("[data-bank]", (b) => { $app.querySelector("#bank").value = b.dataset.bank; haptic("select"); $app.querySelectorAll("[data-bank]").forEach((x) => x.classList.toggle("on", x === b)); });
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
  const live = data.status === "approved" && !data.asleep && data.online;
  if (!view(c, `${titleBlock("Заявки", "Реквизиты под сумму покупателя — берёте те, что подходят")}
    ${data.status !== "approved" ? `<div class="line"><span class="dot"></span><div class="mid"><b>Приостановлено администрацией</b><small>Заявки не приходят</small></div></div>`
      : data.asleep ? `<div class="line"><span class="dot"></span><div class="mid"><b>Пауза до ${when(data.sleep_until)}</b><small>${data.terms.strike_limit} раза подряд не было реквизитов</small></div></div>`
      : `<div class="line"><span class="dot ${live ? "on" : ""}"></span><div class="mid"><b>${live ? "На линии" : "Не на линии"}</b><small>${live ? "Заявки приходят — выключите, когда уходите" : "Заявки не приходят и не берутся"}</small></div><button class="sw green ${live ? "on" : ""}" id="line" aria-label="На линии"></button></div>`}
    <div class="tiles" style="margin-top:8px">
      <div class="tile"><small>Сегодня</small><b>+${usdt(st.today.income)}</b><em>${st.today.n} · ${rub(st.today.rub)}</em></div>
      <div class="tile"><small>Репутация</small><b>${data.reputation.score == null ? "—" : data.reputation.score}</b><em>${esc(data.reputation.line)}</em></div>
      <div class="tile"><small>Курс</small><b>${NF.format(n(data.terms.rate))} ₽</b><em>за USDT, без процента</em></div>
      <div class="tile"><small>С баланса до</small><b>${rub(data.cover_rub)}</b><em>свободно ${usdt(data.balance)} USDT</em></div>
    </div>
    ${data.strikes && !data.asleep ? `<p class="hint">Пропусков реквизитов подряд: ${data.strikes} из ${data.terms.strike_limit} — потом пауза.</p>` : ""}
    ${sec("В работе")}<div class="list">${data.working.length ? data.working.map(dealRow).join("") : empty("deals", "Взятых заявок нет")}</div>
    ${sec("Свободные заявки", `<button id="refresh">${ic("refresh").replace("<svg", '<svg style="width:15px;height:15px;vertical-align:-3px"')}</button>`)}
    <div class="list">${data.offers.length ? data.offers.map((d) => dealRow({ ...d, role: "offer" })).join("") : empty("bell", live ? "Свободных заявок нет — новые придут уведомлением" : data.status === "approved" && !data.asleep ? "Вы не на линии — включите, чтобы видеть заявки" : "Заявки не приходят, пока доступ на паузе")}</div>
    ${sec("Время на оплату по умолчанию")}
    <div class="opts">${data.pay_choices.map((x) => `<button class="opt ${x === data.pay_minutes ? "on" : ""}" data-pm="${x}">${x} мин</button>`).join("")}</div>
    <div class="foot">7 дней: ${st.week.n} · +${usdt(st.week.income)} USDT · всего ${st.all.n}${st.all.success != null ? ` · успешных ${st.all.success}%` : ""}</div>`)) return;
  on("#refresh", () => { S.keep = true; render(); });
  on("#line", (sw) => busy(sw, async () => { try { await api("merchant", { body: { online: !data.online } }); haptic("success"); toast(data.online ? "Вы не на линии — заявки не приходят" : "Вы на линии — заявки приходят"); S.me = null; await me(true); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
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
      <p class="hint">Не хватает баланса — пополните кошелёк USDT в сети BEP-20 (BSC) и погасите с баланса.</p>` : ""}`)) return;
  on("#refresh", () => { S.keep = true; render(); });
  on("#repay", async (b) => {
    if (!(await confirmBox(`Погасить ${usdt(Math.min(n(data.debt), n(data.balance)))} USDT долга с баланса?`))) return;
    busy(b, async () => { try { const r = await api("operator/repay", { method: "POST" }); toast(`Погашено ${usdt(r.repaid)} USDT`); S.keep = true; render(); } catch (e) { toast(e.message, true); } });
  });
  every(8000, async () => { try { const f = await api("operator"); if (c.alive() && JSON.stringify([f.free, f.working]) !== JSON.stringify([data.free, data.working])) { S.keep = true; render(); } } catch (e) { /* retry */ } });
}

async function adminScreen(c) {
  const [a, m] = await Promise.all([api("admin"), me()]);
  const k = a.counts, mo = a.money;
  const tile = (go, label, value, alert, sub) => `<button class="tile ${alert ? "alert" : ""}" ${go ? `data-go="${go}"` : ""}><small>${label}</small><b>${value}</b>${sub ? `<em>${sub}</em>` : ""}</button>`;
  const hub = (go, icon, tone, title, sub, badge) => `<button class="row" data-go="${go}"><span class="ic ${tone}">${ic(icon)}</span><span class="mid"><b>${title}</b><small>${sub}</small></span>${badge ? `<span class="badge">${badge}</span>` : ""}${chev()}</button>`;
  if (!view(c, `${titleBlock("Администрирование", `24 часа: ${a.day.deals} сделок · ${rub(a.day.rub)} · +${usdt(a.day.income)} USDT`)}
    <div class="tiles four" style="margin-top:10px">
      ${tile("admin/deals?dispute", "Споры", k.disputes, k.disputes)}
      ${tile("admin/deals?paid", "Ждут подтверждения", k.slow, k.slow, "дольше срока")}
      ${tile("admin/deals?open", "Открытых сделок", k.open, false, `ищут реквизиты: ${k.searching}`)}
      ${tile("admin/desk", "Выводы", k.unknown_wd || k.queued_wd, k.unknown_wd, k.unknown_wd ? "на проверке" : "в очереди")}
    </div>
    <div class="grid2"><div class="col">
      ${sec("Разделы")}<div class="list">
        ${hub("admin/users", "people", "accent", "Пользователи", "Поиск, баланс, бан, рейтинг мерчанта")}
        ${hub("admin/signups", "user", "amber", "Заявки на вход", k.signups ? "Ждут решения" : "Новых нет", k.signups)}
        ${hub("admin/desk", "wallet", "accent", "Касса BEP-20", mo.hot && !mo.hot.error ? `${usdt(mo.hot.usdt)} USDT · ${mo.hot.bnb} BNB` : "Балансы, очередь выводов")}
        ${hub("admin/finance", "chart", "green", "Финансы", "Что есть, что должны, сколько можно забрать")}
        ${hub("admin/deals?all", "search", "", "Все сделки", "Поиск по номеру или ID")}
      </div>
      ${sec("Деньги")}<div class="box pad">
        ${kv("Балансы пользователей", `<span class="num">${usdt(mo.users)} USDT</span>`)}
        ${mo.hot ? (mo.hot.error ? kv("Касса BEP-20", "нет ответа сети") : kv("Касса BEP-20", `<span class="num">${usdt(mo.hot.usdt)} USDT · <span class="${mo.hot.low ? "minus" : ""}">${mo.hot.bnb} BNB</span></span>`)) : kv("Касса BEP-20", "выключена")}
        ${n(mo.unswept) ? kv("Не собрано с адресов", `<span class="num">${usdt(mo.unswept)} USDT</span>`) : ""}
        ${n(mo.op_debt) ? kv("Долг операторов", `<span class="num">${usdt(mo.op_debt)} USDT</span>`) : ""}
      </div>
      ${k.tickets || k.merchants || k.adjustments ? `${sec("Ещё в боте")}<div class="box pad">
        ${k.merchants ? kv("Анкет мерчантов", k.merchants) : ""}${k.tickets ? kv("Обращений", k.tickets) : ""}${k.adjustments ? kv("Корректировок ждут подтверждения", k.adjustments) : ""}</div>` : ""}
    </div><div class="col">
      ${a.disputes.length ? `${sec("Споры")}<div class="list">${a.disputes.map(dealRow).join("")}</div>` : ""}
      ${a.slow.length ? `${sec("Продавец молчит")}<div class="list">${a.slow.map(dealRow).join("")}</div>` : ""}
      ${a.withdrawals.length ? `${sec("Выводы на проверке")}<div class="list">${a.withdrawals.map((w) => `<div class="row"><span class="ic red">${ic("up")}</span><span class="mid"><b class="num">#${w.id} · ${usdt(w.amount)} USDT</b><small class="mono">${esc((w.address || "").slice(0, 8))}…${esc((w.address || "").slice(-6))} · ID ${w.user_id}</small></span>
        <button class="btn sm soft" data-wd="${w.id}" data-act="done">Выполнен</button><button class="btn sm line red" data-wd="${w.id}" data-act="refund" style="margin-left:6px">Вернуть</button></div>`).join("")}</div><p class="hint">Старые сети (TON / xRocket): проверьте перевод в обозревателе. Решают владельцы.</p>` : ""}
      ${!a.disputes.length && !a.slow.length && !a.withdrawals.length ? `${sec("Очереди")}<div class="list">${empty("check", "Всё обработано — споров и проверок нет")}</div>` : ""}
    </div></div>`)) return;
  on("[data-wd]", async (b) => {
    const refund = b.dataset.act === "refund";
    if (!(await confirmBox(refund ? `Вернуть средства по выводу #${b.dataset.wd}? Только если перевод точно не ушёл.` : `Подтвердить выполнение вывода #${b.dataset.wd}? Только если нашли перевод в сети.`))) return;
    busy(b, async () => { try { const r = await api(`admin/withdrawals/${b.dataset.wd}`, { body: { action: b.dataset.act } }); toast(r.message); S.keep = true; render(); } catch (e) { toast(e.message, true); } });
  });
  if (m) drawNav(path());
  every(10000, async () => { try { const f = await api("admin"); if (c.alive() && JSON.stringify([f.counts, f.disputes.map((x) => x.id)]) !== JSON.stringify([a.counts, a.disputes.map((x) => x.id)])) { S.keep = true; render(); } } catch (e) { /* retry */ } });
}

const stars = (v) => { const k = Math.round(n(v) / 2); return `<span class="stars">${"★".repeat(k)}${"☆".repeat(5 - k)}</span>`; };

async function adminUsers(c) {
  const q = S.userQ || "";
  const list = await api(`admin/users${q ? "?q=" + encodeURIComponent(q) : ""}`);
  if (!view(c, `${titleBlock("Пользователи", "ID, @юзернейм или часть имени")}
    <label class="field"><div class="inp">${ic("search").replace("<svg", '<svg style="width:18px;height:18px;color:var(--muted)"')}<input id="q" placeholder="Поиск" value="${esc(q)}" autocomplete="off"></div></label>
    <div class="list" style="margin-top:12px">${list.users.length ? list.users.map((u) => `<button class="row" data-go="admin/user/${u.id}"><span class="ic ${u.banned ? "red" : u.online ? "green" : "accent"}">${ic(u.banned ? "ban" : "user")}</span>
      <span class="mid"><b>${esc(u.name || "—")}${u.username ? ` · @${esc(u.username)}` : ""}</b><small>ID ${u.id} · был ${when(u.seen)}</small></span><span class="end"><b>${usdt(u.balance)}</b><small>USDT</small></span></button>`).join("") : empty("search", "Никого не нашли")}</div>`, { keepScroll: S.keep })) return;
  const input = $app.querySelector("#q");
  if (S.keep) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  input.oninput = debounce(() => { S.userQ = input.value.trim(); S.keep = true; render(); }, 400);
}

async function adminUser(c, id) {
  const u = await api(`admin/users/${id}`);
  const r = u.roles;
  const tags = ROLE_TAGS(r).map(([tt, tone]) => tag(tt, tone)).join(" ");
  if (!view(c, `<div style="display:flex;align-items:center;gap:14px;margin-top:4px">
      <div class="ava xl" style="width:64px;height:64px;font-size:22px">${esc((u.name || "?").trim()[0] || "?")}</div>
      <div style="min-width:0"><h1 style="margin:0;font-size:20px;font-weight:600">${esc(u.name || "—")}</h1>
        <div class="hint" style="margin:2px 0 0">${u.username ? "@" + esc(u.username) + " · " : ""}<button data-copy="${u.id}" data-what="ID" class="num" style="color:var(--muted)">ID ${u.id}</button></div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">${u.banned ? tag("Заблокирован", "red", "ban") : ""}${tags}</div></div></div>
    <div class="tiles" style="margin-top:14px">
      <div class="tile accent"><small>Доступно</small><b>${usdt(u.balance)}</b><em>USDT</em></div>
      <div class="tile"><small>В сделках</small><b>${usdt(u.frozen)}</b><em>USDT${n(u.deposit_lock) ? ` · не прокручено ${usdt(u.deposit_lock)}` : ""}</em></div>
      <div class="tile"><small>Сделок</small><b>${u.deals.done}</b><em>открыто ${u.deals.open}</em></div>
      <div class="tile ${u.debt ? "alert" : ""}"><small>${u.debt ? "Долг оператора" : "Командный баланс"}</small><b>${usdt(u.debt || u.team_balance)}</b><em>USDT</em></div>
    </div>
    ${sec("Баланс")}<div class="box pad">
      <div class="opts" style="margin-top:0">${["-50", "-10", "+10", "+50"].map((v) => `<button class="opt" data-bal="${v}">${v}</button>`).join("")}</div>
      <label class="field"><span>Своя сумма: +25 начислить, -5 списать</span><div class="inp"><input id="delta" inputmode="decimal" placeholder="+25"><span class="unit">USDT</span></div></label>
      <label class="field"><span>Причина (увидит пользователь)</span><div class="inp"><input id="why" maxlength="200" placeholder="Например: компенсация по сделке #15"></div></label>
      <button class="btn" id="apply">${ic("coin")}Провести</button>
      <p class="hint">${u.owner ? "Вы владелец: проводится сразу." : "Свой баланс и суммы выше порога ждут второго администратора."}</p></div>
    ${sec("Рейтинг мерчанта")}<div class="box pad">
      <div style="font-size:15px">${u.rating != null ? `${stars(u.rating)} <b>${u.rating}</b> из 10 · вручную` : esc(u.rating_lines[0]).replace(/&lt;\/?b&gt;/g, "")}</div>
      <div class="hint" style="margin-top:2px">${esc(u.rating_lines[1] || "").replace(/&lt;\/?b&gt;/g, "")}</div>
      <div class="opts">${[3, 5, 6, 7, 8, 9, 10].map((v) => `<button class="opt ${n(u.rating) === v ? "on" : ""}" data-rate="${v}">${v}</button>`).join("")}<button class="opt" data-rate="-">Авто</button></div>
      <label class="field"><span>Точный рейтинг 1–10 (можно 8.5)</span><div class="inp"><input id="rate" inputmode="decimal" placeholder="8.5"><button class="max" id="rset">Задать</button></div></label></div>
    ${sec("Доступ")}<div class="list">
      <button class="row" data-q="${u.id}"><span class="ic">${ic("deals")}</span><span class="mid"><b>Сделки пользователя</b><small>Все его покупки и продажи</small></span>${chev()}</button>
      ${u.admin ? "" : `<button class="row" id="ban"><span class="ic ${u.banned ? "green" : "red"}">${ic(u.banned ? "check" : "ban")}</span><span class="mid"><b>${u.banned ? "Разблокировать" : "Заблокировать"}</b><small>${u.banned ? "Вернуть доступ к боту и приложению" : "Сделки отменятся или уйдут в спор, баланс сохранится"}</small></span></button>`}
    </div>`)) return;
  const act = async (b, body, ask) => {
    if (ask && !(await confirmBox(ask))) return;
    busy(b, async () => { try { const res = await api(`admin/users/${id}`, { body }); toast(res.message); S.keep = true; render(); } catch (e) { toast(e.message, true); } });
  };
  on("[data-bal]", (b) => act(b, { action: "balance", value: b.dataset.bal, comment: $app.querySelector("#why").value }, `${b.dataset.bal} USDT пользователю ${u.name || u.id}?`));
  on("#apply", (b) => { const v = amountOf($app.querySelector("#delta").value); if (!v) { toast("Введите сумму: +25 или -5", true); return; } act(b, { action: "balance", value: v, comment: $app.querySelector("#why").value }, `${v} USDT пользователю ${u.name || u.id}?`); });
  on("[data-rate]", (b) => act(b, { action: "rating", value: b.dataset.rate }));
  on("#rset", (b) => act(b, { action: "rating", value: amountOf($app.querySelector("#rate").value) }));
  on("#ban", (b) => act(b, { action: u.banned ? "unban" : "ban" }, u.banned ? "Разблокировать пользователя?" : "Заблокировать? Его открытые сделки отменятся или уйдут в спор."));
  on("[data-q]", (b) => { S.adminQ = b.dataset.q; go("admin/deals?all"); });
}

async function adminDesk(c) {
  const d = await api("admin/desk");
  if (!d.on) { view(c, `${titleBlock("Касса BEP-20")}<div class="box pad" style="margin-top:10px"><p style="margin:0">Касса выключена: ${esc(d.error)}</p></div>`); return; }
  if (!view(c, `${titleBlock("Касса USDT · BEP-20", "Горячий кошелёк платит выводы и газ")}
    <section class="balance" style="margin-top:12px">${WAVES}
      <div class="lbl"><span>Свободно на горячем</span><span>${d.pending ? `в сети ${d.pending}` : ""}</span></div>
      <div class="sum">${d.chain_error ? "—" : usdt(d.usdt)}<small>USDT</small></div>
      <div class="sub">${d.chain_error ? "Сеть не ответила" : `Касса всего ${usdt(d.total)} USDT · газ ${d.bnb} BNB${d.low ? " — мало" : ""}`}</div>
    </section>
    <div class="box pad" style="margin-top:10px">
      <button class="req" data-copy="${esc(d.address)}" data-what="Адрес" style="margin-top:0"><span class="v"><small>Адрес горячего кошелька · BSC</small><span class="mono" style="font-size:14px">${esc(d.address)}</span></span>${ic("copy")}</button>
      ${d.cold != null ? kv("Холодный кошелёк", `<span class="num">${usdt(d.cold)} USDT</span>`) : ""}
      ${n(d.unswept) ? kv("На адресах пополнения", `<span class="num">${usdt(d.unswept)} USDT</span>`) : ""}
      ${kv("Очередь выводов", `<span class="num">${usdt(d.queued)} USDT</span>`)}
      <div class="btns"><button class="btn soft" data-open="${esc(d.explorer)}">${ic("search")}BscScan</button>${d.owner ? `<button class="btn line red" id="key">${ic("key")}Ключ кассы</button>` : ""}</div>
      ${d.low ? `<p class="err">Газа мало — пополните BNB (~0,005) на адрес выше, иначе выплаты встанут.</p>` : ""}
    </div>
    ${sec(`Выводы в работе · ${d.queue.length}`)}
    <div class="list">${d.queue.length ? d.queue.map((w) => `<button class="row" data-go="admin/user/${w.user_id}"><span class="ic ${w.status === "queued" ? "amber" : "accent"}">${ic(w.status === "queued" ? "clock" : "up")}</span>
      <span class="mid"><b class="num">#${w.id} · ${usdt(w.amount)} USDT</b><small class="mono">${esc(w.address.slice(0, 8))}…${esc(w.address.slice(-6))} · ${w.status === "queued" ? "в очереди" : w.status === "sent" ? "в сети" : "готовится"}${w.signed ? " · подписан" : ""}</small></span>${chev()}</button>`).join("") : empty("check", "Очередь пуста")}</div>`)) return;
  on("#key", async (b) => {
    if (!(await confirmBox("Прислать seed кассы вам в личку с ботом? 12 слов дают полный доступ к деньгам. Сообщение удалится через 2 минуты."))) return;
    busy(b, async () => { try { const r = await api("admin/desk/key", { method: "POST" }); toast(`Ключ в чате с ботом — удалится через ${r.minutes} мин`); } catch (e) { toast(e.message, true); } });
  });
  every(15000, async () => { try { const f = await api("admin/desk"); if (c.alive() && JSON.stringify(f.queue) !== JSON.stringify(d.queue)) { S.keep = true; render(); } } catch (e) { /* retry */ } });
}

async function adminFinance(c) {
  const f = await api("admin/finance");
  const free = n(f.free);
  view(c, `${titleBlock("Финансы", "Что есть, что должны пользователям и что можно забрать")}
    <div class="tiles" style="margin-top:10px">
      <div class="tile ${free >= 0 ? "accent" : "alert"}"><small>${free >= 0 ? "Можно забрать" : "Не хватает"}</small><b>${usdt(Math.abs(free))}</b><em>USDT</em></div>
      <div class="tile"><small>Прибыль 24 ч</small><b class="plus">+${usdt(f.profit["24h"])}</b><em>7 д +${usdt(f.profit["7d"])}</em></div>
    </div>
    <div class="grid2"><div class="col">
      ${sec("Что есть")}<div class="box pad">
        ${kv("Горячий кошелёк", f.hot == null ? "нет ответа" : `<span class="num">${usdt(f.hot)} USDT</span>`)}
        ${f.cold != null ? kv("Холодный", `<span class="num">${usdt(f.cold)} USDT</span>`) : ""}
        ${n(f.unswept) ? kv("Не собрано с адресов", `<span class="num">${usdt(f.unswept)} USDT</span>`) : ""}
        <div class="kv total"><span>Итого</span><b>${usdt(f.assets)} USDT</b></div></div>
      ${sec("Должны пользователям")}<div class="box pad">
        ${kv("Балансы", `<span class="num">${usdt(f.users)}</span>`)}${kv("В сделках", `<span class="num">${usdt(f.frozen)}</span>`)}
        ${n(f.team) ? kv("Командные балансы", `<span class="num">${usdt(f.team)}</span>`) : ""}
        ${f.unpaid_n ? kv(`Выводы в пути (${f.unpaid_n})`, `<span class="num">${usdt(f.unpaid)}</span>`) : ""}
        <div class="kv total"><span>Итого</span><b>${usdt(f.liabilities)} USDT</b></div></div>
    </div><div class="col">
      ${sec("Прибыль площадки")}<div class="box pad">
        ${kv("24 часа", `<span class="num plus">+${usdt(f.profit["24h"])}</span>`)}${kv("7 дней", `<span class="num">+${usdt(f.profit["7d"])}</span>`)}
        ${kv("30 дней", `<span class="num">+${usdt(f.profit["30d"])}</span>`)}${kv("Всего", `<span class="num">+${usdt(f.profit.all)}</span>`)}</div>
      ${sec("Оборот")}<div class="box pad">
        ${kv("Сделок за 24 ч", `${f.volume["24h"].n} · ${rub(f.volume["24h"].rub)}`)}${kv("За 7 дней", `${f.volume["7d"].n} · ${rub(f.volume["7d"].rub)}`)}
        ${kv("Пользователей", `${f.users_n} · на смене ${f.online}`)}
        ${n(f.op_debt) ? kv("Долг операторов", `<span class="num">${usdt(f.op_debt)} USDT</span>`) : ""}</div>
    </div></div>
    <p class="hint">«Можно забрать» = что есть − что должны пользователям; прибыль уже внутри.</p>`);
}

async function adminSignups(c) {
  const list = await api("admin/signups");
  if (!view(c, `${titleBlock("Заявки на вход", "Кто хочет работать в Strait Pay")}
    <div class="list" style="margin-top:12px">${list.signups.length ? list.signups.map((s) => `<div class="row" style="flex-wrap:wrap"><span class="ic amber">${ic("user")}</span>
      <span class="mid"><b>${esc(s.name || "—")}${s.username ? ` · @${esc(s.username)}` : ""}</b><small>${esc(s.role)} · оборот ${esc(s.turnover)}${s.proof ? " · скриншот в боте" : ""} · ${when(s.created_at)}</small></span>
      <div style="display:flex;gap:6px;width:100%;padding-left:50px"><button class="btn sm" data-ok="${s.id}">${ic("check")}Одобрить</button><button class="btn sm line red" data-no="${s.id}">Отклонить</button></div></div>`).join("") : empty("check", "Новых заявок нет")}</div>`)) return;
  on("[data-ok]", (b) => busy(b, async () => { try { const r = await api(`admin/signups/${b.dataset.ok}`, { body: { approve: true } }); toast(r.message); S.keep = true; render(); } catch (e) { toast(e.message, true); } }));
  on("[data-no]", (b) => {
    const sh = sheet(`<h3>Отклонить заявку</h3><label class="field"><span>Причина (увидит пользователь)</span><div class="inp"><textarea id="why" rows="2" maxlength="300" placeholder="Например: нет опыта P2P"></textarea></div></label><button class="btn red" id="no">Отклонить</button>`);
    on("#no", (x) => busy(x, async () => { try { const r = await api(`admin/signups/${b.dataset.no}`, { body: { approve: false, reason: sh.querySelector("#why").value } }); closeSheet(); toast(r.message); S.keep = true; render(); } catch (e) { toast(e.message, true); } }), sh);
  });
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
    ${sec("Оформление")}<div class="seg" style="margin-top:0">${[["auto", "Как в Telegram", "auto"], ["light", "Светлая", "sun"], ["dark", "Тёмная", "moon"]].map(([k, label]) => `<button data-theme="${k}" class="${themeMode() === k ? "on" : ""}">${label}</button>`).join("")}</div>
    ${sec("Аккаунт")}<div class="list">
      ${m.roles.team ? `<button class="row" data-go="team"><span class="ic accent">${ic("people")}</span><span class="mid"><b>${m.roles.team.leader ? "Моя команда" : "Команда"} · ${esc(m.roles.team.name)}</b><small>${m.roles.team.leader ? "Участники, ссылка, доход тимлида" : "Ваша команда"}</small></span>${chev()}</button>` : ""}
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
  on("[data-theme]", (b) => { setTheme(b.dataset.theme); haptic("select"); $app.querySelectorAll("[data-theme]").forEach((x) => x.classList.toggle("on", x === b)); drawSide(path()); });
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
    <div class="box pad"><div class="bars">${st.days.map((d) => `<div data-bar="${esc(d.day)}|${d.n}|${d.income}" title="${esc(d.day)}: ${d.n} сд., +${usdt(d.income)} USDT"><i class="${n(d.income) ? "" : "zero"}" style="height:${Math.round((n(d.income) / max) * 100)}%"></i><span>${new Date(d.day).getDate()}</span></div>`).join("")}</div></div>
    ${sec("Покупки")}
    <div class="box pad">${kv("Сделок", st.buyer.n)}${kv("Переведено", `<span class="num">${rub(st.buyer.rub)}</span>`)}<div class="kv total"><span>Получено</span><b>${usdt(st.buyer.usdt)} USDT</b></div></div>`)) return;
  on("[data-p]", (b) => { S.period = b.dataset.p; haptic("select"); S.keep = true; render(); });
  on("[data-bar]", (b) => { const [day, k, inc] = b.dataset.bar.split("|"); haptic("select"); toast(`${dayLabel(day + "T12:00:00")}: ${k} сделок · +${usdt(inc)} USDT`); });
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
  [/^deal\/(\d+)\/chat$/, chatScreen, "chat"], [/^request\/(\d+)$/, (c, id) => dealScreen(c, id, "deal", true), "deal"],
  [/^deposit$/, depositScreen, "page"], [/^withdraw$/, withdrawScreen, "page"], [/^history$/, historyScreen, "page"],
  [/^work$/, workScreen, "page"], [/^cards$/, cardsScreen, "page"], [/^card\/new$/, cardNew, "page"], [/^card\/(\d+)$/, cardScreen, "page"],
  [/^orders$/, ordersScreen, "page"], [/^operator$/, operatorScreen, "page"], [/^team$/, teamScreen, "page"],
  [/^admin$/, adminScreen, "page"], [/^admin\/deals(?:\?(\w+))?$/, adminDeals, "page"],
  [/^admin\/users$/, adminUsers, "page"], [/^admin\/user\/(\d+)$/, adminUser, "page"], [/^admin\/desk$/, adminDesk, "page"],
  [/^admin\/finance$/, adminFinance, "page"], [/^admin\/signups$/, adminSignups, "page"],
  [/^profile$/, profile, "page"], [/^stats$/, statsScreen, "page"], [/^guides$/, guidesScreen, "page"], [/^guide\/([a-z]+)$/, guideScreen, "page"],
];
const ROOTS = ["", "buy", "orders", "deals", "work", "profile"];
const isRoot = (p) => ROOTS.includes(p);

function roles() { return (S.me && S.me.roles) || {}; }
function tabs() {
  const r = roles();
  return [["", "Главная", "home"], r.merchant === "approved" ? ["orders", "Заявки", "orders"] : ["buy", "Обмен", "swap"],
    ["deals", "Сделки", "deals"], ["work", "Работа", "work"], ["profile", "Профиль", "user"]];
}
function TAB_OF(p) {
  const has = (k) => tabs().some(([t]) => t === k);
  if (/^(buy|sell)$/.test(p)) return has("buy") ? "buy" : "";
  if (/^(orders|request\/)/.test(p)) return has("orders") ? "orders" : "work";
  if (/^(deals|deal\/)/.test(p)) return "deals";
  if (/^(work|cards|card\/|operator|admin|stats|team)/.test(p)) return "work";
  if (/^(profile|guides|guide\/)/.test(p)) return "profile";
  return "";
}

function parent(p) {
  if (/^deal\/\d+\/chat$/.test(p)) return p.replace(/\/chat$/, "");
  if (/^request\//.test(p)) return "orders";
  if (/^deal\//.test(p)) return "deals";
  if (/^card\//.test(p)) return "cards";
  if (/^admin\/user\//.test(p)) return "admin/users";
  if (/^admin\//.test(p)) return "admin";
  if (/^(cards|operator|admin|stats|team)$/.test(p)) return "work";
  if (/^guide\//.test(p)) return "guides";
  if (p === "guides") return "profile";
  if (p === "sell") return "buy";
  return "";
}

const path = () => (location.hash.startsWith("#/") ? decodeURIComponent(location.hash.slice(2)) : "");

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
  if (S.viewer) { closeViewer(); return; }
  if (S.sheet) { closeSheet(); return; }
  const p = path();
  let prev = S.stack.length ? S.stack.pop() : parent(p);
  if (prev === p) prev = parent(p);
  history.replaceState(null, "", location.pathname + location.search + "#/" + prev);
  render();
}

function syncBack() {
  if (!tg || !tg.BackButton) return;
  try { if (S.viewer || S.sheet || !isRoot(path())) tg.BackButton.show(); else tg.BackButton.hide(); } catch (e) { /* old client */ }
}

function counts() {
  const m = S.me || {};
  const unread = S.unreadTotal != null ? S.unreadTotal : (m.counts || {}).unread || 0;
  return { deals: ((m.counts || {}).action || 0) + unread, orders: (m.work || {}).offers || 0, admin: (m.work || {}).disputes || 0 };
}

function setBadge(count) {
  if (S.me) S.me.counts.action = count;
  drawBadges();
}

function drawNav(p) {
  const tab = TAB_OF(p);
  const k = counts();
  $navIn.innerHTML = tabs().map(([t, label, icon]) => `<button data-tab="${t}" class="${t === tab ? "on" : ""}"><span class="pill">${ic(icon)}</span>${label}${k[t] ? `<span class="badge">${k[t]}</span>` : `<span class="badge" hidden></span>`}</button>`).join("");
  $nav.hidden = false;
  $navIn.querySelectorAll("[data-tab]").forEach((b) => { b.onclick = () => { haptic("select"); if (b.dataset.tab === tab && isRoot(p)) { window.scrollTo({ top: 0, behavior: "smooth" }); return; } go(b.dataset.tab); }; });
  drawSide(p);
  syncBack();
}

function drawSide(p) {
  const r = roles(), k = counts();
  const head = p.split("/")[0] || "";
  const item = (to, label, icon, badge) => `<button data-side="${to}" class="${(to === head || (to === "admin" && head === "admin")) && !(to === "" && p !== "") ? "on" : ""}">${ic(icon)}${label}<span class="badge" ${badge ? "" : "hidden"}>${badge || ""}</span></button>`;
  const mode = themeMode();
  $side.innerHTML = `<div class="brand">${LOGO}Strait Pay</div>
    ${item("", "Главная", "home")}${item("buy", "Обмен", "swap")}${item("deals", "Сделки", "deals", k.deals)}${item("history", "История", "clock")}
    <div class="sub">Работа</div>
    ${r.merchant === "approved" ? item("orders", "Заявки", "orders", k.orders) : ""}${item("cards", "Карты и смена", "card")}
    ${r.operator ? item("operator", "Оператор", "shield") : ""}${r.team ? item("team", r.team.leader ? "Моя команда" : "Команда", "people") : ""}
    ${r.admin ? item("admin", "Администрирование", "scale", k.admin) : ""}${item("stats", "Статистика", "chart")}
    <div class="grow"></div>
    ${item("guides", "Инструкции", "book")}${item("profile", "Профиль", "user")}
    <button data-theme-cycle>${ic(mode === "dark" ? "moon" : mode === "light" ? "sun" : "auto")}Тема: ${mode === "dark" ? "тёмная" : mode === "light" ? "светлая" : "как в Telegram"}</button>`;
  $side.querySelectorAll("[data-side]").forEach((b) => { b.onclick = () => go(b.dataset.side); });
  const cyc = $side.querySelector("[data-theme-cycle]");
  if (cyc) cyc.onclick = () => { setTheme({ auto: "light", light: "dark", dark: "auto" }[themeMode()]); drawSide(path()); };
}

async function render() {
  clearTimers();
  closeSheet(false);
  closeViewer(false);
  MB.clear();
  const t = ++S.seq;
  const p = path();
  const route = ROUTES.find(([re]) => re.test(p));
  if (!route) { go("", { replace: true }); return; }
  document.body.classList.toggle("no-nav", route[2] === "chat");
  drawNav(p);
  const c = { t, alive: () => t === S.seq && path() === p };
  if (!S.keep && route[2] !== "chat") { $app.innerHTML = `<div class="screen">${SK[route[2]]()}</div>`; window.scrollTo(0, 0); }
  try { await route[1](c, ...p.match(route[0]).slice(1)); }
  catch (e) { if (c.alive()) failView(c, e); }
  finally { if (t === S.seq) S.keep = false; }
}

/* ---------- start ---------- */

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-go],[data-copy],[data-open],[data-back],[data-fs]");
  if (!t || t.disabled) return;
  if (t.dataset.copy !== undefined) { e.preventDefault(); copy(t.dataset.copy, t.dataset.what); return; }
  if (t.dataset.open) { e.preventDefault(); openUrl(t.dataset.open); return; }
  if (t.dataset.back !== undefined) { e.preventDefault(); haptic("tap"); back(); return; }
  if (t.dataset.fs !== undefined) { e.preventDefault(); haptic("tap"); fs.toggle(); return; }
  e.preventDefault();
  haptic("tap");
  closeSheet(false);
  go(t.dataset.go);
});
window.addEventListener("hashchange", () => render());  // a link to #/… inside a page (our own moves do not fire it)
window.addEventListener("error", (e) => report(e.error || e.message, "window"));
window.addEventListener("unhandledrejection", (e) => { if (!(e.reason instanceof ApiError)) report(e.reason, "promise"); });
window.addEventListener("keydown", (e) => { if (e.key === "Escape" && (S.viewer || S.sheet)) back(); });
document.addEventListener("fullscreenchange", () => fs.sync());
if (window.visualViewport) window.visualViewport.addEventListener("resize", syncHeight);
document.addEventListener("visibilitychange", () => {  // back from the background: lists show what happened meanwhile
  if (document.hidden || S.sheet || S.viewer) return;
  const p = path();
  const typing = document.activeElement && /INPUT|TEXTAREA/.test(document.activeElement.tagName);
  if (!typing && /^(|deals|orders|operator|admin|deal\/\d+|team)$/.test(p)) { S.keep = true; render(); }
});
function netBanner() {
  let b = document.getElementById("offline");
  if (navigator.onLine) { if (b) b.remove(); return; }
  if (!b) { b = document.createElement("div"); b.id = "offline"; b.className = "offline"; b.innerHTML = `${ic("alert")}Нет интернета — покажем свежие данные, как только связь вернётся`; document.body.appendChild(b); }
}
window.addEventListener("offline", netBanner);
window.addEventListener("online", () => { netBanner(); toast("Связь восстановлена"); if (!S.sheet && !S.viewer) { S.keep = true; render(); } });
window.addEventListener("resize", syncHeight);

(function start() {
  applyTheme();
  syncHeight();
  if (!tg || !tg.initData) { gate("Откройте Strait Pay в Telegram — кнопкой «Открыть приложение» в боте."); return; }
  try { tg.ready(); tg.expand(); } catch (e) { /* old client */ }
  try {
    if (tg.isVersionAtLeast("7.7")) tg.disableVerticalSwipes();
    tg.onEvent("themeChanged", applyTheme);
    tg.onEvent("viewportChanged", syncHeight);
    tg.onEvent("fullscreenChanged", () => fs.sync());
    tg.onEvent("fullscreenFailed", () => toast("Полный экран недоступен в этой версии Telegram", true));
  } catch (e) { /* older clients */ }
  try { if (tg.BackButton) tg.BackButton.onClick(back); } catch (e) { /* old client */ }
  try { if (MB.ok()) tg.MainButton.onClick(() => MB.click()); } catch (e) { /* old client */ }
  // the start page: ?p=deal/5 from the bot's buttons, startapp=deal-5 from t.me links; a hash survives a reload.
  // Whatever page it is, the way out is there: «Назад» leads up to its section, «Главная» and the tabs anywhere.
  const fromQuery = new URLSearchParams(location.search).get("p");
  const fromLink = ((tg.initDataUnsafe && tg.initDataUnsafe.start_param) || "").replace(/-/g, "/");
  const fromHash = path();
  const first = [fromHash, fromQuery, fromLink].find((x) => x && ROUTES.some(([re]) => re.test(x))) || "";
  if (first && !isRoot(first)) {
    const chain = [];
    for (let q = parent(first); ; q = parent(q)) { chain.unshift(q); if (q === "" || chain.length > 4) break; }
    S.stack = chain;
  }
  history.replaceState(null, "", location.pathname.replace(/\/$/, "") + "/#/" + first);
  me().then(() => { drawNav(path()); return refreshUnread(); }).catch(() => {});
  setInterval(refreshUnread, 20000);  // the deals tab shows new chat messages wherever the user is
  render();
  loadAvatar();
})();
