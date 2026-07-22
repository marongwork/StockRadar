#!/usr/bin/env python3
"""Virtual account driven by stock radar signals.

This is a paper-trading layer. It never touches real holdings or transactions.
Rules:
- Initial capital: 500,000 CNY.
- Only buy saved radar signals with a concrete buy_point.
- Limit-up / observation signals are recorded as observations, not buys.
- Sell by stop_loss, target, stale holding window, or strategy feedback.
- Persist account, holdings, trades, daily equity and strategy feedback.
"""
import argparse
import datetime as dt
import html
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.parse
from pathlib import Path

try:
    from strategy_backtest import fetch_realtime_price, market_code
except Exception:
    fetch_realtime_price = None
    market_code = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "invest.db"
INITIAL_CASH = 500000.0
ACCOUNT_ID = "radar_500k_v1"
FEE_RATE = 0.0005
MIN_CASH_RATIO = 0.18
MAX_POSITION_PCT = 0.08
DEFAULT_POSITION_PCT = 0.03
MAX_HOLD_DAYS = 20
MONTHLY_STOP_LOSS_PCT = -10.0
LARK_CLI = "/home/ubuntu/.npm-global/bin/lark-cli"
MAX_BUY_POINT_PREMIUM = 0.02
MAX_BUY_POINT_DISCOUNT = 0.03
MAX_REPEAT_SIGNALS = 2
STRATEGY_POOLS = {
    "trend_event": {"label": "趋势事件", "capital_pct": 0.30, "max_positions": 3, "min_score": 70},
    "k50_momentum": {"label": "科创动量", "capital_pct": 0.20, "max_positions": 3, "min_score": 70},
    "limit_up_confirm": {"label": "涨停次日确认", "capital_pct": 0.15, "max_positions": 2, "min_score": 85},
    "vibe_alpha": {"label": "Vibe Alpha", "capital_pct": 0.15, "max_positions": 2, "min_score": 70},
}
BLOCK_TAGS = (
    "观察不买", "观察不打板", "重复冷却剔除", "暂停买入", "趋势熔断",
    "⚠️解禁", "⚠️减持", "⚠️监管", "⚠️舆情利空", "⚠️资金D档", "⚠️资金E档",
    "⚠️破位", "⚠️非科技圈", "⚠️弱主题", "⚠️弱涨停题材",
)


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return dt.date.today().isoformat()


def next_trade_date(d=None):
    d = d or dt.date.today()
    n = d + dt.timedelta(days=1)
    while n.weekday() >= 5:
        n += dt.timedelta(days=1)
    return n.isoformat()


def previous_trade_date(d=None):
    d = d or dt.date.today()
    n = d - dt.timedelta(days=1)
    while n.weekday() >= 5:
        n -= dt.timedelta(days=1)
    return n.isoformat()


def in_entry_window(at=None):
    at = at or dt.datetime.now()
    if at.weekday() >= 5:
        return False
    t = at.time()
    return dt.time(9, 30) <= t <= dt.time(11, 25) or dt.time(13, 0) <= t <= dt.time(14, 55)


def is_sellable(pos):
    sellable = pos.get("sellable_date") or pos.get("opened_at", "")[:10]
    return today_str() >= sellable


def norm_code(code):
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def is_beijing_code(code):
    return norm_code(code).startswith(("4", "8", "92"))


