#!/usr/bin/env python3
"""每分钟扫描东方财富公开资金流榜，捕捉未涨停的盘中资金加速信号。"""
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

import stock_picker

DB_PATH = stock_picker.DB_PATH
LARK_CLI = stock_picker.LARK_CLI
MAX_DAILY_PUSHES = 5
SIGNAL_COOLDOWN_MINUTES = 30


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS public_flow_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sampled_at TEXT NOT NULL, code TEXT NOT NULL,
        name TEXT, price REAL, chg_pct REAL, main_net REAL, main_ratio REAL,
        amount REAL, turnover REAL, volume_ratio REAL, flow_rank INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS public_flow_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_at TEXT NOT NULL, code TEXT NOT NULL,
        name TEXT, signal_type TEXT, score REAL, summary TEXT, payload_json TEXT, pushed INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS public_api_state (
        source TEXT PRIMARY KEY, consecutive_failures INTEGER DEFAULT 0,
        blocked_until TEXT, last_success_at TEXT, last_error TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_public_flow_sample ON public_flow_snapshots(sampled_at,code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_public_flow_signal ON public_flow_signals(signal_at,code)")
    conn.commit()


def detect_signal(row, previous=None):
    chg = float(row.get("最新涨跌幅") or 0)
    main = float(row.get("主力资金净流入") or 0)
    ratio = float(row.get("主力净流入占比") or 0)
    volume_ratio = float(row.get("量比") or 0)
    turnover = float(row.get("换手率") or 0)
    rank = int(row.get("资金排名") or 9999)
    if not (-1 <= chg < 6.5) or main < 8e7 or ratio < 5:
        return None
    score = 55
    tags = ["未涨停", "资金净流入估算"]
    if main >= 2e8:
        score += 12; tags.append("净流入超2亿")
    elif main >= 1e8:
        score += 8
    if ratio >= 10:
        score += 10; tags.append("净流入占比高")
    elif ratio >= 7:
        score += 6
    if 1.2 <= volume_ratio <= 3.5:
        score += 8; tags.append("量比确认")
    if 2 <= turnover <= 15:
        score += 5; tags.append("换手健康")
    signal_type = "flow_entry"
    if previous:
        delta = main - float(previous.get("main_net") or 0)
        rank_jump = int(previous.get("flow_rank") or rank) - rank
        if delta >= 5e7 or rank_jump >= 20:
            score += 12; tags.append("资金加速")
            signal_type = "flow_acceleration"
        elif rank > 30:
            return None
    elif rank > 20:
        return None
    return signal_type, score, "+".join(tags)


def push_signals(signals, at):
    chat = stock_picker.feishu_chat_id()
    if not chat or not os.path.exists(LARK_CLI) or not signals:
        return False
    lines = [f"📡 盘中资金异动（{at:%H:%M}）", "来源：东方财富公开行情资金流估算；非交易所主力身份数据。"]
    for idx, signal in enumerate(signals[:3], 1):
        row = signal["row"]
        lines.append(
            f"{idx}. {row['股票简称']}({row['股票代码']}) {row['最新涨跌幅']:+.2f}% "
            f"资金估算{stock_picker.fmt(row['主力资金净流入'])} 占比{row.get('主力净流入占比') or 0:.1f}% "
            f"评分{signal['score']:.0f} [{signal['summary']}]"
        )
    lines.extend(["仅进入观察池；通过事件、风险和策略闸门后才生成买点。", "https://mazhi.icu/invest/stock-lab.html"])
    result = subprocess.run(
        [LARK_CLI, "im", "+messages-send", "--as", "bot", "--chat-id", chat, "--text", "\n".join(lines)],
        capture_output=True, text=True, timeout=20,
    )
    return result.returncode == 0


def run():
    now = datetime.now()
    if now.weekday() >= 5:
        return 0
    hm = (now.hour, now.minute)
    if not ((9, 30) <= hm <= (11, 30) or (13, 0) <= hm <= (15, 0)):
        return 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    state = conn.execute("SELECT * FROM public_api_state WHERE source='eastmoney_flow'").fetchone()
    if state and state["blocked_until"] and now < datetime.strptime(state["blocked_until"], "%Y-%m-%d %H:%M:%S"):
        conn.close(); return 0
    started = time.monotonic()
    rows = stock_picker.fetch_public_fund_flow_pool(limit=300)
    if not rows:
        failures = int(state["consecutive_failures"] or 0) + 1 if state else 1
        wait_minutes = min(30, 2 ** min(failures, 5))
        conn.execute(
            """INSERT INTO public_api_state(source,consecutive_failures,blocked_until,last_error)
               VALUES('eastmoney_flow',?,?,?) ON CONFLICT(source) DO UPDATE SET
               consecutive_failures=excluded.consecutive_failures,blocked_until=excluded.blocked_until,last_error=excluded.last_error""",
            (failures, (now + timedelta(minutes=wait_minutes)).strftime("%Y-%m-%d %H:%M:%S"), "空响应或请求失败"),
        )
        conn.commit(); conn.close(); return 1
    previous_at = conn.execute("SELECT MAX(sampled_at) FROM public_flow_snapshots").fetchone()[0]
    previous = {}
    if previous_at:
        previous = {r["code"]: dict(r) for r in conn.execute(
            "SELECT * FROM public_flow_snapshots WHERE sampled_at=?", (previous_at,)
        )}
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    signals = []
    for rank, row in enumerate(rows, 1):
        row["资金排名"] = rank
        code = str(row.get("股票代码") or "")
        conn.execute(
            """INSERT INTO public_flow_snapshots
               (sampled_at,code,name,price,chg_pct,main_net,main_ratio,amount,turnover,volume_ratio,flow_rank)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (stamp, code, row.get("股票简称"), row.get("最新价"), row.get("最新涨跌幅"),
             row.get("主力资金净流入"), row.get("主力净流入占比"), row.get("成交额"),
             row.get("换手率"), row.get("量比"), rank),
        )
        detected = detect_signal(row, previous.get(code)) if previous_at else None
        if not detected:
            continue
        last = conn.execute(
            "SELECT signal_at FROM public_flow_signals WHERE code=? ORDER BY signal_at DESC,id DESC LIMIT 1", (code,)
        ).fetchone()
        if last and now - datetime.strptime(last[0], "%Y-%m-%d %H:%M:%S") < timedelta(minutes=SIGNAL_COOLDOWN_MINUTES):
            continue
        signal_type, score, summary = detected
        signals.append({"row": row, "type": signal_type, "score": score, "summary": summary})
    signals.sort(key=lambda x: -x["score"])
    pushed_today = conn.execute(
        "SELECT COUNT(*) FROM public_flow_signals WHERE pushed=1 AND substr(signal_at,1,10)=?", (now.date().isoformat(),)
    ).fetchone()[0]
    selected = signals[:max(0, min(3, MAX_DAILY_PUSHES - pushed_today))]
    pushed = push_signals(selected, now) if selected else False
    for signal in signals[:10]:
        row = signal["row"]
        is_pushed = int(pushed and signal in selected)
        conn.execute(
            """INSERT INTO public_flow_signals(signal_at,code,name,signal_type,score,summary,payload_json,pushed)
               VALUES(?,?,?,?,?,?,?,?)""",
            (stamp, row["股票代码"], row["股票简称"], signal["type"], signal["score"], signal["summary"],
             json.dumps(row, ensure_ascii=False), is_pushed),
        )
    conn.execute(
        """INSERT INTO public_api_state(source,consecutive_failures,blocked_until,last_success_at,last_error)
           VALUES('eastmoney_flow',0,NULL,?,NULL) ON CONFLICT(source) DO UPDATE SET
           consecutive_failures=0,blocked_until=NULL,last_success_at=excluded.last_success_at,last_error=NULL""", (stamp,),
    )
    cutoff = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM public_flow_snapshots WHERE sampled_at<?", (cutoff,))
    conn.commit(); conn.close()
    print(f"公开行情扫描 {len(rows)}只，信号{len(signals)}，推送{len(selected) if pushed else 0}，耗时{time.monotonic()-started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
