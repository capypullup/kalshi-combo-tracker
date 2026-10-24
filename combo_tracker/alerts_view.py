"""The Alerts tool (Tools tab): what the Telegram alert bot sends, changed on the fly.

Every switch and number here is a field of alerts.Settings. The list under them runs the bot's own
tests (alerts.clears and alerts.filtered_by) over the last hour of bets with the settings as they
are on screen, so you see what each filter leaves out before you save. Saving writes
alert_settings.json to the alert-settings branch (ghfile.GitHubFile), and the bot re-reads it every
minute.
"""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timezone

import streamlit as st

from . import alerts
from .alerts import Settings

SHOW = 30  # bets listed in each section
CAPS = {"max_legs": "More legs than", "max_legs_live": "Live: more legs than", "max_leagues": "More leagues than"}
CSS = """<style>
.al {font-family: "Nunito", ui-rounded, "SF Pro Rounded", ui-sans-serif, -apple-system, system-ui, sans-serif; color: #e6fbff;
     font-variant-numeric: tabular-nums; font-size: 13px; line-height: 1.3;}
.al .dim {color: #6f8f99;} .al .cy {color: #00e5ff;} .al .warn {color: #ffc857;} .al .pos {color: #2ee6a6;}
.al .sum {font-size: 13px; margin: 2px 2px 8px;}
.al .sum b {color: #00e5ff;}
.al .hd {font-size: 15px; font-weight: 900; margin: 10px 2px 4px;}
.al .row {border: 1px solid #1b3540; border-radius: 14px; padding: 8px 10px; margin: 0 0 6px;}
.al .row.ok {border-color: #00e5ff; background: rgba(0, 229, 255, 0.05);}
.al .row .t1 {font-weight: 800;} .al .row .t1 .ev {color: #2ee6a6;}
.al .row .t2 {color: #8fb0b9; font-size: 12px; margin-top: 2px; overflow-wrap: anywhere;}
.al .row .t3 {margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; align-items: center;}
.al .tag {font-size: 11px; font-weight: 800; color: #ffc857; border: 1px solid #5a4a1f; border-radius: 999px; padding: 1px 7px;}
.al .row a {color: #00e5ff; font-size: 12px; font-weight: 800; text-decoration: none; margin-left: auto;}
.al .catch {font-size: 12px; color: #6f8f99; margin: -6px 0 6px 2px;}
.al .catch b {color: #ffc857;}
</style>"""


# ---------- the settings as on screen ----------
def draft_of(s: Settings) -> dict:
    """The widgets' values for these settings. A switched-off cap keeps a number to switch back on to,
    and a kind that's off keeps its default tiers."""
    d = Settings()
    out = {"basis": s.basis, "skip_thin": s.skip_thin, "thin_spread_cents": round(s.thin_spread * 100, 4),
           "thin_size": s.thin_size, "skip_same_game": s.skip_same_game}
    for kind in ("pregame", "live"):
        out[f"{kind}_on"] = bool(getattr(s, kind))
        out[kind] = [[stake, round(ev * 100, 4)] for stake, ev in (getattr(s, kind) or getattr(d, kind))]
    for cap in CAPS:
        out[f"{cap}_on"] = getattr(s, cap) is not None
        out[cap] = getattr(s, cap) or getattr(d, cap)
    return out


def settings_of(d: dict) -> Settings:
    def tiers(kind):
        return sorted((float(stake), round(float(ev) / 100, 6)) for stake, ev in d[kind]) if d[f"{kind}_on"] else []
    return Settings(pregame=tiers("pregame"), live=tiers("live"), basis=d["basis"], skip_thin=d["skip_thin"],
                    thin_spread=round(float(d["thin_spread_cents"]) / 100, 6), thin_size=float(d["thin_size"]),
                    skip_same_game=d["skip_same_game"],
                    **{cap: int(d[cap]) if d[f"{cap}_on"] else None for cap in CAPS})


def everything_on(d: dict) -> Settings:
    """The on-screen settings with every filter switched on at its number: what each one would catch."""
    return settings_of({**d, "skip_thin": True, "skip_same_game": True, **{f"{cap}_on": True for cap in CAPS}})


def preview(rows: list[dict], s: Settings, all_on: Settings | None = None) -> dict:
    """The bets that clear their EV bar under `s`, split into what would alert and what a filter leaves
    out (with why), newest first, and how many of them each filter catches when it's on."""
    cleared = sorted((r for r in rows if alerts.clears(r, s)), key=lambda r: r["minutes_ago"])
    out = {"rows": len(rows), "cleared": len(cleared), "alert": [], "dropped": [],
           "catches": Counter(why for r in cleared for why in alerts.filtered_by(r, all_on or s))}
    for r in cleared:
        why = alerts.filtered_by(r, s)
        (out["dropped"] if why else out["alert"]).append((r, why))
    return out


