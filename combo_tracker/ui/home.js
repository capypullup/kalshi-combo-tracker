// Home: the best +EV combos as big cards, so a bet can be copied in two taps on a phone.
// Filters run in the browser; only refresh and "Bet in CapyEV" go back to Python.

const STORE = "cev-home-v1";
const MIN_EV = [0, 0.1, 0.25, 1];
const MIN_BET = [0, 250, 500, 1000];
const SHOW_LEGS = 3;  // legs on a closed card; tap the card for the rest
const LOGO = "__LOGO__";  // app.py puts the CapyEV logo (capyev.svg) here
// kalshi.com's combo builder with the combo in its slip (the Kalshi app can't open one from a link).
// Same as links.bet_url (tested).
export const betUrl = (ticker) => (/^KXMVE/i.test(ticker) ? "https://kalshi.com/combos?marketTicker="
  : "https://kalshi.com/markets_by_ticker/") + encodeURIComponent(ticker);

const ICON_OUT = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>';
const ICON_REFRESH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
// Same icons as the Combos page.
const SPORT_ICON = { football: "🏈", basketball: "🏀", baseball: "⚾", hockey: "🏒", tennis: "🎾", soccer: "⚽",
                     mma: "🥊", boxing: "🥊", golf: "⛳", racing: "🏎️", esports: "🎮", cricket: "🏏", crypto: "🪙",
                     commodities: "🛢️", finance: "📈", politics: "🏛️", other: "🎯" };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (x) => typeof x === "number" && isFinite(x);

// An EV as the badge prints it: whole percents from +10%, one decimal under that. Colors, the 🔥 and the
// ring go by this value too, so two badges that read the same look the same (same as Combos).
function evShown(x) {
  if (!isNum(x)) return x;
  const d = Math.round(x * 1000) / 1000;
  return Math.abs(d) >= 0.1 ? Math.round(x * 100) / 100 : d;
}

export function evText(x) {
  x = evShown(x);
  if (!isNum(x)) return "–";
  if (Math.abs(x) < 0.0005) return "+0.0%";  // a tiny edge either way
  const p = x * 100;
  return `${p >= 0 ? "+" : ""}${Math.abs(p) >= 10 ? Math.round(p).toLocaleString() : p.toFixed(1)}%`;
}

// How strong an edge looks, 0..1 on a log scale: +1% ≈ 0.1, +10% ≈ 0.36, +100% ≈ 0.69, +800% = 1.
export function evHeat(x) {
  if (!isNum(x) || x <= 0) return 0;
  return Math.min(1, Math.log1p(x * 100) / Math.log1p(800));
}

// The EV badge's colors: a pale mint wash under +10%, deep green with light text from +10%, then from
// +20% bright green with dark text that keeps brightening and turns yellower up to neon chartreuse at
// +800% (the hue moves most at the top so +350% and +800% still differ), with a glow from +200%, so a
// huge edge is obvious at a glance. The Combos page uses the same colors (tests/test_table_js.py
// checks they match).
export function evStyle(x) {
  x = evShown(x);
  const h = evHeat(x);
  if (!(x >= 0.1)) {  // under +10%: tint only
    const a = (0.1 + 0.3 * h).toFixed(2);
    return `background:rgba(46,230,166,${a});color:#b9f7dc;box-shadow:inset 0 0 0 1px rgba(46,230,166,0.28)`;
  }
  const h10 = evHeat(0.1);
  const t = (h - h10) / (1 - h10);  // 0 at +10%, 1 at +800%
  const t20 = (evHeat(0.2) - h10) / (1 - h10);
  const hue = (160 - 80 * t ** 1.5).toFixed(0);
  const dark = x >= 0.2;
  const u = dark ? (t - t20) / (1 - t20) : t / t20;
  const sat = dark ? 70 + 30 * u ** 0.8 : 64 + 6 * u;
  const light = dark ? 37 + 18 * u : 23 + 3 * u;
  const bg = `hsl(${hue} ${sat.toFixed(0)}% ${light.toFixed(0)}%)`;
  const fg = dark ? "#00170c" : "#f2fff9";
  const glow = x >= 2 ? `0 0 ${Math.round(24 * h)}px hsl(${hue} 100% 50% / ${(0.6 * h).toFixed(2)})` : "none";
  return `background:${bg};color:${fg};box-shadow:${glow}`;
}