def conn_db():
    if not DB_PATH.exists():
        raise SystemExit(f"ERROR: {DB_PATH} not found")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS virtual_accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        initial_cash REAL NOT NULL,
        cash REAL NOT NULL,
        realized_pnl REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        strategy_version TEXT NOT NULL DEFAULT 'radar_500k_v1',
        status TEXT NOT NULL DEFAULT 'running'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS virtual_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        qty INTEGER NOT NULL,
        avg_cost REAL NOT NULL,
        last_price REAL,
        market_value REAL,
        unrealized_pnl REAL DEFAULT 0,
        stop_loss REAL,
        target REAL,
        opened_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_pick_id INTEGER,
        strategy TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        note TEXT,
        UNIQUE(account_id, code, status)
    )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(virtual_positions)")}
    if "sellable_date" not in cols:
        conn.execute("ALTER TABLE virtual_positions ADD COLUMN sellable_date TEXT")
    if "pending_exit_reason" not in cols:
        conn.execute("ALTER TABLE virtual_positions ADD COLUMN pending_exit_reason TEXT")
    if "max_price" not in cols:
        conn.execute("ALTER TABLE virtual_positions ADD COLUMN max_price REAL")
    if "day_chg_pct" not in cols:
        conn.execute("ALTER TABLE virtual_positions ADD COLUMN day_chg_pct REAL")
    if "quote_at" not in cols:
        conn.execute("ALTER TABLE virtual_positions ADD COLUMN quote_at TEXT")
    if "quote_source" not in cols:
        conn.execute("ALTER TABLE virtual_positions ADD COLUMN quote_source TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS virtual_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        trade_at TEXT NOT NULL,
        action TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        price REAL NOT NULL,
        qty INTEGER NOT NULL,
        amount REAL NOT NULL,
        fee REAL NOT NULL DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        cash_after REAL,
        source_pick_id INTEGER,
        reason TEXT,
        strategy TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS virtual_equity_curve (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        snapshot_at TEXT NOT NULL,
        cash REAL NOT NULL,
        market_value REAL NOT NULL,
        equity REAL NOT NULL,
        realized_pnl REAL NOT NULL,
        unrealized_pnl REAL NOT NULL,
        total_return_pct REAL NOT NULL,
        drawdown_pct REAL NOT NULL,
        open_positions INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS virtual_signal_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        pick_id INTEGER,
        code TEXT,
        name TEXT,
        score REAL,
        theme TEXT,
        action TEXT,
        reason TEXT,
        UNIQUE(account_id, pick_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS virtual_strategy_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metric_window TEXT,
        trades INTEGER,
        win_rate REAL,
        avg_return_pct REAL,
        total_return_pct REAL,
        max_drawdown_pct REAL,
        suggestion TEXT,
        config_json TEXT
    )""")
    row = conn.execute("SELECT id FROM virtual_accounts WHERE id=?", (ACCOUNT_ID,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO virtual_accounts(id,name,initial_cash,cash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (ACCOUNT_ID, "选股雷达 50万虚拟账户", INITIAL_CASH, INITIAL_CASH, now_str(), now_str()),
        )
    conn.execute(
        """UPDATE virtual_positions SET strategy='k50_momentum'
           WHERE account_id=? AND status='open' AND strategy='trend'
             AND source_pick_id IN (SELECT id FROM stock_picks WHERE theme LIKE '%科创50%')""",
        (ACCOUNT_ID,),
    )
    conn.execute(
        "UPDATE virtual_positions SET strategy='trend_event' WHERE account_id=? AND status='open' AND strategy='trend'",
        (ACCOUNT_ID,),
    )
    conn.commit()


def pick_strategy(row):
    text = f"{row['theme'] or ''}+{row['reason'] or ''}"
    if "涨停" in text:
        return "limit_up_confirm"
    if "Vibe" in text or "Alpha" in text:
        return "vibe_alpha"
    if "科创50" in text:
        return "k50_momentum"
    return "trend_event"


def quote_symbol(code):
    if market_code:
        return market_code(norm_code(code))
    code = norm_code(code)
    return ("sh" if code.startswith(("600", "601", "603", "605", "688", "689", "900")) else "sz") + code


def fetch_eastmoney_quote(code):
    normalized = norm_code(code)
    market = "1" if normalized.startswith(("600", "601", "603", "605", "688", "689", "900")) else "0"
    params = {
        "secid": f"{market}.{normalized}", "fltt": "2", "invt": "2",
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f86,f170",
    }
    req = urllib.request.Request(
        "http://push2.eastmoney.com/api/qt/stock/get?" + urllib.parse.urlencode(params),
        headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = (json.loads(resp.read().decode("utf-8", "ignore")).get("data") or {})
        current = float(data.get("f43") or 0)
        previous = float(data.get("f60") or 0)
        if current <= 0 or previous <= 0:
            return None
        quote_dt = dt.datetime.fromtimestamp(float(data.get("f86") or 0)) if data.get("f86") else dt.datetime.now()
        return {
            "name": data.get("f58") or "", "open": float(data.get("f46") or 0),
            "previous_close": previous, "current": current, "high": float(data.get("f44") or 0),
            "low": float(data.get("f45") or 0), "date": quote_dt.strftime("%Y-%m-%d"),
            "time": quote_dt.strftime("%H:%M:%S"), "chg_pct": float(data.get("f170") or 0),
            "open_gap_pct": (float(data.get("f46") or 0) / previous - 1) * 100 if data.get("f46") else None,
            "amount": float(data.get("f48") or 0), "source": "东方财富公开行情",
        }
    except Exception:
        return None


def fetch_sina_quote(code):
    req = urllib.request.Request(
        f"http://hq.sinajs.cn/list={quote_symbol(code)}",
        headers={"Referer": "https://finance.sina.com.cn"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="replace")
        body = raw.split('="', 1)[1].rsplit('"', 1)[0]
        parts = body.split(",")
        if len(parts) < 32:
            return None
        current = float(parts[3] or 0)
        previous_close = float(parts[2] or 0)
        if current <= 0 or previous_close <= 0:
            return None
        return {
            "name": parts[0],
            "open": float(parts[1] or 0),
            "previous_close": previous_close,
            "current": current,
            "high": float(parts[4] or 0),
            "low": float(parts[5] or 0),
            "date": parts[30],
            "time": parts[31],
            "chg_pct": (current / previous_close - 1) * 100,
            "open_gap_pct": (float(parts[1] or 0) / previous_close - 1) * 100 if float(parts[1] or 0) else None,
            "source": "新浪公开行情",
        }
    except Exception:
        return None


def fetch_realtime_quote(code):
    return fetch_eastmoney_quote(code) or fetch_sina_quote(code)


def latest_price(row):
    quote = fetch_realtime_quote(row["code"])
    if quote:
        return quote["current"]
    code = norm_code(row["code"])
    if fetch_realtime_price:
        try:
            px = fetch_realtime_price(code)
            if px and px > 0:
                return float(px)
        except Exception:
            pass
    for key in ("buy_point", "last_price", "target", "stop_loss"):
        try:
            v = row[key]
        except Exception:
            v = None
        if v:
            return float(v)
    return None


def account(conn):
    return conn.execute("SELECT * FROM virtual_accounts WHERE id=?", (ACCOUNT_ID,)).fetchone()


def open_positions(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM virtual_positions WHERE account_id=? AND status='open' ORDER BY opened_at", (ACCOUNT_ID,))]


def position_value(conn):
    mv = conn.execute("SELECT COALESCE(SUM(market_value),0) FROM virtual_positions WHERE account_id=? AND status='open'", (ACCOUNT_ID,)).fetchone()[0]
    upnl = conn.execute("SELECT COALESCE(SUM(unrealized_pnl),0) FROM virtual_positions WHERE account_id=? AND status='open'", (ACCOUNT_ID,)).fetchone()[0]
    return float(mv or 0), float(upnl or 0)


def pool_exposure(conn, strategy):
    aliases = (strategy, "trend") if strategy == "trend_event" else (strategy,)
    marks = ",".join("?" for _ in aliases)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(market_value),0), COUNT(*) FROM virtual_positions
             WHERE account_id=? AND status='open' AND strategy IN ({marks})""",
        (ACCOUNT_ID, *aliases),
    ).fetchone()
    return float(row[0] or 0), int(row[1] or 0)


def repeat_count(pick):
    matches = re.findall(r"(?:5日内已推|已推)(\d+)次", pick["reason"] or "")
    return max([int(v) for v in matches] or [0])


def has_nearby_risk(conn, code):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='holding_risk_alerts'").fetchone():
        return None
    start = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    end = (dt.date.today() + dt.timedelta(days=14)).isoformat()
    return conn.execute(
        """SELECT risk_type,event_date,impact FROM holding_risk_alerts
           WHERE code=? AND event_date BETWEEN ? AND ?
           ORDER BY CASE impact WHEN '高' THEN 0 WHEN '中' THEN 1 ELSE 2 END, event_date LIMIT 1""",
        (norm_code(code), start, end),
    ).fetchone()


def strategy_gate(conn, pick, quote):
    strategy = pick_strategy(pick)
    cfg = STRATEGY_POOLS[strategy]
    reason = pick["reason"] or ""
    score = float(pick["score"] or 0)
    if is_beijing_code(pick["code"]):
        return False, strategy, "账户规则禁止交易北交所股票"
    if not in_entry_window():
        return False, strategy, "非A股连续竞价时段，不模拟成交"
    if not quote or quote.get("date") != today_str():
        return False, strategy, "缺少当日实时行情，不使用旧价格成交"
    if score < cfg["min_score"]:
        return False, strategy, f"评分{score:.1f}低于{cfg['label']}门槛{cfg['min_score']}"
    ignored = {"观察不打板", "观察不买"} if strategy == "limit_up_confirm" else set()
    blocked = next((tag for tag in BLOCK_TAGS if tag not in ignored and tag in reason), None)
    if blocked:
        return False, strategy, f"命中风控标签：{blocked}"
    if repeat_count(pick) >= MAX_REPEAT_SIGNALS:
        return False, strategy, f"5日内重复推送{repeat_count(pick)}次，进入冷却"
    risk = has_nearby_risk(conn, pick["code"])
    if risk:
        return False, strategy, f"事件风险：{risk['risk_type']} {risk['event_date']}（{risk['impact'] or '待评估'}）"
    grade = (pick["fund_grade"] or "").upper()
    if grade in ("D", "E"):
        return False, strategy, f"资金{grade}档，不开仓"
    px = float(quote["current"])
    chg = float(quote["chg_pct"])
    if strategy == "limit_up_confirm":
        if (pick["picked_date"] or (pick["picked_at"] or "")[:10]) != previous_trade_date():
            return False, strategy, "涨停票仅验证前一交易日信号"
        if dt.datetime.now().time() > dt.time(10, 0):
            return False, strategy, "涨停接力仅在次日10:00前确认"
        if grade not in ("A", "B"):
            return False, strategy, "涨停接力要求资金A/B档"
        gap = quote.get("open_gap_pct")
        if gap is None or not (0.5 <= gap <= 5.5):
            return False, strategy, f"次日开盘涨幅{gap if gap is not None else 0:.2f}%不在0.5%~5.5%"
        if not (0.5 <= chg <= 7.0) or px < float(quote["open"]) * 0.99:
            return False, strategy, f"竞价后未形成可成交的强转强，当前涨幅{chg:+.2f}%"
        return True, strategy, "前一日涨停，次日可成交且强转强确认"
    buy_point = float(pick["buy_point"] or 0)
    if buy_point <= 0:
        return False, strategy, "没有明确买点"
    deviation = px / buy_point - 1
    if deviation > MAX_BUY_POINT_PREMIUM:
        return False, strategy, f"现价高于买点{deviation * 100:.2f}%，超过追价上限2%"
    if deviation < -MAX_BUY_POINT_DISCOUNT:
        return False, strategy, f"现价低于买点{abs(deviation) * 100:.2f}%，疑似转弱不接飞刀"
    if not (-2.0 <= chg <= 6.5):
        return False, strategy, f"当日涨幅{chg:+.2f}%不在可执行区间-2%~6.5%"
    if grade not in ("A", "B"):
        return False, strategy, "缺少资金A/B档确认"
    return True, strategy, f"{cfg['label']}通过评分、资金、价格和风险闸门"


def update_prices(conn):
    for pos in open_positions(conn):
        quote = fetch_realtime_quote(pos["code"])
        px = quote["current"] if quote else None
        if not px:
            px = pos.get("last_price") or pos.get("avg_cost")
        market_value = px * pos["qty"]
        upnl = (px - pos["avg_cost"]) * pos["qty"]
        max_price = max(float(pos.get("max_price") or pos["avg_cost"]), float(px))
        quote_at = f"{quote.get('date')} {quote.get('time')}" if quote else now_str()
        conn.execute(
            """UPDATE virtual_positions SET last_price=?,market_value=?,unrealized_pnl=?,max_price=?,
               day_chg_pct=?,quote_at=?,quote_source=?,updated_at=? WHERE id=?""",
            (px, market_value, upnl, max_price, quote.get("chg_pct") if quote else pos.get("day_chg_pct"),
             quote_at, quote.get("source") if quote else pos.get("quote_source"), now_str(), pos["id"]),
        )
    conn.commit()


def monthly_return_pct(conn):
    month_start = dt.date.today().replace(day=1).isoformat()
    first = conn.execute(
        "SELECT equity FROM virtual_equity_curve WHERE account_id=? AND snapshot_at>=? ORDER BY snapshot_at ASC,id ASC LIMIT 1",
        (ACCOUNT_ID, month_start),
    ).fetchone()
    last = conn.execute(
        "SELECT equity FROM virtual_equity_curve WHERE account_id=? ORDER BY snapshot_at DESC,id DESC LIMIT 1",
        (ACCOUNT_ID,),
    ).fetchone()
    if not last or not last[0]:
        return 0.0
    acc = account(conn)
    created_month = (acc["created_at"] or "")[:7]
    base = float(acc["initial_cash"]) if created_month == month_start[:7] else float(first[0] if first else acc["initial_cash"])
    return (float(last[0]) / base - 1.0) * 100


def observation_only(conn):
    return monthly_return_pct(conn) <= MONTHLY_STOP_LOSS_PCT


def intraday_entry_block(conn, at=None):
    """市场二/三级风险发生后30分钟内停止开新仓，持仓仍继续监控。"""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_intraday_alerts'"
    ).fetchone():
        return None
    row = conn.execute(
        """SELECT alert_at,severity,title FROM market_intraday_alerts
           WHERE trade_date=? ORDER BY alert_at DESC,id DESC LIMIT 1""", (today_str(),)
    ).fetchone()
    if not row or int(row[1] or 0) < 2:
        return None
    at = at or dt.datetime.now()
    try:
        age = at - dt.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if dt.timedelta(0) <= age <= dt.timedelta(minutes=30):
        return f"市场风险冷静期：{row[2] or '指数转弱'}，30分钟内不开新仓"
    return None


