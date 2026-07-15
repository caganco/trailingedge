"""Network signal: do connected institutional insiders carry a capturable edge?

The premise of the whole project was to trace the connected actors who position ahead of a
move. Every isolated public signal tested here fails after cost (see docs/METHODOLOGY.md).
This asks the network version: split disclosed insider BUYS by the actor's connectivity - how
many distinct companies they have traded in, computed POINT-IN-TIME (past trades only, no
look-ahead) - and by whether the actor is an institution (holding / fund / bank) rather than
an individual.

The finding, on the full 2015-2026 archive:

    regime   institutional-hub buy (>=3 cos)   everyone else
    2015-18  net +22.3%  t=4.40                net -0.8%
    2019-20  net  +5.3%  t=3.10                net -11.6%
    2021-26  net  -7.3%  t=-2.59               net  -5.6%

Connected institutional accumulation was a real, point-in-time, net-of-cost edge through
2020 - the single strongest result in the project, and a validation of the "follow the
connected actors" thesis - and it decayed to negative in 2021-2026 along with every other
signal. A coordinated PACK (three or more different insiders piling into one name inside 20
days) is the opposite: it UNDERPERFORMS, sharply so recently (net -10% in 2021-26), i.e. the
crowd piling in is a contrarian tell, not the smart money.

    uv run python scripts/network_signal.py
"""
from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import math
import statistics
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, "src")

from sqlalchemy import text  # noqa: E402

from trailing_edge.core.db import get_session, init_db  # noqa: E402
from trailing_edge.signals.costs import round_trip_cost  # noqa: E402

ORDER = Decimal("25000")
HUB_MIN_COMPANIES = 3
PACK_WINDOW_DAYS = 20
PACK_MIN_BUYERS = 3

# Insider-name field carries parse artefacts (mis-captured table headers); drop them so the
# network is built from real actor names only.
_BAD_NAME = ("PAY ALIM", "BILDIRIM", "BİLDİRİM", "UNKNOWN", "HISSE", "HİSSE", "SENED", "SATIM")
_INST = ("HOLD", "ANON", "YATIRIM", "FON", "SERMAYE", "BANKASI", "GİRİŞİM", "A.Ş.")


def clean_name(raw: str | None) -> str | None:
    n = (raw or "").strip().replace("\n", " ").replace("\t", " ")
    while "  " in n:
        n = n.replace("  ", " ")
    up = n.upper()
    if len(n) < 5 or any(b in up for b in _BAD_NAME):
        return None
    return n


def is_institution(name: str) -> bool:
    return any(k in name.upper() for k in _INST)


def regime(year: int) -> str:
    if year <= 2018:
        return "2015-2018"
    if year <= 2020:
        return "2019-2020"
    return "2021-2026"


def _report(rows: list[tuple[float, float | None]]) -> tuple[int, float, float, float]:
    ars = [a for a, _ in rows]
    nets = [n for _, n in rows if n is not None]
    mean = statistics.fmean(ars)
    sd = statistics.stdev(ars) if len(ars) > 1 else 0.0
    t = mean / (sd / math.sqrt(len(ars))) if sd > 0 else 0.0
    net = statistics.fmean(nets) if nets else float("nan")
    return len(ars), mean, net, t


