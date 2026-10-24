// Ranked bettors (the Whales and Tennis tabs): the people behind combo bets, named by RFQ code,
// ranked by size, PnL, PnL vs EV, bets and z. Sorting, the whale size, min bets, search and stars run
// in the browser; the period, the 95¢+ toggle, opening a bettor, "Quote it" and a star go to Python.

const SIZES = [5000, 10000, 25000, 50000, 100000];
const MIN_BETS = [2, 5, 10, 25, 50];
const PAGE = 50;
const STORE = "ranks-ui-v1";
const TAGS = "bettor-tags";  // stars kept on this phone (and on GitHub when the app has a token)
const LOGO = "__LOGO__";  // app.py puts the CapyEV logo (capyev.svg) here

const ICON_REFRESH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
const STAR = "★", STAR_OFF = "☆";

const DEFAULTS = {
  whales: { size: null, period: "all", locks: false, minN: null, only: "all", sort: "vol", dir: -1 },
  tennis: { size: null, period: "30", locks: false, minN: 5, only: "all", sort: "z", dir: -1 },
};

function loadUi(tab) {
  const base = { ...DEFAULTS[tab], q: "", open: null, show: PAGE };
  try {
    const saved = JSON.parse(localStorage.getItem(`${STORE}-${tab}`) || "{}");
    return { ...base, ...saved, q: "", open: null, show: PAGE };
  } catch (e) {
    return base;
  }
}

function saveUi(tab, S) {
  try {
    const { q, open, show, ...keep } = S;
    localStorage.setItem(`${STORE}-${tab}`, JSON.stringify(keep));
  } catch (e) { /* private mode etc. */ }
}

export function localTags() {
  try { return new Set(JSON.parse(localStorage.getItem(TAGS) || "[]")); } catch (e) { return new Set(); }
}

function toggleLocalTag(id) {
  const tags = localTags();
  tags.has(id) ? tags.delete(id) : tags.add(id);
  try { localStorage.setItem(TAGS, JSON.stringify([...tags])); } catch (e) { /* ignore */ }
  return tags;
}

// Which totals Python should send: scope (a all bets, t tennis; a+ with 95¢+ bets) and period.
export const variantOf = (tab, S) => `${tab === "tennis" ? "t" : "a"}${tab === "whales" && S.locks ? "+" : ""}:${S.period}`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (x) => typeof x === "number" && isFinite(x);
const pct = (x) => (isNum(x) ? `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "–");
const share = (x) => (isNum(x) ? `${Math.round(x * 100)}%` : "–");
export function money0(x) {
  if (!isNum(x)) return "–";
  const a = Math.abs(x);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M` : a >= 1e4 ? `${Math.round(a / 1e3)}k` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}k` : Math.round(a).toLocaleString();
  return `${x < 0 ? "-" : x > 0 ? "+" : ""}$${s}`;
}
const plain0 = (x) => (isNum(x) ? money0(x).replace("+", "") : "–");
const int = (x) => (isNum(x) ? Math.round(x).toLocaleString() : "–");
const cls = (x) => (isNum(x) ? (x > 0 ? "pos" : x < 0 ? "neg" : "") : "");
const cents = (p) => (isNum(p) ? `${(p * 100).toFixed(p < 0.1 ? 1 : 0)}¢` : "–");
const sizeLabel = (v) => `$${v >= 1000 ? `${v / 1000}k` : v}+`;
export function odds(p) {
  if (!isNum(p) || p <= 0 || p >= 1) return "";
  return p > 0.5 ? `${Math.round((-100 * p) / (1 - p))}` : `+${Math.round((100 * (1 - p)) / p)}`;
}
function ago(ms) {
  if (!isNum(ms)) return "–";
  const m = Math.max(0, (Date.now() - ms) / 60000);
  return m < 1 ? "now" : m < 60 ? `${Math.floor(m)}m` : m < 2880 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`;
}
const day = (ms) => (isNum(ms) ? new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "–");
export const label = (r) => r.name || `#${r.code.slice(0, 8)}`;
const tagged = (r, tags) => !!r.tg || tags.has(r.code) || (r.name && tags.has(r.name));