def record_observation(conn, pick, action, reason):
    conn.execute(
        """INSERT INTO virtual_signal_observations
           (account_id, observed_at, pick_id, code, name, score, theme, action, reason)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(account_id,pick_id) DO UPDATE SET
             observed_at=excluded.observed_at, action=excluded.action, reason=excluded.reason""",
        (ACCOUNT_ID, now_str(), pick["id"], norm_code(pick["code"]), pick["name"], pick["score"], pick["theme"], action, reason[:800]),
    )


def buy(conn, pick, px, reason, strategy=None):
    if is_beijing_code(pick["code"]):
        record_observation(conn, pick, "skip", "账户规则禁止交易北交所股票")
        return False
    acc = account(conn)
    cash = float(acc["cash"])
    market_value, _ = position_value(conn)
    equity = cash + market_value
    strategy = strategy or pick_strategy(pick)
    pool = STRATEGY_POOLS[strategy]
    pool_value, pool_positions = pool_exposure(conn, strategy)
    pool_remaining = INITIAL_CASH * pool["capital_pct"] - pool_value
    if pool_positions >= pool["max_positions"]:
        record_observation(conn, pick, "skip", f"{pool['label']}已达{pool['max_positions']}只持仓上限")
        return False
    if pool_remaining < px * 100:
        record_observation(conn, pick, "skip", f"{pool['label']}策略额度不足")
        return False
    if cash / max(equity, 1) <= MIN_CASH_RATIO:
        record_observation(conn, pick, "skip", "现金比例低于18%，不再开新仓")
        return False
    if conn.execute("SELECT 1 FROM virtual_positions WHERE account_id=? AND code=? AND status='open'", (ACCOUNT_ID, norm_code(pick["code"]))).fetchone():
        record_observation(conn, pick, "skip", "已有持仓，不重复买入")
        return False
    raw_pct = pick["position_pct"] if "position_pct" in pick.keys() and pick["position_pct"] else DEFAULT_POSITION_PCT * 100
    pct = min(MAX_POSITION_PCT, max(0.01, float(raw_pct) / 100.0))
    budget = min(equity * pct, max(0, cash - equity * MIN_CASH_RATIO), pool_remaining)
    qty = int(budget / px / 100) * 100
    if qty <= 0:
        record_observation(conn, pick, "skip", "预算不足100股，不下单")
        return False
    amount = qty * px
    fee = amount * FEE_RATE
    if amount + fee > cash:
        qty = int((cash - fee) / px / 100) * 100
        amount = qty * px
        fee = amount * FEE_RATE
    if qty <= 0:
        return False
    cash_after = cash - amount - fee
    stop_loss = pick["stop_loss"] or (px * (0.95 if strategy == "limit_up_confirm" else 0.93))
    target = pick["target"] or (px * (1.10 if strategy == "limit_up_confirm" else 1.20))
    conn.execute("UPDATE virtual_accounts SET cash=?, updated_at=? WHERE id=?", (cash_after, now_str(), ACCOUNT_ID))
    conn.execute(
        """INSERT INTO virtual_positions(account_id,code,name,qty,avg_cost,last_price,market_value,unrealized_pnl,stop_loss,target,opened_at,updated_at,source_pick_id,strategy,note,sellable_date,max_price)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ACCOUNT_ID, norm_code(pick["code"]), pick["name"], qty, px, px, amount, -fee, stop_loss, target, now_str(), now_str(), pick["id"], strategy, reason[:500], next_trade_date(), px),
    )
    conn.execute(
        """INSERT INTO virtual_trades(account_id,trade_at,action,code,name,price,qty,amount,fee,realized_pnl,cash_after,source_pick_id,reason,strategy)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ACCOUNT_ID, now_str(), "BUY", norm_code(pick["code"]), pick["name"], px, qty, amount, fee, -fee, cash_after, pick["id"], reason[:500], strategy),
    )
    return True