function odds(p) {
  if (!isNum(p) || p <= 0 || p >= 1) return "n/a";
  const o = p > 0.5 ? -Math.round((100 * p) / (1 - p)) : Math.round((100 * (1 - p)) / p);
  return o > 0 ? `+${o}` : `${o}`;
}
function mult(p) {
  if (!isNum(p) || p <= 0) return "–";
  const m = 1 / p;
  return `${m >= 100 ? Math.round(m) : m.toFixed(m >= 10 ? 1 : 2)}x`;
}
function money0(x) {
  if (!isNum(x)) return "–";
  const a = Math.abs(x);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(1)}M` : a >= 1e4 ? `${Math.round(a / 1e3)}k` : Math.round(a).toLocaleString();
  return `${x < 0 ? "-" : ""}$${s}`;
}
function ago(ms) {
  const m = Math.max(0, (Date.now() - ms) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${Math.floor(m)}m ago` : `${Math.floor(m / 60)}h ago`;
}
function when(ms) {
  const d = new Date(ms);
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return `Today ${t}`;
  const tmr = new Date(today.getTime() + 864e5);
  if (d.toDateString() === tmr.toDateString()) return `Tmrw ${t}`;
  return `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
}

// "🏀 NBA", "🏀 NBA + ⚾ MLB", or "🎯 4 leagues" from the legs' leagues.
function sportLabel(r) {
  const seen = new Map();
  for (const l of r.legs) if (l.lg) seen.set(l.lg, l.spt);
  if (!seen.size) return "";
  if (seen.size > 2) return `🎯 ${seen.size} leagues`;
  return [...seen].map(([lg, spt]) => `${SPORT_ICON[spt] || "🎯"} ${esc(lg)}`).join(" + ");
}

// Kelly units at the settings from the Combos page's gear and its Mid / Cross switch (defaults ¼ Kelly,
// 1u = 1% of bankroll, mid), so both pages size a bet the same.
export function homeUnits(r, prefs) {
  const p = { kelly: 0.25, unitPct: 1, basis: "mid", ...prefs };
  const k = p.basis === "cross" ? r.kc : r.km;
  return isNum(k) && isNum(p.kelly) && p.unitPct > 0 ? (k * p.kelly * 100) / p.unitPct : null;
}
export const fmtU = (u) => (u >= 10 ? `${Math.round(u)}u` : `${u.toFixed(2)}u`);
function unitPrefs() {
  try {
    const { kelly, unitPct, basis } = JSON.parse(localStorage.getItem("cev-ui-v1") || "{}");  // the Combos page's store
    return Object.fromEntries(Object.entries({ kelly, unitPct, basis }).filter(([, v]) => v !== undefined));
  } catch (e) {
    return {};
  }
}

function loadUi() {
  const base = { phase: "all", minEv: 0, minBet: 0, open: null, refreshing: null };
  try {
    return { ...base, ...JSON.parse(localStorage.getItem(STORE) || "{}"), open: null, refreshing: null };
  } catch (e) {
    return base;
  }
}
function saveUi(S) {
  try {
    const { open, refreshing, ...keep } = S;
    localStorage.setItem(STORE, JSON.stringify(keep));
  } catch (e) { /* private mode etc. */ }
}

const id = (r) => `${r.ticker}|${r.side}`;

function visible(S, rows) {
  return rows.filter((r) => (S.phase === "all" || (S.phase === "live") === !!r.live)
    && (r.evm ?? -1) >= S.minEv && (r.stake ?? 0) >= S.minBet);
}

function cardHtml(S, r, prefs) {
  const open = S.open === id(r);
  const legs = open ? r.legs : r.legs.slice(0, SHOW_LEGS);
  const more = r.legs.length - legs.length;
  const time = r.live ? '<span class="live"><i></i>Live</span>' : r.start ? esc(when(r.start)) : "";
  const warn = [r.sg ? "same game" : "", /CHECK/.test(r.flags) ? "check the book" : "", /IN BAND/.test(r.flags) ? "inside spread" : ""]
    .filter(Boolean).map((w) => `<span class="warn">${w}</span>`).join("");
  const evm = evShown(r.evm), evc = evShown(r.evc);
  const hot = evm >= 2 ? '<span class="fire">🔥</span>' : "";
  const ring = evm >= 5 ? " ring" : "";  // +500% and up, like the Combos pill
  const lo = evc <= -0.0005 ? ' class="lo"' : "";  // a negative cross gets a red chip
  const u = homeUnits(r, prefs);
  return `<div class="card${open ? " open" : ""}" data-id="${esc(id(r))}">
    <div class="head" data-act="toggle">
      <div class="ev${ring}" style="${evStyle(r.evm)}">${hot}
        <b>${evText(r.evm)}</b><span${lo}>${evText(r.evc)}</span><small>mid / cross</small>
      </div>
      <div class="what">
        <div class="meta"><span class="sport">${sportLabel(r)}</span><span class="when">${time}</span></div>
        <div class="title">${r.L}-leg combo <em class="side ${r.side}">${r.side.toUpperCase()}</em>`
          + `${u >= 0.005 ? `<span class="u">${fmtU(u)}</span>` : ""}</div>
        <div class="bet"><b>${money0(r.stake)}</b> @ ${mult(r.price)} <span class="dim">${odds(r.price)} · ${ago(r.t)}</span></div>
      </div>
    </div>
    <ul class="legs" data-act="toggle">${legs.map((l) => `<li><em class="side ${l.s}">${l.s.toUpperCase()}</em>`
      + `<span class="leg">${esc(l.n)}</span><span class="evt">${l.lv ? '<i class="ldot"></i>' : ""}${esc(l.e)}</span></li>`).join("")}
      ${more > 0 ? `<li class="more">+ ${more} more leg${more > 1 ? "s" : ""}</li>` : ""}</ul>
    <div class="foot">
      <div class="fair"><span>Fair <b>${mult(r.fm)} / ${mult(r.fc)}</b></span><span class="dim">vs ${mult(r.price)} paid</span>${warn}</div>
      <div class="acts">
        <a class="ghost" href="${betUrl(r.ticker)}" target="_blank" rel="noopener">kalshi.com ${ICON_OUT}</a>
        <button class="go" data-act="quote">Bet in CapyEV</button>
      </div>
    </div>
    ${r.side === "no" ? '<div class="note">This bettor took NO. Bet in CapyEV quotes NO; the kalshi.com slip starts on YES.</div>' : ""}
  </div>`;
}

function seg(S, rows) {
  const n = (p) => rows.filter((r) => p === "all" || (p === "live") === !!r.live).length;
  const b = (p, label) => `<button class="${S.phase === p ? "on" : ""}" data-act="phase" data-v="${p}">${label}<span>${n(p)}</span></button>`;
  return `<div class="seg">${b("pre", "Pre-match")}${b("live", "Live")}${b("all", "All")}</div>`;
}
const chips = (vals, cur, fmt, act) => vals.map((v) => `<button class="chip${v === cur ? " on" : ""}" data-act="${act}" data-v="${v}">${fmt(v)}</button>`).join("");

export default function (component) {
  const { parentElement, data, setTriggerValue } = component;
  const S = (window.__homeUi = window.__homeUi || loadUi());
  window.__homeCtx = { data, setTriggerValue };
  if (data.refreshed && data.refreshed === S.refreshing) S.refreshing = null;

  let root = parentElement.querySelector(".home");
  if (!root) {
    root = document.createElement("div");
    root.className = "home";
    parentElement.appendChild(root);
    bind(root, S);
  }
  paint(root, S);
}

function paint(root, S) {
  const D = window.__homeCtx.data;
  const rows = visible(S, D.rows);
  const prefs = unitPrefs();
  const empty = D.rows.length
    ? "No bets match these filters. Lower the min EV or bet size."
    : "No +EV combos in the last hour yet. The capybara is still sipping… 🧋";
  root.innerHTML = `
    <div class="hero">
      <div><h1>Best bets</h1><p>${D.demo ? "Demo · " : ""}${D.count.toLocaleString()} combos of $${D.minBet}+ in the last hour · ${ago(D.updated)}</p></div>
      <button class="iconbtn${S.refreshing ? " spin" : ""}" data-act="refresh" aria-label="Refresh">${ICON_REFRESH}</button>
    </div>
    ${seg(S, D.rows)}
    <div class="frow"><label>Min EV</label>${chips(MIN_EV, S.minEv, (v) => (v ? `${v * 100}%` : "Any"), "minEv")}</div>
    <div class="frow"><label>Min bet</label>${chips(MIN_BET, S.minBet, (v) => (v ? money0(v) : "Any"), "minBet")}</div>
    ${D.error ? `<div class="err">${esc(D.error)}</div>` : ""}
    <div class="cards">${rows.length ? rows.map((r) => cardHtml(S, r, prefs)).join("") : `<div class="empty"><img src="data:image/svg+xml;utf8,${encodeURIComponent(LOGO)}" alt="">${empty}</div>`}</div>`;
}

function bind(root, S) {
  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const ctx = window.__homeCtx;
    const card = b.closest(".card");
    const r = card && ctx.data.rows.find((x) => id(x) === card.dataset.id);
    const v = b.dataset.v;
    switch (b.dataset.act) {
      case "phase": S.phase = v; break;
      case "minEv": S.minEv = +v; break;
      case "minBet": S.minBet = +v; break;
      case "toggle": S.open = S.open === card.dataset.id ? null : card.dataset.id; break;
      case "quote": return r && ctx.setTriggerValue("quote", { t: r.ticker, side: r.side, p: r.price, n: r.n });
      case "refresh": {
        if (S.refreshing) return;
        S.refreshing = Date.now();
        setTimeout(() => { if (S.refreshing) { S.refreshing = null; paint(root, S); } }, 60000);
        paint(root, S);
        return ctx.setTriggerValue("refresh", S.refreshing);
      }
      default: return;
    }
    saveUi(S);
    paint(root, S);
  });
}