async def main_async() -> None:
    await init_db()
    async with get_session() as s:
        buys = (
            await s.execute(
                text(
                    """
                    SELECT t.insider_name, t.ticker, d.published_at::date AS pub
                    FROM kap_insider_transactions t
                    JOIN kap_disclosures d ON d.id = t.disclosure_id
                    WHERE t.transaction_type = 'BUY' AND d.published_at IS NOT NULL
                    ORDER BY d.published_at
                    """
                )
            )
        ).all()

        px = (
            await s.execute(
                text(
                    """
                    SELECT ticker, price_date, close_try, raw_close_try, volume
                    FROM price_history WHERE close_try > 0 ORDER BY ticker, price_date
                    """
                )
            )
        ).all()
        series: dict[str, list] = defaultdict(list)
        for tic, d, adj, raw, vol in px:
            series[tic].append((d, float(adj), float(raw) if raw else None, vol or 0))
        xu_rows = (
            await s.execute(
                text("SELECT price_date, close_try FROM price_history WHERE ticker='XU100' ORDER BY price_date")
            )
        ).all()
        xu = {d: float(c) for d, c in xu_rows if c and c > 0}
        xu_days = sorted(xu)

        def xu_ret(d0, d1) -> float | None:
            def near(d):
                i = bisect.bisect_right(xu_days, d) - 1
                return xu[xu_days[i]] if i >= 0 else None

            a, b = near(d0), near(d1)
            return (b / a - 1) if a and b and a > 0 else None

        # Per-ticker buy events, and point-in-time actor connectivity.
        by_ticker: dict[str, list[tuple[dt.date, str]]] = defaultdict(list)
        for name, tic, pub in buys:
            nm = clean_name(name)
            if not nm:
                continue
            for c in (x for x in str(tic).replace(" ", "").split(",") if x):
                by_ticker[c].append((pub, nm))

        seen: dict[str, set[str]] = defaultdict(set)
        hub_buckets: dict[tuple[str, bool], list] = defaultdict(list)
        pack_buckets: dict[tuple[str, bool], list] = defaultdict(list)

        for name, tic, pub in buys:  # chronological
            nm = clean_name(name)
            comps = [x for x in str(tic).replace(" ", "").split(",") if x]
            pit_degree = len(seen[nm]) if nm else 0
            hub_inst = bool(nm) and is_institution(nm) and pit_degree >= HUB_MIN_COMPANIES
            if nm:
                for c in comps:
                    seen[nm].add(c)

            for c in comps:
                ser = series.get(c)
                if not ser:
                    continue
                dates = [x[0] for x in ser]
                i = bisect.bisect_right(dates, pub)  # entry at t+1 after public disclosure
                if i < 22 or i >= len(ser) - 20:
                    continue
                p0, p1 = ser[i][1], ser[i + 20][1]
                if p0 <= 0 or p1 <= 0:
                    continue
                mkt = xu_ret(ser[i][0], ser[i + 20][0])
                if mkt is None:
                    continue
                ar = (p1 / p0 - 1 - mkt) * 100

                net = None
                raws = [ser[k][2] for k in range(i - 21, i)]
                if all(r is not None for r in raws):
                    closes = [Decimal(str(ser[k][1])) for k in range(i - 21, i)]
                    adv = statistics.fmean(float(raws[q]) * ser[i - 21 + q][3] for q in range(21))
                    rt = round_trip_cost(
                        closes, closes, closes, ORDER, Decimal(str(adv)),
                        last_traded_price=Decimal(str(raws[-1])),
                    )
                    net = ar - float(rt.total_pct) if rt else None

                hub_buckets[(regime(pub.year), hub_inst)].append((ar, net))

                pack = len(
                    {n2 for p2, n2 in by_ticker[c]
                     if pub - dt.timedelta(days=PACK_WINDOW_DAYS) <= p2 <= pub}
                )
                pack_buckets[(regime(pub.year), pack >= PACK_MIN_BUYERS)].append((ar, net))

    def print_table(title: str, buckets: dict, yes_label: str, no_label: str) -> None:
        print(f"\n{title}")
        print(f"    {'regime':<12}{'group':<16}{'N':>6}{'gross%':>8}{'net%':>8}{'t':>7}")
        for reg in ("2015-2018", "2019-2020", "2021-2026"):
            for flag, label in ((True, yes_label), (False, no_label)):
                v = buckets.get((reg, flag), [])
                if len(v) < 25:
                    continue
                n, mean, net, t = _report(v)
                print(f"    {reg:<12}{label:<16}{n:>6}{mean:>8.2f}{net:>8.2f}{t:>7.2f}")

    print_table(
        "Connected institutional insider buys (point-in-time >=3 companies):",
        hub_buckets, "HUB-institution", "everyone else",
    )
    print_table(
        "Coordinated PACK (>=3 distinct buyers in one name within 20 days):",
        pack_buckets, "PACK", "single/few",
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
