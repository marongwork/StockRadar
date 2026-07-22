#!/usr/bin/env python3
"""盘中指数异动监控：识别冲高回落、翻绿和短时加速下跌。"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timedelta, time
from pathlib import Path

import stock_picker

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "invest.db"
LARK_CLI = "/home/ubuntu/.npm-global/bin/lark-cli"
INDEXES = {
    "sh000688": "科创50",
    "sh000001": "上证指数",
    "sz399006": "创业板指",
}
COOLDOWN_MINUTES = 30
RESCAN_COOLDOWN_MINUTES = 60
MAX_ADAPTIVE_RESCANS_PER_DAY = 3


def in_market_session(at=None):
    at = at or datetime.now()
    if at.weekday() >= 5:
        return False
    current = at.time()
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


def fetch_index_quotes():
    url = "http://hq.sinajs.cn/list=" + ",".join(INDEXES)
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0",
    })
    raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
    quotes = {}
    for symbol, payload in re.findall(r'hq_str_(\w+)="([^"]*)"', raw):
        parts = payload.split(",")
        if symbol not in INDEXES or len(parts) < 32:
            continue
        try:
            open_price, previous, current = map(float, parts[1:4])
            high, low = float(parts[4]), float(parts[5])
            if previous <= 0 or current <= 0:
                continue
            chg = (current / previous - 1) * 100
            high_chg = (high / previous - 1) * 100
            quotes[symbol] = {
                "code": symbol, "name": INDEXES[symbol], "open": open_price,
                "previous": previous, "current": current, "high": high, "low": low,
                "chg_pct": round(chg, 3), "high_chg_pct": round(high_chg, 3),
                "retreat_pct": round(high_chg - chg, 3),
                "amount": float(parts[9] or 0), "trade_date": parts[30], "quote_time": parts[31],
            }
        except (TypeError, ValueError):
            continue
    return quotes


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS market_intraday_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sampled_at TEXT NOT NULL,
        trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
        current REAL, open REAL, previous REAL, high REAL, low REAL,
        chg_pct REAL, high_chg_pct REAL, retreat_pct REAL, amount REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_intraday_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, alert_at TEXT NOT NULL,
        trade_date TEXT NOT NULL, code TEXT NOT NULL, alert_type TEXT NOT NULL,
        severity INTEGER NOT NULL, title TEXT, analysis TEXT, payload_json TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intraday_sample ON market_intraday_samples(code, sampled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intraday_alert ON market_intraday_alerts(code, alert_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_adaptive_rescans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, triggered_at TEXT NOT NULL,
        trade_date TEXT NOT NULL, alert_type TEXT NOT NULL, status TEXT NOT NULL,
        note TEXT
    )""")
    conn.commit()