// The rows that pass the filters, sorted. Each row carries one summary `s` (the scope, period and
// 95¢+ setting Python was asked for). On Whales only whales show, and only those whose biggest bet
// ever (r.big, locks included) clears the chosen size; on Tennis anyone with tennis bets in the period.
export function visibleRows(tab, S, D, tags = new Set()) {
  const q = S.q.trim().toLowerCase().replace(/^[@#]/, "");
  const least = tab === "whales" ? Math.max(S.size || 0, D.whale_min || 0) : 0;
  const rows = D.rows.filter((r) => {
    const s = r.s;
    if (!s || !(s.n > 0)) return false;
    if (tab === "whales" && !(r.w && Math.max(r.big || 0, s.big || 0) >= least)) return false;
    if (S.only === "tagged" && !tagged(r, tags)) return false;
    if (S.only === "named" && !r.name) return false;
    if (S.minN !== null && !(s.n >= S.minN)) return false;
    if (q && !(r.code.startsWith(q) || (r.name || "").toLowerCase().includes(q))) return false;
    return true;
  });
  const v = (x) => (isNum(x) ? x : -Infinity);
  const key = (r) => v(r.s[S.sort]);
  rows.sort((a, b) => S.dir * (key(a) - key(b)) || a.code.localeCompare(b.code));
  return rows;
}

function shell(root, tab) {
  const whales = tab === "whales";
  root.innerHTML = `
    <div class="top">
      <span class="dot"></span><span class="meta" data-ref="updated"></span><span class="sp"></span>
      <button class="iconbtn" data-act="refresh" title="Load the newest data">${ICON_REFRESH}</button>
    </div>
    <div class="filters">
      <div class="search"><input type="search" placeholder="Find a bettor by name or code…" data-ref="q" autocomplete="off" autocapitalize="off">
        <span class="count" data-ref="count"></span></div>
      ${whales ? `<div class="frow"><span class="flabel">Whales</span><span data-ref="sizes"></span></div>` : ""}
      <div class="frow"><span class="flabel">Period</span>
        <button class="chip" data-act="period" data-v="all">All</button>
        <button class="chip" data-act="period" data-v="30">30 days</button>
        <button class="chip" data-act="period" data-v="7">7 days</button><span class="sp"></span>
        ${whales ? `<button class="tog" data-act="locks"><span class="sw"></span>95¢+ bets</button>` : ""}</div>
      <div class="frow"><span class="flabel">Who</span>
        <button class="chip" data-act="only" data-v="all">All</button>
        <button class="chip" data-act="only" data-v="tagged">${STAR} Starred</button>
        <button class="chip" data-act="only" data-v="named">Named</button></div>
      <div class="frow"><span class="flabel">Min bets</span><span data-ref="minN"></span></div>
      <div class="note" data-ref="note"></div>
    </div>
    <div class="wgrid thead">
      <button data-sort="last">Bettor<span class="clock">last bet</span></button>
      <button class="r" data-sort="vol">Size<span class="clock" data-sort="big">biggest</span></button>
      <button class="r" data-sort="pnl">PnL<span class="clock" data-sort="pr">ROI</span></button>
      <button class="r" data-sort="edge">vs EV<span class="clock" data-sort="z">z</span></button>
      <button class="r" data-sort="n">Bets<span class="clock" data-sort="win">win</span></button>
    </div>
    <div data-ref="rows"></div>
    <div class="toast"></div>`;
}

function rowHtml(S, D, r, tags) {
  const s = r.s;
  const open = S.open === r.code;
  const star = tagged(r, tags);
  const tagList = [r.name ? `#${r.code.slice(0, 6)}` : "", r.w ? "whale" : "", s.ten && s.ten === s.n ? "tennis" : s.ten ? `${s.ten} tennis` : "",
    isNum(s.lv) && s.lv >= 0.5 ? "live" : ""].filter(Boolean);
  const pnl = s.st ? `<div class="ev ${cls(s.pnl)}">${money0(s.pnl)}</div><div class="sub">${pct(s.pr)}</div>`
    : `<div class="dimc">open</div><div class="sub">${plain0(s.open)}</div>`;
  const beat = isNum(s.edge) ? `<div class="ev ${cls(s.edge)}">${money0(s.edge)}</div><div class="sub ${isNum(s.z) && s.z >= 2 ? "hot" : ""}">${isNum(s.z) ? `z ${s.z.toFixed(1)}` : "–"}</div>`
    : `<div class="dimc">–</div><div class="sub">${isNum(s.ev) ? `EV ${money0(s.ev)}` : ""}</div>`;
  return `<div class="row ${open ? "open" : ""}" data-id="${esc(r.code)}">
    <div class="wgrid" data-act="toggle">
      <div class="who"><b>${star ? `<span class="star on">${STAR}</span> ` : ""}${esc(label(r))}</b><div class="sub">${esc(ago(s.last))}${tagList.length ? ` · <span class="tag">${esc(tagList.join(" · "))}</span>` : ""}</div></div>
      <div class="r"><b>${plain0(s.vol)}</b><div class="sub">${plain0(s.big)}</div></div>
      <div class="r">${pnl}</div>
      <div class="r">${beat}</div>
      <div class="r"><b>${int(s.n)}</b><div class="sub">${share(s.win)}</div></div>
    </div>
    ${open ? detailHtml(S, D, r, star) : ""}
  </div>`;
}

export function betHtml(b) {
  const stake = b.n * b.p;
  const ev = isNum(b.fm) && b.p ? b.fm / b.p - 1 : null;
  const res = b.r === "yes" || b.r === "no"
    ? `<span class="${cls(b.pnl)}">${b.r === b.s ? "won" : "lost"} ${money0(b.pnl)}</span>` : b.r === "void" ? `<span class="dimc">void</span>` : `<span class="dimc">open</span>`;
  const what = b.ti || (b.L ? `Combo, ${b.L} legs` : b.tk);
  const side = b.s === "no" ? "no" : "yes";
  return `<div class="fill"><span class="s ${side}">${side.toUpperCase()}</span>
    <span class="n">${esc(what)}<i> · ${ago(b.t)} ago${b.lv ? " · live" : ""}</i></span>
    <span class="q">${plain0(stake)} @ ${cents(b.p)} <span class="o">${odds(b.p)}</span><br>
      <span class="o">EV <span class="${cls(ev)}">${pct(ev)}</span> · ${res}</span>
      ${b.r ? "" : `<button class="mini" data-act="quote" data-tk="${esc(b.tk)}" data-s="${side}" data-p="${+b.p}" data-n="${+b.n}">Quote</button>`}</span></div>`;
}

function detailHtml(S, D, r, star) {
  const s = r.s;
  const when = { all: "since tracking began", 30: "last 30 days", 7: "last 7 days" }[S.period];
  const lines = [];
  lines.push(`Code <b>${esc(r.code.slice(0, 16))}…</b> · first seen ${esc(day(r.seen))}${r.name ? ` · trades show the public name <b>${esc(r.name)}</b>` : ""}`);
  if (r.name && (isNum(r.pa) || isNum(r.p30))) lines.push(`Kalshi profile PnL <b class="${cls(r.pa)}">${money0(r.pa)}</b> all-time · <b class="${cls(r.p30)}">${money0(r.p30)}</b> last 30 days`);
  lines.push(`${esc(when)}${S.locks ? "" : ", without 95¢+ bets"}: <b>${int(s.n)}</b> bets · <b>${plain0(s.vol)}</b> bet · biggest <b>${plain0(s.big)}</b> · average price ${cents(s.avgp)} (${odds(s.avgp) || "–"}) · ${share(s.lv)} live · ${int(s.ten)} with tennis · ${share(s.sure)} sure matches`);
  lines.push(`EV vs the book when placed <b class="${cls(s.ev)}">${money0(s.ev)}</b> (${pct(s.evr)}; ${share(s.cov)} priced) · settled PnL <b class="${cls(s.pnl)}">${money0(s.pnl)}</b> on ${int(s.st)} bets (${pct(s.pr)}) · still open ${plain0(s.open)}`);
  if (isNum(s.edge)) {
    const verdict = isNum(s.z) && s.z >= 2 ? ` · <span class="hot">beats the book by more than luck explains</span>`
      : isNum(s.z) && s.z <= -2 ? ` · worse than the book by more than bad luck explains` : "";
    lines.push(`PnL − EV <b class="${cls(s.edge)}">${money0(s.edge)}</b> · z <b>${isNum(s.z) ? s.z.toFixed(2) : "–"}</b>${verdict}`);
  }
  const detail = D.detail && D.detail.code === r.code ? D.detail : null;
  const bets = detail ? (detail.recent.length ? detail.recent.map(betHtml).join("") : `<div class="dimc">No whole bets kept (older ones are folded into totals).</div>`)
    : `<div class="dimc">Loading their bets…</div>`;
  return `<div class="detail">
    <div class="facts">${lines.join("<br>")}</div>
    <div class="flabel2">Latest bets</div>${bets}
    <div class="actions"><span class="bk"></span>
      <button class="btn ${star ? "primary" : ""}" data-act="tag" data-v="${esc(r.code)}">${star ? `${STAR} Starred` : `${STAR_OFF} Star`}</button>
      <button class="btn" data-act="copy" data-v="${esc(r.code)}" data-what="Code">Copy code</button>
      ${r.name ? `<button class="btn" data-act="copy" data-v="${esc(r.name)}" data-what="Name">Copy name</button>` : ""}
    </div>
  </div>`;
}

export default function (component) {
  const { parentElement, setTriggerValue } = component;
  const tab = component.data.tab || "whales";
  const states = (window.__rkUi = window.__rkUi || {});
  const S = (states[tab] = states[tab] || loadUi(tab));
  window.__rkCtx = { data: component.data, setTriggerValue, tab };

  let root = parentElement.querySelector(".cev");
  if (root && root.dataset.tab !== tab) {
    clearInterval(root._tick);
    root.remove();
    root = null;
  }
  if (!root) {
    root = document.createElement("div");
    root.className = "cev";
    root.dataset.tab = tab;
    parentElement.appendChild(root);
    shell(root, tab);
    bind(root);
  }
  const $ = (name) => root.querySelector(`[data-ref="${name}"]`);
  // Python sends the totals it was last asked for, and the bets of the bettor that's open; ask for
  // what this tab shows (once each, so a slow answer can't loop).
  const asked = (window.__rkAsked = window.__rkAsked || {});
  const ask = (name, value) => {
    asked[name] = value;
    window.__rkCtx.setTriggerValue(name, value);
  };
  const want = variantOf(tab, S);
  if (component.data.variant === asked.view) delete asked.view;  // answered: a later change may ask again
  if ((component.data.detail || {}).code === asked.open) delete asked.open;
  if (component.data.variant !== want && asked.view !== want) ask("view", want);
  if (S.open && (component.data.detail || {}).code !== S.open && asked.open !== S.open) ask("open", S.open);

  function paintControls() {
    const D = window.__rkCtx.data;
    if (tab === "whales") {
      const sizes = SIZES.filter((v) => v >= (D.whale_min || 0));
      $("sizes").innerHTML = sizes.map((v) => `<button class="chip ${(S.size || sizes[0]) === v ? "on" : ""}" data-act="size" data-v="${v}">${sizeLabel(v)}</button>`).join(" ");
      root.querySelector('[data-act="locks"]').classList.toggle("on", S.locks);
    }
    $("minN").innerHTML = MIN_BETS.map((v) => `<button class="chip ${v === S.minN ? "on" : ""}" data-act="minN" data-v="${v}">${v}+</button>`).join(" ");
    root.querySelectorAll('[data-act="period"]').forEach((b) => b.classList.toggle("on", S.period === b.dataset.v));
    root.querySelectorAll('[data-act="only"]').forEach((b) => b.classList.toggle("on", S.only === b.dataset.v));
    root.querySelectorAll(".thead [data-sort]").forEach((b) => {
      const on = b.dataset.sort === S.sort;
      b.classList.toggle("on", on);
      b.dataset.arrow = on ? (S.dir < 0 ? " ↓" : " ↑") : "";
    });
    if ($("q").value !== S.q) $("q").value = S.q;
  }

  function paintRows() {
    const D = window.__rkCtx.data;
    const tags = localTags();
    const rows = visibleRows(tab, S, D, tags);
    $("count").textContent = `${rows.length} of ${D.rows.length}`;
    const shown = rows.slice(0, S.show);
    const empty = D.error && !D.rows.length ? `<b>No data yet</b>${esc(D.error)}`
      : D.rows.length ? `<b>No bettors match these filters</b>${tab === "whales" ? "Try a smaller whale size, a longer period or fewer min bets." : "Try a longer period, fewer min bets or All."}`
      : "<b>No bettors yet</b>The recorder saves what it has every few minutes once it's running.";
    $("rows").innerHTML = shown.length
      ? shown.map((r) => rowHtml(S, D, r, tags)).join("") +
        (rows.length > shown.length ? `<button class="more" data-act="more">Show more (${rows.length - shown.length} left)</button>` : "")
      : `<div class="empty">${LOGO}${empty}</div>`;
  }

  function paintMeta() {
    const D = window.__rkCtx.data;
    const since = isNum(D.since) ? ` · recording since ${day(D.since)}${isNum(D.coverage) ? ` (${share(D.coverage)} of the time)` : ""}` : "";
    $("updated").textContent = `${D.demo ? "demo · " : ""}${isNum(D.saved) ? `saved ${ago(D.saved) === "now" ? "just now" : ago(D.saved) + " ago"}` : "no data yet"}${since}`;
    root.querySelector(".dot").classList.toggle("stale", !!D.error || !isNum(D.saved) || Date.now() - D.saved > 30 * 60000);
    $("note").textContent = [D.rows.length ? D.error : null, D.note].filter(Boolean).join(" ");
  }

  if (!root._tick) root._tick = setInterval(paintMeta, 15000);
  root._paint = () => { paintControls(); paintMeta(); paintRows(); };
  root._paintRows = paintRows;
  root._paintControls = paintControls;
  root._paint();
  return () => { clearInterval(root._tick); root._tick = null; };  // unmount: stop the clock

  function bind(el) {
    const update = (fn, rowsOnly = false) => {
      fn();
      saveUi(tab, S);
      if (!rowsOnly) el._paintControls();
      el._paintRows();
    };
    el.addEventListener("input", (e) => {
      if (e.target.dataset.ref === "q") update(() => { S.q = e.target.value; S.show = PAGE; }, true);
    });
    el.addEventListener("click", (e) => {
      const ctx = window.__rkCtx;
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
        case "size": return update(() => { S.size = +v; S.show = PAGE; });
        case "period": update(() => { S.period = v; S.show = PAGE; }); return ask("view", variantOf(tab, S));
        case "locks": update(() => { S.locks = !S.locks; }); return ask("view", variantOf(tab, S));
        case "only": return update(() => { S.only = v; S.show = PAGE; });
        case "minN": return update(() => { S.minN = S.minN === +v ? null : +v; S.show = PAGE; });
        case "more": return update(() => { S.show += PAGE; }, true);
        case "toggle":
          update(() => { S.open = S.open === row.dataset.id ? null : row.dataset.id; }, true);
          return S.open && ask("open", S.open);
        case "refresh": return ctx.setTriggerValue("refresh", Date.now());
        case "quote": return ctx.setTriggerValue("quote", { t: b.dataset.tk, side: b.dataset.s, p: +b.dataset.p, n: +b.dataset.n });
        case "tag": {
          const on = toggleLocalTag(v).has(v);
          update(() => {}, true);
          return ctx.setTriggerValue("tag", { id: v, on, at: Date.now() });
        }
        case "copy": return copyText(v, `${b.dataset.what} copied ✓`, el);
      }
    });
  }
}

function copyText(text, said, root) {
  const done = () => {
    const t = root.querySelector(".toast");
    t.textContent = said;
    t.classList.add("show");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("show"), 1400);
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => {});
}