# ---------- drawing ----------
def _bet_html(row: dict, why: list[str], basis: str) -> str:
    legs = " · ".join(f"{leg['side'].upper()} {leg['leg']}" for leg in row["leg_detail"])
    q = f"t={row['ticker']}&side={row['side']}&p={row['price']:.4f}&n={row['contracts']:.0f}"
    tags = "".join(f'<span class="tag">{alerts.REASONS[w]}</span>' for w in why)
    title = alerts.title(row, basis)
    head, ev = title.rsplit(" ", 2)[0], " ".join(title.rsplit(" ", 2)[1:])
    return (f'<div class="row{"" if why else " ok"}"><div class="t1">{html.escape(head)} <span class="ev">{html.escape(ev)}</span> · '
            f'{html.escape(alerts.bet_line(row))}</div>'
            f'<div class="t2">{html.escape(legs)} · {row["minutes_ago"]:.0f} min ago</div>'
            f'<div class="t3">{tags}<a href="quote?{html.escape(q)}" target="_blank">Quote it</a></div></div>')


def _list(title: str, items: list, basis: str) -> str:
    out = [f'<div class="hd">{title} <span class="dim">{len(items)}</span></div>']
    out += [_bet_html(r, why, basis) for r, why in items[:SHOW]]
    if len(items) > SHOW:
        out.append(f'<div class="dim" style="margin:0 2px 6px">and {len(items) - SHOW} more</div>')
    if not items:
        out.append('<div class="dim" style="margin:0 2px 6px">None right now.</div>')
    return "".join(out)


def _catch(n: int, on: bool) -> None:
    word = "leaves out" if on else "would leave out"
    st.html(f'<div class="al"><div class="catch">{word} <b>{n}</b> of the bets below</div></div>')


