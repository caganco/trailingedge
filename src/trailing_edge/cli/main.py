"""CLI entrypoint for trailingedge."""
import asyncio
from datetime import date, timedelta

import click

from trailing_edge.core.db import init_db
from trailing_edge.core.logging import configure_logging, get_logger
from trailing_edge.core.time import now_tr
from trailing_edge.scrapers.kap.insider import KapInsiderScraper

_log = get_logger(__name__)


@click.group()
def cli() -> None:
    """TrailingEdge - BIST insider-transaction disclosure analytics."""


# ── scrape ────────────────────────────────────────────────────────────────────

@cli.group()
def scrape() -> None:
    """Scrape commands."""


@scrape.command("kap-insider")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--until", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--last-hours", type=int, default=None)
def kap_insider(since, until, last_hours) -> None:
    """Scrape KAP DKB insider transaction disclosures."""
    configure_logging()

    if last_hours is not None:
        to_date = now_tr().date()
        from_date = (now_tr() - timedelta(hours=last_hours)).date()
    elif since and until:
        from_date = since.date()
        to_date = until.date()
    else:
        raise click.UsageError("Provide --last-hours OR both --since and --until")

    asyncio.run(_run_scrape(from_date, to_date))


async def _run_scrape(from_date: date, to_date: date) -> None:
    await init_db()
    scraper = KapInsiderScraper()
    await scraper.run(from_date, to_date)


# ── prices ────────────────────────────────────────────────────────────────────

@cli.group()
def prices() -> None:
    """Price data commands."""


@prices.command("backfill")
@click.option("--days", default=400, show_default=True, help="Days of history to fetch")
def prices_backfill(days: int) -> None:
    """Fetch BIST OHLCV history from yfinance for all known tickers."""
    configure_logging()
    asyncio.run(_run_prices_backfill(days))


async def _run_prices_backfill(days: int) -> None:
    from sqlalchemy import select

    from trailing_edge.core.config import get_config
    from trailing_edge.core.db import get_session
    from trailing_edge.data.prices import fetch_and_store_prices
    from trailing_edge.models.kap import KapInsiderTransaction

    await init_db()

    async with get_session() as session:
        result = await session.execute(
            select(KapInsiderTransaction.ticker).distinct()
        )
        tickers = [row[0] for row in result.all()]

    # The benchmark is a price series like any other. Without it every abnormal
    # return is NULL and the base rate has nothing to report, so it is fetched
    # alongside the signal universe rather than left to a separate manual step.
    benchmark = get_config()["signals"]["returns"]["benchmark"]
    if benchmark not in tickers:
        tickers.append(benchmark)

    _log.info(
        "prices_backfill_start",
        ticker_count=len(tickers),
        days=days,
        benchmark=benchmark,
    )

    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    results = await fetch_and_store_prices(tickers, start_date, end_date)

    if results.get(benchmark, 0) == 0:
        click.echo(
            f"WARNING: benchmark {benchmark} returned no price rows - "
            "abnormal returns cannot be computed.",
            err=True,
        )

    successful = sum(1 for v in results.values() if v > 0)
    total_rows = sum(results.values())
    _log.info("prices_backfill_done", successful=successful, total_tickers=len(tickers), total_rows=total_rows)
    click.echo(f"Backfill complete: {successful}/{len(tickers)} tickers, {total_rows:,} price rows stored.")


# ── signal ────────────────────────────────────────────────────────────────────

@cli.group()
def signal() -> None:
    """Signal detection and reporting commands."""


@signal.command("detect")
def signal_detect() -> None:
    """Detect insider clusters across full history and compute forward returns."""
    configure_logging()
    asyncio.run(_run_signal_detect())


async def _run_signal_detect() -> None:
    from trailing_edge.core.config import get_config
    from trailing_edge.signals.cluster import detect_clusters
    from trailing_edge.signals.returns import calculate_outcomes

    await init_db()
    cfg = get_config()["signals"]["returns"]
    horizons: list[int] = cfg["horizons"]

    clusters = await detect_clusters()
    click.echo(f"Detected {len(clusters)} cluster event(s).")

    await calculate_outcomes(clusters, horizons)
    click.echo(f"Outcomes calculated for {len(clusters)} cluster(s) x {len(horizons)} horizons.")


@signal.command("daily-report")
@click.option(
    "--date", "as_of",
    default=None,
    help="Report date YYYY-MM-DD (default: today)",
)
def signal_daily_report(as_of: str | None) -> None:
    """Generate ranked daily signal report (stdout + JSON)."""
    configure_logging()
    asyncio.run(_run_daily_report(as_of))


async def _run_daily_report(as_of: str | None) -> None:
    from trailing_edge.reports.daily_signal import generate_daily_report

    await init_db()
    target = date.fromisoformat(as_of) if as_of else None
    report = await generate_daily_report(target)
    click.echo(f"\nReport saved: {report.report_path}")


