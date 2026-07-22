#!/usr/bin/env python3
"""Run the three radar backtests once after each trading day."""
import fcntl
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCK_PATH = BASE_DIR / ".daily_backtest.lock"
DB_PATH = BASE_DIR / "invest.db"


def run(command):
    print("RUN", " ".join(command), flush=True)
    subprocess.run(command, cwd=BASE_DIR, check=True)


def prune_same_day_runs(db_path=DB_PATH, run_date=None):
    """Keep only the newest successful run per strategy for a calendar day."""
    run_date = run_date or date.today().isoformat()
    conn = sqlite3.connect(db_path)
    stale_ids = [
        row[0] for row in conn.execute(
            """SELECT id FROM backtest_runs
               WHERE substr(run_at,1,10)=?
                 AND id NOT IN (
                   SELECT MAX(id) FROM backtest_runs
                   WHERE substr(run_at,1,10)=? GROUP BY strategy
                 )""",
            (run_date, run_date),
        )
    ]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        conn.execute(f"DELETE FROM backtest_trades WHERE run_id IN ({placeholders})", stale_ids)
        conn.execute(f"DELETE FROM backtest_runs WHERE id IN ({placeholders})", stale_ids)
        conn.commit()
        print(f"Pruned {len(stale_ids)} superseded same-day backtest runs.")
    conn.close()


def main():
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Daily backtest is already running; skipped.")
            return 0

        end = date.today()
        start = end - timedelta(days=31)
        common = [
            sys.executable, str(BASE_DIR / "strategy_backtest.py"),
            "--start", start.isoformat(), "--end", end.isoformat(),
            "--fee-bps", "5", "--slippage-bps", "20", "--min-age-days", "2",
        ]
        for strategy in ("all", "trend", "limit_up", "agent_council"):
            run(common + ["--strategy", strategy])
        prune_same_day_runs()
        run([sys.executable, str(BASE_DIR / "generate_stock_lab.py"), str(BASE_DIR)])
        print(f"Daily backtest complete: {start} to {end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