def recent_history(conn, code, before, minutes=20):
    start = (before - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    return [dict(zip(("sampled_at", "chg_pct", "retreat_pct"), row)) for row in conn.execute(
        """SELECT sampled_at,chg_pct,retreat_pct FROM market_intraday_samples
           WHERE code=? AND sampled_at>=? ORDER BY sampled_at""", (code, start)
    )]


def detect_event(quote, history):
    """返回 (类型, 风险级别, 标题)；级别 1-3。"""
    chg = quote["chg_pct"]
    high_chg = quote["high_chg_pct"]
    retreat = quote["retreat_pct"]
    prior = history[-1]["chg_pct"] if history else None
    oldest = history[0]["chg_pct"] if history else None
    if high_chg >= 0.8 and retreat >= 2.0 and chg <= -0.5:
        return "surge_reversal", 3, "冲高后大幅转弱"
    if high_chg >= 0.5 and retreat >= 1.2 and chg <= -0.1:
        return "red_to_green", 2, "由红翻绿"
    if prior is not None and prior >= 0.3 and chg <= -0.1:
        return "red_to_green", 2, "由红翻绿"
    if oldest is not None and oldest - chg >= 0.8 and chg <= 0:
        return "accelerating_down", 2, "15分钟加速走弱"
    if high_chg >= 0.8 and retreat >= 0.9 and chg <= 0.4:
        return "high_retreat", 1, "冲高回落"
    return None


def market_context():
    rows = stock_picker.iw_query(
        "同花顺全A 今日涨跌幅 上涨家数 下跌家数 涨停家数 跌停家数 主力资金净流入 沪深两市成交额",
        skill="hithink-zhishu-query", limit="2",
    )
    if not rows:
        return {}
    row = rows[0]
    return {
        "up": stock_picker.num(row, "上涨家数"),
        "down": stock_picker.num(row, "下跌家数"),
        "main_net": stock_picker.num(row, "主力净买入额", "主力资金净流入"),
        "market_chg": stock_picker.num(row, "最新涨跌幅", "涨跌幅"),
    }


def explain(quotes, context):
    k50 = quotes["sh000688"]["chg_pct"]
    sh = quotes.get("sh000001", {}).get("chg_pct")
    cyb = quotes.get("sz399006", {}).get("chg_pct")
    reasons = []
    if cyb is not None and cyb <= -1 and k50 <= -1:
        reasons.append("科创50与创业板同步走弱，数据指向成长风格整体退潮")
    if sh is not None and sh - k50 >= 1:
        reasons.append(f"科创50弱于上证{sh-k50:.2f}个百分点，科技成长明显跑输权重")
    up, down = context.get("up"), context.get("down")
    if up is not None and down is not None and up + down > 0:
        ratio = up / (up + down) * 100
        if ratio < 40:
            reasons.append(f"全市场仅约{ratio:.0f}%股票上涨，风险偏好正在收缩")
    main_net = context.get("main_net")
    if main_net is not None:
        direction = "净流出" if main_net < 0 else "净流入"
        reasons.append(f"全A主力资金{direction}{stock_picker.fmt(abs(main_net))}")
    return "；".join(reasons) or "指数价格已转弱，但暂缺全市场资金数据，先按技术性风险处理"


def should_push(conn, event, quote, now):
    row = conn.execute(
        """SELECT alert_at,alert_type,severity,payload_json FROM market_intraday_alerts
           WHERE code=? AND trade_date=? ORDER BY alert_at DESC LIMIT 1""",
        (quote["code"], quote["trade_date"]),
    ).fetchone()
    if not row:
        return True
    last_at = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    if now - last_at >= timedelta(minutes=COOLDOWN_MINUTES):
        return True
    old = json.loads(row[3] or "{}")
    return event[1] > row[2] or quote["retreat_pct"] >= float(old.get("retreat_pct", 0)) + 0.6


def push_feishu(quote, quotes, event, analysis):
    chat = stock_picker.feishu_chat_id()
    if not chat or not os.path.exists(LARK_CLI):
        return False, "飞书配置不可用"
    sh = quotes.get("sh000001", {}).get("chg_pct")
    cyb = quotes.get("sz399006", {}).get("chg_pct")
    icon = "🚨" if event[1] >= 3 else "⚠️"
    lines = [
        f"{icon} 科创50盘中提醒：{event[2]}（{quote['quote_time'][:5]}）",
        f"当前 {quote['chg_pct']:+.2f}% · 日内最高 {quote['high_chg_pct']:+.2f}% · 高位回落 {quote['retreat_pct']:.2f}个百分点",
        f"联动：上证 {sh:+.2f}% · 创业板 {cyb:+.2f}%" if sh is not None and cyb is not None else "",
        f"判断：{analysis}",
        "应对：暂停追高科创和高位科技；等待指数止跌、市场宽度及资金重新转强后再恢复试仓。",
        "复盘：https://mazhi.icu/invest/stock-lab.html#market-fund",
        "⚠️ 数据触发的风险提示，不构成投资建议",
    ]
    result = subprocess.run(
        [LARK_CLI, "im", "+messages-send", "--as", "bot", "--chat-id", chat, "--text", "\n".join(x for x in lines if x)],
        capture_output=True, text=True, timeout=20,
    )
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def trigger_adaptive_rescan(conn, event, now):
    """重大市场状态变化最多每小时重选一次，每日不超过三次。"""
    if event[1] < 2:
        return False, "一级波动不触发重选"
    today = now.date().isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM market_adaptive_rescans WHERE trade_date=?", (today,)
    ).fetchone()[0]
    if count >= MAX_ADAPTIVE_RESCANS_PER_DAY:
        return False, "已达到每日3次自适应重选上限"
    last = conn.execute(
        "SELECT triggered_at FROM market_adaptive_rescans ORDER BY triggered_at DESC,id DESC LIMIT 1"
    ).fetchone()
    if last:
        last_at = datetime.strptime(last[0], "%Y-%m-%d %H:%M:%S")
        if now - last_at < timedelta(minutes=RESCAN_COOLDOWN_MINUTES):
            return False, "距离上次自适应重选不足60分钟"
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        """INSERT INTO market_adaptive_rescans(triggered_at,trade_date,alert_type,status,note)
           VALUES(?,?,?,?,?)""", (stamp, today, event[0], "running", event[2])
    )
    rescan_id = cursor.lastrowid
    conn.commit()
    try:
        picker = subprocess.run(
            ["/usr/bin/python3", str(BASE_DIR / "stock_picker.py"), "--market-reversal"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=300,
        )
        if picker.returncode != 0:
            raise RuntimeError((picker.stderr or picker.stdout)[-800:])
        trader = subprocess.run(
            ["/usr/bin/python3", str(BASE_DIR / "virtual_trader.py"), "--run"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=120,
        )
        status = "complete" if trader.returncode == 0 else "picker_only"
        note = (picker.stdout[-500:] + "\n" + (trader.stdout or trader.stderr)[-300:]).strip()
        conn.execute("UPDATE market_adaptive_rescans SET status=?,note=? WHERE id=?", (status, note, rescan_id))
        conn.commit()
        return True, status
    except Exception as exc:
        conn.execute("UPDATE market_adaptive_rescans SET status='failed',note=? WHERE id=?", (str(exc)[:1000], rescan_id))
        conn.commit()
        return False, str(exc)


def run(no_push=False, force=False):
    now = datetime.now()
    if not force and not in_market_session(now):
        print("非交易时段，跳过")
        return 0
    quotes = fetch_index_quotes()
    if "sh000688" not in quotes:
        raise RuntimeError("未取得科创50实时行情")
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    quote = quotes["sh000688"]
    history = recent_history(conn, quote["code"], now)
    event = detect_event(quote, history)
    sampled_at = now.strftime("%Y-%m-%d %H:%M:%S")
    for q in quotes.values():
        conn.execute(
            """INSERT INTO market_intraday_samples
               (sampled_at,trade_date,code,name,current,open,previous,high,low,chg_pct,high_chg_pct,retreat_pct,amount)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sampled_at, q["trade_date"], q["code"], q["name"], q["current"], q["open"], q["previous"],
             q["high"], q["low"], q["chg_pct"], q["high_chg_pct"], q["retreat_pct"], q["amount"]),
        )
    conn.commit()
    print(f"科创50 {quote['chg_pct']:+.2f}% 最高 {quote['high_chg_pct']:+.2f}% 回落 {quote['retreat_pct']:.2f}pp")
    if not event:
        conn.close()
        return 0
    if not should_push(conn, event, quote, now):
        print("冷却期内且风险未显著扩大，不重复推送")
        conn.close()
        return 0
    context = market_context()
    analysis = explain(quotes, context)
    print(f"触发：{event[2]}｜{analysis}")
    payload = dict(quote, indexes={k: v["chg_pct"] for k, v in quotes.items()}, context=context)
    conn.execute(
        """INSERT INTO market_intraday_alerts
           (alert_at,trade_date,code,alert_type,severity,title,analysis,payload_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sampled_at, quote["trade_date"], quote["code"], event[0], event[1], event[2], analysis,
         json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    if not no_push:
        ok, result = push_feishu(quote, quotes, event, analysis)
        print("飞书推送成功" if ok else f"飞书推送失败：{result}")
        rescanned, detail = trigger_adaptive_rescan(conn, event, now)
        print(f"自适应重选：{'完成' if rescanned else '跳过'}（{detail}）")
    conn.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(no_push=args.no_push, force=args.force))