def sell(conn, pos, px, reason):
    acc = account(conn)
    cash = float(acc["cash"])
    amount = pos["qty"] * px
    fee = amount * FEE_RATE
    pnl = (px - pos["avg_cost"]) * pos["qty"] - fee
    cash_after = cash + amount - fee
    conn.execute("UPDATE virtual_accounts SET cash=?, realized_pnl=realized_pnl+?, updated_at=? WHERE id=?", (cash_after, pnl, now_str(), ACCOUNT_ID))
    conn.execute("UPDATE virtual_positions SET status='closed', last_price=?, market_value=0, unrealized_pnl=0, updated_at=?, note=COALESCE(note,'') || ? WHERE id=?", (px, now_str(), " | " + reason, pos["id"]))
    conn.execute(
        """INSERT INTO virtual_trades(account_id,trade_at,action,code,name,price,qty,amount,fee,realized_pnl,cash_after,source_pick_id,reason,strategy)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ACCOUNT_ID, now_str(), "SELL", pos["code"], pos["name"], px, pos["qty"], amount, fee, pnl, cash_after, pos.get("source_pick_id"), reason, pos.get("strategy")),
    )


def manage_exits(conn):
    for pos in open_positions(conn):
        quote = fetch_realtime_quote(pos["code"])
        if not quote or quote.get("date") != today_str():
            continue
        px = quote["current"] if quote else (pos.get("last_price") or pos.get("avg_cost"))
        opened = dt.datetime.strptime(pos["opened_at"][:10], "%Y-%m-%d").date()
        held = (dt.date.today() - opened).days
        strategy = pos.get("strategy") or "trend_event"
        max_days = 5 if strategy == "limit_up_confirm" else (15 if strategy in ("k50_momentum", "vibe_alpha") else MAX_HOLD_DAYS)
        max_price = max(float(pos.get("max_price") or pos["avg_cost"]), float(px))
        reason = None
        if pos.get("pending_exit_reason") and is_sellable(pos):
            reason = pos["pending_exit_reason"]
        elif pos.get("stop_loss") and px <= pos["stop_loss"]:
            reason = f"触发止损 {px:.2f} <= {pos['stop_loss']:.2f}"
        elif pos.get("target") and px >= pos["target"]:
            reason = f"触发目标价 {px:.2f} >= {pos['target']:.2f}"
        elif max_price >= pos["avg_cost"] * 1.10 and px <= max_price * 0.94:
            reason = f"盈利后回撤6%，移动止盈 {px:.2f}"
        elif held >= max_days:
            reason = f"持仓{held}天超过{max_days}天，时间止盈/止损"
        if reason:
            if not is_sellable(pos):
                conn.execute(
                    "UPDATE virtual_positions SET pending_exit_reason=?, updated_at=? WHERE id=?",
                    (f"T+1限制，当日不可卖；{reason}", now_str(), pos["id"]),
                )
            else:
                limit_pct = 0.05 if "ST" in str(pos.get("name") or "").upper() else (0.20 if str(pos.get("code", "")).startswith(("300", "301", "688", "689")) else 0.10)
                at_limit_down = bool(quote and px <= quote["previous_close"] * (1 - limit_pct) * 1.002)
                if at_limit_down:
                    conn.execute(
                        "UPDATE virtual_positions SET pending_exit_reason=?, updated_at=? WHERE id=?",
                        (f"跌停价无法保证成交；{reason}", now_str(), pos["id"]),
                    )
                else:
                    sell(conn, pos, px, reason)
        else:
            mv = px * pos["qty"]
            upnl = (px - pos["avg_cost"]) * pos["qty"]
            conn.execute("UPDATE virtual_positions SET last_price=?, market_value=?, unrealized_pnl=?, max_price=?, updated_at=? WHERE id=?", (px, mv, upnl, max_price, now_str(), pos["id"]))
    conn.commit()


def eligible_picks(conn, limit=60):
    rows = conn.execute(
        """SELECT * FROM stock_picks
           WHERE picked_date >= ?
           ORDER BY picked_at DESC, score DESC, rank ASC
           LIMIT ?""",
        (previous_trade_date(), limit),
    ).fetchall()
    result = []
    seen = set()
    for row in rows:
        if is_beijing_code(row["code"]):
            record_observation(conn, row, "skip", "账户规则禁止交易北交所股票")
            continue
        strategy = pick_strategy(row)
        key = (norm_code(row["code"]), strategy)
        if key in seen:
            continue
        if strategy != "limit_up_confirm" and row["buy_point"] is None:
            continue
        seen.add(key)
        result.append(row)
    return result


def observe_latest(conn, limit=20):
    for pick in conn.execute("SELECT * FROM stock_picks ORDER BY picked_at DESC,id DESC LIMIT ?", (limit,)):
        if is_beijing_code(pick["code"]):
            record_observation(conn, pick, "skip", "账户规则禁止交易北交所股票")
            continue
        if pick["buy_point"] is None:
            text = pick["reason"] or ""
            action = "watch_limit_up" if "涨停" in f"{pick['theme'] or ''}+{text}" else "watch"
            record_observation(conn, pick, action, "无可执行买点，仅观察")


def run_once(conn):
    ensure_schema(conn)
    update_prices(conn)
    if in_entry_window():
        manage_exits(conn)
    observe_latest(conn)
    if observation_only(conn):
        record_system_observation(conn, f"本月收益 {monthly_return_pct(conn):.2f}%，触发-10%红线，只观察不交易")
        snapshot(conn)
        feedback(conn)
        conn.commit()
        return 0
    buys = 0
    if not in_entry_window():
        record_system_observation(conn, "当前不在连续竞价时段，只更新净值和观察记录，不模拟成交")
        snapshot(conn)
        feedback(conn)
        conn.commit()
        return buys
    market_block = intraday_entry_block(conn)
    if market_block:
        record_system_observation(conn, market_block)
        snapshot(conn)
        feedback(conn)
        conn.commit()
        return buys
    for pick in eligible_picks(conn):
        quote = fetch_realtime_quote(pick["code"])
        passed, strategy, gate_reason = strategy_gate(conn, pick, quote)
        if not passed:
            record_observation(conn, pick, "skip", gate_reason)
            continue
        px = float(quote["current"])
        if buy(conn, pick, px, gate_reason, strategy=strategy):
            buys += 1
    update_prices(conn)
    snapshot(conn)
    feedback(conn)
    conn.commit()
    return buys


def record_system_observation(conn, reason):
    conn.execute(
        """INSERT INTO virtual_signal_observations(account_id, observed_at, pick_id, code, name, score, theme, action, reason)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (ACCOUNT_ID, now_str(), None, "SYSTEM", "系统风控", None, "账户风控", "observe_only", reason),
    )


