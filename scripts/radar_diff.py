"""Radar state diff - compare current cluster snapshot with last run.

Usage:
    uv run python scripts/radar_diff.py [--state PATH]

Stdout: space-separated list of tickers with new or meaningfully changed
clusters (empty string if nothing changed). The caller (run_daily_radar.sh)
decides whether to generate reports.

State file format (.radar_state.json):
  { "clusters": { "TICKER|window_start": { "score": "12.5000",
                                            "insider_count": 3 } } }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

STATE_PATH_DEFAULT = ROOT / "output_reports" / ".radar_state.json"
SCORE_CHANGE_THRESHOLD = 5.0  # report when |new_score - prev_score| >= this


def _cluster_key(ticker: str, window_start: str, window_end: str) -> str:
    return f"{ticker}|{window_start}|{window_end}"


def _dedupe_clusters(clusters: list[dict]) -> dict[str, dict]:
    """Keep highest-score entry per ticker+window_start; drop overlapping windows.

    window_end drifts as signal detection refines the window, so the same
    real event produces multiple rows with the same window_start but different
    window_end values. Using ticker|window_start as the state key collapses
    these to one authoritative entry (max score wins).
    """
    best: dict[str, dict] = {}
    for c in clusters:
        key = f"{c['ticker']}|{c['window_start']}"
        if key not in best or float(c["score"]) > float(best[key]["score"]):
            best[key] = c
    return best


async def _get_current_clusters() -> list[dict]:
    """Query insider_clusters and return raw rows as a list of dicts."""
    from sqlalchemy import text

    from trailing_edge.core.db import get_session, init_db

    await init_db()
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT ticker, window_start::text, window_end::text, "
                    "cluster_score::text, insider_count "
                    "FROM insider_clusters"
                )
            )
        ).all()

    return [
        {
            "ticker": row.ticker,
            "window_start": row.window_start,
            "window_end": row.window_end,
            "score": row.cluster_score,
            "insider_count": row.insider_count,
        }
        for row in rows
    ]


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state_path: Path, current: dict[str, dict]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "clusters": {
            k: {"score": v["score"], "insider_count": v["insider_count"]}
            for k, v in current.items()
        }
    }
    state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _diff(current: dict[str, dict], previous: dict) -> list[str]:
    """Return sorted list of tickers with new or meaningfully changed clusters.

    A cluster is reportable when:
    - Key not in previous state (new cluster), OR
    - |score_change| >= SCORE_CHANGE_THRESHOLD, OR
    - insider_count increased (new insider joined the cluster)
    """
    prev_clusters: dict[str, dict] = previous.get("clusters", {})
    changed: set[str] = set()

    for key, info in current.items():
        if key not in prev_clusters:
            changed.add(info["ticker"])
            continue
        prev = prev_clusters[key]
        if abs(float(info["score"]) - float(prev.get("score", 0))) >= SCORE_CHANGE_THRESHOLD:
            changed.add(info["ticker"])
            continue
        if info["insider_count"] > int(prev.get("insider_count", 0)):
            changed.add(info["ticker"])

    return sorted(changed)


async def main(state_path: Path) -> None:
    current = _dedupe_clusters(await _get_current_clusters())
    previous = _load_state(state_path)
    changed = _diff(current, previous)
    _save_state(state_path, current)
    if changed:
        print(" ".join(changed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Radar state diff - output changed tickers")
    parser.add_argument(
        "--state", default=str(STATE_PATH_DEFAULT), help="Path to .radar_state.json"
    )
    args = parser.parse_args()
    asyncio.run(main(Path(args.state)))
