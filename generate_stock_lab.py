#!/usr/bin/env python3
"""Generate the public stock radar/backtest page.

This page intentionally excludes personal account, holdings, cash, fund, and
transaction data. It only exposes stock-pick signals and strategy backtests.
"""
import json
import html
import math
import re
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

try:
    import stock_picker
except Exception:
    stock_picker = None

try:
    from agent_council import apply_candidate_review
except Exception:
    apply_candidate_review = None


def connect(base_dir):
    db = Path(base_dir) / "invest.db"
    if not db.exists():
        raise SystemExit(f"ERROR: {db} not found")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def clean(obj):
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj

# 前端渲染实际使用的字段白名单，减少内嵌JSON体积
_SIGNAL_FIELDS = {
    "id", "code", "name", "theme", "score", "rank", "run_id",
    "picked_at", "picked_date", "picked_time", "chg_pct",
    "buy_point", "stop_loss", "target", "cap", "pe",
    "main_net", "super_net", "big_net", "lobby_net",
    "seal_amount", "break_count", "turnover", "board_count",
    "fund_grade", "fund_score", "fund_tags", "reason", "weight",
    "industry", "concepts", "sector_chg", "sector_main_net",
    "support_price", "resistance_price", "focus_high", "focus_low",
    "position_pct",
    "agent_consensus", "agent_confidence", "agent_disagreement",
    "agent_reviews_json", "risk_level", "risk_veto", "risk_reasons",
    "market_snapshot_at", "quote_as_of", "first_limit_at", "final_limit_at",
}

def slim_signals(signals):
    """只保留前端需要的字段，减少页面体积。"""
    return [{k: v for k, v in s.items() if k in _SIGNAL_FIELDS and v is not None} for s in signals]


def table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_data(conn):
    tables = table_names(conn)
    latest_picks = []
    all_signals = []
    pick_history = []
    backtest_runs = []
    backtest_trades = []
    if "stock_picks" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_picks)")}
        if "run_id" in cols:
            picked_time_expr = "COALESCE(picked_time, substr(picked_at,12,5)) AS picked_time" if "picked_time" in cols else "substr(picked_at,12,5) AS picked_time"
            latest = conn.execute(
                "SELECT run_id FROM stock_picks WHERE run_id IS NOT NULL ORDER BY picked_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if latest:
                latest_picks = [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT *, {picked_time_expr} FROM stock_picks WHERE run_id=? ORDER BY rank",
                        (latest["run_id"],),
                    )
                ]
            all_signals = [
                dict(r)
                for r in conn.execute(
                    f"""SELECT *, {picked_time_expr}
                        FROM stock_picks
                        WHERE run_id IS NOT NULL
                        ORDER BY picked_at DESC, id DESC
                        LIMIT 200"""
                )
            ]
            pick_history = [
                dict(r)
                for r in conn.execute(
                    """SELECT picked_date, run_id, MIN(picked_at) first_signal_at, MAX(picked_at) last_signal_at,
                              COUNT(*) picks,
                              SUM(CASE WHEN eval_status='success' THEN 1 ELSE 0 END) success,
                              SUM(CASE WHEN eval_status='stop' THEN 1 ELSE 0 END) stopped,
                              SUM(CASE WHEN eval_status IS NOT NULL THEN 1 ELSE 0 END) evaluated,
                              AVG(eval_return_pct) avg_return
                       FROM stock_picks
                       WHERE run_id IS NOT NULL
                       GROUP BY picked_date, run_id
                       ORDER BY picked_at DESC
                       LIMIT 50"""
                )
            ]
        else:
            latest_picks = [dict(r) for r in conn.execute("SELECT * FROM stock_picks ORDER BY rank")]
            all_signals = latest_picks

    if "backtest_runs" in tables:
        raw_runs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM backtest_runs ORDER BY run_at DESC, id DESC LIMIT 80"
            )
        ]
        seen_runs = set()
        for r in raw_runs:
            key = (
                r.get("strategy"),
                r.get("start_date"),
                r.get("end_date"),
                r.get("horizon_days"),
                r.get("fee_bps"),
                r.get("slippage_bps"),
            )
            if key in seen_runs:
                continue
            seen_runs.add(key)
            backtest_runs.append(r)
            if len(backtest_runs) >= 24:
                break
    if "backtest_trades" in tables and backtest_runs:
        run_ids = [r["id"] for r in backtest_runs[:12] if r.get("id")]
        placeholders = ",".join("?" for _ in run_ids)
        backtest_trades = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM backtest_trades WHERE run_id IN ({placeholders}) ORDER BY signal_date DESC, rank LIMIT 240",
                run_ids,
            )
        ] if run_ids else []
    latest_picks = [p for p in latest_picks if not is_beijing_code(p.get("code"))]
    all_signals = [p for p in all_signals if not is_beijing_code(p.get("code"))]
    backtest_trades = [t for t in backtest_trades if not is_beijing_code(t.get("code"))]
    return latest_picks, all_signals, pick_history, backtest_runs, backtest_trades


def dumps(data):
    return json.dumps(clean(data), ensure_ascii=False)


def norm_code(code):
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def is_beijing_code(code):
    return norm_code(code).startswith(("4", "8", "92"))


def quote_url(code):
    c = norm_code(code)
    market = "sh" if c.startswith(("600", "601", "603", "605", "688", "689")) else "sz"
    return f"https://quote.eastmoney.com/{market}{c}.html"


def detail_url(code):
    return f"stock-lab/{norm_code(code)}.html"


def limit_snapshot_picks(snapshot):
    """Convert the current limit-up snapshot into non-executable detail records."""
    snapshot_at = str((snapshot or {}).get("snapshot_at") or datetime.now().strftime("%Y-%m-%d %H:%M"))[:16]
    picks = []
    for rank, row in enumerate((snapshot or {}).get("limit") or [], 1):
        code = norm_code(pick_value(row, "股票代码"))
        if not code or is_beijing_code(code):
            continue
        name = pick_value(row, "股票简称") or code
        catalyst = short_text(pick_value(row, "涨停原因", "所属概念") or "涨停梯队", 120)
        main_net = pick_num(row, "主力资金流向", "主力资金净流入")
        board_count = int(pick_num(row, "连续涨停天数") or 1)
        pick = {
            "code": code,
            "name": name,
            "theme": f"涨停板/{short_text(catalyst, 18)}",
            "score": "-",
            "rank": rank,
            "reason": f"观察不打板+涨停短线+当日涨停不可追买+次日竞价确认+{catalyst}",
            "picked_at": snapshot_at,
            "picked_date": snapshot_at[:10],
            "picked_time": snapshot_at[11:16],
            "run_id": f"limit-{snapshot_at.replace('-', '').replace(':', '').replace(' ', '-')}",
            "chg": pick_num(row, "最新涨跌幅", "涨跌幅"),
            "chg_pct": pick_num(row, "最新涨跌幅", "涨跌幅"),
            "main": main_net,
            "main_net": main_net,
            "seal_amount": pick_num(row, "涨停封单额", "封单金额", "封板资金"),
            "break_count": pick_num(row, "涨停开板次数", "炸板次数", "开板次数"),
            "board_count": board_count,
            "turnover": pick_num(row, "换手率"),
            "cap": pick_num(row, "总市值"),
            "pe": pick_num(row, "动态市盈率", "市盈率"),
            "industry": short_text(pick_value(row, "所属同花顺行业", "所属行业") or "-", 80),
            "concepts": short_text(pick_value(row, "所属概念") or catalyst, 160),
            "position_pct": 0,
            "fund_grade": "B" if (main_net or 0) > 0 else ("D" if (main_net or 0) < 0 else "C"),
        }
        if apply_candidate_review:
            apply_candidate_review(pick)
        picks.append(pick)
    return picks


def signal_detail_url(pick):
    return f"stock-lab/signals/{int(pick.get('id') or 0)}.html"


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def fmt_num(value):
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return str(value)
    if not math.isfinite(v):
        return "-"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.2f}"


def pick_value(row, *needles):
    for k, v in (row or {}).items():
        key = str(k)
        if any(n in key for n in needles):
            if v not in (None, "", "--", "-"):
                return v
    return None


def pick_num(row, *needles):
    v = pick_value(row, *needles)
    if v is None:
        return None
    try:
        parsed = float(v)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def fmt_pct(value, digits=2):
    try:
        v = float(value)
    except Exception:
        return "-"
    if not math.isfinite(v):
        return "-"
    return f"{v:+.{digits}f}%"


def short_text(value, limit=34):
    if isinstance(value, (list, tuple, set)):
        text = "、".join(str(item).strip() for item in value if str(item).strip())
    elif isinstance(value, dict):
        text = "、".join(f"{key}:{item}" for key, item in value.items())
    else:
        text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def signal_time(pick):
    picked_at = pick.get("picked_at")
    if picked_at:
        return str(picked_at)[:16]
    d = pick.get("picked_date") or "-"
    t = pick.get("picked_time") or "--:--"
    return f"{d} {t}"


def signal_action(pick):
    reason = pick.get("reason") or ""
    if pick.get("buy_point"):
        return "可执行", "buy"
    if "涨停短线" in reason or "涨停板" in (pick.get("theme") or ""):
        return "待次日竞价", "short"
    return "观察", "watch"


def reason_tags(pick):
    return [t for t in (pick.get("reason") or "").split("+") if t]


def key_decision_points(pick):
    tags = reason_tags(pick)
    points = []
    if pick.get("fund_grade"):
        fund = f"资金{pick.get('fund_grade')}档"
        if pick.get("fund_score") is not None:
            fund += f" {pick.get('fund_score')}分"
        if pick.get("fund_tags"):
            fund += f"：{pick.get('fund_tags')}"
        points.append(fund)
    if pick.get("buy_point"):
        points.append(f"买点 {pick.get('buy_point')}，止损 {pick.get('stop_loss')}，目标 {pick.get('target')}")
    else:
        points.append("当前信号不直接给买点，等待风险解除或强度确认。")
    for tag in tags:
        if tag.startswith("⚠️") or tag in {"观察不买", "观察不打板", "等转强", "资金流入", "卡位", "事件催化", "强封单"}:
            points.append(tag)
        if len(points) >= 6:
            break
    return points


def iw(query, skill, limit="5"):
    if not stock_picker:
        return []
    try:
        return stock_picker.iw_query(query, skill=skill, limit=limit)
    except Exception:
        return []


def compact_row(row, max_items=8):
    parts = []
    for k, v in row.items():
        if v in (None, "", "--", "-"):
            continue
        text = str(v)
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{k}: {text}")
        if len(parts) >= max_items:
            break
    return "；".join(parts)


def list_block(rows):
    if not rows:
        return '<div class="empty">暂无可用数据</div>'
    items = "".join(f"<li>{esc(compact_row(r))}</li>" for r in rows[:5])
    return f"<ul>{items}</ul>"


