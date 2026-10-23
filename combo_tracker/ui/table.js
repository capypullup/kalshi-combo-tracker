// Combo EV table: filters, sorting and expansion all run in the browser, so they're
// instant. Only min bet, window, refresh, "load full book" and "Quote it" go back to Python.
// Each bet is a card led by its EV mid / cross, colored on the same green scale as Home.

const MIN_EV = [null, 0, 5, 10, 20, 50];
const MIN_QK = [null, 0.1, 0.2, 0.5, 1, 2];
const MIN_BET = [5, 10, 50, 100, 500];
const WINDOWS = [[15, "15m"], [60, "1h"], [180, "3h"]];
const KELLY = [[1, "Full"], [0.5, "½"], [0.25, "¼"], [0.125, "⅛"]];
const SORTS = [["time", "Newest"], ["ev", "EV"], ["stake", "Bet $"], ["qk", "QK"]];
const BIG_BET = 500;  // stakes from here up show in cyan
const PAGE = 100;
const REFRESH_TIMEOUT = 60000;  // ms without an answer before the refresh button gives up
const RULE = "────────────";
const STORE = "cev-ui-v1";
const LOGO = "__LOGO__";  // app.py puts the CapyEV logo (capyev.svg) here
// Kalshi's app opens these links; its website puts the combo in its bet slip. Same as links.bet_url (tested).
const betUrl = (ticker) => `https://kalshi.com/markets_by_ticker/${encodeURIComponent(ticker)}`;
// Same icons as the Home page.
const SPORT_ICON = { football: "🏈", basketball: "🏀", baseball: "⚾", hockey: "🏒", tennis: "🎾", soccer: "⚽",
                     mma: "🥊", boxing: "🥊", golf: "⛳", racing: "🏎️", esports: "🎮", cricket: "🏏", crypto: "🪙",
                     commodities: "🛢️", finance: "📈", politics: "🏛️", other: "🎯" };

const ICON_BOOK = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 4h7a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H2z"/><path d="M22 4h-7a3 3 0 0 0-3 3v13a2 2 0 0 1 2-2h8z"/></svg>';
const ICON_CLIP = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="8" y="3" width="8" height="4" rx="1"/><path d="M16 5h2a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/></svg>';
const ICON_OUT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>';
const ICON_REFRESH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
const ICON_GEAR = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>';
const ICON_HELP = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9.5"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.2-2.4 3.8"/><path d="M12 17.2h0"/></svg>';
const ICON_FILTER = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>';

function loadUi() {
  const base = { minEv: 0, minQk: null, indep: false, basis: "mid", phase: null, q: "", minBet: null, window: null,
                 hideThin: false, thinSpread: 3, thinSize: 50, drawer: false, help: false,
                 sort: "time", dir: -1, kelly: 0.25, unitPct: 1, open: null, show: PAGE, settings: false, seen: null,
                 refreshing: null, lastTap: null, refreshNote: null };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
    const S = { ...base, ...saved, q: "", open: null, show: PAGE, settings: false, help: false, seen: null, refreshing: null, lastTap: null, refreshNote: null };
    if (!SORTS.some(([k]) => k === S.sort)) S.sort = "time";  // the old table also sorted by odds and legs
    return S;
  } catch (e) {
    return base;
  }
}