def _load(store) -> dict:
    """The saved settings (over the bot's defaults) and when they were saved, or the error reading them."""
    try:
        data, _ = store.read()
        if data is None:
            return {"settings": Settings(), "saved_at": None, "error": None}
        return {"settings": Settings.from_dict(data), "saved_at": data.get("saved_at"), "error": None}
    except Exception as exc:
        return {"settings": Settings(), "saved_at": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _reset(ss, s: Settings) -> None:
    """Put `s` on screen: new widget keys, since a widget keeps what the phone shows under its old key."""
    ss.al_draft = draft_of(s)
    ss.al_gen = ss.get("al_gen", 0) + 1


def _when(iso: str | None) -> str:
    if not iso:
        return "never (the bot uses its defaults, or its ALERT_* Variables; a save replaces them all)"
    try:
        t = datetime.fromisoformat(iso)
        tz = st.context.timezone
        if tz:
            from zoneinfo import ZoneInfo
            t = t.astimezone(ZoneInfo(tz))
        return t.strftime("%b %-d, %-I:%M %p")
    except Exception:
        return iso


def render(rows: list[dict], store, can_save: bool, save_note: str | None = None, unlock=None,
           error: str | None = None, window: tuple[int, float] = (60, 50)) -> None:
    """`rows`: scored bets (pipeline rows) for the preview; `store`: where settings are kept (GitHubFile or
    MemoryFile); `can_save`: this session may write them (else `save_note` says why, and `unlock` draws the
    PIN form when that's all it takes)."""
    ss = st.session_state
    if ss.get("al_toast"):
        st.toast(ss.pop("al_toast"))
    if "al_saved" not in ss:
        ss.al_saved = _load(store)
    saved = ss.al_saved
    if "al_draft" not in ss:
        _reset(ss, saved["settings"])
    d, gen = ss.al_draft, ss.al_gen

    def key(name: str) -> str:
        return f"al_{name}_{gen}"

    # Widgets already hold this run's taps: copy them into the draft before anything is worked out.
    for name in list(d):
        if name in ("pregame", "live"):
            for i, tier in enumerate(d[name]):
                for j, part in enumerate("se"):
                    if key(f"{name}{i}{part}") in ss:
                        tier[j] = ss[key(f"{name}{i}{part}")]
        elif key(name) in ss and ss[key(name)] is not None:
            d[name] = ss[key(name)]

    def seed(name: str, value) -> str:
        k = key(name)
        if k not in ss:
            ss[k] = value
        return k

    s = settings_of(d)
    view = preview(rows, s, everything_on(d))
    changed = s != saved["settings"]

    st.html(CSS + '<div class="al"><div class="sum">Changes here go to the Telegram alerts within a minute of '
            f'saving. Last saved: <b>{html.escape(str(_when(saved["saved_at"])))}</b>.</div></div>')
    if saved["error"]:
        st.warning(f"Couldn't read the saved settings, so these are the defaults: {saved['error']}")

    st.markdown("**When to alert**")
    st.segmented_control("EV basis", ["mid", "crx"], key=seed("basis", d["basis"]), required=True,
                         format_func=lambda b: f"EV {b.upper()}",
                         help="Which EV the tiers compare: at leg mids, or crossing each leg's spread (CRX, the "
                              "conservative one).")
    for kind, label in (("pregame", "Pregame"), ("live", "Live (a game has started)")):
        on = st.toggle(f"{label} alerts", key=seed(f"{kind}_on", d[f"{kind}_on"]))
        if not on:
            continue
        for i, (stake, ev) in enumerate(d[kind]):
            with st.container(horizontal=True, vertical_alignment="bottom"):
                st.number_input("Bets from \\$", min_value=1.0, step=100.0, format="%.0f",
                                key=seed(f"{kind}{i}s", float(stake)))
                st.number_input("need EV over %", min_value=0.0, step=5.0, format="%.0f",
                                key=seed(f"{kind}{i}e", float(ev)))
                if st.button("✕", key=key(f"{kind}{i}x"), help="Remove this tier", disabled=len(d[kind]) == 1):
                    del d[kind][i]
                    ss.al_gen += 1
                    st.rerun()
        if st.button(f"＋ Add a {kind} tier", key=key(f"{kind}add")):
            top = max(stake for stake, _ in d[kind])
            d[kind].append([top * 2, 50.0])
            ss.al_gen += 1
            st.rerun()

    st.markdown("**Leave out**")
    on = st.toggle("Thin leg books", key=seed("skip_thin", d["skip_thin"]),
                   help="A leg with no quote, a wide spread or little size at the top of its book: the fair price "
                        "is shaky.")
    with st.container(horizontal=True):
        st.number_input("spread over ¢", min_value=0.0, step=0.5, key=seed("thin_spread_cents", float(d["thin_spread_cents"])),
                        disabled=not on)
        st.number_input("or top of book under", min_value=0.0, step=10.0, format="%.0f",
                        key=seed("thin_size", float(d["thin_size"])), disabled=not on)
    _catch(view["catches"]["thin"], on)
    on = st.toggle("Same-game legs", key=seed("skip_same_game", d["skip_same_game"]),
                   help="Legs from one game aren't independent, so multiplying their prices gets the fair price wrong.")
    _catch(view["catches"]["same_game"], on)
    for cap, label in CAPS.items():
        with st.container(horizontal=True, vertical_alignment="center"):
            on = st.toggle(label, key=seed(f"{cap}_on", d[f"{cap}_on"]))
            st.number_input(label, min_value=1, step=1, key=seed(cap, int(d[cap])), disabled=not on,
                            label_visibility="collapsed", width=110)
        _catch(view["catches"]["legs" if cap == "max_legs" else "legs_live" if cap == "max_legs_live" else "leagues"], on)
    st.caption("Combos with a crypto, commodity or other financial leg never alert, as everywhere in the app.")

    with st.container(horizontal=True):
        if st.button("Save", type="primary", disabled=not (can_save and changed), key=key("save")):
            try:
                store.write({**s.to_dict(), "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                            "Alert settings from the app")
                ss.al_saved = _load(store)
                ss.al_toast = "Saved. The bot picks these up within a minute."
                st.rerun()
            except Exception as exc:
                st.error(f"Couldn't save: {type(exc).__name__}: {exc}"[:200])
        if st.button("Undo changes", disabled=not changed, key=key("undo")):
            _reset(ss, saved["settings"])
            st.rerun()
        if st.button("Defaults", disabled=s == Settings(), key=key("defaults")):
            _reset(ss, Settings())
            st.rerun()
        if st.button("↻", help="Read the saved settings again", key=key("reload")):
            ss.al_saved = _load(store)
            _reset(ss, ss.al_saved["settings"])
            st.rerun()
    if changed:
        st.caption("Unsaved changes: the list below already uses them." + (f" {save_note}" if save_note and not can_save else ""))
    elif save_note and not can_save:
        st.caption(save_note)
    if unlock is not None and not can_save:
        unlock()

    minutes, min_bet = window
    head = (f'<div class="al"><div class="hd">With these settings</div><div class="sum">The last {minutes} min of '
            f'${min_bet:,.0f}+ bets: <b>{view["rows"]:,}</b> scored, <b>{view["cleared"]}</b> clear their EV bar, '
            f'<b>{len(view["alert"])}</b> would alert and <b>{len(view["dropped"])}</b> are left out. The bot itself '
            'only alerts a bet in the 5 minutes after a fill, once.</div>')
    if error:
        head += f'<div class="sum warn">{html.escape(error)}</div>'
    st.html(head + _list("Would alert", view["alert"], s.basis) + _list("Left out", view["dropped"], s.basis) + "</div>")