def load_market_fund(conn):
    if not conn or "market_fund_snapshots" not in table_names(conn):
        return {}
    row = conn.execute(
        "SELECT * FROM market_fund_snapshots ORDER BY snapshot_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    out = {"snapshot_at": row["snapshot_at"]}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_fund_snapshots)")}
    pairs = [("market", "market_json"), ("sectors", "sectors_json"), ("limit", "limit_json"), ("lhb", "lhb_json")]
    if "sentiment_json" in cols:
        pairs.append(("sentiment", "sentiment_json"))
    if "lhb_recent_json" in cols:
        pairs.append(("lhb_recent", "lhb_recent_json"))
    for key, col in pairs:
        try:
            out[key] = json.loads(row[col] or "[]")
        except Exception:
            out[key] = []
    # 旧 sectors_json 曾混入个股资金榜。板块迁徙只读取独立的大板块快照。
    history = []
    if "sector_fund_snapshots" in table_names(conn):
        sector_rows = conn.execute(
            "SELECT * FROM sector_fund_snapshots ORDER BY snapshot_at DESC,id DESC LIMIT 400"
        ).fetchall()
        for historic in sector_rows:
            try:
                concept_rows = json.loads(historic["concept_json"] or "[]")
                industry_rows = json.loads(historic["industry_json"] or "[]")
                history.append({
                    "snapshot_at": historic["snapshot_at"],
                    "phase": historic["phase"] if "phase" in historic.keys() else "trading",
                    "sectors": concept_rows + industry_rows,
                })
            except Exception:
                continue
        if sector_rows:
            try:
                out["concepts"] = json.loads(sector_rows[0]["concept_json"] or "[]")
                out["industries"] = json.loads(sector_rows[0]["industry_json"] or "[]")
                out["sectors"] = json.loads(sector_rows[0]["combined_json"] or "[]")
                out["sector_snapshot_at"] = sector_rows[0]["snapshot_at"]
                out["sector_source"] = sector_rows[0]["source"]
            except Exception:
                pass
    out["sector_history"] = history
    if "market_intraday_alerts" in table_names(conn):
        out["intraday_alerts"] = [dict(r) for r in conn.execute(
            """SELECT alert_at,alert_type,severity,title,analysis,payload_json
               FROM market_intraday_alerts ORDER BY alert_at DESC,id DESC LIMIT 8"""
        )]
    return out


def attach_market_timing(signals, snapshot):
    """Attach quote and limit-up event times without treating scan time as trade time."""
    snapshot_at = str((snapshot or {}).get("snapshot_at") or "")[:16]
    snapshot_date = snapshot_at[:10]
    quote_as_of = f"{snapshot_date} 15:00" if snapshot_at[11:16] >= "15:00" else snapshot_at
    limit_by_code = {
        norm_code(pick_value(row, "股票代码")): row
        for row in (snapshot or {}).get("limit") or []
        if norm_code(pick_value(row, "股票代码"))
    }
    for pick in signals or []:
        if str(pick.get("picked_date") or "") != snapshot_date:
            continue
        pick["market_snapshot_at"] = snapshot_at
        pick["quote_as_of"] = quote_as_of
        limit_row = limit_by_code.get(norm_code(pick.get("code")))
        if not limit_row:
            continue
        pick["first_limit_at"] = pick_value(limit_row, "首次涨停时间")
        pick["final_limit_at"] = pick_value(limit_row, "最终涨停时间")
    return signals