def snapshot(conn):
    acc = account(conn)
    market_value, upnl = position_value(conn)
    equity = float(acc["cash"]) + market_value
    peak = conn.execute("SELECT MAX(equity) FROM virtual_equity_curve WHERE account_id=?", (ACCOUNT_ID,)).fetchone()[0]
    peak = max(float(peak or INITIAL_CASH), equity)
    dd = (equity / peak - 1) * 100 if peak else 0
    ret = (equity / INITIAL_CASH - 1) * 100
    open_n = conn.execute("SELECT COUNT(*) FROM virtual_positions WHERE account_id=? AND status='open'", (ACCOUNT_ID,)).fetchone()[0]
    conn.execute(
        """INSERT INTO virtual_equity_curve(account_id,snapshot_at,cash,market_value,equity,realized_pnl,unrealized_pnl,total_return_pct,drawdown_pct,open_positions)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (ACCOUNT_ID, now_str(), acc["cash"], market_value, equity, acc["realized_pnl"], upnl, ret, dd, open_n),
    )


def feedback(conn):
    trades = [dict(r) for r in conn.execute("SELECT * FROM virtual_trades WHERE account_id=? AND action='SELL' ORDER BY trade_at DESC LIMIT 50", (ACCOUNT_ID,))]
    if not trades:
        suggestion = "暂无平仓样本，维持小仓验证，不根据浮盈修改参数。"
        config = {
            "buy_requires": ["trade_window", "score", "fund_confirm", "price_band", "event_risk_clear"],
            "strategy_pools": STRATEGY_POOLS,
            "max_position_pct": MAX_POSITION_PCT,
        }
        vals = (0, None, None)
    else:
        pnls = [t["realized_pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        win_rate = len(wins) / len(pnls) * 100
        avg_ret = sum(pnls) / len(pnls) / INITIAL_CASH * 100
        if win_rate < 45:
            suggestion = "胜率低于45%，下一轮降低单票仓位并提高资金档位要求。"
        elif avg_ret < 0:
            suggestion = "平均收益为负，收紧止损和重复信号惩罚。"
        else:
            suggestion = "虚拟交易表现为正，维持当前仓位上限，继续观察样本量。"
        config = {"win_rate": round(win_rate, 2), "avg_trade_pnl": round(sum(pnls) / len(pnls), 2), "max_position_pct": MAX_POSITION_PCT}
        vals = (len(pnls), win_rate, avg_ret)
    latest = conn.execute("SELECT total_return_pct, drawdown_pct FROM virtual_equity_curve WHERE account_id=? ORDER BY snapshot_at DESC,id DESC LIMIT 1", (ACCOUNT_ID,)).fetchone()
    conn.execute(
        """INSERT INTO virtual_strategy_feedback(account_id,created_at,metric_window,trades,win_rate,avg_return_pct,total_return_pct,max_drawdown_pct,suggestion,config_json)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (ACCOUNT_ID, now_str(), "recent_50_closed_trades", vals[0], vals[1], vals[2], latest[0] if latest else 0, latest[1] if latest else 0, suggestion, json.dumps(config, ensure_ascii=False)),
    )


def fmt_money(v):
    return f"¥{float(v or 0):,.0f}"


def fmt_pct(v):
    return "-" if v is None else f"{float(v):+.2f}%"


