// Live bets: the open combo bets, newest first, of the bettors worth watching (whales, the top tennis
// bettors, anyone starred, and public profiles the tracker follows), each with the bettor's 30-day record.

const PAGE = 40;
const STORE = "live-ui-v1";
const TAGS = "bettor-tags";
const LOGO = "__LOGO__";

const ICON_REFRESH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
const STAR = "★";

function loadUi() {
  const base = { who: "all", min: 0, q: "", show: PAGE };
  try {
    return { ...base, ...JSON.parse(localStorage.getItem(STORE) || "{}"), q: "", show: PAGE };
  } catch (e) {
    return base;
  }
}

function saveUi(S) {
  try {
    const { q, show, ...keep } = S;
    localStorage.setItem(STORE, JSON.stringify(keep));
  } catch (e) { /* ignore */ }
}

function localTags() {
  try { return new Set(JSON.parse(localStorage.getItem(TAGS) || "[]")); } catch (e) { return new Set(); }
}

const MINS = [0, 100, 1000, 5000];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (x) => typeof x === "number" && isFinite(x);
const pct = (x) => (isNum(x) ? `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "–");
function money0(x) {
  if (!isNum(x)) return "–";
  const a = Math.abs(x);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e4 ? `${Math.round(a / 1e3)}k` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}k` : Math.round(a).toLocaleString();
  return `${x < 0 ? "-" : x > 0 ? "+" : ""}$${s}`;
}
const plain0 = (x) => (isNum(x) ? money0(x).replace("+", "") : "–");
const cls = (x) => (isNum(x) ? (x > 0 ? "pos" : x < 0 ? "neg" : "") : "");
const cents = (p) => (isNum(p) ? `${(p * 100).toFixed(p < 0.1 ? 1 : 0)}¢` : "–");
function odds(p) {
  if (!isNum(p) || p <= 0 || p >= 1) return "";
  return p > 0.5 ? `${Math.round((-100 * p) / (1 - p))}` : `+${Math.round((100 * (1 - p)) / p)}`;
}
function ago(ms) {
  if (!isNum(ms)) return "–";
  const m = Math.max(0, (Date.now() - ms) / 60000);
  return m < 1 ? "now" : m < 60 ? `${Math.floor(m)}m` : m < 2880 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`;
}
const who = (b) => b.name || (b.c ? `#${b.c.slice(0, 8)}` : "?");
const starred = (b, tags) => !!b.tg || (b.c && tags.has(b.c)) || (b.name && tags.has(b.name));

export function visibleBets(S, D, tags = new Set()) {
  const q = S.q.trim().toLowerCase().replace(/^[@#]/, "");
  return D.live.filter((b) => {
    if (S.min && !(b.n * b.p >= S.min)) return false;
    if (S.who === "whales" && !b.w) return false;
    if (S.who === "tennis" && !(b.t30 && b.t30.n > 0)) return false;
    if (S.who === "tagged" && !starred(b, tags)) return false;
    if (S.who === "profiles" && b.c) return false;
    if (q && !((b.c || "").startsWith(q) || (b.name || "").toLowerCase().includes(q) || (b.ti || "").toLowerCase().includes(q))) return false;
    return true;
  });
}

function shell(root) {
  root.innerHTML = `
    <div class="top">
      <span class="dot"></span><span class="meta" data-ref="updated"></span><span class="sp"></span>
      <button class="iconbtn" data-act="refresh" title="Load the newest data">${ICON_REFRESH}</button>
    </div>
    <div class="filters">
      <div class="search"><input type="search" placeholder="Find a bettor or a leg…" data-ref="q" autocomplete="off" autocapitalize="off">
        <span class="count" data-ref="count"></span></div>
      <div class="frow"><span class="flabel">Who</span>
        <button class="chip" data-act="who" data-v="all">All</button>
        <button class="chip" data-act="who" data-v="whales">Whales</button>
        <button class="chip" data-act="who" data-v="tennis">Tennis</button>
        <button class="chip" data-act="who" data-v="tagged">${STAR} Starred</button>
        <button class="chip" data-act="who" data-v="profiles">Profiles</button></div>
      <div class="frow"><span class="flabel">Min bet</span><span data-ref="mins"></span></div>
      <div class="note" data-ref="note"></div>
    </div>
    <div data-ref="rows"></div>`;
}

function record(b) {
  const s = (b.t30 && b.t30.n > 0 && b.c && !b.w) ? b.t30 : b.s30;  // whales: all their bets; others: tennis
  if (!s || !s.n) return "no record yet";
  const scope = s === b.t30 ? "tennis" : "all";
  const parts = [`${s.n} bets 30d (${scope})`];
  if (s.st) parts.push(`PnL <b class="${cls(s.pnl)}">${money0(s.pnl)}</b>`, `won ${Math.round(s.win * 100)}%`);
  if (isNum(s.z)) parts.push(`<b class="${isNum(s.z) && s.z >= 2 ? "hot" : ""}">z ${s.z.toFixed(1)}</b>`);
  return parts.join(" · ");
}

function betHtml(b, tags) {
  const stake = b.n * b.p;
  const ev = isNum(b.fm) && b.p ? b.fm / b.p - 1 : null;
  const what = b.ti || (b.L ? `Combo, ${b.L} legs` : b.tk);
  const kind = !b.c ? "profile" : b.w ? "whale" : "";
  const side = b.s === "no" ? "no" : "yes";
  return `<div class="lbet">
    <div class="lhead"><b>${starred(b, tags) ? `<span class="star on">${STAR}</span> ` : ""}${esc(who(b))}</b>
      ${kind ? `<span class="tag">${kind}</span>` : ""}<span class="sp"></span><span class="sub">${esc(ago(b.t))} ago${b.lv ? " · live" : ""}</span></div>
    <div class="lrec sub">${record(b)}</div>
    <div class="lbody">
      <span class="s ${side}">${side.toUpperCase()}</span>
      <span class="n">${esc(what)}</span>
      <span class="q"><b>${plain0(stake)}</b> @ ${cents(b.p)} <span class="o">${odds(b.p)}</span><br>
        <span class="o">EV at bet <span class="${cls(ev)}">${pct(ev)}</span> · to win ${plain0(b.n - stake)}</span></span>
    </div>
    <div class="lacts">
      <button class="btn primary" data-act="quote" data-tk="${esc(b.tk)}" data-s="${side}" data-p="${+b.p}" data-n="${+b.n}">Quote it</button>
      ${b.c ? `<button class="btn" data-act="tag" data-v="${esc(b.c)}">${starred(b, tags) ? `${STAR} Starred` : "☆ Star"}</button>` : ""}
    </div>
  </div>`;
}

export default function (component) {
  const { parentElement, setTriggerValue } = component;
  const S = (window.__lvUi = window.__lvUi || loadUi());
  window.__lvCtx = { data: component.data, setTriggerValue };

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
    root.querySelectorAll('[data-act="who"]').forEach((b) => b.classList.toggle("on", S.who === b.dataset.v));
    $("mins").innerHTML = MINS.map((v) => `<button class="chip ${S.min === v ? "on" : ""}" data-act="min" data-v="${v}">${v ? `$${v >= 1000 ? `${v / 1000}k` : v}+` : "Any"}</button>`).join(" ");
    if ($("q").value !== S.q) $("q").value = S.q;
  }

  function paintRows() {
    const D = window.__lvCtx.data;
    const tags = localTags();
    const bets = visibleBets(S, D, tags);
    $("count").textContent = `${bets.length} of ${D.live.length}`;
    const shown = bets.slice(0, S.show);
    const empty = D.error && !D.live.length ? `<b>No data yet</b>${esc(D.error)}`
      : D.live.length ? "<b>No open bets match</b>Try All, or a lower min bet."
      : "<b>No open bets right now</b>Bets from the last two days that haven't settled show here.";
    $("rows").innerHTML = shown.length
      ? shown.map((b) => betHtml(b, tags)).join("") +
        (bets.length > shown.length ? `<button class="more" data-act="more">Show more (${bets.length - shown.length} left)</button>` : "")
      : `<div class="empty">${LOGO}${empty}</div>`;
  }

  function paintMeta() {
    const D = window.__lvCtx.data;
    $("updated").textContent = `${D.demo ? "demo · " : ""}${isNum(D.saved) ? `saved ${ago(D.saved) === "now" ? "just now" : ago(D.saved) + " ago"}` : "no data yet"}`;
    root.querySelector(".dot").classList.toggle("stale", !!D.error || !isNum(D.saved) || Date.now() - D.saved > 30 * 60000);
    $("note").textContent = [D.live.length ? D.error : null, D.note].filter(Boolean).join(" ");
  }

  if (!root._tick) root._tick = setInterval(paintMeta, 15000);
  root._paint = () => { paintControls(); paintMeta(); paintRows(); };
  root._paint();

  function bind(el) {
    const update = (fn) => { fn(); saveUi(S); el._paint(); };
    el.addEventListener("input", (e) => {
      if (e.target.dataset.ref === "q") update(() => { S.q = e.target.value; S.show = PAGE; });
    });
    el.addEventListener("click", (e) => {
      const ctx = window.__lvCtx;
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const v = b.dataset.v;
      switch (b.dataset.act) {
        case "who": return update(() => { S.who = v; S.show = PAGE; });
        case "min": return update(() => { S.min = +v; S.show = PAGE; });
        case "more": return update(() => { S.show += PAGE; });
        case "refresh": return ctx.setTriggerValue("refresh", Date.now());
        case "quote": return ctx.setTriggerValue("quote", { t: b.dataset.tk, side: b.dataset.s, p: +b.dataset.p, n: +b.dataset.n });
        case "tag": {
          const tags = localTags();
          tags.has(v) ? tags.delete(v) : tags.add(v);
          try { localStorage.setItem(TAGS, JSON.stringify([...tags])); } catch (err) { /* ignore */ }
          update(() => {});
          return ctx.setTriggerValue("tag", { id: v, at: Date.now() });
        }
      }
    });
  }
}