def market_fund_panel(snapshot):
    if not snapshot:
        return """<div class="panel" id="market-fund"><div class="title">大盘资金雷达 <span class="subtitle">主力 / 游资 / 板块</span></div><div class="panel-pad"><div class="empty">暂无大盘资金快照，下一次选股任务会自动生成。</div></div></div>"""
    market_rows = snapshot.get("market") or []
    market = next(
        (r for r in market_rows if "全A" in str(pick_value(r, "指数简称", "名称") or r)),
        market_rows[0] if market_rows else {},
    )
    up = pick_num(market, "上涨家数")
    down = pick_num(market, "下跌家数")
    limit_up = pick_num(market, "涨停家数")
    limit_down = pick_num(market, "跌停家数")
    latest = pick_num(market, "最新涨跌幅")
    turnover = pick_num(market, "成交额")
    main_net = pick_num(market, "主力净买入额", "主力资金净流入")
    hot_rows = snapshot.get("limit") or []
    high_board = max([pick_num(r, "连续涨停天数") or 0 for r in hot_rows] or [0])
    break_counts = [pick_num(r, "涨停开板次数", "炸板次数", "开板次数") or 0 for r in hot_rows]
    seal_values = [pick_num(r, "涨停封单额", "封单金额", "封板资金") or 0 for r in hot_rows]
    broken = sum(1 for x in break_counts if x > 0)
    break_rate = broken / len(break_counts) * 100 if break_counts else None
    avg_seal = sum(seal_values) / len(seal_values) if seal_values else None
    breadth_total = (up or 0) + (down or 0)
    breadth = (up / breadth_total * 100) if breadth_total else None
    limit_ratio = (limit_up / max(1, (limit_up or 0) + (limit_down or 0)) * 100) if (limit_up is not None or limit_down is not None) else None
    emotion_score = 0
    if breadth is not None:
        emotion_score += min(45, breadth * 0.45)
    if limit_ratio is not None:
        emotion_score += min(25, limit_ratio * 0.25)
    emotion_score += min(20, high_board * 4)
    if break_rate is not None:
        emotion_score -= min(20, break_rate * 0.2)
    emotion_score = max(0, min(100, round(emotion_score)))
    emotion_label = "强势" if emotion_score >= 70 else ("修复" if emotion_score >= 50 else ("偏弱" if emotion_score >= 30 else "冰点"))
    if emotion_score >= 70 and (breadth or 0) >= 52:
        posture, posture_cls = "进攻", "attack"
        posture_note = "情绪和市场宽度共振，可聚焦主线前排，但仍按计划止损。"
    elif emotion_score >= 50 and (main_net or 0) > 0:
        posture, posture_cls = "谨慎试错", "probe"
        posture_note = "主力资金回流但上涨家数未过半，只做资金前排，不追一致高潮。"
    else:
        posture, posture_cls = "防守观察", "defend"
        posture_note = "赚钱效应不足，减少出手频率，等待指数与市场宽度同步转强。"

    index_changes = []
    def index_cards():
        wanted = [("同花顺全A", "全A"), ("上证指数", "上证"), ("创业板指", "创业板"), ("科创50", "科创50")]
        cards = []
        used = set()
        for full, short in wanted:
            row = next((r for r in market_rows if full in str(pick_value(r, "指数简称", "名称") or r)), None)
            if row is None and full == "同花顺全A":
                row = market
            if row is None:
                chg = pick_num(market, full)
                cards.append(f"<div><span>{esc(short)}</span><b>-</b><small>今日涨跌</small></div>")
                continue
            name = pick_value(row, "指数简称", "名称") or short
            if name in used:
                continue
            used.add(name)
            chg = pick_num(row, "最新涨跌幅", "涨跌幅", full)
            price = pick_num(row, "最新价")
            row_net = pick_num(row, "主力净买入额", "主力资金净流入")
            row_turnover = pick_num(row, "成交额")
            index_changes.append(chg or 0)
            cards.append(
                f"<article class='market-index-card {'up' if (chg or 0) >= 0 else 'down'}'>"
                f"<div><span>{esc(short)}</span><small>{esc(fmt_num(price))}</small></div>"
                f"<b>{esc(fmt_pct(chg))}</b>"
                f"<em>主力 {esc(fmt_num(row_net))} · 额 {esc(fmt_num(row_turnover))}</em></article>"
            )
        return "".join(cards)

    index_html = index_cards()
    avg_index = sum(index_changes) / len(index_changes) if index_changes else None
    breadth_up = max(0, min(100, breadth or 0))
    breadth_down = 100 - breadth_up
    market_divergence = sum(1 for x in index_changes if x >= 0)
    freshness = str(snapshot.get("snapshot_at") or "-")[:16]
    intraday_alerts = snapshot.get("intraday_alerts") or []
    latest_alert = intraday_alerts[0] if intraday_alerts else None
    intraday_html = ""
    if latest_alert:
        try:
            alert_payload = json.loads(latest_alert.get("payload_json") or "{}")
        except Exception:
            alert_payload = {}
        intraday_html = f"""
          <section class='intraday-alert severity-{int(latest_alert.get('severity') or 1)}'>
            <div><span>盘中异动 · {esc(str(latest_alert.get('alert_at') or '')[11:16])}</span><b>{esc(latest_alert.get('title') or '指数转弱')}</b></div>
            <p>{esc(latest_alert.get('analysis') or '')}</p>
            <strong>科创50 {esc(fmt_pct(alert_payload.get('chg_pct')))} · 高位回落 {esc(f"{alert_payload.get('retreat_pct', 0):.2f}个百分点")}</strong>
          </section>"""

    command_html = f"""
      <section class="market-command {posture_cls}">
        <div class="command-copy">
          <span class="command-kicker">MARKET POSTURE · {esc(freshness)}</span>
          <div class="command-title"><b>{esc(posture)}</b><span>{esc(emotion_label)} {esc(emotion_score)}/100</span></div>
          <p>{esc(posture_note)}</p>
          <div class="command-facts">
            <span>全A <b class="{'pos' if (latest or 0) >= 0 else 'neg'}">{esc(fmt_pct(latest))}</b></span>
            <span>主力 <b class="{'pos' if (main_net or 0) >= 0 else 'neg'}">{esc(fmt_num(main_net))}</b></span>
            <span>成交 <b>{esc(fmt_num(turnover))}</b></span>
            <span>指数红盘 <b>{market_divergence}/{len(index_changes) or 4}</b></span>
          </div>
        </div>
        <div class="market-gauges">
          <div class="emotion-dial" style="--score:{emotion_score}"><div><strong>{emotion_score}</strong><span>情绪温度</span></div></div>
          <div class="breadth-chart">
            <div class="chart-head"><span>市场宽度</span><b>{esc(f'{breadth:.0f}%' if breadth is not None else '-')}</b></div>
            <div class="breadth-track"><i class="up" style="width:{breadth_up:.1f}%"></i><i class="down" style="width:{breadth_down:.1f}%"></i></div>
            <div class="breadth-legend"><span><i></i>上涨 {esc(int(up) if up is not None else '-')}</span><span><i></i>下跌 {esc(int(down) if down is not None else '-')}</span></div>
          </div>
        </div>
      </section>
      {intraday_html}
      <section class="market-index-strip">{index_html}</section>"""

    flow_motion_html = ""
    if snapshot.get("sector_history"):
        flow_motion_html = """
        <div class="market-section sector-flow-motion-section">
          <div class="box-title"><div><b>热门大板块分钟走势</b><small>主力净流入累计曲线（亿元）· 每分钟检查新快照</small></div><span>LIVE SECTOR FLOW</span></div>
          <div class="sector-line-toolbar"><div class="sector-line-tools"><div class="sector-line-tabs" role="group" aria-label="板块类型"><button class="active" type="button" data-sector-type="概念">概念板块</button><button type="button" data-sector-type="行业">行业板块</button></div><select id="sectorFlowDate" aria-label="选择板块资金日期"></select></div><div><span id="sectorFlowTime">-</span><b id="sectorFlowState">等待数据</b></div></div>
          <div class="sector-flow-line-chart" id="sectorFlowChart" role="img" aria-label="热门板块分钟资金流向折线图"></div>
          <div class="sector-flow-latest" id="sectorFlowLatest"></div>
          <div class="sector-preopen-review" id="sectorPreopenReview"></div>
        </div>"""

    def sector_html(rows, direction="inflow"):
        cards = []
        directional_rows = []
        for row in rows:
            flow = pick_num(row, "主力资金流向", "主力资金净流入")
            if flow is None:
                continue
            if direction == "inflow" and flow > 0:
                directional_rows.append(row)
            elif direction == "outflow" and flow < 0:
                directional_rows.append(row)
        ranked = sorted(
            directional_rows,
            key=lambda r: pick_num(r, "主力资金流向", "主力资金净流入") or 0,
            reverse=direction == "inflow",
        )[:8]
        flows = [abs(pick_num(r, "主力资金流向", "主力资金净流入") or 0) for r in ranked]
        max_flow = max(flows or [1])
        for idx, r in enumerate(ranked, 1):
            name = pick_value(r, "板块名称", "指数简称", "概念名称") or "-"
            board_type = pick_value(r, "板块类型") or "板块"
            leader = pick_value(r, "领涨股", "概念龙头") or ""
            theme = f"{board_type} · 领涨 {leader}" if leader else str(board_type)
            flow = pick_num(r, "主力资金流向", "主力资金净流入")
            chg = pick_num(r, "涨跌幅")
            rank = pick_value(r, "主力资金流向排名", "排名")
            width = abs(flow or 0) / max_flow * 100 if max_flow else 0
            cards.append(
                f"<article class='flow-row {direction}'><span class='flow-rank'>{idx:02d}</span><div class='flow-name'><b>{esc(name)}</b><small>{esc(short_text(theme, 34))}</small></div>"
                f"<div class='flow-chart'><i style='width:{width:.0f}%'></i></div>"
                f"<div class='flow-value'><strong>{esc(fmt_num(flow))}</strong><small class=\"{'pos' if (chg or 0) >= 0 else 'neg'}\">{esc(fmt_pct(chg))} · {esc(rank or '-')}</small></div></article>"
            )
        if not cards:
            label = "净流入" if direction == "inflow" else "净流出"
            return f"<div class='empty'>暂无板块主力{label}数据</div>"
        return "<div class='flow-grid'>" + "".join(cards) + "</div>"

    def sector_rotation_html(history):
        if len(history) < 2:
            return "<div class='empty'>板块快照不足，至少需要两个时间点。</div>"
        def board_map(item):
            result = {}
            for rank, row in enumerate(item.get("sectors") or [], 1):
                name = pick_value(row, "板块名称", "指数简称", "概念名称")
                if name and name not in result:
                    result[str(name)] = {
                        "rank": rank,
                        "flow": pick_num(row, "主力资金流向", "主力资金净流入"),
                        "chg": pick_num(row, "涨跌幅"),
                    }
            return result
        current = board_map(history[0])
        previous = board_map(history[1])
        rows = []
        for name, now_item in list(current.items())[:10]:
            old = previous.get(name)
            if old is None:
                movement, movement_cls = "新进", "new"
            else:
                delta = old["rank"] - now_item["rank"]
                movement = f"上升 {delta}" if delta > 0 else (f"下降 {abs(delta)}" if delta < 0 else "持续")
                movement_cls = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            rows.append(
                f"<div class='rotation-row'><span class='rotation-rank'>{now_item['rank']:02d}</span>"
                f"<div><b>{esc(name)}</b><small>{esc(fmt_pct(now_item['chg']))} · 净流入 {esc(fmt_num(now_item['flow']))}</small></div>"
                f"<strong class='{movement_cls}'>{esc(movement)}</strong></div>"
            )
        lost = [name for name in list(previous)[:10] if name not in current]
        lost_html = "".join(f"<span>{esc(name)} · 掉出TOP</span>" for name in lost[:5])
        times = f"{str(history[1].get('snapshot_at') or '')[11:16]} → {str(history[0].get('snapshot_at') or '')[11:16]}"
        return f"<div class='rotation-time'>{esc(times)} · 比较相邻资金快照</div>{''.join(rows)}<div class='rotation-lost'>{lost_html}</div>"

    def limit_html(rows):
        def seal_time(row, *needles):
            raw = str(pick_value(row, *needles) or "-")
            match = re.search(r"(\d{2}:\d{2})(?::\d{2})?$", raw)
            return match.group(1) if match else raw

        groups = {}
        for r in rows:
            boards = int(pick_num(r, "连续涨停天数") or 1)
            groups.setdefault(boards, []).append(r)
        if not groups:
            return "<div class='empty'>暂无涨停情绪数据</div>"
        levels = []
        max_boards = max(groups)
        span = max(1, max_boards - 1)
        for level_index, boards in enumerate(sorted(groups, reverse=True)):
            stocks = []
            ranked_group = sorted(
                groups[boards],
                key=lambda row: (
                    -(pick_num(row, "涨停封单额", "封单金额", "封板资金") or 0),
                    str(pick_value(row, "首次涨停时间", "首次封板时间") or "99:99:99"),
                ),
            )
            for r in ranked_group:
                name = pick_value(r, "股票简称") or "-"
                code = norm_code(pick_value(r, "股票代码"))
                seal = pick_num(r, "涨停封单额", "封单金额", "封板资金")
                breaks = pick_num(r, "涨停开板次数", "炸板次数", "开板次数")
                reason = short_text(pick_value(r, "涨停原因", "所属概念") or "", 180)
                first_time = seal_time(r, "首次涨停时间", "首次封板时间")
                href = detail_url(code) if code else "#limit-ladder"
                stocks.append(
                    f"<a class='pyramid-stock' href='{esc(href)}' title='查看 {esc(name)} 详情分析：{esc(reason)}'><div><b>{esc(name)}</b><strong>封 {esc(fmt_num(seal))}</strong></div>"
                    f"<small><time>{esc(first_time)} 首封</time><span>开板 {esc(int(breaks) if breaks is not None else '-')} · 详情 ↗</span></small></a>"
                )
            width = 44 + ((max_boards - boards) / span) * 52 if max_boards > 1 else 96
            columns = min(6, max(1, len(ranked_group)))
            intensity = (boards - 1) / span if max_boards > 1 else 0
            light_rgb = (255, 241, 238)
            deep_rgb = (153, 27, 39)
            layer_rgb = tuple(round(light + (deep - light) * intensity) for light, deep in zip(light_rgb, deep_rgb))
            layer_bg = f"rgb({layer_rgb[0]},{layer_rgb[1]},{layer_rgb[2]})"
            layer_ink = "#fffaf0" if intensity >= 0.52 else "#8f1f2d"
            layer_border = "#f2c14f" if boards == max_boards else f"rgb({max(120, layer_rgb[0] - 18)},{max(20, layer_rgb[1] - 14)},{max(28, layer_rgb[2] - 12)})"
            levels.append(
                f"<section class='pyramid-level' style='--level-width:{width:.0f}%;--level-cols:{columns};--level-order:{level_index};--layer-bg:{layer_bg};--layer-ink:{layer_ink};--layer-border:{layer_border}'>"
                f"<header><b>{boards}板</b><span>{len(ranked_group)}只 · 按封单与首封排序</span></header>"
                f"<div class='pyramid-stocks'>{''.join(stocks)}</div></section>"
            )
        ranked_rows = sorted(rows, key=lambda row: (
            -int(pick_num(row, "连续涨停天数") or 1),
            -(pick_num(row, "涨停封单额", "封单金额", "封板资金") or 0),
            seal_time(row, "首次涨停时间", "首次封板时间"),
        ))
        total = len(ranked_rows)
        first_count = sum(int(pick_num(row, "连续涨停天数") or 1) == 1 for row in ranked_rows)
        link_count = total - first_count
        broken_count = sum((pick_num(row, "涨停开板次数", "炸板次数", "开板次数") or 0) > 0 for row in ranked_rows)
        stable_rate = (total - broken_count) / total * 100 if total else 0
        avg_seal = sum(pick_num(row, "涨停封单额", "封单金额", "封板资金") or 0 for row in ranked_rows) / max(1, total)

        bucket_defs = [
            ("竞价", "00:00", "09:30"), ("早盘", "09:30", "10:00"),
            ("上午", "10:00", "11:31"), ("午后", "13:00", "14:00"),
            ("尾盘", "14:00", "15:01"),
        ]
        bucket_counts = []
        for label, start, end in bucket_defs:
            count = sum(start <= seal_time(row, "首次涨停时间", "首次封板时间") < end for row in ranked_rows)
            bucket_counts.append((label, count))
        max_bucket = max([count for _, count in bucket_counts] or [1])
        histogram = "".join(
            f"<div><b>{count}</b><i><em style='height:{(count / max_bucket * 100) if max_bucket else 0:.0f}%'></em></i><span>{esc(label)}</span></div>"
            for label, count in bucket_counts
        )

        table_rows = []
        for row in ranked_rows:
            code = norm_code(pick_value(row, "股票代码"))
            name = pick_value(row, "股票简称") or "-"
            boards = int(pick_num(row, "连续涨停天数") or 1)
            breaks = int(pick_num(row, "涨停开板次数", "炸板次数", "开板次数") or 0)
            first_time = seal_time(row, "首次涨停时间", "首次封板时间")
            final_time = seal_time(row, "最终涨停时间", "最终封板时间")
            reason = short_text(pick_value(row, "涨停原因", "所属概念") or "-", 180)
            industry = short_text(pick_value(row, "所属同花顺行业", "所属行业") or "-", 80)
            filter_tokens = " ".join(["first" if boards == 1 else "link", "broken" if breaks > 0 else "stable", "early" if first_time <= "10:00" else "late"])
            table_rows.append(
                f"<tr class='limit-stock-row' data-limit-tags='{filter_tokens}'><td><a href='{esc(detail_url(code))}'><b>{esc(name)}</b><small>{esc(code)}</small></a></td>"
                f"<td class='pos'>{esc(fmt_pct(pick_num(row, '最新涨跌幅', '涨跌幅')))}</td><td>{esc(fmt_num(pick_num(row, '总市值')))}</td>"
                f"<td class='pos'>{esc(fmt_num(pick_num(row, '涨停封单额', '封单金额', '封板资金')))}</td><td>{esc(first_time)}<small>末封 {esc(final_time)}</small></td>"
                f"<td class='{'neg' if breaks else 'pos'}'>{breaks}</td><td><b>{boards}板</b></td><td>{esc(short_text(industry, 12))}</td>"
                f"<td><a href='{esc(detail_url(code))}'>{esc(short_text(reason, 34))}</a></td></tr>"
            )
        kpis = (
            f"<div><span>涨停池</span><b>{total}只</b><small>首板 {first_count} / 连板 {link_count}</small></div>"
            f"<div><span>零开板占比</span><b>{stable_rate:.0f}%</b><small>开板过 {broken_count} 只</small></div>"
            f"<div><span>最高高度</span><b>{max_boards}板</b><small>连板情绪塔尖</small></div>"
            f"<div><span>平均封单</span><b>{esc(fmt_num(avg_seal))}</b><small>样本内实时均值</small></div>"
        )
        pool = (
            "<section class='limit-pool-analysis'><div class='limit-pool-head'><div class='limit-kpis'>" + kpis + "</div>"
            "<div class='limit-histogram'><header><b>首次封板分布</b><span>越早封板，通常承接越稳定</span></header><div class='limit-hist-bars'>" + histogram + "</div></div></div>"
            "<div class='limit-pool-toolbar' role='group' aria-label='涨停池筛选'><button class='active' data-limit-filter='all'>全部</button><button data-limit-filter='first'>首板</button><button data-limit-filter='link'>连板</button><button data-limit-filter='broken'>开板过</button><button data-limit-filter='early'>10点前封板</button><span id='limitPoolCount'>" + str(total) + " 只</span></div>"
            "<div class='limit-table-wrap'><table class='limit-pool-table'><thead><tr><th>股票</th><th>涨幅</th><th>市值</th><th>封单额</th><th>首封 / 末封</th><th>开板</th><th>高度</th><th>行业</th><th>涨停原因 / 详情</th></tr></thead><tbody>" + "".join(table_rows) + "</tbody></table></div></section>"
        )
        return "<div class='limit-pyramid'>" + "".join(levels) + "</div>" + pool

    def heat_html(rows):
        ranked = sorted(rows, key=lambda r: pick_num(r, "板块热度") or 0, reverse=True)[:6]
        max_heat = max([pick_num(r, "板块热度") or 0 for r in ranked] or [1])
        items = []
        for r in ranked:
            name = pick_value(r, "股票简称") or "-"
            name = pick_value(r, "指数简称", "板块名称", "名称") or name
            heat = pick_num(r, "板块热度") or 0
            chg = pick_num(r, "涨跌幅", "最新涨跌幅")
            items.append(
                f"<div class='heat-row'><span>{esc(name)}</span><i><b style='width:{heat / max_heat * 100:.0f}%'></b></i>"
                f"<strong class=\"{'pos' if (chg or 0) >= 0 else 'neg'}\">{esc(fmt_pct(chg))}</strong></div>"
            )
        return "".join(items) or "<div class='empty'>暂无行业热度数据</div>"

    def lhb_html(rows):
        if not rows:
            return "<div class='empty'>今日和近5日龙虎榜接口均暂未返回数据；盘后通常更完整。</div>"
        best = {}
        for r in rows:
            code = norm_code(pick_value(r, "股票代码"))
            date = pick_value(r, "上榜日期", "日期", "交易日期")
            key = (code, str(date))
            net = pick_num(r, "净买入额", "机构净买入", "机构净额") or 0
            if key not in best or abs(net) > abs(pick_num(best[key], "净买入额", "机构净买入", "机构净额") or 0):
                best[key] = r
        compact = sorted(best.values(), key=lambda r: abs(pick_num(r, "净买入额", "机构净买入", "机构净额") or 0), reverse=True)[:8]
        body = []
        for r in compact:
            name = pick_value(r, "股票简称", "股票名称", "营业部") or "-"
            code = norm_code(pick_value(r, "股票代码"))
            net = pick_num(r, "净买入额", "机构净买入", "机构净额")
            buy = pick_num(r, "买入额", "买入金额")
            sell = pick_num(r, "卖出额", "卖出金额")
            date = pick_value(r, "上榜日期", "日期", "交易日期")
            reason = r.get("上榜原因") or r.get("榜单类型") or "-"
            seat = pick_value(r, "营业部名称", "知名游资营业部", "营业部") or "-"
            seat_type = pick_value(r, "营业部类型") or ""
            if isinstance(seat_type, (list, tuple)):
                seat_type = "/".join(str(x) for x in seat_type)
            body.append(
                f"<tr><td><b>{esc(name)}</b><small>{esc(code)}</small></td>"
                f"<td><span class='lhb-seat'>{esc(short_text(seat_type, 12) or '席位')}</span>{esc(short_text(seat, 22))}</td>"
                f"<td><b class=\"{'pos' if (net or 0) >= 0 else 'neg'}\">{esc(fmt_num(net))}</b><small>买 {esc(fmt_num(buy))} / 卖 {esc(fmt_num(sell))}</small></td>"
                f"<td><span>{esc(str(date)[4:6] + '-' + str(date)[6:8] if date and len(str(date)) >= 8 else date or '-')}</span><small>{esc(short_text(reason, 24))}</small></td></tr>"
            )
        return "<div class='lhb-table-wrap'><table class='lhb-table'><thead><tr><th>标的</th><th>核心席位</th><th>净买入</th><th>日期 / 原因</th></tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"

    def emotion_detail_html():
        items = [
            ("赚钱效应", f"{breadth:.0f}%" if breadth is not None else "-", "上涨家数 / 全市场", breadth or 0, "good" if (breadth or 0) >= 50 else "bad"),
            ("涨跌停强弱", f"{limit_ratio:.0f}%" if limit_ratio is not None else "-", "涨停占涨跌停比例", limit_ratio or 0, "good" if (limit_ratio or 0) >= 60 else "bad"),
            ("炸板压力", f"{break_rate:.0f}%" if break_rate is not None else "-", "样本内开板股票占比", break_rate or 0, "bad" if (break_rate or 0) >= 25 else "good"),
            ("高标高度", f"{int(high_board)}板", "市场最高连板", min(100, high_board * 20), "good" if high_board >= 3 else "bad"),
            ("平均封单", fmt_num(avg_seal), "涨停样本封单均值", min(100, (avg_seal or 0) / 5e8 * 100), "good" if (avg_seal or 0) >= 2e8 else "bad"),
            ("情绪结论", emotion_label, f"综合分 {emotion_score}/100", emotion_score, "good" if emotion_score >= 50 else "bad"),
        ]
        return "".join(
            f"<div class='emotion-card {cls}'><span>{esc(title)}</span><b>{esc(value)}</b><small>{esc(note)}</small><em><i style='width:{max(0,min(100,float(width))):.0f}%'></i></em></div>"
            for title, value, note, width, cls in items
        )

    return f"""<div class="panel market-panel" id="market-fund">
    <div class="title market-title"><span>大盘资金雷达 <em>MARKET INTELLIGENCE</em></span><span class="market-live"><i></i>{esc(freshness)} 快照</span></div>
    <div class="market-shell">
      {command_html}
      <section class="market-dashboard">
        <div class="market-section flow-section"><div class="box-title"><div><b>大板块主力资金方向</b><small>概念 + 行业双向排名 · 个股仅作为板块证据</small></div><span>SECTOR FLOW</span></div>
          <div class="sector-direction-grid">
            <section class="sector-direction-column inflow"><header><div><b>大板块主力净流入 TOP</b><small>增量资金正在进入</small></div><span>INFLOW</span></header>{sector_html(snapshot.get('sectors') or [], 'inflow')}</section>
            <section class="sector-direction-column outflow"><header><div><b>大板块主力净流出 TOP</b><small>存量资金正在撤离</small></div><span>OUTFLOW</span></header>{sector_html(snapshot.get('sectors') or [], 'outflow')}</section>
          </div>
        </div>
        {flow_motion_html}
        <div class="market-section rotation-section"><div class="box-title"><div><b>大板块排名变化</b><small>新进 / 上升 / 持续 / 掉出榜单</small></div><span>SECTOR ROTATION</span></div>{sector_rotation_html(snapshot.get('sector_history') or [])}</div>
        <div class="market-section profile-section"><div class="box-title"><div><b>市场情绪剖面</b><small>宽度、涨跌停与高标强度</small></div><span>RISK MAP</span></div>
          <div class="emotion-grid">{emotion_detail_html()}</div>
          <div class="market-statline"><span>涨停 <b class="pos">{esc(int(limit_up) if limit_up is not None else '-')}</b></span><span>跌停 <b class="neg">{esc(int(limit_down) if limit_down is not None else '-')}</b></span><span>最高 <b>{esc(int(high_board))}板</b></span><span>指数均值 <b class="{'pos' if (avg_index or 0) >= 0 else 'neg'}">{esc(fmt_pct(avg_index))}</b></span></div>
          <div class="heat-title"><b>行业热度</b><span>热度与涨跌方向交叉看</span></div>{heat_html(snapshot.get('sentiment') or [])}
        </div>
        <div class="market-section ladder-section" id="limit-ladder"><div class="box-title"><div><b>连板梯队</b><small>高度、封单与开板质量 · 每个梯队完整展示</small></div><span>LIMIT-UP LADDER</span></div><div class="limit-ladder">{limit_html(snapshot.get('limit') or [])}</div></div>
        <div class="market-section lhb-section"><div class="box-title"><div><b>龙虎榜资金</b><small>按单股单日去重，净额绝对值排序</small></div><span>{esc('今日榜单' if snapshot.get('lhb') else '近5日回退')}</span></div>{lhb_html((snapshot.get('lhb') or snapshot.get('lhb_recent') or []))}</div>
      </section>
    </div></div>"""


