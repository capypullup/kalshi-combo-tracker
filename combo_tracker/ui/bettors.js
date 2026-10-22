// Bettors table: public Kalshi profiles ranked by PnL, trades and EV against the book.
// Sorting, filters and expanding run in the browser; "track" and refresh go to Python.

const MIN_TRADES = [20, 50, 100, 300];
const PAGE = 60;
const STORE = "bettors-ui-v1";

const ICON_REFRESH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';

function loadUi() {
  const base = { scope: "tennis", src: "all", minN: null, hidePrivate: false, q: "", sort: "z", dir: -1, open: null, show: PAGE };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
    return { ...base, ...saved, q: "", open: null, show: PAGE };
  } catch (e) {
    return base;
  }
}

function saveUi(S) {
  try {
    const { q, open, show, ...keep } = S;
    localStorage.setItem(STORE, JSON.stringify(keep));
  } catch (e) { /* private mode etc. */ }
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (x) => typeof x === "number" && isFinite(x);
const pct = (x) => (isNum(x) ? `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "–");
function money0(x) {
  if (!isNum(x)) return "–";
  const a = Math.abs(x);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M` : a >= 1e4 ? `${Math.round(a / 1e3)}k` : Math.round(a).toLocaleString();
  return `${x < 0 ? "-" : x > 0 ? "+" : ""}$${s}`;
}
const plain0 = (x) => (isNum(x) ? money0(x).replace("+", "") : "–");
const int = (x) => (isNum(x) ? Math.round(x).toLocaleString() : "–");
const cls = (x) => (isNum(x) ? (x > 0 ? "pos" : x < 0 ? "neg" : "") : "");
function ago(ms) {
  if (!isNum(ms)) return "–";
  const m = Math.max(0, (Date.now() - ms) / 60000);
  return m < 1 ? "now" : m < 60 ? `${Math.floor(m)}m` : m < 2880 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`;
}
const cents = (p) => (isNum(p) ? `${(p * 100).toFixed(p < 0.1 ? 1 : 0)}¢` : "–");

function scopeOf(S, r) { return r[S.scope]; }

function visibleRows(S, data) {
  const q = S.q.trim().toLowerCase();
  const rows = data.rows.filter((r) => {
    const s = scopeOf(S, r);
    if (S.src === "feed" && !r.src.includes("feed")) return false;
    if (S.src === "board" && !(r.src.includes("board") || r.src.includes("board30"))) return false;
    if (S.hidePrivate && r.hidden) return false;
    if (S.minN !== null && !(s.n >= S.minN)) return false;
    if (q && !r.nick.toLowerCase().includes(q)) return false;
    return true;
  });
  const v = (x) => (isNum(x) ? x : -Infinity);
  const key = {
    last: (r) => v(Math.max(scopeOf(S, r).last || 0, r.fl || 0)),
    pnl: (r) => v(r.pa),
    p30: (r) => v(r.p30),
    n: (r) => v(scopeOf(S, r).n),
    ev: (r) => v(scopeOf(S, r).ev),
    z: (r) => v(scopeOf(S, r).z),
    edge: (r) => v(scopeOf(S, r).edge),
  }[S.sort];
  rows.sort((a, b) => S.dir * (key(a) - key(b)) || a.nick.localeCompare(b.nick));
  return rows;
}

function shell(root) {
  root.innerHTML = `
    <div class="top">
      <span class="brand">BETTORS</span><span class="dot"></span>
      <span class="meta" data-ref="updated"></span><span class="sp"></span>
      <button class="iconbtn" data-act="refresh" title="Show latest">${ICON_REFRESH}</button>
    </div>
    <div class="filters">
      <div class="search"><input type="search" placeholder="Find or add a nickname…" data-ref="q" autocomplete="off" autocapitalize="off">
        <span class="count" data-ref="count"></span></div>
      <div data-ref="track"></div>
      <div class="frow"><span class="flabel">SCOPE</span>
        <button class="chip" data-act="scope" data-v="tennis">Tennis</button>
        <button class="chip" data-act="scope" data-v="all">All bets</button><span class="sp"></span>
        <button class="tog" data-act="hidePrivate"><span class="sw"></span>HIDE PRIVATE</button></div>
      <div class="frow"><span class="flabel">FROM</span>
        <button class="chip" data-act="src" data-v="all">All</button>
        <button class="chip" data-act="src" data-v="feed">Tennis feed</button>
        <button class="chip" data-act="src" data-v="board">Leaderboard</button></div>
      <div class="frow"><span class="flabel">MIN TRADES</span><span data-ref="minN"></span></div>
      <div class="note" data-ref="note"></div>
    </div>
    <div class="bgrid thead">
      <button data-sort="last">BETTOR<span class="clock">LAST BET</span></button>
      <button class="r" data-sort="pnl">PNL<span class="clock" data-sort="p30">30D</span></button>
      <button class="r" data-sort="n">TRADES<span class="clock">MKTS</span></button>
      <button class="r" data-sort="ev">EV<span class="clock">ROI</span></button>
      <button class="r" data-sort="z">BEAT<span class="clock">Z</span></button>
    </div>
    <div data-ref="rows"></div>
    <div class="toast"></div>`;
}

function srcTag(r) {
  const tags = [];
  if (isNum(r.rank)) tags.push(`#${r.rank}`);
  else if (isNum(r.rank30)) tags.push(`#${r.rank30} 30d`);
  if (r.src.includes("you")) tags.push("added");
  return tags.join(" · ");
}

function rowHtml(S, r) {
  const s = scopeOf(S, r);
  const last = Math.max(s.last || 0, r.fl || 0) || null;
  const hidden = r.hidden === true;
  const pending = r.hidden === null || r.hidden === undefined;
  const evCell = hidden ? `<div class="r dimc">hidden</div>` : pending ? `<div class="r dimc">…</div>`
    : `<div class="r"><div class="ev ${cls(s.ev)}">${money0(s.ev)}</div><div class="sub">${pct(s.evr)}</div></div>`;
  const beatCell = hidden || pending ? `<div class="r dimc"></div>`
    : `<div class="r"><div class="ev ${cls(s.edge)}">${money0(s.edge)}</div><div class="sub ${isNum(s.z) && s.z >= 2 ? "hot" : ""}">${isNum(s.z) ? `z ${s.z.toFixed(1)}` : "–"}</div></div>`;
  const open = S.open === r.nick;
  return `<div class="row ${open ? "open" : ""}" data-id="${esc(r.nick)}">
    <div class="bgrid" data-act="toggle">
      <div class="who"><b>${esc(r.nick)}</b><div class="sub">${esc(ago(last))}${srcTag(r) ? ` · <span class="tag">${esc(srcTag(r))}</span>` : ""}</div></div>
      <div class="r"><div class="ev ${cls(r.pa)}">${money0(r.pa)}</div><div class="sub">${money0(r.p30)}</div></div>
      <div class="r"><b>${hidden ? "–" : int(s.n)}</b><div class="sub">${int(r.ma)}</div></div>
      ${evCell}${beatCell}
    </div>
    ${open ? detailHtml(S, r) : ""}
  </div>`;
}

function detailHtml(S, r) {
  const s = scopeOf(S, r);
  const scopeName = S.scope === "tennis" ? "tennis" : "all";
  const since = isNum(s.first) ? new Date(s.first).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "–";
  const lines = [];
  lines.push(`Kalshi profile: PnL <b>${money0(r.pa)}</b> all-time · <b>${money0(r.p30)}</b> last 30d · <b>${int(r.ma)}</b> markets traded (${int(r.m30)} in 30d)` +
    (isNum(r.rank) ? ` · #${r.rank} Sports all-time` : "") + (isNum(r.rank30) ? ` · #${r.rank30} Sports 30d` : ""));
  if (r.hidden) {
    lines.push(`<span class="w">This profile hides its trades, so trade count, EV and PnL vs EV can't be measured.</span>`);
  } else {
    lines.push(`Sample (${scopeName}): <b>${int(s.n)}</b> trades in <b>${int(s.mk)}</b> markets since ${esc(since)} · <b>${plain0(s.vol)}</b> staked · taker ${isNum(s.tk) ? Math.round(s.tk * 100) + "%" : "–"} · priced ${isNum(s.cov) ? Math.round(s.cov * 100) + "%" : "–"}`);
    lines.push(`EV vs book <b class="${cls(s.ev)}">${money0(s.ev)}</b> (${pct(s.evr)}) · settled PnL <b class="${cls(s.pnl)}">${money0(s.pnl)}</b> on ${int(s.st)} trades (${pct(s.pr)}) · open ${plain0(s.open)}`);
    lines.push(`PnL − EV <b class="${cls(s.edge)}">${money0(s.edge)}</b> · z <b>${isNum(s.z) ? s.z.toFixed(2) : "–"}</b>${isNum(s.z) && s.z >= 2 ? ` · <span class="hot">beats the book by more than luck explains</span>` : ""}`);
  }
  if (r.fn) lines.push(`Tennis feed: ${int(r.fn)} trades, ${plain0(r.fv)} in the last day`);
  const fills = (r.recent || []).map((f) => {
    const res = isNum(f.pnl) ? `<span class="${cls(f.pnl)}">${money0(f.pnl)}</span>` : `<span class="dimc">open</span>`;
    return `<div class="fill"><span class="s ${f.s}">${f.s.toUpperCase()}</span>
      <span class="n">${esc(f.L ? `Combo ${f.L}L` : f.title)}<i> · ${ago(f.t)} · ${f.tk ? "took" : "made"}</i></span>
      <span class="q">${cents(f.p)} × ${int(f.n)}<br><span class="o">fair ${cents(f.fair)} · EV <span class="${cls(f.ev)}">${money0(f.ev)}</span> · ${res}</span></span></div>`;
  }).join("");
  return `<div class="detail">
    <div class="facts">${lines.join("<br>")}</div>
    ${fills ? `<div class="flabel2">LATEST TRADES</div>${fills}` : ""}
    <div class="actions"><span class="bk"></span>
      <button class="btn" data-act="copy">Copy name</button>
      <button class="btn primary" data-act="reload">Re-check now</button>
    </div>
  </div>`;
}

export default function (component) {
  const { parentElement, setTriggerValue } = component;
  const S = (window.__btUi = window.__btUi || loadUi());
  window.__btCtx = { data: component.data, setTriggerValue };

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
    $("minN").innerHTML = MIN_TRADES.map((v) => `<button class="chip ${v === S.minN ? "on" : ""}" data-act="minN" data-v="${v}">${v}</button>`).join(" ");
    root.querySelectorAll('[data-act="scope"]').forEach((b) => b.classList.toggle("on", S.scope === b.dataset.v));
    root.querySelectorAll('[data-act="src"]').forEach((b) => b.classList.toggle("on", S.src === b.dataset.v));
    root.querySelector('[data-act="hidePrivate"]').classList.toggle("on", S.hidePrivate);
    root.querySelectorAll(".thead [data-sort]").forEach((b) => {
      const on = b.dataset.sort === S.sort;
      b.classList.toggle("on", on);
      b.dataset.arrow = on ? (S.dir < 0 ? " ↓" : " ↑") : "";
    });
    if ($("q").value !== S.q) $("q").value = S.q;
  }

  function paintRows() {
    const D = window.__btCtx.data;
    const rows = visibleRows(S, D);
    $("count").textContent = `${rows.length} of ${D.rows.length}`;
    const q = S.q.trim().replace(/^@/, "");
    const exact = D.rows.some((r) => r.nick.toLowerCase() === q.toLowerCase());
    $("track").innerHTML = q && !exact && /^[\w.\-]{2,40}$/.test(q)
      ? `<button class="btn trackbtn" data-act="track" data-v="${esc(q)}">+ Analyze “${esc(q)}”</button>` : "";
    const shown = rows.slice(0, S.show);
    $("rows").innerHTML = shown.length
      ? shown.map((r) => rowHtml(S, r)).join("") +
        (rows.length > shown.length ? `<button class="more" data-act="more">Show more (${rows.length - shown.length} left)</button>` : "")
      : `<div class="empty">${D.rows.length ? "No bettors match these filters." : "Finding bettors… the first pass takes a few minutes."}</div>`;
  }

  function paintMeta() {
    const D = window.__btCtx.data;
    const busy = D.status && D.status !== "idle";
    $("updated").textContent = `${D.demo ? "demo · " : ""}updated ${ago(D.updated) === "now" ? "just now" : ago(D.updated) + " ago"}${busy ? ` · ${D.status}…` : ""}`;
    root.querySelector(".dot").classList.toggle("stale", !!D.error || busy);
    $("note").textContent = [D.error, D.note].filter(Boolean).join(" ");
  }

  if (!root._tick) root._tick = setInterval(paintMeta, 15000);
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
      if (e.target.dataset.ref === "q") update(() => { S.q = e.target.value; S.show = PAGE; }, true);
    });
    el.addEventListener("click", (e) => {
      const ctx = window.__btCtx;
      const sortBtn = e.target.closest("[data-sort]");
      if (sortBtn) {
        e.stopPropagation();
        const k = sortBtn.dataset.sort;
        return update(() => { S.dir = S.sort === k ? -S.dir : -1; S.sort = k; });
      }
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const v = b.dataset.v;
      const row = b.closest(".row");
      switch (b.dataset.act) {
        case "scope": return update(() => { S.scope = v; });
        case "src": return update(() => { S.src = v; S.show = PAGE; });
        case "minN": return update(() => { S.minN = S.minN === +v ? null : +v; S.show = PAGE; });
        case "hidePrivate": return update(() => { S.hidePrivate = !S.hidePrivate; });
        case "more": return update(() => { S.show += PAGE; }, true);
        case "toggle": return update(() => { S.open = S.open === row.dataset.id ? null : row.dataset.id; }, true);
        case "refresh": return ctx.setTriggerValue("refresh", Date.now());
        case "track": update(() => { S.q = ""; S.open = v; }); return ctx.setTriggerValue("track", v);
        case "reload": return row && ctx.setTriggerValue("track", row.dataset.id);
        case "copy": return row && copyName(row.dataset.id, el);
      }
    });
  }
}

function copyName(text, root) {
  const done = () => {
    const t = root.querySelector(".toast");
    t.textContent = "Name copied";
    t.classList.add("show");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("show"), 1400);
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => {});
}
