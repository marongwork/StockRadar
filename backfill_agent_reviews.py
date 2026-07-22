#!/usr/bin/env python3
"""Backfill Agent Council metadata without rewriting historical decisions."""

import json
import sqlite3
import sys
from pathlib import Path

import stock_picker
from agent_council import evaluate_candidate


def backfill(base_dir: str = ".") -> int:
    db_path = Path(base_dir) / "invest.db"
    if not db_path.exists():
        raise SystemExit(f"ERROR: {db_path} not found")
    original = stock_picker.DB_PATH
    stock_picker.DB_PATH = db_path
    conn = stock_picker.ensure_stock_picks_schema()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM stock_picks ORDER BY id").fetchall()
    for row in rows:
        pick = dict(row)
        pick["main"] = pick.get("main_net")
        pick["chg"] = pick.get("chg_pct")
        review = evaluate_candidate(pick)
        conn.execute(
            """UPDATE stock_picks
               SET agent_consensus=?,agent_confidence=?,agent_disagreement=?,agent_reviews_json=?,
                   risk_level=?,risk_veto=?,risk_reasons=? WHERE id=?""",
            (
                review["consensus"], review["confidence"], review["disagreement"],
                json.dumps(review, ensure_ascii=False, separators=(",", ":")),
                review["risk_level"], int(review["risk_veto"]), "+".join(review["risk_flags"]), row["id"],
            ),
        )
    conn.commit()
    conn.close()
    stock_picker.DB_PATH = original
    print(f"Backfilled Agent Council metadata: {len(rows)} signals")
    return len(rows)


if __name__ == "__main__":
    backfill(sys.argv[1] if len(sys.argv) > 1 else ".")