def ticker_tape_html(latest_picks, market_fund):
    items = []
    snapshot_at = market_fund.get("snapshot_at") if market_fund else None
    if snapshot_at:
        items.append(("info", "资金快照", str(snapshot_at)[:16]))

    market_rows = (market_fund or {}).get("market") or []
    for label, needle in [("上证", "上证"), ("创业板", "创业板"), ("科创50", "科创50"), ("全A", "全A")]:
        row = next((r for r in market_rows if needle in str(pick_value(r, "指数简称", "名称") or r)), None)
        if row:
            chg = pick_num(row, "最新涨跌幅", "涨跌幅")
            items.append(("up" if (chg or 0) >= 0 else "down", label, fmt_pct(chg)))

    sectors = (market_fund or {}).get("sectors") or []
    sector_rows = sorted(
        sectors[:12],
        key=lambda r: abs(pick_num(r, "主力资金流向", "主力资金净流入") or 0),
        reverse=True,
    )[:3]
    for row in sector_rows:
        name = pick_value(row, "股票简称", "概念名称", "板块名称") or "板块"
        flow = pick_num(row, "主力资金流向", "主力资金净流入")
        items.append(("up" if (flow or 0) >= 0 else "down", f"主力 {name}", fmt_num(flow)))

    for pick in (latest_picks or [])[:6]:
        name = pick.get("name") or "-"
        code = norm_code(pick.get("code"))
        chg = pick.get("chg_pct")
        score = pick.get("score")
        action = "买点" if pick.get("buy_point") else ("竞价" if "涨停" in str(pick.get("theme") or pick.get("reason") or "") else "观察")
        text = f"{code} {fmt_pct(chg)} · {action} · {fmt_num(score)}分"
        try:
            chg_num = float(chg or 0)
        except Exception:
            chg_num = 0
        items.append(("up" if chg_num >= 0 else "down", name, text))

    discipline = [
        ("quote", "交易纪律", "没有买点，只进观察池"),
        ("quote", "风险优先", "先看回撤，再看胜率"),
        ("quote", "大师原则", "不要因为熟悉就重复买"),
        ("quote", "仓位控制", "单票试仓优先，错了要快"),
    ]
    items.extend(discipline)
    if not items:
        return ""

    chunk = "".join(
        f"<span class=\"tape-item {esc(kind)}\"><b>{esc(label)}</b><em>{esc(value)}</em></span>"
        for kind, label, value in items[:24]
    )
    return f"""<div class="market-tape" aria-label="市场信息滚动栏"><div class="tape-track">{chunk}{chunk}</div></div>"""