@signal.command("base-rate")
@click.option("--horizon", default=20, show_default=True, type=int, help="Horizon in trading days")
@click.option("--min-score", default=0.0, show_default=True, type=float, help="Minimum cluster score")
def signal_base_rate(horizon: int, min_score: float) -> None:
    """Print historical base rate statistics for a given horizon."""
    configure_logging()
    asyncio.run(_run_base_rate(horizon, min_score))


async def _run_base_rate(horizon: int, min_score: float) -> None:
    from trailing_edge.signals.base_rate import compute_base_rate

    await init_db()
    stats = await compute_base_rate(horizon, min_score)

    click.echo(f"\n-- Base Rate: {horizon}d horizon (min score: {min_score}) --")
    click.echo(f"  Benchmark:              {stats.benchmark_ticker or '-'}")
    click.echo(f"  Total signals:          {stats.total_signals}")
    click.echo(f"  With outcome (priced):  {stats.signals_with_outcome}")
    click.echo(
        f"  Hit rate (AR > 0):      {stats.hit_rate_pct:.1f}%  "
        f"95% CI [{stats.hit_rate_ci_low_pct:.1f}, {stats.hit_rate_ci_high_pct:.1f}]"
    )
    click.echo(f"  Mean abnormal return:   {stats.mean_abnormal_return_pct:+.2f}%")
    click.echo(f"  Median abnormal return: {stats.median_abnormal_return_pct:+.2f}%")
    click.echo(f"  t / p:                  {stats.t_stat:.2f} / {stats.p_value:.4f}")
    click.echo(f"  Best / worst AR:        {stats.best_abnormal_return_pct:+.2f}% / {stats.worst_abnormal_return_pct:+.2f}%")
    click.echo("")
    click.echo(f"  VERDICT: {stats.verdict}")
    if stats.verdict == "INSUFFICIENT_POWER":
        click.echo(
            f"  n={stats.signals_with_outcome}, need ~{stats.required_n_for_power}. "
            "The estimates above are not evidence in either direction."
        )
    click.echo(
        f"  (Raw return would have claimed {stats.mean_raw_return_pct:+.2f}%; "
        f"the market alone gave {stats.mean_benchmark_return_pct:+.2f}%.)"
    )


# ── graph ─────────────────────────────────────────────────────────────────────

@cli.group()
def graph() -> None:
    """Network graph commands."""


@graph.command("scrape-management")
@click.option("--ticker", default=None, help="Single ticker (test mode, e.g. KAPLM)")
def graph_scrape_management(ticker: str | None) -> None:
    """Scrape KAP management board for all companies (or --ticker for one)."""
    configure_logging()
    asyncio.run(_run_scrape_management(ticker))


async def _run_scrape_management(ticker: str | None) -> None:
    from trailing_edge.scrapers.kap.management import scrape_all_companies

    await init_db()
    tickers = [ticker] if ticker else None
    results = await scrape_all_companies(tickers)
    total_members = sum(results.values())
    ok = sum(1 for v in results.values() if v > 0)
    click.echo(
        f"Management scrape done: {ok}/{len(results)} companies had board data, "
        f"{total_members} roles inserted."
    )


@graph.command("build")
def graph_build() -> None:
    """Build company network graph from board_interlocks and print stats."""
    configure_logging()
    asyncio.run(_run_graph_build())