function saveUi(S) {
  try {
    const { q, open, show, settings, help, seen, refreshing, lastTap, refreshNote, ...keep } = S;
    localStorage.setItem(STORE, JSON.stringify(keep));
  } catch (e) { /* private mode etc. */ }
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (x) => typeof x === "number" && isFinite(x);

function odds(p) {
  if (!isNum(p) || p <= 0 || p >= 1) return "n/a";
  const o = p > 0.5 ? -Math.round((100 * p) / (1 - p)) : Math.round((100 * (1 - p)) / p);
  return o > 0 ? `+${o}` : `${o}`;
}
const pct = (x) => (isNum(x) ? `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "–");
// An EV as its pill prints it: whole percents from +10% so a big edge stays short, one decimal under that.
// Colors, tiers and the 🔥 go by this value too, so two pills that read the same look the same.
function evShown(x) {
  if (!isNum(x)) return x;
  const d = Math.round(x * 1000) / 1000;
  return Math.abs(d) >= 0.1 ? Math.round(x * 100) / 100 : d;
}
const ZERO = 0.0005;
// The pill's text, with a tiny edge either way as +0.0% (same text as Home's badge).
function pctShort(x) {
  const d = evShown(x);
  if (!isNum(d)) return "–";
  if (Math.abs(d) < ZERO) return "+0.0%";
  return Math.abs(d) >= 0.1 ? `${d >= 0 ? "+" : ""}${Math.round(d * 100).toLocaleString()}%` : pct(d);
}
function ago(ms) {
  const m = Math.max(0, (Date.now() - ms) / 60000);
  return m < 1 ? "now" : m < 60 ? `${Math.floor(m)}m` : `${Math.floor(m / 60)}h${String(Math.floor(m % 60)).padStart(2, "0")}`;
}
const agoTxt = (ms) => (ago(ms) === "now" ? "just now" : `${ago(ms)} ago`);
// Clock time in the phone's own format ("12:45 AM"), with the weekday when it isn't today ("Sat 7:05 PM").
function clock(ms, now = Date.now()) {
  if (!isNum(ms)) return "";
  const d = new Date(ms);
  const t = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date(now).toDateString() ? t : `${d.toLocaleDateString(undefined, { weekday: "short" })} ${t}`;
}
const cents = (x) => (isNum(x) ? `${Math.round(x * 100)}¢` : "—");
const money = (x) => (isNum(x) ? `${x < 0 ? "-" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "–");
const qty = (n) => n.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(n) ? 0 : 1 });
// Short forms for the cards: $692, $3,712, $17k, $1.2M · 4.58x
function money0(x) {
  if (!isNum(x)) return "–";
  const a = Math.abs(x);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(1)}M` : a >= 1e4 ? `${Math.round(a / 1e3)}k` : Math.round(a).toLocaleString();
  return `${x < 0 ? "-" : ""}$${s}`;
}
function mult(p) {
  if (!isNum(p) || p <= 0) return "–";
  const m = 1 / p;
  return `${m >= 100 ? Math.round(m) : m.toFixed(m >= 10 ? 1 : 2)}x`;
}

function units(S, kelly) {
  return isNum(kelly) ? (kelly * S.kelly * 100) / S.unitPct : null;
}
function fmtU(u) {
  if (!isNum(u)) return "–";
  return u >= 10 ? `${Math.round(u)}u` : `${u.toFixed(2)}u`;
}

// How strong an edge looks, 0..1 on a log scale: +1% ≈ 0.1, +10% ≈ 0.36, +100% ≈ 0.69, +800% = 1.
// evHeat and evStyle give the same colors as the Home page's EV badge (tests compare them with home.js).
function evHeat(x) {
  if (!isNum(x) || x <= 0) return 0;
  return Math.min(1, Math.log1p(x * 100) / Math.log1p(800));
}
const H10 = evHeat(0.1);
const T20 = (evHeat(0.2) - H10) / (1 - H10);
// From +10%: deep green with light text, then from +20% a jump to bright green with dark text that keeps
// brightening and turns yellower, up to neon chartreuse at +800%. The step at +20% keeps both text colors
// well clear of the low-contrast middle; the hue moves most at the top so +350% and +800% still differ.
function evGreen(x) {
  const t = (evHeat(x) - H10) / (1 - H10);
  const h = 160 - 80 * t ** 1.5;
  if (x < 0.2) { const u = t / T20; return { h, s: 64 + 6 * u, l: 23 + 3 * u }; }
  const u = (t - T20) / (1 - T20);
  return { h, s: 70 + 30 * u ** 0.8, l: 37 + 18 * u };
}
const hsl = (g, a) => `hsl(${g.h.toFixed(0)} ${g.s.toFixed(0)}% ${g.l.toFixed(0)}%${a === undefined ? "" : ` / ${a}`})`;
const evDark = (x) => evShown(x) >= 0.2;  // the bright greens take dark text
// A pale mint wash under +10%, then solid green that brightens with the edge, up to neon with a
// glow from +200% (where CHECK starts), so a huge edge is obvious at a glance.
function evStyle(x) {
  x = evShown(x);
  const h = evHeat(x);
  if (!(x >= 0.1)) {  // under +10%: tint only
    const a = (0.1 + 0.3 * h).toFixed(2);
    return `background:rgba(46,230,166,${a});color:#b9f7dc;box-shadow:inset 0 0 0 1px rgba(46,230,166,0.28)`;
  }
  const g = evGreen(x);
  const fg = evDark(x) ? "#00170c" : "#f2fff9";
  const glow = x >= 2 ? `0 0 ${Math.round(24 * h)}px hsl(${g.h.toFixed(0)} 100% 50% / ${(0.6 * h).toFixed(2)})` : "none";
  return `background:${hsl(g)};color:${fg};box-shadow:${glow}`;
}
// A card's tier: its stripe, border and glow step up with the edge.
function evTier(x) {
  x = evShown(x);
  if (!isNum(x)) return "none";
  if (Math.abs(x) < ZERO) return "zero";  // shows as +0.0%: no edge either way
  if (x < 0) return "neg";
  if (x < 0.1) return "t0";
  if (x < 1) return "t1";
  if (x < 2) return "t2";
  return x >= 5 ? "t4" : "t3";  // hot from +200% (Home's 🔥, and CHECK), white-hot from +500%
}
// The card's stripe (--hc) and border (--hb) colors. Computed here rather than with CSS color-mix,
// which iOS Safari only has from 16.2.
function evCardVars(x, tier) {
  x = evShown(x);
  if (!tier.startsWith("t")) return "";
  if (tier === "t0") return `--hc:rgba(46,230,166,${(0.3 + 0.4 * evHeat(x)).toFixed(2)})`;
  const g = evGreen(x);
  return `--hc:${hsl(g)};--hb:${hsl(g, { t1: 0.18, t2: 0.4, t3: 0.65, t4: 1 }[tier])}`;
}