def fund_fallback_row(pick):
    row = {
        "资金档位": (pick.get("fund_grade") or "-") + (f" / {pick.get('fund_score')}分" if pick.get("fund_score") is not None else ""),
        "主力资金净流入": fmt_num(pick.get("main_net") or pick.get("main")),
        "超大单净流入": fmt_num(pick.get("super_net")),
        "大单净流入": fmt_num(pick.get("big_net")),
        "龙虎榜机构净额": fmt_num(pick.get("lobby_net")),
        "封单金额": fmt_num(pick.get("seal_amount")),
        "炸板次数": pick.get("break_count"),
        "连板数": pick.get("board_count"),
        "换手率": pick.get("turnover"),
        "资金标签": pick.get("fund_tags"),
    }
    return {k: v for k, v in row.items() if v not in (None, "", "-")}


def analysis_notes(pick):
    reason = pick.get("reason") or ""
    notes = []
    if "涨停短线" in reason:
        notes.append("短线属性强，只适合小仓试错；次日竞价必须强转强，否则不追。")
        notes.append("执行优先级：封单/竞价强度 > 题材逻辑 > 分时冲高。跌破短线止损直接退出。")
    else:
        notes.append("趋势事件票，重点验证事件催化是否继续发酵，以及资金是否持续流入。")
        notes.append("适合按计划试仓，不适合一次打满；涨 10%/20% 按纪律分批兑现。")
    chg = pick.get("chg_pct")
    if chg is not None:
        try:
            chg_v = float(chg)
            if chg_v >= 7:
                notes.append("当日涨幅偏高，买点应等回踩或次日确认，避免情绪高点追入。")
            elif chg_v < 0:
                notes.append("当日回落，需观察是否止跌转强，不能只因低吸逻辑买入。")
        except Exception:
            pass
    if "资金流出" in reason:
        notes.append("评分虽高但资金项偏弱，若后续继续净流出，应降低仓位或仅观察。")
    return notes


def load_signal_history(conn, code, limit=20):
    if not conn or "stock_picks" not in table_names(conn):
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_picks)")}
        picked_time_expr = "COALESCE(picked_time, substr(picked_at,12,5)) AS picked_time" if "picked_time" in cols else "substr(picked_at,12,5) AS picked_time"
        return [
            dict(r)
            for r in conn.execute(
                f"""SELECT id, picked_at, picked_date, {picked_time_expr},
                          run_id, rank, code, name, theme, score, chg_pct, buy_point, stop_loss,
                          target, reason, eval_status, eval_return_pct
                   FROM stock_picks
                   WHERE code=?
                   ORDER BY picked_at DESC, id DESC
                   LIMIT ?""",
                (code, limit),
            )
        ]
    except Exception:
        return []


def load_ai_report(conn, code):
    if not conn or "ai_reports" not in table_names(conn):
        return None
    try:
        row = conn.execute(
            """SELECT id, code, name, title, report_html, report_text, source, model, job_id, created_at
               FROM ai_reports
               WHERE code=?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (code,),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def load_all_ai_reports(conn, limit=100):
    if not conn or "ai_reports" not in table_names(conn):
        return []
    try:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT id, code, name, title, report_html, report_text, source, model, job_id, created_at
                   FROM ai_reports
                   ORDER BY created_at DESC, id DESC
                   LIMIT ?""",
                (limit,),
            )
        ]
    except Exception:
        return []


def ai_report_url(code):
    return f"stock-lab/ai/{norm_code(code)}.html"


def ai_report_block(report, code, name):
    if not report:
        cmd = f"python3 ai_research.py analyze --code {norm_code(code)} --name {name or norm_code(code)}"
        return (
            '<div class="empty">暂未生成 AI 深度分析。服务器执行后会自动展示：</div>'
            f'<pre class="cmd">{esc(cmd)}</pre>'
        )
    return (
        f'<div class="dim">生成时间 {esc(report.get("created_at"))} · 模型 {esc(report.get("model") or report.get("source") or "AI")}'
        f' · <a href="/invest/{esc(ai_report_url(report.get("code")))}">独立报告页</a></div>'
        f'<div class="ai-body">{report.get("report_html") or ""}</div>'
    )


def render_ai_report_page(report, now):
    code = norm_code(report.get("code"))
    title = report.get("title") or f"{report.get('name') or code}({code}) AI深度分析"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="{esc(report.get('name') or code)}({esc(code)}) 的选股雷达 AI 深度分析，覆盖公告、板块、新闻、风险和执行点位。">
