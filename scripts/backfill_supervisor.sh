#!/usr/bin/env bash
# Supervises the KAP backfill: runs it, and when it exits (normal finish OR a silent death
# like the ones that kept happening mid-backoff), re-runs it. Two reasons a re-run is needed
# even when nothing crashed:
#   1. the process has died silently more than once under nohup; auto-restart removes the
#      human from that loop.
#   2. backfill_kap_insider.py computes its todo list once at startup, so months that go
#      PARTIAL *during* a run are only swept by the NEXT run. Re-running is how SUCCESS
#      climbs to 139.
# It stops when SUCCESS stops rising across a full pass (the sweep has converged), or when
# SUCCESS reaches the month count. Everything goes to backfill.log; the supervisor's own
# lines are tagged SUPERVISOR.
set -u
cd "$(dirname "$0")/.."

LOG=backfill.log
TOTAL_MONTHS=139

success_count() {
  uv run python - <<'PY' 2>/dev/null | grep -oE '^[0-9]+$' | tail -1
import asyncio, sys
sys.path.insert(0, "src")
from sqlalchemy import text
from trailing_edge.core.db import get_session, init_db
async def m():
    await init_db()
    async with get_session() as s:
        n = (await s.execute(text(
            "SELECT count(DISTINCT metadata->>'from_date') FROM scraper_runs WHERE status='SUCCESS'"
        ))).scalar()
        print(int(n or 0))
asyncio.run(m())
PY
}

# Total stored disclosures. This, not the SUCCESS month count, is the progress signal: during
# the forward scan SUCCESS barely moves (fresh months land as PARTIAL), but the disclosure
# count climbs every pass. It only plateaus once the frontier has reached the present AND the
# sweeps stop recovering deferred filings - which is the true convergence.
disclosure_count() {
  uv run python - <<'PY' 2>/dev/null | grep -oE '^[0-9]+$' | tail -1
import asyncio, sys
sys.path.insert(0, "src")
from sqlalchemy import text
from trailing_edge.core.db import get_session, init_db
async def m():
    await init_db()
    async with get_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM kap_disclosures"))).scalar()
        print(int(n or 0))
asyncio.run(m())
PY
}

prev_succ=-1
prev_disc=-1
pass_num=0
while true; do
  pass_num=$((pass_num + 1))
  succ=$(success_count)
  disc=$(disclosure_count)
  echo "{\"event\":\"SUPERVISOR\",\"pass\":$pass_num,\"success\":$succ,\"disclosures\":$disc}" >> "$LOG"

  if [ "$succ" -ge "$TOTAL_MONTHS" ]; then
    echo "{\"event\":\"SUPERVISOR\",\"done\":true,\"reason\":\"success>=months\",\"success\":$succ}" >> "$LOG"
    break
  fi
  # Converged only when a WHOLE pass moved NEITHER the SUCCESS month count NOR the disclosure
  # count. SUCCESS alone is the wrong signal - it is flat all through the forward scan while
  # disclosures pour in. Requiring both to stall means the frontier has reached the present
  # and the sweeps have stopped recovering anything.
  if [ "$pass_num" -gt 1 ] && [ "$succ" -le "$prev_succ" ] && [ "$disc" -le "$prev_disc" ]; then
    echo "{\"event\":\"SUPERVISOR\",\"done\":true,\"reason\":\"plateau\",\"success\":$succ,\"disclosures\":$disc}" >> "$LOG"
    break
  fi
  prev_succ=$succ
  prev_disc=$disc

  uv run python scripts/backfill_kap_insider.py --from 2015-01-01 >> "$LOG" 2>&1
  echo "{\"event\":\"SUPERVISOR\",\"run_exited\":true,\"pass\":$pass_num}" >> "$LOG"
  sleep 5
done