def fmt_price(v):
    return "-" if v is None else f"¥{float(v):,.2f}"


def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def holding_return_pct(position):
    cost = float(position.get("avg_cost") or 0)
    current = float(position.get("last_price") or 0)
    return (current / cost - 1) * 100 if cost > 0 and current > 0 else None


def generate_stock_detail_pages(conn, out_dir=None):
    out_dir = Path(out_dir or BASE_DIR / "virtual-account")
    out_dir.mkdir(parents=True, exist_ok=True)
    codes = {r[0] for r in conn.execute("SELECT DISTINCT code FROM virtual_trades WHERE account_id=?", (ACCOUNT_ID,))}
    codes.update(r[0] for r in conn.execute("SELECT code FROM virtual_positions WHERE account_id=?", (ACCOUNT_ID,)))
    for code in codes:
        position_row = conn.execute(
            "SELECT * FROM virtual_positions WHERE account_id=? AND code=? ORDER BY id DESC LIMIT 1", (ACCOUNT_ID, code)
        ).fetchone()
        position = dict(position_row) if position_row else {}
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM virtual_trades WHERE account_id=? AND code=? ORDER BY trade_at DESC,id DESC", (ACCOUNT_ID, code)
        )]
        signals = [dict(r) for r in conn.execute(
            """SELECT id,picked_at,theme,score,chg_pct,buy_point,stop_loss,target,reason
               FROM stock_picks WHERE code=? ORDER BY picked_at DESC,id DESC LIMIT 100""", (code,)
        )]
        name = position.get("name") or (trades[0].get("name") if trades else code)
        hold_ret = holding_return_pct(position)
        trade_rows = "".join(
            f"<tr><td>{esc(t['trade_at'][:16])}</td><td>{esc(t['action'])}</td><td>{t['qty']}</td><td>{fmt_price(t['price'])}</td><td>{fmt_money(t['amount'])}</td><td class='{'pos' if (t.get('realized_pnl') or 0)>=0 else 'neg'}'>{fmt_money(t.get('realized_pnl'))}</td><td>{esc(t.get('reason') or '-')}</td></tr>"
            for t in trades
        ) or "<tr><td colspan='7'>暂无交易记录</td></tr>"
        signal_rows = "".join(
            f"<tr><td>{esc(s['picked_at'][:16])}</td><td>{esc(s.get('theme') or '-')}</td><td>{float(s.get('score') or 0):.1f}</td><td>{fmt_pct(s.get('chg_pct'))}</td><td>{fmt_price(s.get('buy_point'))}</td><td>{esc(s.get('reason') or '-')}</td></tr>"
            for s in signals
        ) or "<tr><td colspan='6'>暂无历史信号</td></tr>"
        page = f"""<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1.0'><title>{esc(name)} 模拟交易档案</title><style>
:root{{--bg:#edf3ef;--card:#fff;--ink:#142234;--muted:#687788;--line:#d7e0dc;--green:#08785b;--red:#c4374c}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif}}.wrap{{max-width:1120px;margin:auto;padding:24px 16px 50px}}a{{color:#08785b}}h1{{margin:8px 0 2px}}.sub,small{{color:var(--muted)}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:18px 0}}.metric,.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px}}.metric{{padding:14px}}.metric span{{display:block;color:var(--muted);font-size:12px}}.metric b{{display:block;font-size:21px;margin-top:3px}}.panel{{margin:14px 0;overflow:auto}}.title{{padding:12px 15px;border-bottom:1px solid var(--line);font-weight:800}}table{{width:100%;border-collapse:collapse;min-width:760px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{font-size:12px;color:var(--muted)}}.pos{{color:var(--green)}}.neg{{color:var(--red)}}
</style></head><body><main class='wrap'><a href='/invest/virtual-account.html'>← 返回模拟盘</a><h1>{esc(name)} <small>{esc(code)}</small></h1><div class='sub'>独立交易档案 · 行情来源 {esc(position.get('quote_source') or '-')} · 更新 {esc(position.get('quote_at') or '-')}</div><section class='metrics'><div class='metric'><span>持仓收益率</span><b class='{'pos' if (hold_ret or 0)>=0 else 'neg'}'>{fmt_pct(hold_ret)}</b></div><div class='metric'><span>今日涨跌</span><b class='{'pos' if (position.get('day_chg_pct') or 0)>=0 else 'neg'}'>{fmt_pct(position.get('day_chg_pct'))}</b></div><div class='metric'><span>持仓成本</span><b>{fmt_price(position.get('avg_cost'))}</b></div><div class='metric'><span>最新价格</span><b>{fmt_price(position.get('last_price'))}</b></div><div class='metric'><span>浮动盈亏</span><b class='{'pos' if (position.get('unrealized_pnl') or 0)>=0 else 'neg'}'>{fmt_money(position.get('unrealized_pnl'))}</b></div><div class='metric'><span>可卖日期</span><b>{esc(position.get('sellable_date') or '-')}</b></div><div class='metric'><span>止损 / 目标</span><b>{fmt_price(position.get('stop_loss'))} / {fmt_price(position.get('target'))}</b></div></section><section class='panel'><div class='title'>历史交易记录</div><table><thead><tr><th>时间</th><th>方向</th><th>数量</th><th>成交价</th><th>金额</th><th>已实现盈亏</th><th>原因</th></tr></thead><tbody>{trade_rows}</tbody></table></section><section class='panel'><div class='title'>历史选股信号</div><table><thead><tr><th>信号时间</th><th>策略</th><th>评分</th><th>当日涨跌</th><th>买点</th><th>原因</th></tr></thead><tbody>{signal_rows}</tbody></table></section></main></body></html>"""
        (out_dir / f"{code}.html").write_text(page, encoding="utf-8")


