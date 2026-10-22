// Combo EV table: filters, sorting and expansion all run in the browser, so they're
// instant. Only min bet, window, refresh and "load full book" go back to Python.

const MIN_EV = [0, 5, 10, 20, 50];
const MIN_QK = [0.1, 0.2, 0.5, 1, 2];
const MIN_BET = [5, 10, 50, 100, 500];
const WINDOWS = [[15, "15m"], [60, "1h"], [180, "3h"]];
const KELLY = [[1, "Full"], [0.5, "½"], [0.25, "¼"], [0.125, "⅛"]];
const PAGE = 100;
const RULE = "────────────";
const STORE = "cev-ui-v1";

const ICON_BOOK = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 4h7a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H2z"/><path d="M22 4h-7a3 3 0 0 0-3 3v13a2 2 0 0 1 2-2h8z"/></svg>';
const ICON_CLIP = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="8" y="3" width="8" height="4" rx="1"/><path d="M16 5h2a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/></svg>';
const ICON_REFRESH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
const ICON_GEAR = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>';

function loadUi() {
  const base = { minEv: 0, minQk: null, indep: false, basis: "mid", phase: null, q: "", minBet: null, window: null,
                 sort: "evm", dir: -1, kelly: 0.25, unitPct: 1, open: null, show: PAGE, settings: false };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
    return { ...base, ...saved, q: "", open: null, show: PAGE, settings: false };
  } catch (e) {
    return base;
  }
}