<meta name="theme-color" content="#f3f6f4">
<link rel="canonical" href="https://mazhi.icu/invest/stock-lab/ai/{esc(code)}.html">
<link rel="manifest" href="/site.webmanifest">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>{esc(title)} | 选股雷达实验室</title>
<style>
:root{{--bg:#0a0e17;--panel:#131826;--line:#25304a;--text:#e7ecf5;--muted:#8390ad;--green:#00d68f;--yellow:#ffab40;}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.75 -apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;}}
a{{color:inherit}} .top{{position:sticky;top:0;background:rgba(10,14,23,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);z-index:10}}
.top-inner,.wrap{{max-width:980px;margin:0 auto;padding:16px 22px}} .top-inner{{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}}
.brand{{font-size:20px;font-weight:900}} .brand span{{color:var(--green)}} .nav a{{text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:6px 10px;color:var(--muted);margin-left:8px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:22px 0}} .title{{font-weight:900;font-size:17px;padding:14px 18px;border-bottom:1px solid var(--line)}} .pad{{padding:18px}}
.dim{{color:var(--muted);font-size:12px}} .ai-body h3,.ai-body h4,.ai-body h5{{margin:20px 0 8px;color:#fff}} .ai-body p{{margin:8px 0}} .ai-body ul{{margin:8px 0 12px;padding-left:20px}} .ai-body li{{margin:6px 0}}
.notice{{border-left:3px solid var(--yellow);padding-left:12px;color:var(--muted);font-size:13px}}
@media(max-width:800px){{.wrap,.top-inner{{padding:14px}}}}
</style>
<link rel="stylesheet" href="/assets/invest-terminal.css?v=202607141525">
</head><body>
<header class="top"><div class="top-inner"><div class="brand">{esc(report.get('name') or code)} <span>{esc(code)}</span></div><nav class="nav" aria-label="AI 报告导航"><a href="/invest/stock-lab.html">返回实验室</a><a href="{esc(quote_url(code))}" target="_blank" rel="noopener">行情页</a></nav></div></header>
<main class="wrap">
  <div class="panel"><div class="title">{esc(title)}</div><div class="pad">
    <div class="dim">报告生成 {esc(report.get('created_at'))} · 页面生成 {esc(now)} · 模型 {esc(report.get('model') or report.get('source') or 'AI')}</div>
    <p class="notice">仅用于策略研究与复盘，不构成投资建议；公告、行情和新闻必须以交易所及实时行情源复核。</p>
    <div class="ai-body">{report.get('report_html') or ''}</div>
  </div></div>
</main></body></html>"""


def generate_ai_report_pages(base_dir, reports, now):
    out_dir = Path(base_dir) / "stock-lab" / "ai"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    for report in reports:
        code = norm_code(report.get("code"))
        if not code or code in written:
            continue
        written.add(code)
        (out_dir / f"{code}.html").write_text(render_ai_report_page(report, now), encoding="utf-8")
    index_cards = []
    for report in reports:
        code = norm_code(report.get("code"))
        if not code:
            continue
        text = report.get("report_text") or ""
        summary = text.replace("#", "").replace("\n", " ")
        if len(summary) > 130:
            summary = summary[:127] + "..."
        index_cards.append(
            f'<a class="card" href="{esc(code)}.html"><b>{esc(report.get("name") or code)} <span>{esc(code)}</span></b>'
            f'<small>{esc(report.get("created_at"))} · {esc(report.get("model") or report.get("source") or "AI")}</small>'
            f'<p>{esc(summary)}</p></a>'
        )
    unique_codes = sorted({norm_code(r.get("code")) for r in reports if norm_code(r.get("code"))})
    latest = reports[0] if reports else {}
    model_count = len({(r.get("model") or r.get("source") or "AI") for r in reports})
    summary_cards = [
        ("报告总数", len(reports), "已生成的单股深度报告"),
        ("覆盖标的", len(unique_codes), "去重后的股票数量"),
        ("模型来源", model_count, "参与生成的模型/来源"),
        ("最新报告", latest.get("created_at") or "-", f"{latest.get('name') or latest.get('code') or '暂无'}"),
    ]
    summary_html = "".join(
        f'<div class="summary-card"><span>{esc(label)}</span><b>{esc(value)}</b><small>{esc(desc)}</small></div>'
        for label, value, desc in summary_cards
    )
    index_html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="选股雷达 AI 深度分析报告库，集中查看单股公告、板块、新闻、风险和执行点位研究档案。">
<meta name="theme-color" content="#f3f6f4">
<link rel="canonical" href="https://mazhi.icu/invest/stock-lab/ai/">
<link rel="manifest" href="/site.webmanifest">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>AI 深度分析报告 | 选股雷达实验室</title>
<link rel="stylesheet" href="/assets/invest-terminal.css?v=202607141705">
<style>
:root{{--bg:#0a0e17;--panel:#131826;--line:#25304a;--text:#e7ecf5;--muted:#8390ad;--green:#00d68f;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.7 -apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;}}
.wrap{{max-width:1120px;margin:0 auto;padding:24px 22px}}
.top{{border-bottom:1px solid var(--line);background:rgba(10,14,23,.92);backdrop-filter:blur(14px)}}
.top .wrap{{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;padding-top:16px;padding-bottom:16px}}
h1{{font-size:clamp(24px,3vw,34px);margin:0;letter-spacing:0}} h1 span{{color:var(--green)}} a{{color:inherit}}
.nav a{{text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:7px 11px;color:var(--muted);background:rgba(255,255,255,.02)}}
.hero{{padding:28px 22px 8px}}
.hero h2{{font-size:clamp(24px,4vw,42px);line-height:1.08;margin:0 0 12px}}
.hero p{{max-width:760px;color:var(--muted);margin:0}}
.summary-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0 22px}}
.summary-card{{background:linear-gradient(180deg,rgba(255,255,255,.035),transparent 44%),var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}}
.summary-card span,.summary-card small{{display:block;color:var(--muted)}} .summary-card b{{display:block;font-size:22px;margin:2px 0;color:var(--text)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
.card{{display:block;min-height:158px;text-decoration:none;background:linear-gradient(135deg,rgba(0,214,143,.06),transparent 44%),var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;transition:border-color .18s,transform .18s}}
.card:hover{{border-color:var(--green);transform:translateY(-2px)}} .card b{{display:block;font-size:16px}} .card b span{{color:var(--green)}} .card small{{display:block;color:var(--muted);font-size:12px;margin:8px 0}} .card p{{color:var(--muted);margin:0;line-height:1.7}}
.empty{{color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:18px}}
@media(max-width:760px){{.wrap,.hero{{padding:16px}}.summary-grid{{grid-template-columns:1fr}}.grid{{grid-template-columns:1fr}}}}
</style>
</head><body>
<header class="top"><div class="wrap"><h1>AI 深度分析 <span>报告库</span></h1><nav class="nav" aria-label="AI 报告库导航"><a href="/invest/stock-lab.html">返回实验室</a></nav></div></header>
<section class="hero wrap"><h2>公告、板块、新闻和执行点位的单股研究档案</h2><p>报告库只收录已生成的深度分析，用于复盘和二次验证；缺失报告的股票会在单股详情页提示生成命令。</p></section>
<main class="wrap">
  <section class="summary-grid">{summary_html}</section>
  <section class="grid">{''.join(index_cards) if index_cards else '<div class="empty">暂无 AI 报告</div>'}</section>
</main>
</body></html>"""
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")


def fetch_analysis(pick, conn=None):
    code = norm_code(pick.get("code"))
    name = pick.get("name") or code
    theme = pick.get("concepts") or pick.get("industry") or pick.get("theme") or ""
    return {
        "market": iw(f"{code} {name} 最新价 涨跌幅 总市值 市盈率 市净率 换手率 成交额 量比 主力资金近5日净流入 所属同花顺行业", "hithink-market-query", "5"),
        "fund_flow": iw(f"{code} {name} 今日主力资金净流入 超大单净流入 大单净流入 主力净占比 换手率 量比 龙虎榜 机构净买入额 买入营业部 卖出营业部 封单金额 炸板次数 连续涨停天数", "hithink-astock-selector", "6"),
        "technical": iw(f"{code} {name} KDJ MACD RSI BOLL 均线 MA5 MA10 MA20 近5日涨跌幅 近20日涨跌幅 成交量 换手率", "hithink-market-query", "5"),
        "fundamental": iw(f"{code} {name} 总市值 市盈率 市净率 营收同比 净利润同比 毛利率 ROE 资产负债率 机构评级", "hithink-astock-selector", "5"),
        "announcements": iw(f"{code} {name} 最新公告 业绩预告 限售解禁 解禁数量 解禁市值 股东减持 股权质押 监管函 问询函 立案", "hithink-event-query", "8"),
        "sector": iw(f"{theme} 概念板块 涨跌幅 主力资金净流入 龙头股 产业链", "hithink-sector-selector", "5") if theme else [],
        "news": iw(f"{name} 最新新闻 利好 利空 合作 订单 产业链", "news-search", "5"),
        "history": load_signal_history(conn, code),
    }


def history_block(rows):
    if not rows:
        return '<div class="empty">暂无历史同股信号</div>'
    out = []
    for r in rows[:12]:
        label, cls = signal_action(r)
        ret = r.get("eval_return_pct")
        ret_text = "-" if ret is None else f"{float(ret):+.2f}%"
        out.append(
            f"<tr><td>{esc(signal_time(r))}</td><td>{esc(r.get('run_id'))}</td>"
            f"<td>{esc(r.get('rank'))}</td><td>{esc(r.get('score'))}</td>"
            f"<td><span class='pill {cls}'>{esc(label)}</span></td>"
            f"<td class=\"{'pos' if ret is not None and float(ret) >= 0 else 'neg'}\">{esc(ret_text)}</td>"
            f"<td class='dim'>{esc(r.get('reason') or '')}</td></tr>"
        )
    return "<div class='scroll'><table><thead><tr><th>扫描生成时间</th><th>批次</th><th>排名</th><th>评分</th><th>动作</th><th>回测</th><th>原因</th></tr></thead><tbody>" + "".join(out) + "</tbody></table></div>"


def tag_block(tags):
    if not tags:
        return '<div class="empty">暂无信号标签</div>'
    return "<div class='tags'>" + "".join(f"<span>{esc(t)}</span>" for t in tags) + "</div>"


def agent_council_block(pick):
    try:
        review = json.loads(pick.get("agent_reviews_json") or "{}")
    except (TypeError, ValueError):
        review = {}
    opinions = review.get("opinions") if isinstance(review, dict) else []
    if not isinstance(opinions, list) or not opinions:
        return '<div class="empty">该历史信号尚未经过 Agent 议会评审。</div>'
    signal_text = {"buy": "看多", "hold": "中性", "sell": "看空"}
    signal_cls = {"buy": "pos", "hold": "yellow", "sell": "neg"}
    cards = []
    for item in opinions:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or "hold")
        evidence = item.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = []
        cards.append(
            f"<article class='agent-detail {esc(signal)}'><div><b>{esc(item.get('label') or item.get('agent') or 'Agent')}</b>"
            f"<span class='{signal_cls.get(signal, 'yellow')}'>{esc(signal_text.get(signal, signal))} · {float(item.get('confidence') or 0):.0%}</span></div>"
            f"<p>{esc('；'.join(str(v) for v in evidence) or '证据不足')}</p></article>"
        )
    consensus = str(review.get("consensus") or "hold")
    veto = bool(review.get("risk_veto"))
    header = (
        f"<div class='agent-verdict'><div><span>最终共识</span><b class='{signal_cls.get(consensus, 'yellow')}'>{esc(signal_text.get(consensus, consensus))}</b></div>"
        f"<div><span>置信度</span><b>{float(review.get('confidence') or 0):.0%}</b></div>"
        f"<div><span>分歧度</span><b>{float(review.get('disagreement') or 0):.0f}</b></div>"
        f"<div><span>数据完整度</span><b class='{'pos' if review.get('data_quality') == 'complete' else 'yellow'}'>{'完整' if review.get('data_quality') == 'complete' else '部分缺失'}</b></div>"
        f"<div><span>风险闸门</span><b class='{'neg' if veto else 'pos'}'>{'已否决买入' if veto else '通过'}</b></div></div>"
    )
    strategy_items = review.get("strategy_assessments") or []
    strategy_text = {"pass": "命中", "watch": "观察", "block": "阻断", "inactive": "未激活"}
    strategy_html = "<div class='strategy-review'>" + "".join(
        f"<span class='{esc(item.get('status') or 'watch')}' title='{esc(item.get('evidence') or '')}'><b>{esc(item.get('label') or '-')}</b><em>{esc(strategy_text.get(item.get('status'), item.get('status') or '-'))}</em></span>"
        for item in strategy_items if isinstance(item, dict)
    ) + "</div>"
    return header + strategy_html + "<div class='agent-detail-grid'>" + "".join(cards) + "</div>"


def generate_detail_page(base_dir, pick, now, conn=None, analysis_cache=None, rich=True, write_code_page=True):
    code = norm_code(pick.get("code"))
    if not code:
        return
    analysis_cache = analysis_cache if analysis_cache is not None else {}
    if rich:
        if code not in analysis_cache:
            analysis_cache[code] = fetch_analysis(pick, conn)
        analysis = analysis_cache[code]
    else:
        analysis = {"market": [], "fund_flow": [], "technical": [], "fundamental": [], "announcements": [], "sector": [], "news": [], "history": load_signal_history(conn, code)}
    if not analysis.get("fund_flow"):
        fallback = fund_fallback_row(pick)
        analysis["fund_flow"] = [fallback] if fallback else []
    notes = analysis_notes(pick)
    out_dir = Path(base_dir) / "stock-lab"
    out_dir.mkdir(exist_ok=True)
    signal_dir = out_dir / "signals"
    signal_dir.mkdir(exist_ok=True)
    chg = pick.get("chg_pct")
    chg_text = "-" if chg is None else f"{float(chg):+.1f}%"
    action_label, action_cls = signal_action(pick)
    decision_points = key_decision_points(pick)
    tags = reason_tags(pick)
    signal_at = signal_time(pick)
    first_limit = str(pick.get("first_limit_at") or "")
    final_limit = str(pick.get("final_limit_at") or "")
    limit_timing_html = ""
    if first_limit or final_limit:
        first_text = first_limit[11:19] if len(first_limit) >= 16 else "-"
        final_text = final_limit[11:19] if len(final_limit) >= 16 else "-"
        limit_timing_html = (
            f'<div class="limit-timing"><b>当日涨停记录</b><span>首次封板 {esc(first_text)} · '
            f'最终封板 {esc(final_text)} · 15:00 收盘后不可交易</span></div>'
        )
    signal_id = int(pick.get("id") or 0)
    canonical_path = f"signals/{signal_id}.html" if signal_id else f"{code}.html"
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="{esc(pick.get('name'))}({esc(code)}) 选股雷达详情页，包含扫描生成时间、行情时间、技术指标、基本面、公告风险、板块新闻和执行点位。">
<meta name="theme-color" content="#0a0e17">
<link rel="canonical" href="https://mazhi.icu/invest/stock-lab/{esc(canonical_path)}">
<link rel="manifest" href="/site.webmanifest">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>{esc(pick.get('name'))}({esc(code)}) 分析 | 选股雷达实验室</title>
<style>
	:root{{--bg:#0a0e17;--panel:#131826;--panel2:#0f1421;--line:#25304a;--text:#e7ecf5;--muted:#8390ad;--green:#00d68f;--red:#ff5252;--yellow:#ffab40;--blue:#448aff;}}
	*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.7 -apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;}}
a{{color:inherit}} .top{{position:sticky;top:0;background:rgba(10,14,23,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);z-index:10}}
.top-inner,.wrap{{max-width:1120px;margin:0 auto;padding:16px 22px}} .top-inner{{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}}
.brand{{font-size:20px;font-weight:900}} .brand span{{color:var(--green)}} .nav a{{text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:6px 10px;color:var(--muted)}}
	.hero{{display:grid;grid-template-columns:1fr 360px;gap:16px;margin:20px 0}} .panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-bottom:16px}}
	.title{{font-weight:900;font-size:16px;padding:14px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px}} .pad{{padding:16px}}
	.dim{{color:var(--muted);font-size:12px}} .badge,.pill{{font-size:11px;font-weight:900;padding:2px 8px;border-radius:4px;background:rgba(0,214,143,.12);color:var(--green)}}
	.pill.short{{color:var(--blue);background:rgba(68,138,255,.12)}} .pill.watch{{color:var(--yellow);background:rgba(255,171,64,.12)}} .pill.buy{{color:var(--green);background:rgba(0,214,143,.12)}}
	.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}} .box{{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:10px}} .box span{{display:block;color:var(--muted);font-size:11px}}
	.decision{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}} .decision div{{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:10px}}
	.limit-timing{{display:flex;gap:10px;align-items:center;margin:8px 0 0;padding:8px 10px;border-left:3px solid var(--red);background:rgba(255,82,82,.08);font-size:12px}} .limit-timing span{{color:var(--muted)}}
		.tags{{display:flex;flex-wrap:wrap;gap:8px}} .tags span{{border:1px solid var(--line);background:var(--panel2);border-radius:5px;padding:4px 8px;font-size:12px;color:var(--muted)}}
		.agent-verdict{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}} .agent-verdict>div{{padding:10px;border:1px solid var(--line);background:var(--panel2)}} .agent-verdict span{{display:block;color:var(--muted);font-size:11px}} .agent-detail-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}} .agent-detail{{padding:11px;border:1px solid var(--line);border-left:3px solid var(--yellow);background:var(--panel2)}} .agent-detail.buy{{border-left-color:var(--green)}} .agent-detail.sell{{border-left-color:var(--red)}} .agent-detail>div{{display:flex;justify-content:space-between;gap:8px}} .agent-detail p{{margin:6px 0 0;color:var(--muted);font-size:12px}}
		.strategy-review{{display:grid;grid-template-columns:repeat(7,1fr);gap:7px;margin:0 0 12px}} .strategy-review span{{display:flex;justify-content:space-between;gap:5px;padding:8px;border:1px solid var(--line);background:var(--panel2)}} .strategy-review em{{font-style:normal;color:var(--muted)}} .strategy-review .pass{{border-color:rgba(0,214,143,.55)}} .strategy-review .pass em{{color:var(--green)}} .strategy-review .block{{border-color:rgba(255,82,82,.55)}} .strategy-review .block em{{color:var(--red)}}
	.agent-verdict{{grid-template-columns:repeat(5,1fr)}} .pos{{color:var(--green)}} .neg{{color:var(--red)}} .yellow{{color:var(--yellow)}} ul{{margin:0;padding-left:18px}} li{{margin:7px 0}} .empty{{color:var(--muted);font-size:13px}}
	.cmd{{white-space:pre-wrap;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:10px;color:var(--yellow);overflow:auto}} .ai-body{{margin-top:12px}} .ai-body h3,.ai-body h4,.ai-body h5{{margin:18px 0 8px;color:#fff}} .ai-body p{{margin:8px 0}} .ai-body ul{{margin:8px 0 12px;padding-left:20px}}
	table{{width:100%;border-collapse:collapse;min-width:760px}} th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{color:var(--muted);font-size:12px}} .scroll{{overflow-x:auto}}
	@media(max-width:800px){{.hero{{grid-template-columns:1fr}}.grid{{grid-template-columns:1fr}}.wrap,.top-inner{{padding:14px}}}}
</style>
<link rel="stylesheet" href="/assets/invest-terminal.css?v=202607141525">
</head><body>
	<header class="top"><div class="top-inner"><div class="brand">{esc(pick.get('name'))} <span>{esc(code)}</span></div><nav class="nav" aria-label="单股分析导航"><a href="/invest/stock-lab.html">返回实验室</a><a href="#fund-flow">资金面</a><a href="#ai-report">AI分析</a><a href="{esc(quote_url(code))}" target="_blank" rel="noopener">行情页</a></nav></div></header>
<main class="wrap">
	  <div class="hero">
	    <div class="panel"><div class="title">选股结论 <span class="badge">{esc(pick.get('theme'))}</span></div><div class="pad">
	      <div class="dim">扫描生成 {esc(signal_at)} · 行情截至 {esc(pick.get('quote_as_of') or signal_at)} · 批次 {esc(pick.get('run_id'))} · 页面生成 {esc(now)}</div>
	      {limit_timing_html}
	      <h2 style="margin:8px 0 10px">评分 {esc(pick.get('score'))} · <span class="pill {esc(action_cls)}">{esc(action_label)}</span> · 当日涨跌 <span class="{'pos' if str(chg_text).startswith('+') else 'neg'}">{esc(chg_text)}</span></h2>
	      <div class="decision">{''.join(f'<div>{esc(p)}</div>' for p in decision_points)}</div>
	    </div></div>
	    <div class="panel"><div class="title">执行点位</div><div class="pad grid">
	      <div class="box"><span>买点</span><b>{esc(pick.get('buy_point') or '不买')}</b></div>
	      <div class="box"><span>止损</span><b class="neg">{esc(pick.get('stop_loss') or '-')}</b></div>
	      <div class="box"><span>目标</span><b class="pos">{esc(pick.get('target') or '-')}</b></div>
	      <div class="box"><span>市值</span><b>{esc(fmt_num(pick.get('cap')))}</b></div>
	      <div class="box"><span>PE</span><b>{esc(pick.get('pe') or '-')}</b></div>
	      <div class="box"><span>主力资金</span><b>{esc(fmt_num(pick.get('main_net') or pick.get('main')))}</b></div>
	      <div class="box"><span>资金档位</span><b>{esc((pick.get('fund_grade') or '-') + ((' / ' + str(pick.get('fund_score')) + '分') if pick.get('fund_score') is not None else ''))}</b></div>
	      <div class="box"><span>龙虎榜机构</span><b>{esc(fmt_num(pick.get('lobby_net')))}</b></div>
	      <div class="box"><span>超大单</span><b>{esc(fmt_num(pick.get('super_net')))}</b></div>
	    </div></div>
	  </div>
		  <div class="panel"><div class="title">信号原因拆解</div><div class="pad">{tag_block(tags)}</div></div>
		  <div class="panel"><div class="title">Agent 决策链 <span class="dim">独立评审 → 分歧计算 → 风险否决 → 最终共识</span></div><div class="pad">{agent_council_block(pick)}</div></div>
	  <div class="panel"><div class="title">A股板块归属</div><div class="pad"><div class="grid"><div class="box"><span>行业板块</span><b>{esc(pick.get('industry') or '-')}</b></div><div class="box"><span>概念板块</span><b>{esc(pick.get('concepts') or pick.get('theme') or '-')}</b></div><div class="box"><span>板块当日涨跌</span><b class="{'pos' if (pick.get('sector_chg') or 0) >= 0 else 'neg'}">{esc(fmt_pct(pick.get('sector_chg')))}</b></div></div></div></div>
	  <div class="panel"><div class="title">执行解读</div><div class="pad"><ul>{''.join(f'<li>{esc(n)}</li>' for n in notes)}</ul></div></div>
	  <div class="panel" id="ai-report"><div class="title">AI 深度分析 <span class="dim">公告 / 板块 / 新闻 / 执行计划</span></div><div class="pad">{ai_report_block(load_ai_report(conn, code), code, pick.get('name'))}</div></div>
	  <div class="panel"><div class="title">股市行情分析</div><div class="pad">{list_block(analysis['market'])}</div></div>
	  <div class="panel" id="fund-flow"><div class="title">资金 / 游资痕迹分析 <span class="dim">主力、超大单、龙虎榜、封板、炸板</span></div><div class="pad">{list_block(analysis['fund_flow'])}</div></div>
	  <div class="panel"><div class="title">技术指标详情</div><div class="pad">{list_block(analysis['technical'])}</div></div>
	  <div class="panel"><div class="title">基本面数据</div><div class="pad">{list_block(analysis['fundamental'])}</div></div>
	  <div class="panel"><div class="title">公告 / 风险事件分析</div><div class="pad">{list_block(analysis['announcements'])}</div></div>
	  <div class="panel"><div class="title">相关板块分析</div><div class="pad">{list_block(analysis['sector'])}</div></div>
	  <div class="panel"><div class="title">相关新闻分析</div><div class="pad">{list_block(analysis['news'])}</div></div>
	  <div class="panel"><div class="title">历史同股信号对比</div><div class="pad">{history_block(analysis['history'])}</div></div>
	</main></body></html>"""
    if write_code_page:
        (out_dir / f"{code}.html").write_text(page, encoding="utf-8")
    if signal_id:
        (signal_dir / f"{signal_id}.html").write_text(page, encoding="utf-8")


def generate(base_dir):
    conn = connect(base_dir)
    latest_picks, all_signals, pick_history, backtest_runs, backtest_trades = load_data(conn)
    market_fund = load_market_fund(conn)
    attach_market_timing(latest_picks, market_fund)
    attach_market_timing(all_signals, market_fund)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ai_reports = load_all_ai_reports(conn)
    generate_ai_report_pages(base_dir, ai_reports, now)
    generated = set()
    analysis_cache = {}
    code_written = set()
    rich_limit = 10
    skipped = 0
    out_dir = Path(base_dir) / "stock-lab"
    signal_dir = out_dir / "signals"
    for idx, pick in enumerate(all_signals):
        sid = pick.get("id")
        if sid in generated:
            continue
        generated.add(sid)
        code = norm_code(pick.get("code"))
        # 增量生成：信号数据不可变，文件已存在则跳过
        signal_file = signal_dir / f"{int(sid)}.html" if sid else None
        code_file = out_dir / f"{code}.html" if code else None
        write_code_page = code not in code_written
        if signal_file and signal_file.exists() and (not write_code_page or code_file.exists()):
            try:
                template_current = "Agent 决策链" in signal_file.read_text(encoding="utf-8", errors="ignore")
                if write_code_page and code_file:
                    template_current = template_current and "Agent 决策链" in code_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                template_current = False
            if template_current:
                skipped += 1
                if write_code_page:
                    code_written.add(code)
                continue
        if write_code_page:
            code_written.add(code)
        generate_detail_page(base_dir, pick, now, conn, analysis_cache, rich=(idx < rich_limit), write_code_page=write_code_page)
    limit_detail_count = 0
    for pick in limit_snapshot_picks(market_fund):
        code = norm_code(pick.get("code"))
        if not code or code in code_written:
            continue
        generate_detail_page(base_dir, pick, now, conn, analysis_cache, rich=False, write_code_page=True)
        code_written.add(code)
        limit_detail_count += 1
    if skipped:
        print(f"增量生成：跳过 {skipped} 个未变化的详情页")
    if limit_detail_count:
        print(f"涨停梯队详情：补齐 {limit_detail_count} 个站内分析页")
    conn.close()
    available_dates = sorted({str(p.get("picked_date")) for p in all_signals if p.get("picked_date")}, reverse=True)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="选股雷达实验室公开展示历史选股信号、单股详情、AI 报告、策略回测、Vibe Alpha 因子和重复信号复盘。">
<meta name="theme-color" content="#f3f6f4">
<meta property="og:type" content="website">
<meta property="og:title" content="选股雷达实验室 | mazhi.icu">
<meta property="og:description" content="选股信号、单股详情、策略回测和 Vibe Alpha 因子实验。">
<meta property="og:url" content="https://mazhi.icu/invest/stock-lab.html">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="https://mazhi.icu/invest/stock-lab.html">
<link rel="manifest" href="/site.webmanifest">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;700;800&family=Outfit:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<title>选股雷达实验室 | mazhi.icu</title>
<link rel="stylesheet" href="/assets/stock-lab.css?v=202607221835">
</head><body>
<header class="top"><div class="top-inner">
  <div><div class="brand">选股雷达 <span>实验室</span></div><div class="meta">公开页，不包含个人账户/持仓/基金数据 · 生成 {now}</div></div>
	  <nav class="nav" aria-label="选股实验室导航"><a class="nav-primary" href="#picks">信号复盘</a><a href="#market-fund">大盘资金</a><a href="/invest/virtual-account.html">模拟盘</a><a href="#backtest">策略回测</a><a href="/invest/risk-alert.html">风险排雷</a><details class="nav-more"><summary>更多工具</summary><div class="nav-menu"><a href="#history">历史批次</a><a href="stock-lab/ai/">AI报告</a><a href="stock-lab/graph.html">知识图谱</a><a href="stock-lab/vibe/alphas.html">Alpha因子库</a><a href="stock-lab/vibe/bench.html">Alpha回测</a><a href="stock-lab/vibe/filters.html">过滤器搜索</a><a href="stock-lab/vibe/review.html">Vibe复盘</a></div></details></nav>
</div></header>
{ticker_tape_html(latest_picks, market_fund)}
	<main class="wrap">
  <h1 class="sr-only">选股雷达实验室</h1>
  <div class="decision-strip" id="decisionStrip"></div>
	  <div class="mobile-jump"><a href="#picks">看信号</a><a href="#market-fund">大盘资金</a><a href="/invest/virtual-account.html">模拟盘</a><a href="#backtest">看回测</a><a href="/invest/risk-alert.html">风险排雷</a></div>
	  <div class="hero overview-grid">
	    <div class="panel"><div class="title">策略框架 <span class="subtitle">多 Agent 加权决策 · 风险独立否决</span></div>
	      <div class="panel-pad rules">
	        <div class="rule agent-rule"><b>五路独立投票</b><span class="dim">资金 30% · 技术 24% · 事件 20% · 板块 15% · 基本面 11%。每路只提交看多、中性或看空及证据。</span></div>
	        <div class="rule agent-rule"><b>看多共识门槛</b><span class="dim">加权分至少 0.28、至少 3 路看多，且资金、技术、事件三项必须同时确认；缺一项只进观察池。</span></div>
	        <div class="rule risk-rule"><b>风险 Agent 一票否决</b><span class="dim">解禁、减持、监管、立案、处罚、业绩预亏或趋势熔断等硬风险，不参与加权，直接撤销买入结论。</span></div>
	        <div class="rule execute-rule"><b>策略与成交分离</b><span class="dim">综合评分用于排序，不代表买点。最终还需满足 A 股 T+1、100 股整数倍、交易时段、非涨停且实际可成交。</span></div>
	        <div class="rule guard-rule"><b>账户级风控</b><span class="dim">单票默认试仓 3%，止损优先；不交易北交所，月亏损达到 10% 后停止下单，仅保留观察和复盘。</span></div>
	      </div>
	      <div class="strategy-map" aria-label="多 Agent 策略决策流程图">
	        <div class="strategy-map-head"><b>决策流程图</b><span>分数只负责排序，闸门决定能否交易</span></div>
	        <div class="strategy-pipeline">
	          <div class="strategy-node source"><small>01</small><b>候选池</b><span>行情 · 资金 · 事件</span></div>
	          <div class="strategy-arrow" aria-hidden="true"><i></i></div>
	          <div class="agent-stack">
	            <div style="--weight:30%"><b>资金</b><i></i><em>30%</em></div>
	            <div style="--weight:24%"><b>技术</b><i></i><em>24%</em></div>
	            <div style="--weight:20%"><b>事件</b><i></i><em>20%</em></div>
	            <div style="--weight:15%"><b>板块</b><i></i><em>15%</em></div>
	            <div style="--weight:11%"><b>基本面</b><i></i><em>11%</em></div>
	          </div>
	          <div class="strategy-arrow" aria-hidden="true"><i></i></div>
	          <div class="strategy-node consensus"><small>02</small><b>共识门</b><span>≥0.28 · ≥3 看多</span></div>
	          <div class="strategy-arrow" aria-hidden="true"><i></i></div>
	          <div class="strategy-node veto"><small>03</small><b>风险门</b><span>硬风险一票否决</span></div>
	          <div class="strategy-arrow" aria-hidden="true"><i></i></div>
	          <div class="strategy-outcomes"><div class="pass"><b>可执行</b><span>真实成交约束</span></div><div class="watch"><b>观察池</b><span>等待证据补齐</span></div></div>
	        </div>
	      </div>
	    </div>
	    <div class="panel"><div class="title">今日概览 <span class="subtitle">只显示决策所需信息</span></div><div class="panel-pad" id="summary"></div></div>
	  </div>
	  {market_fund_panel(market_fund)}
		  <div class="panel" id="picks"><div class="title">信号复盘 <span class="subtitle" id="pickMeta"></span></div><div class="panel-pad">
		    <div class="filters">
		      <input id="q" aria-label="搜索信号" placeholder="搜索代码 / 名称 / 主题 / 原因">
		      <select id="dateFilter" aria-label="按日期筛选"><option value="">全部日期</option>{''.join(f'<option value="{esc(d)}">{esc(d)}</option>' for d in available_dates)}</select>
		      <select id="actionFilter" aria-label="按动作筛选"><option value="">全部动作</option><option value="buy">可执行</option><option value="short">待次日竞价</option><option value="watch">观察</option></select>
		      <select id="agentFilter" aria-label="按 Agent 共识筛选"><option value="">全部 Agent 共识</option><option value="buy">Agent 看多</option><option value="hold">Agent 中性</option><option value="sell">Agent 看空</option><option value="veto">风险否决</option></select>
		      <select id="conceptFilter" aria-label="按板块概念筛选"><option value="">全部板块概念</option></select>
			      <select id="sortBy" aria-label="排序方式"><option value="time_desc">时间倒序</option><option value="time_asc">时间正序</option><option value="score_desc">评分高到低</option><option value="chg_desc">涨幅高到低</option><option value="chg_asc">跌幅高到低</option></select>
			      <select id="uniqueMode" aria-label="重复信号显示方式"><option value="daily">每日去重</option><option value="raw">全部原始信号</option></select>
			      <select id="runFilter" aria-label="按批次筛选"><option value="">全部批次</option></select>
			    </div>
		    <div class="grid" id="pickCards"><div class="card skeleton skeleton-card"></div><div class="card skeleton skeleton-card"></div><div class="card skeleton skeleton-card"></div></div><div class="scroll" style="margin-top:14px"><table id="pickTable"></table></div></div></div>
	  <div class="panel backtest-panel" id="backtest"><div class="title">策略验收 <span class="subtitle">先验收，再执行</span></div><div class="panel-pad">
    <div class="backtest-toolbar"><b>选择回测周期</b><select id="backtestPeriod" aria-label="选择回测周期"></select></div>
    <div class="backtest-meta" id="backtestMeta"></div>
    <div class="backtest-copy">
      <div><b>验收口径</b><span>只看已经保存的历史信号；次日开盘入场，加入手续费和滑点，止损优先。胜率只是辅助，最终看期望值、回撤和相对三大指数的超额收益。</span></div>
      <div><b>执行判定</b><span>期望值为正、回撤可控、样本量足够时才打开买点；否则只保留观察信号，用于复盘和继续调参。</span></div>
    </div>
    <div class="section-label">大盘指数复盘 <span>同一时间参数，用作所有策略的共同基准</span></div>
    <div class="benchmark-strip" id="benchmarkStrip"></div>
    <div class="section-label">策略复盘 <span>每张卡只看策略自身结果和相对大盘超额</span></div>
    <div id="runCards"></div>
    <div class="trade-detail-title">最近交易明细</div>
    <div class="scroll"><table id="tradeTable"></table></div>
  </div></div>
		  <div class="panel" id="history"><div class="title">历史批次</div><div class="panel-pad scroll"><table id="historyTable"></table></div></div>
</main>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/gsap.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/ScrollTrigger.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
const PICKS = {dumps(latest_picks)};
const SIGNALS = {json.dumps(slim_signals(all_signals), ensure_ascii=False)};
const HISTORY = {dumps(pick_history)};
const RUNS = {dumps(backtest_runs)};
const TRADES = {dumps(backtest_trades)};
let SECTOR_FLOW_HISTORY = {json.dumps(clean(market_fund.get('sector_history') or []), ensure_ascii=False)};
</script>
<script src="/assets/stock-lab.js?v=202607221845"></script></body></html>"""
    out = Path(base_dir) / "stock-lab.html"
    out.write_text(html, encoding="utf-8")
    (Path(base_dir) / "sector-flow.json").write_text(json.dumps({
        "updated_at": market_fund.get("sector_snapshot_at") or market_fund.get("snapshot_at"),
        "history": clean(market_fund.get("sector_history") or []),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"Generated: {out}")


if __name__ == "__main__":
    generate(sys.argv[1] if len(sys.argv) > 1 else ".")
