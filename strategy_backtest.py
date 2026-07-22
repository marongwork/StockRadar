#!/usr/bin/env python3
"""Strategy backtest system for saved stock-pick signals.

MVP scope:
- Uses stock_picks as the signal source, preserving every historical pick batch.
- Fetches A-share daily OHLC from Sina.
- Simulates next-day entry, stop/target/time exits, fees, and slippage.
- Stores run summaries and trade-level results in SQLite for the website.
"""
import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "invest.db"

DEFAULTS = {
    "trend": {"max_days": 20, "stop_pct": 7.0, "target_pct": 20.0},
    "limit_up": {"max_days": 5, "stop_pct": 5.0, "target_pct": 10.0},
}

BENCHMARKS = {
    "sh000001": "上证指数",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}


def db_conn():
    if not DB_PATH.exists():
        raise SystemExit(f"ERROR: {DB_PATH} not found")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            strategy TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            horizon_days INTEGER,
            fee_bps REAL,
            slippage_bps REAL,
            trades INTEGER,
            wins INTEGER,
            losses INTEGER,
            win_rate REAL,
            avg_return REAL,
            median_return REAL,
            total_return REAL,
            max_drawdown REAL,
            profit_factor REAL,
            avg_win REAL,
            avg_loss REAL,
            payoff_ratio REAL,
            expectancy REAL,
            qualified INTEGER,
            notes TEXT
        )"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(backtest_runs)")}
    for col, decl in [
        ("avg_win", "REAL"),
        ("avg_loss", "REAL"),
        ("payoff_ratio", "REAL"),
        ("expectancy", "REAL"),
        ("qualified", "INTEGER"),
        ("benchmark_name", "TEXT"),
        ("benchmark_return", "REAL"),
        ("excess_return", "REAL"),
        ("benchmarks_json", "TEXT"),
        ("sharpe_ratio", "REAL"),
        ("max_consec_loss", "INTEGER"),
        ("entry_days", "INTEGER"),
        ("actual_start_date", "TEXT"),
        ("actual_end_date", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE backtest_runs ADD COLUMN {col} {decl}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            pick_id INTEGER,
            code TEXT NOT NULL,
            name TEXT,
            strategy TEXT,
            signal_date TEXT,
            entry_date TEXT,
            exit_date TEXT,
            entry_price REAL,
            exit_price REAL,
            return_pct REAL,
            exit_reason TEXT,
            max_gain_pct REAL,
            max_loss_pct REAL,
            holding_days INTEGER,
            rank INTEGER,
            score REAL,
            theme TEXT,
            FOREIGN KEY(run_id) REFERENCES backtest_runs(id)
        )"""
    )
    conn.commit()


def market_code(code):
    code = str(code).strip()
    if code.startswith(("688", "600", "601", "603", "605", "689", "900")):
        return f"sh{code}"
    return f"sz{code}"


def is_beijing_code(code):
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[:6]
    return code.startswith(("4", "8", "92"))


def fetch_daily_bars(code, datalen=180):
    symbol = market_code(code)
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    data = json.loads(raw) if raw else []
    bars = []
    for row in data:
        try:
            bars.append(
                {
                    "date": row["day"][:10],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        except Exception:
            continue
    return bars


def fetch_index_bars(symbol, datalen=260):
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    data = json.loads(raw) if raw else []
    bars = []
    for row in data:
        try:
            bars.append(
                {
                    "date": row["day"][:10],
                    "open": float(row["open"]),
                    "close": float(row["close"]),
                }
            )
        except Exception:
            continue
    return bars


def benchmark_returns(start_date, end_date, datalen=260):
    out = []
    for symbol, name in BENCHMARKS.items():
        try:
            bars = fetch_index_bars(symbol, datalen=datalen)
            window = [b for b in bars if (not start_date or b["date"] >= start_date) and (not end_date or b["date"] <= end_date)]
            if len(window) < 2:
                continue
            ret = pct(window[-1]["close"], window[0]["open"])
            out.append({"symbol": symbol, "name": name, "return": round(ret, 3)})
        except Exception:
            continue
    return out


def fetch_realtime_price(code):
    symbol = market_code(code)
    req = urllib.request.Request(f"http://hq.sinajs.cn/list={symbol}", headers={"Referer": "https://finance.sina.com.cn"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="replace")
        parts = raw.split(",")
        if len(parts) > 3:
            price = float(parts[3])
            if price > 0:
                return price
    except Exception:
        return None
    return None


def classify_strategy(pick):
    text = f"{pick.get('theme') or ''}+{pick.get('reason') or ''}"
    return "limit_up" if "涨停" in text else "trend"


def pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0


def median(values):
    if not values:
        return 0.0
    xs = sorted(values)
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def max_drawdown(returns):
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for r in returns:
        equity *= 1 + r / 100.0
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity / peak - 1.0) * 100.0)
    return worst


def sharpe_ratio(returns, risk_free_annual=2.0, periods_per_year=252):
    """年化夏普比率。returns 为每笔交易收益(%)列表，假设每笔约1个交易日。"""
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
    std = variance ** 0.5
    if std == 0:
        return 0.0
    daily_rf = risk_free_annual / periods_per_year
    return round((avg - daily_rf) / std * (periods_per_year ** 0.5), 3)


def max_consecutive_losses(returns):
    """最大连续亏损笔数。"""
    worst = 0
    current = 0
    for r in returns:
        if r < 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def simulate_trade(pick, bars, cfg, fee_bps, slippage_bps, mark_open=True):
    signal_date = pick["picked_date"] or (pick["picked_at"] or "")[:10]
    future = [b for b in bars if b["date"] > signal_date]
    if not future:
        if not mark_open:
            return None
        entry = float(pick.get("buy_point") or 0)
        if entry <= 0:
            return None
        mark = fetch_realtime_price(pick["code"])
        if mark is None and bars:
            mark = bars[-1]["close"]
        if mark is None:
            return None
        ret = pct(mark, entry) - fee_bps / 100.0
        return {
            "pick_id": pick["id"],
            "code": pick["code"],
            "name": pick["name"],
            "strategy": classify_strategy(pick),
            "signal_date": signal_date,
            "entry_date": signal_date,
            "exit_date": dt.date.today().isoformat(),
            "entry_price": round(entry, 3),
            "exit_price": round(mark, 3),
            "return_pct": round(ret, 3),
            "exit_reason": "open",
            "max_gain_pct": round(max(0.0, ret), 3),
            "max_loss_pct": round(min(0.0, ret), 3),
            "holding_days": max(0, (dt.date.today() - dt.date.fromisoformat(signal_date)).days),
            "rank": pick["rank"],
            "score": pick["score"],
            "theme": pick["theme"],
        }

    entry_bar = future[0]
    entry = entry_bar["open"] * (1 + slippage_bps / 10000.0)
    stop = entry * (1 - cfg["stop_pct"] / 100.0)
    target = entry * (1 + cfg["target_pct"] / 100.0)
    max_days = cfg["max_days"]
    max_gain = -999.0
    max_loss = 999.0
    exit_bar = None
    exit_price = None
    exit_reason = "time"

    for i, bar in enumerate(future[:max_days], start=1):
        max_gain = max(max_gain, pct(bar["high"], entry))
        max_loss = min(max_loss, pct(bar["low"], entry))
        # Conservative ordering: if both stop and target happen in same daily bar, assume stop first.
        if bar["low"] <= stop:
            exit_bar = bar
            exit_price = stop * (1 - slippage_bps / 10000.0)
            exit_reason = "stop"
            break
        if bar["high"] >= target:
            exit_bar = bar
            exit_price = target * (1 - slippage_bps / 10000.0)
            exit_reason = "target"
            break
    if exit_bar is None:
        exit_bar = future[min(max_days, len(future)) - 1]
        exit_price = exit_bar["close"] * (1 - slippage_bps / 10000.0)

    round_trip_fee = fee_bps * 2 / 100.0
    ret = pct(exit_price, entry) - round_trip_fee
    return {
        "pick_id": pick["id"],
        "code": pick["code"],
        "name": pick["name"],
        "strategy": classify_strategy(pick),
        "signal_date": signal_date,
        "entry_date": entry_bar["date"],
        "exit_date": exit_bar["date"],
        "entry_price": round(entry, 3),
        "exit_price": round(exit_price, 3),
        "return_pct": round(ret, 3),
        "exit_reason": exit_reason,
        "max_gain_pct": round(max_gain, 3),
        "max_loss_pct": round(max_loss, 3),
        "holding_days": max(1, (dt.date.fromisoformat(exit_bar["date"]) - dt.date.fromisoformat(entry_bar["date"])).days),
        "rank": pick["rank"],
        "score": pick["score"],
        "theme": pick["theme"],
    }


def dedupe_daily_picks(rows):
    """One stock can be pushed multiple times intraday; backtest it once per day."""
    grouped = {}
    for row in rows:
        key = (row.get("picked_date"), row.get("code"))
        old = grouped.get(key)
        if old is None:
            grouped[key] = row
            continue
        # Use the earliest signal of the day to avoid look-ahead from later reruns.
        old_time = old.get("picked_at") or ""
        new_time = row.get("picked_at") or ""
        if new_time and (not old_time or new_time < old_time):
            grouped[key] = row
    return sorted(grouped.values(), key=lambda r: (r.get("picked_date") or "", r.get("picked_at") or "", r.get("rank") or 999))


def load_picks(conn, strategy, start_date, end_date, only_evaluated_window=False, dedupe=True):
    where = ["picked_date IS NOT NULL", "buy_point IS NOT NULL", "code NOT GLOB '4*'", "code NOT GLOB '8*'", "code NOT GLOB '92*'"]
    args = []
    if start_date:
        where.append("picked_date >= ?")
        args.append(start_date)
    if end_date:
        where.append("picked_date <= ?")
        args.append(end_date)
    if only_evaluated_window:
        cutoff = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        where.append("picked_date <= ?")
        args.append(cutoff)
    sql = "SELECT * FROM stock_picks WHERE " + " AND ".join(where) + " ORDER BY picked_date, run_id, rank"
    rows = [dict(r) for r in conn.execute(sql, args) if not is_beijing_code(r["code"])]
    if strategy == "agent_council":
        rows = [r for r in rows if r.get("agent_consensus") == "buy" and not r.get("risk_veto")]
    elif strategy != "all":
        rows = [r for r in rows if classify_strategy(r) == strategy]
    return dedupe_daily_picks(rows) if dedupe else rows


def save_run(conn, strategy, args, trades):
    returns = [t["return_pct"] for t in trades]
    wins = sum(1 for r in returns if r > 0)
    losses = sum(1 for r in returns if r < 0)
    win_returns = [r for r in returns if r > 0]
    loss_returns = [abs(r) for r in returns if r < 0]
    gross_win = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))
    run_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    win_rate = wins / len(trades) * 100 if trades else 0
    avg_win = sum(win_returns) / len(win_returns) if win_returns else 0.0
    avg_loss = sum(loss_returns) / len(loss_returns) if loss_returns else 0.0
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0)
    expectancy = (wins / len(trades) * avg_win - losses / len(trades) * avg_loss) if trades else 0.0
    mdd = max_drawdown(returns)
    entry_dates = sorted({t.get("entry_date") for t in trades if t.get("entry_date")})
    entry_days = len(entry_dates)
    qualified = 1 if len(trades) >= 30 and entry_days >= 20 and expectancy > 0 and mdd >= -30 else 0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    sharpe = sharpe_ratio(returns)
    consec_loss = max_consecutive_losses(returns)
    benches = benchmark_returns(args.start, args.end, datalen=args.datalen)
    bench = next((b for b in benches if b["name"] == "科创50"), benches[0] if benches else None)
    bench_name = bench["name"] if bench else None
    bench_ret = bench["return"] if bench else None
    total_ret = round(sum(returns), 3)
    excess_ret = round(total_ret - bench_ret, 3) if trades and bench_ret is not None else None
    benchmarks_json = json.dumps(
        [{**b, "excess": round(total_ret - b["return"], 3) if trades else None} for b in benches],
        ensure_ascii=False,
    )
    cur = conn.execute(
        """INSERT INTO backtest_runs
        (run_at,strategy,start_date,end_date,horizon_days,fee_bps,slippage_bps,trades,wins,losses,
         win_rate,avg_return,median_return,total_return,max_drawdown,profit_factor,
         avg_win,avg_loss,payoff_ratio,expectancy,qualified,benchmark_name,benchmark_return,excess_return,benchmarks_json,notes,sharpe_ratio,max_consec_loss,
         entry_days,actual_start_date,actual_end_date)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_at,
            strategy,
            args.start,
            args.end,
            args.max_days,
            args.fee_bps,
            args.slippage_bps,
            len(trades),
            wins,
            losses,
            round(win_rate, 2),
            round(sum(returns) / len(returns), 3) if returns else 0,
            round(median(returns), 3),
            total_ret,
            round(mdd, 3),
            round(profit_factor, 3),
            round(avg_win, 3),
            round(avg_loss, 3),
            round(payoff_ratio, 3),
            round(expectancy, 3),
            qualified,
            bench_name,
            bench_ret,
            excess_ret,
            benchmarks_json,
            f"daily OHLC; next-open entry; one code per signal day; actual entry days={entry_days}; qualification requires >=20 entry days and >=30 trades; stop has priority when same-day stop/target both hit",
            sharpe,
            consec_loss,
            entry_days,
            entry_dates[0] if entry_dates else None,
            entry_dates[-1] if entry_dates else None,
        ),
    )
    run_id = cur.lastrowid
    for t in trades:
        conn.execute(
            """INSERT INTO backtest_trades
            (run_id,pick_id,code,name,strategy,signal_date,entry_date,exit_date,entry_price,exit_price,
             return_pct,exit_reason,max_gain_pct,max_loss_pct,holding_days,rank,score,theme)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                t["pick_id"],
                t["code"],
                t["name"],
                t["strategy"],
                t["signal_date"],
                t["entry_date"],
                t["exit_date"],
                t["entry_price"],
                t["exit_price"],
                t["return_pct"],
                t["exit_reason"],
                t["max_gain_pct"],
                t["max_loss_pct"],
                t["holding_days"],
                t["rank"],
                t["score"],
                t["theme"],
            ),
        )
    conn.commit()
    return run_id


def print_summary(conn, run_id):
    run = dict(conn.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone())
    print(
        f"Run #{run_id} {run['strategy']} trades={run['trades']} "
        f"win={run['win_rate']:.1f}% avg={run['avg_return']:+.2f}% "
        f"ev={run['expectancy']:+.2f}% payoff={run['payoff_ratio']:.2f} "
        f"total={run['total_return']:+.2f}% mdd={run['max_drawdown']:+.2f}% pf={run['profit_factor']:.2f} "
        f"sharpe={run.get('sharpe_ratio') or 0:.2f} consec_loss={run.get('max_consec_loss') or 0} "
        f"bench={run['benchmark_name'] or '-'} {run['benchmark_return'] if run['benchmark_return'] is not None else 0:+.2f}% "
        f"excess={run['excess_return'] if run['excess_return'] is not None else 0:+.2f}% "
        f"{'QUALIFIED' if run['qualified'] else 'UNQUALIFIED'}"
    )
    try:
        benches = json.loads(run.get("benchmarks_json") or "[]")
        for b in benches:
            print(f"  benchmark {b['name']}: {b['return']:+.2f}% excess {b['excess']:+.2f}%")
    except Exception:
        pass
    for r in conn.execute(
        "SELECT code,name,strategy,signal_date,entry_date,exit_date,return_pct,exit_reason "
        "FROM backtest_trades WHERE run_id=? ORDER BY return_pct DESC",
        (run_id,),
    ):
        print(
            f"  {r['signal_date']} {r['code']} {r['name']} {r['strategy']} "
            f"{r['entry_date']}→{r['exit_date']} {r['return_pct']:+.2f}% {r['exit_reason']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Backtest saved stock-pick strategies")
    parser.add_argument("--strategy", choices=["all", "trend", "limit_up", "agent_council"], default="all")
    parser.add_argument("--start", help="signal start date, YYYY-MM-DD")
    parser.add_argument("--end", help="signal end date, YYYY-MM-DD")
    parser.add_argument("--max-days", type=int, help="override holding horizon")
    parser.add_argument("--fee-bps", type=float, default=3.0)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--min-age-days", type=int, default=2, help="ignore very fresh signals")
    parser.add_argument("--datalen", type=int, default=180)
    parser.add_argument("--no-mark-open", action="store_true", help="skip fresh signals without a future daily bar")
    parser.add_argument("--no-dedupe", action="store_true", help="count repeated intraday signals separately")
    args = parser.parse_args()

    conn = db_conn()
    ensure_schema(conn)
    picks = load_picks(conn, args.strategy, args.start, args.end, only_evaluated_window=args.min_age_days > 0, dedupe=not args.no_dedupe)
    cutoff = dt.date.today() - dt.timedelta(days=args.min_age_days)
    if args.min_age_days > 0:
        picks = [p for p in picks if dt.date.fromisoformat(p["picked_date"]) <= cutoff]
    if not picks:
        run_id = save_run(conn, args.strategy, args, [])
        print(f"No eligible picks to backtest; saved empty validation run #{run_id}.")
        print_summary(conn, run_id)
        return

    cache = {}
    trades = []
    for p in picks:
        strategy = classify_strategy(p)
        cfg = dict(DEFAULTS[strategy])
        if args.max_days:
            cfg["max_days"] = args.max_days
        try:
            bars = cache.get(p["code"])
            if bars is None:
                bars = fetch_daily_bars(p["code"], datalen=args.datalen)
                cache[p["code"]] = bars
            trade = simulate_trade(p, bars, cfg, args.fee_bps, args.slippage_bps, mark_open=not args.no_mark_open)
            if trade:
                trades.append(trade)
        except Exception as e:
            print(f"WARN: {p['code']} {p.get('name','')} skipped: {e}")
    run_id = save_run(conn, args.strategy, args, trades)
    print_summary(conn, run_id)


if __name__ == "__main__":
    main()