function saveUi(S) {
  try {
    const { q, open, show, settings, ...keep } = S;
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
// Table cells drop the decimal on four-digit edges so they fit a phone column.
const pctShort = (x) => (isNum(x) && Math.abs(x) >= 10 ? `${x >= 0 ? "+" : ""}${Math.round(x * 100)}%` : pct(x));
const dollars = (x) => (isNum(x) ? `$${Math.round(x).toLocaleString()}` : "–");
function ago(ms) {
  const m = Math.max(0, (Date.now() - ms) / 60000);
  return m < 1 ? "now" : m < 60 ? `${Math.floor(m)}m ago` : `${Math.floor(m / 60)}h${String(Math.floor(m % 60)).padStart(2, "0")} ago`;
}
const cents = (x) => (isNum(x) ? `${Math.round(x * 100)}¢` : "—");
const money = (x) => (isNum(x) ? `${x < 0 ? "-" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "–");
const qty = (n) => n.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(n) ? 0 : 1 });

function units(S, kelly) {
  return isNum(kelly) ? (kelly * S.kelly * 100) / S.unitPct : null;
}
function fmtU(u) {
  if (!isNum(u)) return "–";
  return u >= 10 ? `${Math.round(u)}u` : `${u.toFixed(2)}u`;
}

function betCard(S, r) {
  const lines = [`${r.side.toUpperCase()} Combo ${odds(r.price)}`, RULE];
  for (const l of r.legs) lines.push(`${l.s.toUpperCase()} ${l.n} (${l.e})`);
  lines.push(RULE);
  const line = (name, ev, fair, k) => (isNum(ev) && isNum(fair)
    ? `EV ${name} ${pct(ev)} (fair ${odds(fair)}) · ${fmtU(units(S, k))}`
    : `EV ${name} n/a (a leg has no quote)`);
  lines.push(line("MID", r.evm, r.fm, r.km), line("CRX", r.evc, r.fc, r.kc));
  return lines.join("\n");
}

function copyText(text, root, label) {
  const done = () => toast(root, label);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text) && done());
  } else if (fallbackCopy(text)) {
    done();
  }
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

function evOf(S, r) { return S.basis === "mid" ? r.evm : r.evc; }
function kOf(S, r) { return S.basis === "mid" ? r.km : r.kc; }

function visibleRows(S, data) {
  const q = S.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let rows = data.rows.filter((r) => {
    const ev = evOf(S, r);
    if (S.minEv !== null && !(isNum(ev) && ev * 100 >= S.minEv)) return false;
    if (S.minQk !== null && !(units(S, kOf(S, r)) >= S.minQk)) return false;
    if (S.indep && r.sg > 0) return false;
    if (S.phase === "pre" && r.live) return false;
    if (S.phase === "live" && !r.live) return false;
    if (q.length) {
      const hay = r.legs.map((l) => `${l.n} ${l.e}`).join(" ").toLowerCase();
      if (!q.every((w) => hay.includes(w))) return false;
    }
    return true;
  });
  const key = {
    time: (r) => r.t,
    stake: (r) => r.stake,
    evm: (r) => (isNum(r.evm) ? r.evm : -Infinity),
    evc: (r) => (isNum(r.evc) ? r.evc : -Infinity),
    legs: (r) => r.L,
  }[S.sort];
  rows.sort((a, b) => S.dir * (key(a) - key(b)));
  return rows;
}

function shell(root) {
  root.innerHTML = `
    <div class="top">
      <span class="brand">COMBO EV</span><span class="dot"></span>
      <span class="meta" data-ref="updated"></span><span class="sp"></span>
      <button class="iconbtn" data-act="settings" title="Kelly settings">${ICON_GEAR}</button>
      <button class="iconbtn" data-act="refresh" title="Refresh now">${ICON_REFRESH}</button>
    </div>
    <div class="filters">
      <div class="search"><input type="search" placeholder="Filter by leg…" data-ref="q" autocomplete="off">
        <span class="count" data-ref="count"></span></div>
      <div class="frow"><span class="flabel">MIN EV</span><span data-ref="minEv"></span><span class="sp"></span>
        <button class="tog" data-act="indep"><span class="sw"></span>INDEP. ONLY</button></div>
      <div class="frow"><span class="flabel">MIN QK</span><span data-ref="minQk"></span><span class="sp"></span>
        <button class="tog" data-act="basis"><span class="sw"></span><span><span class="bm">MID</span> / <span class="bc">CROSS</span></span></button></div>
      <div class="frow"><span class="flabel">MIN BET</span><span data-ref="minBet"></span></div>
      <div class="frow"><span class="flabel">WINDOW</span><span data-ref="window"></span><span class="sp"></span>
        <button class="phase" data-act="phase" data-v="pre">PREGAME</button>
        <button class="phase" data-act="phase" data-v="live">LIVE</button></div>
      <div class="settings" data-ref="settings">
        <label>Kelly <select data-ref="kelly">${KELLY.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>
        <label>1u = <input type="number" min="0.1" step="0.1" data-ref="unitPct"> % bankroll</label>
      </div>
      <div class="note" data-ref="note"></div>
    </div>
    <div class="grid thead">
      <button data-sort="time">TIME<span class="clock" data-ref="clock"></span></button>
      <span>SIDE</span>
      <button data-sort="stake">PRICE</button>
      <button class="r" data-sort="evm">EV MID</button>
      <button class="r" data-sort="evc">EV CROSS</button>
      <button class="r" data-sort="legs">LEGS</button>
    </div>
    <div data-ref="rows"></div>
    <div class="toast"></div>`;
}

function chips(values, current, fmt, act) {
  return values.map((v) => `<button class="chip ${v === current ? "on" : ""}" data-act="${act}" data-v="${v}">${fmt(v)}</button>`).join(" ");
}

function rowHtml(S, data, r) {
  const tm = new Date(r.t).toLocaleTimeString(undefined, { hour12: false });
  const evCell = (ev, k) => `<div class="r"><div class="ev ${isNum(ev) ? (ev > 0 ? "pos" : "neg") : ""}">${pctShort(ev)}</div><div class="sub">${fmtU(units(S, k))}</div></div>`;
  const open = S.open === r.id;
  return `<div class="row ${open ? "open" : ""}" data-id="${esc(r.id)}">
    <div class="grid" data-act="toggle">
      <div class="time"><b>${esc(tm)}</b><div class="sub">${ago(r.t)}</div></div>
      <div class="side ${r.side}">${r.side.toUpperCase()}</div>
      <div class="price"><b>$${r.price.toFixed(4)}</b> <span class="x">×${qty(r.n)}</span>
        <div class="sub">${odds(r.price)} · ${dollars(r.stake)}</div></div>
      ${evCell(r.evm, r.km)}${evCell(r.evc, r.kc)}
      <div class="legs ${r.sg ? "sg" : ""}" title="${r.sg ? "Same-game legs" : ""}">${r.L}L/${r.G}G</div>
    </div>
    ${open ? detailHtml(S, data, r) : ""}
  </div>`;
}

function detailHtml(S, data, r) {
  const deep = (data.deep || {})[r.id];
  const legs = (deep ? deep.legs : r.legs).map((l) => {
    const thin = isNum(l.d) && l.d < 1;
    return `<div class="leg"><span class="s ${l.s}">${l.s.toUpperCase()}</span>
      <span class="n">${esc(l.n)} <i>(${esc(l.e)})</i></span>
      <span class="q"><span class="o">${odds(l.m)}</span><span class="${thin ? "thin" : ""}">${cents(l.b)} / ${cents(l.a)}${thin ? "*" : ""}</span></span></div>`;
  }).join("");
  const flags = r.flags ? ` · <span class="w">${esc(r.flags)}</span>` : "";
  const hrs = isNum(r.hrs) ? `${Math.max(0, r.hrs).toFixed(0)}h` : "–";
  let deepLine = "";
  if (deep) {
    deepLine = `<br>Full book at ${qty(r.n)} contracts: CRX <b>${pct(deep.evs)}</b> · thinnest leg ${isNum(deep.dr) ? deep.dr.toFixed(2) + "×" : "–"} of the bet size`;
  } else if (data.depthError && data.depthError.id === r.id) {
    deepLine = `<br><span class="w">Couldn't load the full book: ${esc(data.depthError.msg)}</span>`;
  }
  return `<div class="detail">
    ${legs}
    <div class="facts">Fair MID <b>${odds(r.fm)}</b> · CRX <b>${odds(r.fc)}</b> · net after fees <b>${money(r.net)}</b> (fee ${money(r.fee)})
      · resolves in ${hrs}${flags}${deepLine}</div>
    <div class="actions">
      <span class="bk">${isNum(r.cb) || isNum(r.ca) ? `combo bid ${cents(r.cb)}<br>combo ask ${cents(r.ca)}` : "no resting<br>combo orders"}</span>
      <button class="btn" data-act="depth" title="Load full leg orderbooks">${ICON_BOOK}</button>
      <button class="btn" data-act="ticker" title="Copy combo ticker">${ICON_CLIP}</button>
      <button class="btn primary" data-act="card">Copy bet card</button>
    </div>
  </div>`;
}

export { betCard };  // for tests/test_table_js.py

export default function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component;
  const S = (window.__cevUi = window.__cevUi || loadUi());
  const ctx = (window.__cevCtx = { data, setStateValue, setTriggerValue });

  let root = parentElement.querySelector(".cev");
  if (!root) {
    root = document.createElement("div");
    root.className = "cev";
    parentElement.appendChild(root);
    shell(root);
    bind(root);
  }
  const $ = (name) => root.querySelector(`[data-ref="${name}"]`);

  function paintControls() {
    const D = window.__cevCtx.data;
    $("minEv").innerHTML = chips(MIN_EV, S.minEv, (v) => `${v}%`, "minEv");
    $("minQk").innerHTML = chips(MIN_QK, S.minQk, (v) => `${v}u`, "minQk");
    $("minBet").innerHTML = chips(MIN_BET, D.minBet, (v) => `$${v}`, "minBet");
    $("window").innerHTML = WINDOWS.map(([v, l]) => `<button class="chip ${v === D.window ? "on" : ""}" data-act="window" data-v="${v}">${l}</button>`).join(" ");
    root.querySelector('[data-act="indep"]').classList.toggle("on", S.indep);
    root.querySelector('[data-act="basis"]').classList.toggle("on", S.basis === "cross");
    root.querySelectorAll('[data-act="phase"]').forEach((b) => b.classList.toggle("on", S.phase === b.dataset.v));
    root.querySelectorAll(".thead [data-sort]").forEach((b) => {
      const on = b.dataset.sort === S.sort;
      b.classList.toggle("on", on);
      b.dataset.arrow = on ? (S.dir < 0 ? " ↓" : " ↑") : "";
    });
    $("settings").classList.toggle("open", S.settings);
    $("kelly").value = String(S.kelly);
    $("unitPct").value = String(S.unitPct);
    if ($("q").value !== S.q) $("q").value = S.q;
  }

  function paintRows() {
    const D = window.__cevCtx.data;
    const rows = visibleRows(S, D);
    $("count").textContent = `${rows.length.toLocaleString()} bet${rows.length === 1 ? "" : "s"} · ${D.trades.toLocaleString()} fills`;
    const shown = rows.slice(0, S.show);
    $("rows").innerHTML = shown.length
      ? shown.map((r) => rowHtml(S, D, r)).join("") +
        (rows.length > shown.length ? `<button class="more" data-act="more">Show more (${(rows.length - shown.length).toLocaleString()} left)</button>` : "")
      : `<div class="empty">No bets match these filters. Lower MIN EV, MIN QK or MIN BET.</div>`;
  }

  function paintMeta() {
    const D = window.__cevCtx.data;
    const updated = new Date(D.updated);
    const ageMin = (Date.now() - updated.getTime()) / 60000;
    $("updated").textContent = `${D.demo ? "demo · " : ""}${D.scored.toLocaleString()} scored`;
    $("clock").textContent = updated.toLocaleTimeString(undefined, { hour12: false });
    root.querySelector(".dot").classList.toggle("stale", ageMin > 3 || !!D.error);
    $("note").textContent = [D.error, D.note].filter(Boolean).join(" ");
  }

  // Min bet and window live in Python (they change what gets scored). On a fresh page
  // load, send the ones this phone used last time.
  if (!window.__cevSynced) {
    window.__cevSynced = true;
    if (S.minBet !== null && S.minBet !== data.minBet) setStateValue("min_bet", S.minBet);
    if (S.window !== null && S.window !== data.window) setStateValue("window", S.window);
  }

  root._paint = () => { paintControls(); paintMeta(); paintRows(); };
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
    });
    el.addEventListener("change", (e) => {
      if (e.target.dataset.ref === "kelly") update(() => { S.kelly = parseFloat(e.target.value); }, true);
    });
    el.addEventListener("click", (e) => {
      const ctxNow = window.__cevCtx;
      const sortBtn = e.target.closest("[data-sort]");
      if (sortBtn) {
        const k = sortBtn.dataset.sort;
        return update(() => { S.dir = S.sort === k ? -S.dir : -1; S.sort = k; });
      }
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const v = b.dataset.v;
      const row = b.closest(".row");
      const r = row && ctxNow.data.rows.find((x) => x.id === row.dataset.id);
      switch (b.dataset.act) {
        case "minEv": return update(() => { S.minEv = S.minEv === +v ? null : +v; S.show = PAGE; });
        case "minQk": return update(() => { S.minQk = S.minQk === +v ? null : +v; S.show = PAGE; });
        case "indep": return update(() => { S.indep = !S.indep; });
        case "basis": return update(() => { S.basis = S.basis === "mid" ? "cross" : "mid"; });
        case "phase": return update(() => { S.phase = S.phase === v ? null : v; });
        case "settings": return update(() => { S.settings = !S.settings; });
        case "more": return update(() => { S.show += PAGE; }, true);
        case "toggle": return update(() => { S.open = S.open === row.dataset.id ? null : row.dataset.id; }, true);
        case "minBet": S.minBet = +v; saveUi(S); return ctxNow.setStateValue("min_bet", +v);
        case "window": S.window = +v; saveUi(S); return ctxNow.setStateValue("window", +v);
        case "refresh": return ctxNow.setTriggerValue("refresh", Date.now());
        case "depth": return r && ctxNow.setTriggerValue("depth", r.id);
        case "ticker": return r && copyText(r.ticker, el, "Ticker copied");
        case "card": return r && copyText(betCard(S, r), el, "Bet card copied");
      }
    });
  }
}