function betCard(S, r) {
  const lines = [`${r.side.toUpperCase()} Combo ${odds(r.price)}`, RULE];
  for (const l of r.legs) lines.push(`${l.s.toUpperCase()} ${l.n} (${l.e})`);
  lines.push(RULE);
  const line = (name, ev, fair, k) => (isNum(ev) && isNum(fair)
    ? `EV ${name} ${pct(ev)} (fair ${odds(fair)} / ${mult(fair)}) · ${fmtU(units(S, k))}`
    : `EV ${name} n/a (a leg has no quote)`);
  lines.push(line("MID", r.evm, r.fm, r.km), line("CRX", r.evc, r.fc, r.kc));
  return lines.join("\n");
}

// Copies text and says so on the button that was tapped (`btn`), or in a toast.
function copyText(text, root, label, btn) {
  const done = (ok) => (!ok ? toast(root, "Couldn't copy") : btn ? flash(btn, label) : toast(root, label));
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => done(true), () => done(fallbackCopy(text)));
  } else {
    done(fallbackCopy(text));
  }
}
// Swaps a button's label for a moment. The eye is already there, and a toast would cover the bet's buttons.
function flash(btn, label) {
  if (btn._label === undefined) btn._label = btn.innerHTML;
  btn.style.minWidth = `${btn.offsetWidth}px`;
  btn.innerHTML = label;
  btn.classList.add("done");
  clearTimeout(btn._h);
  btn._h = setTimeout(() => {
    btn.innerHTML = btn._label;
    btn._label = undefined;
    btn.classList.remove("done");
    btn.style.minWidth = "";
  }, 1400);
}
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
function toast(root, text) {
  const t = root.querySelector(".toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 1400);
}

// Leg, event, league and sport names arrive once in data.strs; rows refer to them by index.
function hydrate(data) {
  const strs = data.strs || [];
  const str = (v) => (typeof v === "number" ? strs[v] : v);
  const fix = (legs) => legs.map((l) => ({ ...l, n: str(l.n), e: str(l.e), lg: str(l.lg), spt: str(l.spt) }));
  const deep = {};
  for (const [k, v] of Object.entries(data.deep || {})) deep[k] = { ...v, legs: fix(v.legs) };
  const rows = data.rows.map((r) => {
    const legs = fix(r.legs);
    // What the search box matches: leg names, events, leagues and sports.
    const hay = legs.map((l) => `${l.n} ${l.e} ${l.lg || ""} ${l.spt || ""}`).join(" ").toLowerCase();
    return { ...r, id: `${r.ticker}|${r.side}`, legs, hay };
  });
  return { ...data, rows, deep };
}

// A leg's book is thin when its spread is wide or either side of the top of book is small,
// which makes its mid (and so the combo's fair price) unreliable. Cutoffs are in settings.
function legThin(S, l) {
  return !isNum(l.sp) || l.sp * 100 > S.thinSpread + 1e-6 || !(l.z >= S.thinSize);
}
const rowThin = (S, r) => r.legs.some((l) => legThin(S, l));

// When a pre-match combo's first game starts: "starts in 25m" within the hour, then "starts 7:05 PM",
// or "Sat 7:05 PM" on another day.
function startsIn(ms, now = Date.now()) {
  if (!isNum(ms)) return "";
  const m = (ms - now) / 60000;
  if (m <= 0) return "starting";
  if (m < 60) return `starts in ${Math.max(1, Math.floor(m))}m`;
  return new Date(ms).toDateString() === new Date(now).toDateString() ? `starts ${clock(ms, now)}` : clock(ms, now);
}
const firstStart = (r) => r.legs.reduce((a, l) => (isNum(l.st) && (a === null || l.st < a) ? l.st : a), null);

// The legs' leagues in order, each once: [["NBA", "basketball"], ["MLB", "baseball"]]
function leagues(r) {
  const seen = new Map();
  for (const l of r.legs) if (l.lg && !seen.has(l.lg)) seen.set(l.lg, l.spt);
  return [...seen];
}
function sportChips(r, max) {
  const all = leagues(r);
  const shown = all.length > max ? all.slice(0, max - 1) : all;
  const chip = ([lg, spt]) => `<span class="lg">${SPORT_ICON[spt] || SPORT_ICON.other} ${esc(lg)}</span>`;
  return shown.map(chip).join("") + (all.length > shown.length ? `<span class="lg plus">+${all.length - shown.length}</span>` : "");
}

function evOf(S, r) { return S.basis === "mid" ? r.evm : r.evc; }
function kOf(S, r) { return S.basis === "mid" ? r.km : r.kc; }

// Does a row pass the filters? `skip` leaves one out, to count what its other option would show.
function keep(S, words, skip) {
  return (r) => {
    const ev = evOf(S, r);
    if (S.minEv !== null && !(isNum(ev) && ev * 100 >= S.minEv)) return false;
    if (S.minQk !== null && !(units(S, kOf(S, r)) >= S.minQk)) return false;
    if (skip !== "indep" && S.indep && r.sg > 0) return false;
    if (skip !== "thin" && S.hideThin && rowThin(S, r)) return false;
    if (skip !== "phase" && S.phase === "pre" && r.live) return false;
    if (skip !== "phase" && S.phase === "live" && !r.live) return false;
    return words.every((w) => r.hay.includes(w));
  };
}
const searchWords = (S) => S.q.trim().toLowerCase().split(/\s+/).filter(Boolean);

function visibleRows(S, data) {
  const rows = data.rows.filter(keep(S, searchWords(S)));
  const key = {
    time: (r) => r.t,
    stake: (r) => r.stake,
    ev: (r) => (isNum(evOf(S, r)) ? evOf(S, r) : -Infinity),
    qk: (r) => (isNum(kOf(S, r)) ? kOf(S, r) : -Infinity),
  }[S.sort] || ((r) => r.t);
  rows.sort((a, b) => {
    const ka = key(a), kb = key(b);
    if (ka === -Infinity || kb === -Infinity) return (ka === -Infinity) - (kb === -Infinity);  // no EV/QK: last
    return S.dir * (ka - kb);
  });
  return rows;
}

// How many bets each option of the two-way toggles would show, with every other filter as set.
function counts(S, data) {
  const words = searchWords(S);
  const byPhase = data.rows.filter(keep(S, words, "phase"));
  const live = byPhase.filter((r) => r.live).length;
  return {
    all: byPhase.length, live, pre: byPhase.length - live,
    indep: data.rows.filter(keep(S, words, "indep")).filter((r) => !r.sg).length,
    thin: data.rows.filter(keep(S, words, "thin")).filter((r) => !rowThin(S, r)).length,
  };
}

// A segmented control: one rounded track with the chosen option lit. opts: [value, label, count key]
function seg(act, opts, current) {
  return opts.map(([v, label, n]) => `<button class="${v === current ? "on" : ""}" data-act="${act}" data-v="${v ?? ""}">${label}`
    + `${n ? `<span class="n" data-n="${n}"></span>` : ""}</button>`).join("");
}

function shell(root) {
  root.innerHTML = `
    <div class="top">
      <h1>Combos</h1><span class="sp"></span>
      <button class="iconbtn" data-act="help" title="How to read a bet">${ICON_HELP}</button>
      <button class="iconbtn" data-act="settings" title="Kelly and thin-book settings">${ICON_GEAR}</button>
      <button class="iconbtn" data-act="refresh" title="Refresh now">${ICON_REFRESH}</button>
      <div class="meta"><span class="dot"></span><span data-ref="updated"></span></div>
    </div>
    <div class="legend" data-ref="legend">${legendHtml()}</div>
    <div class="filters">
      <div class="search"><label class="box"><input type="search" placeholder="Search legs, leagues…" data-ref="q" autocomplete="off">
        <span class="count" data-ref="count"></span></label>
        <button class="iconbtn fbtn" data-act="drawer" title="More filters">${ICON_FILTER}<span class="badge" data-ref="badge"></span></button></div>
      <div class="seg big" data-ref="phase"></div>
      <div class="frow"><span class="flabel">Min EV</span><div class="seg" data-ref="minEv"></div></div>
      <div class="drawer" data-ref="drawer">
        <div class="frow"><span class="flabel">Min QK</span><div class="seg" data-ref="minQk"></div></div>
        <div class="frow"><span class="flabel">Min bet</span><div class="seg" data-ref="minBet"></div></div>
        <div class="frow"><span class="flabel">Window</span><div class="seg" data-ref="window"></div></div>
        <div class="frow"><span class="flabel">Games</span><div class="seg" data-ref="indep"></div></div>
        <div class="frow"><span class="flabel">Quotes</span><div class="seg" data-ref="thin"></div></div>
      </div>
      <div class="settings" data-ref="settings">
        <label>Kelly <select data-ref="kelly">${KELLY.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>
        <label>1u = <input type="number" min="0.1" step="0.1" data-ref="unitPct"> % bankroll</label>
        <label>Thin leg: spread over <input type="number" min="0" step="1" data-ref="thinSpread">¢</label>
        <label>or top of book under <input type="number" min="0" step="10" data-ref="thinSize"> contracts</label>
      </div>
      <div class="note" data-ref="note"></div>
      <div class="sortbar"><div class="seg" data-ref="sort"></div><div class="seg" data-ref="basis"></div></div>
    </div>
    <div class="list" data-ref="rows"></div>
    <div class="toast"></div>`;
}

// The "?" panel: how to read a bet card, with the EV colors themselves as the key.
function legendHtml() {
  const pill = (x, small) => `<span class="evp ${evTier(x)}${evDark(x) ? " dk" : ""}" style="${evStyle(x)}"><b>${pctShort(x)}</b>`
    + `${small === undefined ? "" : `<i>/</i><span>${pctShort(small)}</span>`}</span>`;
  return `<div class="lrow">${pill(0.21, 0.13)}<span>EV <b>mid</b> / EV <b>cross</b>. The big one is what the Mid / Cross
      switch picks: it sets the color, Min EV, Min QK and the EV and QK sorts. The other gets a red chip when it's
      negative (a green one when it's positive on a red pill).</span></div>
    <div class="ramp">${[0.05, 0.15, 0.3, 1, 2.5, 8].map((x) => pill(x)).join("")}</div>
    <p>Pale under +10%, deep green from +10%, bright from +20% and neon by +800%: 🔥 from +200%, a white ring from
      +500%. Edges from +200% are usually a stale leg book, so they carry <em class="wn">⚠ CHECK</em>.</p>
    <p><b>$232</b> @ <b>21.6x</b> the bet and the multiplier it got · <i>fair</i> <b>17.8x</b> / <b>19.0x</b> the fair
      multipliers at mid / cross (EV = paid ÷ fair − 1) · <b>12:45 AM</b> when it was bet · <b>2.73u</b> Kelly units to
      bet (set Kelly and unit size with the gear) · <b class="c">$500+</b> bets in cyan ·
      <em class="no">NO</em> the bettor took NO · <em class="sg">SG</em> two legs on one game · <em class="wn">⚠ THIN</em> a
      leg's book is wide or small · <em class="wn">IN BAND</em> the price sits inside the leg spreads ·
      <em class="wn">NO QUOTE</em> a leg has no market.
      Tap a bet for its legs, American odds and Bet in Kalshi.</p>`;
}

const WARN_ORDER = ["CHECK", "NO QUOTE", "THIN", "IN BAND"];  // most important first, in case the line runs out

function rowHtml(S, data, r) {
  const ev = evOf(S, r);
  const tier = evTier(ev);
  const green = tier.startsWith("t");
  const u = units(S, kOf(S, r));
  const mid = S.basis === "mid";
  // EV mid / EV cross. The one the Mid / Cross switch picks is big and colors the pill; the other
  // gets a red (or green) chip when its sign disagrees, so a negative cross never reads as green.
  const other = evShown(mid ? r.evc : r.evm);
  const clash = !isNum(other) ? "" : (green || tier === "zero") && other <= -ZERO ? "lo"
    : (tier === "neg" || tier === "zero") && other >= ZERO ? "hi" : "";
  const fig = (x, big) => (big ? `<b>${pctShort(x)}</b>` : `<span class="${clash}">${pctShort(x)}</span>`);
  const pill = `<span class="evp ${tier}${evDark(ev) ? " dk" : ""}"${green ? ` style="${evStyle(ev)}"` : ""}>`
    + `${fig(r.evm, mid)}<i>/</i>${fig(r.evc, !mid)}</span>`;
  const fire = tier === "t3" || tier === "t4" ? '<span class="fire">🔥</span>' : "";
  const warns = [...new Set([...r.flags.split(" · "), rowThin(S, r) ? "THIN" : ""])]
    .filter((f) => WARN_ORDER.includes(f)).sort((a, b) => WARN_ORDER.indexOf(a) - WARN_ORDER.indexOf(b));
  const tags = (r.side === "no" ? '<em class="no">NO</em>' : "") + (r.sg ? '<em class="sg">SG</em>' : "")
    + (warns.length ? `<em class="wn">⚠ ${warns.join(" · ")}</em>` : "");
  const legs = r.legs.map((l) => `${l.s === "no" ? "NO " : ""}${l.n}`).join(" · ");
  const st = firstStart(r);
  const when = r.live ? '<span class="live"><i></i>LIVE</span>'
    : st !== null ? `<span class="pre" data-st="${st}">${startsIn(st)}</span>` : "";
  // The fair multipliers behind EV mid / EV cross; the one the switch picks is brighter, as in the pill.
  const fair = (p, on) => `<span${on ? ' class="on"' : ""}>${mult(p)}</span>`;
  const open = S.open === r.id;
  // Three lines: the bet and the multiplier it got against its EV pill, then the fair multipliers against
  // the time it was bet, then tags, leagues and legs against LIVE or the start time.
  return `<div class="row ${tier}${open ? " open" : ""}" data-id="${esc(r.id)}" style="${evCardVars(ev, tier)}">
    <div class="card" data-act="toggle">
      <div class="c1"><span class="bet"><b class="stake${r.stake >= BIG_BET ? " big" : ""}">${money0(r.stake)}</b><i>@</i><b class="m">${mult(r.price)}</b></span>
        <span class="evx">${u >= 0.005 ? `<span class="u">${fmtU(u)}</span>` : ""}${pill}${fire}</span></div>
      <div class="c2"><span class="fair"><i>fair</i>${fair(r.fm, mid)}<i>/</i>${fair(r.fc, !mid)}</span>
        <span class="tm"><b>${clock(r.t)}</b><span class="age" data-t="${r.t}">${agoTxt(r.t)}</span></span></div>
      <div class="c3"><div class="ctx"><span class="tg">${tags}${sportChips(r, tags ? 2 : 3)}</span>`
        + `<span class="pv"><i>${r.L} legs · </i>${esc(legs)}</span></div>${when}</div>
    </div>
    ${open ? detailHtml(S, data, r) : ""}
  </div>`;
}

function detailHtml(S, data, r) {
  const deep = (data.deep || {})[r.id];
  const legs = (deep ? deep.legs : r.legs).map((l) => {
    const thin = legThin(S, l);
    const when = l.lv ? '<span class="st lv">LIVE</span>' : l.st ? `<span class="st">${clock(l.st)}</span>` : "";
    const lg = l.lg ? `${SPORT_ICON[l.spt] || SPORT_ICON.other} ${esc(l.lg)} · ` : "";
    return `<div class="leg"><span class="s ${l.s}">${l.s.toUpperCase()}</span>
      <span class="n"><b>${esc(l.n)}</b> ${when}<i>${lg}${esc(l.e)}</i></span>
      <span class="q"><span class="o">${odds(l.m)}</span><span class="${thin ? "thin" : ""}">${cents(l.b)} / ${cents(l.a)}${thin ? "*" : ""}</span></span></div>`;
  }).join("");
  // Paid vs fair: the multiplier the bettor got beside the fair ones behind EV mid and EV cross.
  const uOf = (k) => { const u = units(S, k); return u >= 0.005 ? ` · ${fmtU(u)}` : ""; };  // as on the card
  const tile = (label, p, extra, cls) => `<div class="tile ${cls}"><span>${label}</span><b>${mult(p)}</b>`
    + `<i>${isNum(p) && p > 0 && p < 1 ? odds(p) : "–"}${extra}</i></div>`;
  const tiles = tile("Paid", r.price, "", "paid") + tile("Fair mid", r.fm, uOf(r.km), S.basis === "mid" ? "on" : "")
    + tile("Fair cross", r.fc, uOf(r.kc), S.basis === "cross" ? "on" : "");
  const flagList = [r.flags, rowThin(S, r) ? "THIN" : ""].filter(Boolean).join(" · ");
  const flags = flagList ? ` · <span class="w">${esc(flagList)}</span>` : "";
  const hrs = isNum(r.hrs) ? `${Math.max(0, r.hrs).toFixed(0)}h` : "–";
  let deepLine = "";
  if (deep) {
    deepLine = `<br>Full book at ${qty(r.n)} contracts: CRX <b>${pct(deep.evs)}</b> · thinnest leg ${isNum(deep.dr) ? deep.dr.toFixed(2) + "×" : "–"} of the bet size`;
  } else if (data.depthError && data.depthError.id === r.id) {
    deepLine = `<br><span class="w">Couldn't load the full book: ${esc(data.depthError.msg)}</span>`;
  }
  return `<div class="detail">
    ${legs}
    <div class="tiles">${tiles}</div>
    <div class="facts">${r.side.toUpperCase()} · bet <b>${money(r.stake)}</b> at ${clock(r.t)} · <b>${qty(r.n)}</b> contracts at <b>$${r.price.toFixed(4)}</b> (${odds(r.price)}) · pays <b>${money(r.n)}</b><br>Net after fees <b>${money(r.net)}</b> (fee ${money(r.fee)})
      · resolves in ${hrs}${flags}${deepLine}</div>
    <div class="actions go">
      <a class="btn primary" href="${esc(betUrl(r.ticker))}" target="_blank" rel="noopener">Bet in Kalshi ${ICON_OUT}</a>
      <button class="btn" data-act="quote" title="Get a live quote on the Quote page">Quote it</button>
    </div>
    ${r.side === "no" ? '<div class="sidenote">This bet is <b>NO</b> on the combo. Check the side in Kalshi before you buy.</div>' : ""}
    <div class="actions">
      <span class="bk">${isNum(r.cb) || isNum(r.ca) ? `combo bid ${cents(r.cb)}<br>combo ask ${cents(r.ca)}` : "no resting<br>combo orders"}</span>
      <button class="btn" data-act="depth" title="Load full leg orderbooks">${ICON_BOOK}</button>
      <button class="btn" data-act="ticker" title="Copy combo ticker">${ICON_CLIP}</button>
      <button class="btn" data-act="card">Copy bet card</button>
    </div>
  </div>`;
}

export { betCard, betUrl, clock, counts, evHeat, evShown, evStyle, evTier, hydrate, loadUi, pctShort, startsIn, visibleRows };  // for tests/test_table_js.py

export default function (component) {
  const { parentElement, setStateValue, setTriggerValue } = component;
  const data = hydrate(component.data);
  const S = (window.__cevUi = window.__cevUi || loadUi());
  const ctx = (window.__cevCtx = { data, setStateValue, setTriggerValue });

  let root = parentElement.querySelector(".cev");
  if (!root) {
    root = document.createElement("div");
    root.className = "cev combos";  // .combos scopes this page's styles away from the Bettors page
    parentElement.appendChild(root);
    shell(root);
    bind(root);
  }
  const $ = (name) => root.querySelector(`[data-ref="${name}"]`);

  function paintControls() {
    const D = window.__cevCtx.data;
    $("phase").innerHTML = seg("phase", [["pre", "Pre-match", "pre"], ["live", '<span class="ld"></span>Live', "live"],
                                         ["all", "All", "all"]], S.phase || "all");
    $("minEv").innerHTML = seg("minEv", MIN_EV.map((v) => [v, v === null ? "Any" : `${v}%`]), S.minEv);
    $("minQk").innerHTML = seg("minQk", MIN_QK.map((v) => [v, v === null ? "Any" : `${v}u`]), S.minQk);
    $("minBet").innerHTML = seg("minBet", MIN_BET.map((v) => [v, `$${v}`]), D.minBet);
    $("window").innerHTML = seg("window", WINDOWS, D.window);
    $("indep").innerHTML = seg("indep", [["0", "All"], ["1", "Indep. only", "indep"]], S.indep ? "1" : "0");
    $("thin").innerHTML = seg("thin", [["0", "All"], ["1", "Hide thin", "thin"]], S.hideThin ? "1" : "0");
    $("sort").innerHTML = seg("sort", SORTS.map(([k, l]) => [k, k === S.sort ? `${l} ${S.dir < 0 ? "↓" : "↑"}` : l]), S.sort);
    $("basis").innerHTML = `<span class="sl">EV</span>${seg("basis", [["mid", "Mid"], ["cross", "Cross"]], S.basis)}`;
    // The badge counts the drawer's filters that hide bets; window and min bet show in the status line.
    const active = (S.minQk !== null) + S.indep + S.hideThin;
    $("badge").textContent = active ? String(active) : "";
    root.querySelector('[data-act="drawer"]').classList.toggle("on", S.drawer);
    $("drawer").classList.toggle("open", S.drawer);
    root.querySelector('[data-act="settings"]').classList.toggle("on", S.settings);
    root.querySelector('[data-act="help"]').classList.toggle("on", S.help);
    $("legend").classList.toggle("open", S.help);
    $("settings").classList.toggle("open", S.settings);
    $("kelly").value = String(S.kelly);
    $("unitPct").value = String(S.unitPct);
    $("thinSpread").value = String(S.thinSpread);
    $("thinSize").value = String(S.thinSize);
    if ($("q").value !== S.q) $("q").value = S.q;
  }

  // Endless scroll: render PAGE rows at a time and append the next batch when the
  // sentinel under the last row comes within a screen of view.
  function moreHtml(rows) {
    const left = rows.length - S.show;
    return left > 0 ? `<button class="more" data-act="more">Loading more… (${left.toLocaleString()} left)</button>` : "";
  }

  function watchSentinel() {
    if (!("IntersectionObserver" in window)) return;  // the sentinel is also a button
    if (!root._io) {
      root._io = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) root._loadMore();
      }, { rootMargin: "800px 0px" });
    }
    root._io.disconnect();
    const s = root.querySelector(".more");
    if (s) root._io.observe(s);
  }

  function paintRows() {
    const D = window.__cevCtx.data;
    const rows = (root._rows = visibleRows(S, D));
    $("count").innerHTML = `${rows.length.toLocaleString()} bet${rows.length === 1 ? "" : "s"}<span class="fl"> · ${D.trades.toLocaleString()} fills</span>`;
    const n = counts(S, D);
    root.querySelectorAll("[data-n]").forEach((el) => { el.textContent = n[el.dataset.n].toLocaleString(); });
    const shown = rows.slice(0, S.show);
    $("rows").innerHTML = shown.length
      ? shown.map((r) => rowHtml(S, D, r)).join("") + moreHtml(rows)
      : emptyHtml(D);
    watchSentinel();
  }

  // Nothing to show: say which of the filters you set could bring bets back.
  function emptyHtml(D) {
    if (!D.rows.length) {
      return `<div class="empty">${LOGO}<b>No bets yet</b>Nobody has bet $${D.minBet.toLocaleString()}+ on a combo in this window.
        Tap ↻ to check again, or try a lower Min bet.</div>`;
    }
    const tips = [
      S.q.trim() && "clear the search",
      S.minEv > 0 && "lower Min EV",
      S.minEv === 0 && "set Min EV to Any to see negative EV too",
      S.minQk !== null && "set Min QK to Any",
      S.hideThin && "show thin books",
      S.indep && "show same-game combos",
      S.phase && `pick All instead of ${S.phase === "live" ? "Live" : "Pre-match"}`,
    ].filter(Boolean);
    const tip = tips.length > 1 ? `${tips.slice(0, -1).join(", ")} or ${tips[tips.length - 1]}` : tips[0] || "try a lower Min bet";
    return `<div class="empty">${LOGO}<b>No bets match these filters</b>${tip[0].toUpperCase() + tip.slice(1)}.</div>`;
  }

  function loadMore() {
    const D = window.__cevCtx.data;
    const rows = root._rows || [];
    if (S.show >= rows.length) return;
    const next = rows.slice(S.show, S.show + PAGE);
    S.show += next.length;
    const old = root.querySelector(".more");
    if (old) old.remove();
    $("rows").insertAdjacentHTML("beforeend", next.map((r) => rowHtml(S, D, r)).join("") + moreHtml(rows));
    watchSentinel();
  }

  function paintMeta() {
    const D = window.__cevCtx.data;
    const ageMin = (Date.now() - D.updated) / 60000;
    const win = (WINDOWS.find(([v]) => v === D.window) || [0, `${D.window}m`])[1];
    const state = S.refreshing ? "refreshing…" : `updated ${agoTxt(D.updated)}`;
    $("updated").textContent = `${D.demo ? "demo · " : ""}${state} · ${win} · $${D.minBet.toLocaleString()}+ bets`;
    root.querySelector(".dot").classList.toggle("stale", ageMin > 5 || !!D.error);
    root.querySelector('[data-act="refresh"]').classList.toggle("spin", !!S.refreshing);
    $("note").textContent = [D.error, S.refreshNote, D.note].filter(Boolean).join(" ");
  }

  // Scores update in the background; the page only changes when you tap refresh or a
  // filter. Keep the "updated", "ago" and "starts in" labels honest in the meantime.
  if (!root._tick) {
    root._tick = setInterval(() => {
      paintMeta();
      root.querySelectorAll("[data-t]").forEach((el) => { el.textContent = agoTxt(+el.dataset.t); });
      root.querySelectorAll("[data-st]").forEach((el) => { el.textContent = startsIn(+el.dataset.st); });
    }, 15000);
  }

  // Min bet and window live in Python (they change what gets scored). On a fresh page
  // load, send the ones this phone used last time.
  if (!window.__cevSynced) {
    window.__cevSynced = true;
    if (S.minBet !== null && S.minBet !== data.minBet) setStateValue("min_bet", S.minBet);
    if (S.window !== null && S.window !== data.window) setStateValue("window", S.window);
  }

  // The refresh icon spins until Python echoes the tap back with the new scores.
  if (isNum(data.refreshed) && data.refreshed === S.lastTap) {
    S.refreshing = null;
    S.refreshNote = null;
  }

  // Every refresh shows the newest bets first; combo edges die fast.
  if (S.seen !== data.updated) {
    S.seen = data.updated;
    S.sort = "time";
    S.dir = -1;
    S.show = PAGE;
  }

  root._paint = () => { paintControls(); paintMeta(); paintRows(); };
  root._paintMeta = paintMeta;
  root._loadMore = loadMore;
  root._paintRows = paintRows;
  root._paintControls = paintControls;
  root._paint();

  function bind(el) {
    const update = (fn, rowsOnly = false) => {
      fn();
      saveUi(S);
      if (!rowsOnly) el._paintControls();
      el._paintRows();
    };
    el.addEventListener("input", (e) => {
      const ref = e.target.dataset.ref;
      if (ref === "q") update(() => { S.q = e.target.value; S.show = PAGE; }, true);
      if (ref === "unitPct") {
        const v = parseFloat(e.target.value);
        if (v > 0) update(() => { S.unitPct = v; }, true);
      }
      if (ref === "thinSpread" || ref === "thinSize") {
        const v = parseFloat(e.target.value);
        if (v >= 0) update(() => { S[ref] = v; S.show = PAGE; }, true);
      }
    });
    el.addEventListener("change", (e) => {
      if (e.target.dataset.ref === "kelly") update(() => { S.kelly = parseFloat(e.target.value); }, true);
    });
    el.addEventListener("click", (e) => {
      const ctxNow = window.__cevCtx;
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const v = b.dataset.v;
      const row = b.closest(".row");
      const r = row && ctxNow.data.rows.find((x) => x.id === row.dataset.id);
      switch (b.dataset.act) {
        case "phase": return update(() => { S.phase = v === "all" ? null : v; S.show = PAGE; });
        case "minEv": return update(() => { S.minEv = v === "" ? null : +v; S.show = PAGE; });
        case "minQk": return update(() => { S.minQk = v === "" ? null : +v; S.show = PAGE; });
        case "indep": return update(() => { S.indep = v === "1"; S.show = PAGE; });
        case "thin": return update(() => { S.hideThin = v === "1"; S.show = PAGE; });
        case "basis": return update(() => { S.basis = v; S.show = PAGE; });
        case "sort": return update(() => { S.dir = S.sort === v ? -S.dir : -1; S.sort = v; S.show = PAGE; });
        case "drawer": return update(() => { S.drawer = !S.drawer; });
        case "settings": return update(() => { S.settings = !S.settings; });
        case "help": return update(() => { S.help = !S.help; });
        case "more": return el._loadMore();
        case "toggle": {
          update(() => { S.open = S.open === row.dataset.id ? null : row.dataset.id; }, true);
          // Closing a bet above this one pulls this one up; keep its top on screen.
          const opened = S.open && el.querySelector(".row.open");
          if (opened && opened.getBoundingClientRect().top < 0) opened.scrollIntoView({ block: "start" });
          return;
        }
        case "minBet": S.minBet = +v; saveUi(S); return ctxNow.setStateValue("min_bet", +v);
        case "window": S.window = +v; saveUi(S); return ctxNow.setStateValue("window", +v);
        case "refresh": {
          if (S.refreshing && Date.now() - S.refreshing < 5000) return;  // one tap is enough
          const tap = (S.refreshing = S.lastTap = Date.now());
          S.refreshNote = null;
          el._paintMeta();
          setTimeout(() => {
            if (S.refreshing !== tap) return;
            S.refreshing = null;
            S.refreshNote = "Refresh got no answer. Pull down to reload the page.";
            el._paintMeta();
          }, REFRESH_TIMEOUT);
          return ctxNow.setTriggerValue("refresh", tap);
        }
        case "depth": return r && ctxNow.setTriggerValue("depth", r.id);
        case "ticker": return r && copyText(r.ticker, el, "✓", b);
        case "card": return r && copyText(betCard(S, r), el, "Copied 🧋", b);
        case "quote": return r && ctxNow.setTriggerValue("quote", { t: r.ticker, side: r.side, p: r.price, n: r.n });
      }
    });
  }
}