def generate_page(conn, out_path=None):
    ensure_schema(conn)
    update_prices(conn)
    acc = account(conn)
    positions = open_positions(conn)
    market_value, upnl = position_value(conn)
    equity = float(acc["cash"]) + market_value
    ret = (equity / INITIAL_CASH - 1) * 100
    mret = ret if (acc["created_at"] or "")[:7] == today_str()[:7] else monthly_return_pct(conn)
    last_snap = conn.execute("SELECT * FROM virtual_equity_curve WHERE account_id=? ORDER BY snapshot_at DESC,id DESC LIMIT 1", (ACCOUNT_ID,)).fetchone()
    trades = [dict(r) for r in conn.execute("SELECT * FROM virtual_trades WHERE account_id=? ORDER BY trade_at DESC,id DESC LIMIT 80", (ACCOUNT_ID,))]
    obs = [dict(r) for r in conn.execute("SELECT * FROM virtual_signal_observations WHERE account_id=? ORDER BY observed_at DESC,id DESC LIMIT 40", (ACCOUNT_ID,))]
    fb = conn.execute("SELECT * FROM virtual_strategy_feedback WHERE account_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (ACCOUNT_ID,)).fetchone()

    pool_rows = []
    for key, cfg in STRATEGY_POOLS.items():
        exposure, count = pool_exposure(conn, key)
        sold = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(realized_pnl),0),
                      COALESCE(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),0)
               FROM virtual_trades WHERE account_id=? AND action='SELL' AND strategy=?""",
            (ACCOUNT_ID, key),
        ).fetchone()
        pool_rows.append(
            f"<tr><td><b>{esc(cfg['label'])}</b><span>{esc(key)}</span></td>"
            f"<td>{cfg['capital_pct'] * 100:.0f}% / {fmt_money(INITIAL_CASH * cfg['capital_pct'])}</td>"
            f"<td>{fmt_money(exposure)} / {count}只</td><td>{int(sold[0] or 0)}笔 / {float(sold[2] or 0):.1f}%</td>"
            f"<td class='{'pos' if float(sold[1] or 0) >= 0 else 'neg'}'>{fmt_money(sold[1])}</td></tr>"
        )
    pool_rows_html = "".join(pool_rows)

    pos_rows = "".join(
        f"<tr><td><a href='/invest/virtual-account/{esc(p['code'])}.html'><b>{esc(p['name'])}</b><span>{esc(p['code'])} · 查看交易档案</span></a></td><td>{p['qty']}</td><td>{fmt_price(p['avg_cost'])}</td><td>{fmt_price(p['last_price'])}<span>{esc((p.get('quote_source') or '-').replace('公开行情',''))} {esc((p.get('quote_at') or '-')[-8:-3])}</span></td><td class='{ 'pos' if (p.get('day_chg_pct') or 0) >= 0 else 'neg' }'>{fmt_pct(p.get('day_chg_pct'))}</td><td class='{ 'pos' if (holding_return_pct(p) or 0) >= 0 else 'neg' }'>{fmt_pct(holding_return_pct(p))}</td><td>{fmt_money(p['market_value'])}</td><td class='{ 'pos' if p['unrealized_pnl'] >= 0 else 'neg' }'>{fmt_money(p['unrealized_pnl'])}</td><td>{esc(STRATEGY_POOLS.get(p.get('strategy') or '', {}).get('label', p.get('strategy') or '-'))}</td></tr>"
        for p in positions
    ) or "<tr><td colspan='9' class='empty'>暂无持仓。当前策略不会买入无 buy_point 的观察票。</td></tr>"
    trade_rows = "".join(
        f"<tr><td>{esc(t['trade_at'][:16])}</td><td>{esc(t['action'])}</td><td><b>{esc(t['name'])}</b><span>{esc(t['code'])}</span></td><td>{t['qty']}</td><td>{fmt_price(t['price'])}</td><td class='{ 'pos' if (t['realized_pnl'] or 0) >= 0 else 'neg' }'>{fmt_money(t['realized_pnl'])}</td><td>{esc(t.get('reason') or '')}</td></tr>"
        for t in trades
    ) or "<tr><td colspan='7' class='empty'>暂无交易。</td></tr>"
    obs_rows = "".join(
        f"<tr><td>{esc(o['observed_at'][:16])}</td><td><b>{esc(o['name'])}</b><span>{esc(o['code'])}</span></td><td>{esc(o['action'])}</td><td>{esc(o.get('theme') or '-')}</td><td>{esc(o.get('reason') or '')}</td></tr>"
        for o in obs
    )
    suggestion = fb["suggestion"] if fb else "暂无反馈。"
    status = "只观察不交易" if mret <= MONTHLY_STOP_LOSS_PCT else "自动模拟中"
    html_page = f"""<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1.0'><title>虚拟交易账户 | 选股雷达</title><link rel='canonical' href='https://mazhi.icu/invest/virtual-account.html'><style>