async def _run_graph_build() -> None:
    from trailing_edge.signals.graph import build_company_graph, find_interlock_clusters

    await init_db()
    G = await build_company_graph()
    clusters = find_interlock_clusters(G)
    click.echo(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    click.echo(f"Interlock clusters (>=2 companies): {len(clusters)}")


@graph.command("report")
@click.option("--min-score", default=0.0, show_default=True, type=float, help="Minimum network alpha score")
def graph_report(min_score: float) -> None:
    """Generate network analysis report (stdout + JSON)."""
    configure_logging()
    asyncio.run(_run_graph_report(min_score))


async def _run_graph_report(min_score: float) -> None:
    from trailing_edge.reports.network_report import generate_network_report

    await init_db()
    report = await generate_network_report(min_alpha_score=min_score)
    click.echo(f"\nReport saved: {report.report_path}")


@graph.command("show")
@click.argument("ticker")
def graph_show(ticker: str) -> None:
    """Show network neighbors and interlocks for a ticker."""
    configure_logging()
    asyncio.run(_run_graph_show(ticker))


async def _run_graph_show(ticker: str) -> None:
    from trailing_edge.signals.graph import build_company_graph

    await init_db()
    G = await build_company_graph()
    if ticker not in G:
        click.echo(f"No interlocks found for {ticker}")
        return
    neighbors = sorted(G.neighbors(ticker))
    click.echo(f"\n{ticker} -- {len(neighbors)} connected company/companies:")
    for n in neighbors:
        edge = G[ticker][n]
        persons_str = ", ".join(sorted(edge.get("shared_persons", [])))
        click.echo(f"  {n}: weight={edge.get('weight', 1)} -- {persons_str}")


# ── tsg ───────────────────────────────────────────────────────────────────────

@cli.group()
def tsg() -> None:
    """Ticaret Sicil Gazetesi scraping commands."""


@tsg.command("seed")
@click.option("--companies", multiple=True, help="Company names to scrape (repeatable)")
@click.option("--actor", default=None, help="KAP actor full name - uses actor_seeds.yaml")
@click.option("--high-value", is_flag=True, help="Scrape all validated actors from actor_seeds.yaml")
@click.option("--validate-only", is_flag=True, help="Sadece seed'leri doğrula, scrape etme (dry-run)")
@click.option("--top", default=20, show_default=True, type=int,
              help="(vestigial - high-value artık yaml'daki tüm aktörleri alır)")
def tsg_seed(
    companies: tuple[str, ...],
    actor: str | None,
    high_value: bool,
    validate_only: bool,
    top: int,
) -> None:
    """Scrape unlisted companies from TSG by trade name.

    Semi-automatic: a visible browser opens and you sign in, solving the
    login CAPTCHA by hand once; the rest of the run proceeds automatically.
    """
    configure_logging()
    asyncio.run(_run_tsg_seed(list(companies), actor, high_value, validate_only, top))


async def _run_tsg_seed(
    companies: list[str],
    actor: str | None,
    high_value: bool,
    validate_only: bool,
    top: int,
) -> None:
    from trailing_edge.scrapers.ticaret_sicil.scraper import run_seed
    from trailing_edge.scrapers.ticaret_sicil.targets import validate_actor_seeds

    if validate_only:
        valid = validate_actor_seeds()
        total = sum(len(c) for c in valid.values())
        click.echo(f"Geçerli: {len(valid)} aktör, {total} şirket (per-aktör toplam)")
        for a, comps in valid.items():
            click.echo(f"  {a}: {comps}")
        return

    await init_db()

    if high_value:
        valid = validate_actor_seeds()
        seed_list = list(dict.fromkeys(c for comps in valid.values() for c in comps))
        click.echo(f"High-value: {len(valid)} aktör, {len(seed_list)} unique şirket")
    elif actor:
        seed_list = validate_actor_seeds().get(actor, [])
        click.echo(f"Actor '{actor}': {len(seed_list)} seed companies")
    elif companies:
        seed_list = list(companies)
    else:
        raise click.UsageError("Provide --companies NAME, --actor NAME, or --high-value")

    if not seed_list:
        click.echo("No companies to scrape.")
        return

    result = await run_seed(seed_list)
    click.echo("\nTSG seed complete:")
    click.echo(f"  Companies inserted: {result.companies_inserted}")
    click.echo(f"  Roles inserted:     {result.roles_inserted}")
    click.echo(f"  Persons matched (KAP): {result.persons_matched}")
    click.echo(f"  Persons unmatched:     {result.persons_unmatched}")


# ── report ────────────────────────────────────────────────────────────────────

@cli.group()
def report() -> None:
    """Report generation commands."""


@report.command("generate")
@click.option("--ticker", default=None, help="Single ticker (e.g. KAPLM)")
@click.option(
    "--output", default="pdf",
    type=click.Choice(["pdf", "html", "both"]), show_default=True,
    help="Output format",
)
@click.option("--all-signals", is_flag=True, help="Generate for all tickers with active clusters")
def report_generate(ticker: str | None, output: str, all_signals: bool) -> None:
    """Generate insider-activity brief (HTML and/or PDF)."""
    configure_logging()
    asyncio.run(_run_report_generate(ticker, output, all_signals))


async def _run_report_generate(ticker: str | None, output: str, all_signals: bool) -> None:
    from trailing_edge.reports.forensic_report import (
        gather_all_signal_tickers,
        generate_forensic_report,
    )

    await init_db()

    if all_signals:
        tickers = await gather_all_signal_tickers()
    elif ticker:
        tickers = [ticker]
    else:
        raise click.UsageError("Provide --ticker SYMBOL or --all-signals")

    for t in tickers:
        path = await generate_forensic_report(t, output_format=output)
        click.echo(f"  {t}: {path}")

    click.echo(f"\nGenerated {len(tickers)} report(s).")


@report.command("cross-reference")
@click.option("--top", default=20, show_default=True, type=int, help="Number of high-value actors")
@click.option("--person", default=None, help="Single KAP actor full name (e.g. 'RIZA KANDEMİR')")
@click.option(
    "--output", default="pdf",
    type=click.Choice(["pdf", "html", "both"]), show_default=True,
    help="Output format",
)
def report_cross_reference(top: int, person: str | None, output: str) -> None:
    """Generate cross-reference brief (KAP ↔ TSG)."""
    configure_logging()
    asyncio.run(_run_cross_reference(top, person, output))


async def _run_cross_reference(top: int, person: str | None, output: str) -> None:
    from trailing_edge.reports.cross_reference_report import generate_cross_reference_report

    await init_db()
    path = await generate_cross_reference_report(top_n=top, person=person, output_format=output)
    click.echo(f"  Cross-reference report: {path}")
