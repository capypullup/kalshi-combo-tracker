"""Made-up bets that each trip one alert filter (tests/test_alert_settings.py and tests/alerts_app.py)."""

from test_alerts import leg, row


def bets() -> list[dict]:
    nba = [leg(f"KXNBAGAME-26OCT21G{i}", f"KXNBAGAME-26OCT21G{i}-H{i}") for i in range(6)]
    nfl = leg("KXNFLGAME-26OCT21SEADEN", "KXNFLGAME-26OCT21SEADEN-SEA")
    nhl = leg("KXNHLGAME-26OCT21BOSNYR", "KXNHLGAME-26OCT21BOSNYR-BOS")
    good = row("KXMVECOMBO-GOOD")  # $600 at +50%, pregame
    thin = row("KXMVECOMBO-THIN")
    thin["leg_detail"][0]["spread"] = 0.05
    same = row("KXMVECOMBO-SAME", legs=[leg("KXNBAGAME-26OCT21ATLORL", "KXNBAGAME-26OCT21ATLORL-ATL"),
                                        leg("KXNBAPTS-26OCT21ATLORL", "KXNBAPTS-26OCT21ATLORL-ATLTYOUNG11-25")])
    six = row("KXMVECOMBO-SIX", legs=nba)
    live4 = row("KXMVECOMBO-LIVE4", count=12000, price=0.10, legs=nba[:4])  # $1,200 at 0.10 vs fair ~0.29
    for l in live4["leg_detail"]:
        l["hours_to_start"] = -0.5
    three = row("KXMVECOMBO-3LG", legs=nba[:1] + [nfl, nhl])
    small = row("KXMVECOMBO-SMALL", count=1000)  # $200: under every pregame tier here
    return [good, thin, same, six, live4, three, small]