:root{{--bg:#eef4ef;--card:#ffffffd9;--ink:#102032;--muted:#65758a;--line:#d6e0dc;--green:#08785b;--red:#c4374c;--gold:#b88718}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(180deg,#f8faf4,#e6eee8);color:var(--ink);font:14px/1.65 -apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif}}body:before{{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(16,32,50,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(16,32,50,.03) 1px,transparent 1px);background-size:42px 42px;pointer-events:none}}.wrap{{position:relative;max-width:1180px;margin:0 auto;padding:26px 18px 46px}}.top{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:18px}}a{{color:inherit}}.nav a{{display:inline-flex;margin-left:8px;text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:8px 11px;background:#fff9}}h1{{margin:0;font-size:30px;line-height:1.2}}.sub{{color:var(--muted);margin-top:6px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px;box-shadow:0 16px 42px rgba(47,67,86,.10)}}.card span{{display:block;color:var(--muted);font-size:12px}}.card b{{display:block;font-size:22px;margin-top:4px}}.pos{{color:var(--green)!important}}.neg{{color:var(--red)!important}}.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:14px 0;overflow:hidden;box-shadow:0 14px 36px rgba(47,67,86,.09)}}.title{{display:flex;justify-content:space-between;gap:10px;padding:13px 16px;border-bottom:1px solid var(--line);font-weight:800}}.pad{{padding:15px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:11px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{font-size:12px;color:var(--muted);background:#f5f8f5}}td span{{display:block;color:var(--muted);font-size:11px}}tr:last-child td{{border-bottom:0}}.empty{{color:var(--muted);text-align:center}}.feedback{{display:grid;grid-template-columns:1fr 2fr;gap:12px}}.badge{{display:inline-flex;border-radius:999px;padding:5px 10px;background:rgba(8,120,91,.10);color:var(--green);font-weight:800}}@media(max-width:850px){{.top{{display:block}}.nav{{margin-top:10px}}.grid{{grid-template-columns:1fr 1fr}}.feedback{{grid-template-columns:1fr}}.panel{{overflow-x:auto}}table{{min-width:760px}}}}
</style></head><body><main class='wrap'><header class='top'><div><h1>选股雷达 50万虚拟账户</h1><div class='sub'>严格按交易时段、T+1和可成交价格模拟；历史成交不回改。</div></div><nav class='nav'><a href='https://mazhi.icu/invest/stock-lab.html?v=20260722'>返回选股雷达</a></nav></header><section class='grid'><div class='card'><span>账户状态</span><b>{esc(status)}</b></div><div class='card'><span>总资产</span><b>{fmt_money(equity)}</b></div><div class='card'><span>累计收益</span><b class='{ 'pos' if ret >= 0 else 'neg' }'>{fmt_pct(ret)}</b></div><div class='card'><span>本月收益</span><b class='{ 'pos' if mret >= 0 else 'neg' }'>{fmt_pct(mret)}</b></div><div class='card'><span>现金</span><b>{fmt_money(acc['cash'])}</b></div><div class='card'><span>持仓市值</span><b>{fmt_money(market_value)}</b></div></section><section class='panel'><div class='title'>策略分仓 <span class='sub'>预留20%现金；额度不是必须买满</span></div><div class='pad'><table><thead><tr><th>策略</th><th>额度</th><th>当前占用</th><th>平仓 / 胜率</th><th>已实现收益</th></tr></thead><tbody>{pool_rows_html}</tbody></table></div></section><section class='panel'><div class='title'>策略反馈 <span class='badge'>{esc(suggestion)}</span></div><div class='pad feedback'><div><b>成交闸门</b><p class='sub'>仅连续竞价时段；评分和资金达标；现价在买点-3%至+2%；重复、解禁、减持及弱主题信号不交易。</p></div><div><b>退出与反哺</b><p class='sub'>严格T+1；止损、目标价、移动止盈和时间退出。每套策略独立统计，样本不足30笔不扩大仓位；本月亏损10%自动转为只观察。</p></div></div></section><section class='panel'><div class='title'>当前持仓 <span class='sub'>东方财富优先，异常时回退新浪</span></div><div class='pad'><table><thead><tr><th>标的</th><th>数量</th><th>成本</th><th>现价 / 时间</th><th>今日涨跌</th><th>持仓收益率</th><th>市值</th><th>浮盈</th><th>策略</th></tr></thead><tbody>{pos_rows}</tbody></table></div></section><section class='panel'><div class='title'>交易记录</div><div class='pad'><table><thead><tr><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>已实现</th><th>原因</th></tr></thead><tbody>{trade_rows}</tbody></table></div></section><section class='panel'><div class='title'>观察记录</div><div class='pad'><table><thead><tr><th>时间</th><th>标的</th><th>动作</th><th>主题</th><th>原因</th></tr></thead><tbody>{obs_rows}</tbody></table></div></section><div class='sub'>生成时间 {now_str()}</div></main></body></html>"""
    out = Path(out_path or BASE_DIR / "virtual-account.html")
    out.write_text(html_page, encoding="utf-8")
    generate_stock_detail_pages(conn)
    return out


def feishu_chat_id():
    v = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if v:
        return v
    for p in ["/home/ubuntu/.feishu_chat_id", str(BASE_DIR / ".feishu_chat_id")]:
        try:
            v = Path(p).read_text(encoding="utf-8").strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def push_daily_report(conn):
    chat = feishu_chat_id()
    if not chat or not os.path.exists(LARK_CLI):
        print("virtual report skipped: missing feishu chat or lark cli")
        return False
    acc = account(conn)
    market_value, upnl = position_value(conn)
    equity = float(acc["cash"]) + market_value
    ret = (equity / INITIAL_CASH - 1) * 100
    mret = monthly_return_pct(conn)
    positions = open_positions(conn)
    today_trades = [dict(r) for r in conn.execute(
        "SELECT * FROM virtual_trades WHERE account_id=? AND trade_at>=? ORDER BY trade_at,id",
        (ACCOUNT_ID, today_str()),
    )]
    today_obs = [dict(r) for r in conn.execute(
        "SELECT * FROM virtual_signal_observations WHERE account_id=? AND observed_at>=? ORDER BY observed_at DESC,id DESC LIMIT 8",
        (ACCOUNT_ID, today_str()),
    )]
    status = "只观察不交易" if mret <= MONTHLY_STOP_LOSS_PCT else "自动交易运行中"
    lines = [
        f"📘 虚拟交易日报（{dt.datetime.now():%m-%d %H:%M}）",
        f"账户：50万选股雷达模拟盘｜状态：{status}",
        f"总资产 {fmt_money(equity)}｜总收益 {fmt_pct(ret)}｜本月 {fmt_pct(mret)}｜现金 {fmt_money(acc['cash'])}｜持仓 {len(positions)}只",
        "规则：A股T+1；涨停不假装买到；跌停不假装卖出；月亏10%只观察。",
    ]
    if today_trades:
        lines.append("今日交易：")
        for t in today_trades[:8]:
            lines.append(f"- {t['action']} {t['name']}({t['code']}) {t['qty']}股 @ {t['price']:.2f}，原因：{t.get('reason') or '-'}")
    else:
        lines.append("今日交易：无。")
    if positions:
        lines.append("当前持仓：")
        for p in positions[:8]:
            lines.append(f"- {p['name']}({p['code']}) {p['qty']}股，成本{p['avg_cost']:.2f}，现价{(p.get('last_price') or 0):.2f}，浮盈{fmt_money(p.get('unrealized_pnl'))}")
    if today_obs:
        lines.append("观察/未成交原因：")
        for o in today_obs[:6]:
            lines.append(f"- {o.get('name') or '-'}({o.get('code') or '-'}) {o.get('action') or '-'}：{o.get('reason') or '-'}")
    lines.append("页面：https://mazhi.icu/invest/virtual-account.html")
    try:
        r = subprocess.run(
            [LARK_CLI, "im", "+messages-send", "--as", "bot", "--chat-id", chat, "--text", "\n".join(lines)],
            capture_output=True,
            text=True,
            timeout=25,
        )
        ok = '"ok": true' in (r.stdout or "") or '"ok":true' in (r.stdout or "")
        print("virtual report pushed" if ok else f"virtual report push maybe failed: {(r.stdout or r.stderr)[:200]}")
        return ok
    except Exception as e:
        print(f"virtual report push failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    conn = conn_db()
    ensure_schema(conn)
    if args.run:
        buys = run_once(conn)
        print(f"virtual trader completed, buys={buys}")
    if args.generate or args.run or args.init:
        out = generate_page(conn)
        print(f"generated {out}")
    if args.push:
        push_daily_report(conn)
    conn.close()


if __name__ == "__main__":
    main()
