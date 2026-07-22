#!/usr/bin/env python3
"""Search simple filters over saved backtest trades for candidate strategy rules."""
import html
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "invest.db"


def ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS strategy_filter_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            rule TEXT NOT NULL,
            trades INTEGER NOT NULL,
            win_rate REAL NOT NULL,
            avg_win REAL NOT NULL,
            avg_loss REAL NOT NULL,
            payoff_ratio REAL NOT NULL,
            expectancy REAL NOT NULL,
            max_drawdown REAL NOT NULL,
            total_return REAL NOT NULL,
            sample_ids TEXT
        )"""
    )
    conn.commit()


def max_drawdown(returns):
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for ret in returns:
        equity *= 1 + ret / 100.0
        peak = max(peak, equity)
        worst = min(worst, (equity / peak - 1) * 100.0)
    return worst


def metrics(rows):
    returns = [float(r["return_pct"]) for r in rows]
    wins = [r for r in returns if r > 0]
    losses = [abs(r) for r in returns if r < 0]
    n = len(returns)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / n * 100 if n else 0.0
    payoff = avg_win / avg_loss if avg_loss else (999.0 if avg_win else 0.0)
    ev = len(wins) / n * avg_win - len(losses) / n * avg_loss if n else 0.0
    return {
        "trades": n,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "payoff_ratio": round(payoff, 3),
        "expectancy": round(ev, 3),
        "max_drawdown": round(max_drawdown(returns), 3),
        "total_return": round(sum(returns), 3),
    }


def load_rows(conn):
    latest_ids = [r[0] for r in conn.execute("SELECT id FROM backtest_runs ORDER BY id DESC LIMIT 12")]
    placeholders = ",".join("?" for _ in latest_ids) or "0"
    return [
        dict(r)
        for r in conn.execute(
            f"""SELECT t.*, p.reason, p.chg_pct, p.cap, p.main_net, p.pe, p.run_id
                FROM backtest_trades t
                LEFT JOIN stock_picks p ON p.id=t.pick_id
                WHERE t.run_id IN ({placeholders})""",
            latest_ids,
        )
    ]


def rule_specs(rows):
    specs = []
    specs.append(("全部信号", lambda r: True))
    specs.append(("只做涨停短线", lambda r: r.get("strategy") == "limit_up"))
    specs.append(("只做趋势事件", lambda r: r.get("strategy") == "trend"))
    for threshold in (90, 100, 105, 110, 115):
        specs.append((f"评分>={threshold}", lambda r, threshold=threshold: (r.get("score") or 0) >= threshold))
        specs.append((f"涨停短线且评分>={threshold}", lambda r, threshold=threshold: r.get("strategy") == "limit_up" and (r.get("score") or 0) >= threshold))
    for rank in (1, 2, 3):
        specs.append((f"排名<={rank}", lambda r, rank=rank: (r.get("rank") or 999) <= rank))
        specs.append((f"涨停短线且排名<={rank}", lambda r, rank=rank: r.get("strategy") == "limit_up" and (r.get("rank") or 999) <= rank))
    for word in ("AI算力", "机器人", "固态电池", "脑机接口", "主线涨停", "换手健康", "2连板", "首板", "强封单", "封板稳"):
        specs.append((f"包含 {word}", lambda r, word=word: word in ((r.get("theme") or "") + "+" + (r.get("reason") or ""))))
        specs.append((f"涨停短线且包含 {word}", lambda r, word=word: r.get("strategy") == "limit_up" and word in ((r.get("theme") or "") + "+" + (r.get("reason") or ""))))
    specs.append(("涨停短线 + 主线涨停 + 换手健康", lambda r: r.get("strategy") == "limit_up" and "主线涨停" in (r.get("reason") or "") and "换手健康" in (r.get("reason") or "")))
    specs.append(("涨停短线 + AI算力 + 强封单", lambda r: r.get("strategy") == "limit_up" and "AI算力" in (r.get("theme") or "") and "强封单" in (r.get("reason") or "")))
    specs.append(("涨停短线 + 2连板 + 强封单", lambda r: r.get("strategy") == "limit_up" and "2连板" in (r.get("reason") or "") and "强封单" in (r.get("reason") or "")))
    specs.append(("涨停短线 + 首板 + 强封单", lambda r: r.get("strategy") == "limit_up" and "首板" in (r.get("reason") or "") and "强封单" in (r.get("reason") or "")))
    return specs


def search():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = load_rows(conn)
    found = []
    for rule, predicate in rule_specs(rows):
        subset = [r for r in rows if predicate(r)]
        if len(subset) < 5:
            continue
        m = metrics(subset)
        ids = ",".join(str(r.get("pick_id") or "") for r in subset[:30])
        found.append((rule, m, ids))
    found.sort(key=lambda x: (x[1]["expectancy"], x[1]["win_rate"], x[1]["trades"]), reverse=True)
    conn.execute("DELETE FROM strategy_filter_candidates")
    for rule, m, ids in found:
        conn.execute(
            """INSERT INTO strategy_filter_candidates
               (created_at,rule,trades,win_rate,avg_win,avg_loss,payoff_ratio,expectancy,max_drawdown,total_return,sample_ids)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                created_at,
                rule,
                m["trades"],
                m["win_rate"],
                m["avg_win"],
                m["avg_loss"],
                m["payoff_ratio"],
                m["expectancy"],
                m["max_drawdown"],
                m["total_return"],
                ids,
            ),
        )
    conn.commit()
    render_page(conn)
    conn.close()
    print(f"searched {len(found)} filter candidates")
    for rule, m, _ in found[:10]:
        print(f"{rule}: n={m['trades']} win={m['win_rate']} EV={m['expectancy']} payoff={m['payoff_ratio']} mdd={m['max_drawdown']}")


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def render_page(conn):
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM strategy_filter_candidates
               ORDER BY expectancy DESC, win_rate DESC, trades DESC
               LIMIT 50"""
        )
    ]
    viable = [
        r
        for r in rows
        if r["trades"] >= 10 and r["win_rate"] >= 50 and r["expectancy"] > 0
    ]
    positive = [r for r in rows if r["expectancy"] > 0]
    best = rows[0] if rows else None
    created_at = rows[0]["created_at"] if rows else datetime.now().strftime("%Y-%m-%d %H:%M")
    summary_cards = [
        ("候选规则", len(rows), "已搜索的策略过滤条件"),
        ("可用规则", len(viable), "样本≥10、胜率≥50%、EV>0"),
        ("正期望", len(positive), "EV 大于 0 的规则数量"),
        ("最佳 EV", f"{best['expectancy']:.2f}%" if best else "-", best["rule"] if best else "暂无数据"),
    ]
    card_html = []
    for label, value, desc in summary_cards:
        card_html.append(
            f'<div class="summary-card"><span>{esc(label)}</span><b>{esc(value)}</b><small>{esc(desc)}</small></div>'
        )
    rule_cards = []
    for r in rows[:8]:
        ok = r["trades"] >= 10 and r["win_rate"] >= 50 and r["expectancy"] > 0
        status = "可试仓观察" if ok else ("仅记录" if r["expectancy"] > 0 else "淘汰")
        cls = "ok" if ok else ("watch" if r["expectancy"] > 0 else "bad")
        rule_cards.append(
            f"""<article class="rule-card {cls}">
  <div class="rule-head"><b>{esc(r['rule'])}</b><span>{status}</span></div>
  <div class="rule-metrics">
    <div><span>样本</span><strong>{r['trades']}</strong></div>
    <div><span>胜率</span><strong>{r['win_rate']:.1f}%</strong></div>
    <div><span>EV</span><strong class="{'pos' if r['expectancy'] > 0 else 'neg'}">{r['expectancy']:.2f}%</strong></div>
    <div><span>回撤</span><strong class="neg">{r['max_drawdown']:.2f}%</strong></div>
  </div>
</article>"""
        )
    trs = []
    for r in rows:
        ok = r["trades"] >= 10 and r["win_rate"] >= 50 and r["expectancy"] > 0
        status = "可用" if ok else ("观察" if r["expectancy"] > 0 else "淘汰")
        status_cls = "ok" if ok else ("watch" if r["expectancy"] > 0 else "bad")
        trs.append(
            f"<tr class=\"{'okrow' if ok else ''}\"><td>{esc(r['rule'])}</td><td><span class=\"status {status_cls}\">{status}</span></td><td>{r['trades']}</td>"
            f"<td>{r['win_rate']:.1f}%</td><td class=\"{'pos' if r['expectancy'] > 0 else 'neg'}\">{r['expectancy']:.2f}%</td>"
            f"<td>{r['payoff_ratio']:.2f}</td><td class=\"neg\">{r['max_drawdown']:.2f}%</td><td>{r['total_return']:.2f}%</td></tr>"
        )
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>策略过滤器搜索 | 选股雷达实验室</title>
<link rel="stylesheet" href="/assets/vibe-lab.css?v=202607141950">
<style>
body{{margin:0;}}
a{{color:inherit}}
.wrap{{max-width:1220px;margin:0 auto;padding:22px}}
.hero{{padding:26px 0 8px}}
.hero p{{max-width:820px;color:var(--muted);margin:8px 0 0}}
.summary-card small{{display:block;margin-top:6px;color:var(--muted);line-height:1.45}}
.rule-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:18px 0 22px}}
.rule-card{{background:linear-gradient(180deg,rgba(255,255,255,.035),transparent 44%),var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}}
.rule-card.ok{{border-color:rgba(0,214,143,.45);background:linear-gradient(135deg,rgba(0,214,143,.12),transparent 44%),var(--panel)}}
.rule-card.watch{{border-color:rgba(240,185,11,.34)}}
.rule-card.bad{{opacity:.78}}
.rule-head{{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;margin-bottom:12px}}
.rule-head b{{font-size:15px;line-height:1.45}}
.rule-head span,.status{{display:inline-flex;align-items:center;border-radius:999px;padding:3px 8px;font-family:var(--mono);font-size:11px;white-space:nowrap}}
.rule-card.ok .rule-head span,.status.ok{{background:rgba(0,214,143,.12);color:var(--green);border:1px solid rgba(0,214,143,.3)}}
.rule-card.watch .rule-head span,.status.watch{{background:rgba(240,185,11,.1);color:var(--yellow);border:1px solid rgba(240,185,11,.28)}}
.rule-card.bad .rule-head span,.status.bad{{background:rgba(246,70,93,.08);color:var(--red);border:1px solid rgba(246,70,93,.24)}}
.rule-metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}
.rule-metrics div{{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px}}
.rule-metrics span{{display:block;color:var(--muted);font-size:11px}}
.rule-metrics strong{{display:block;margin-top:2px;font-size:14px}}
.panel{{overflow:hidden}}
.scroll{{overflow-x:auto}}
.pos{{color:var(--green)!important}} .neg{{color:var(--red)!important}}
.okrow{{background:rgba(0,214,143,.08)}}
.note{{margin:10px 0 18px;color:var(--muted);font-size:13px;line-height:1.7}}
@media(max-width:680px){{
  .rule-grid{{grid-template-columns:1fr}}
  .rule-metrics{{grid-template-columns:repeat(2,1fr)}}
  table{{min-width:940px}}
  .wrap{{padding:16px}}
}}
</style></head><body><div class="top"><div class="wrap"><h1>策略过滤器 <span>搜索</span></h1><a href="/invest/stock-lab.html">返回实验室</a></div></div>
<main class="wrap">
  <section class="hero">
    <h2>从回测交易里筛出可执行规则</h2>
    <p>更新时间 {esc(created_at)}。先看可用规则卡片，再查完整表；规则只作为下一轮选股过滤条件，不直接等同买入信号。</p>
  </section>
  <section class="summary-grid">{''.join(card_html)}</section>
  <p class="note">可用门槛：样本数 ≥ 10、胜率 ≥ 50%、期望值 EV > 0。回撤过大的规则即使胜率高，也只能小仓观察。</p>
  <section class="rule-grid">{''.join(rule_cards) or '<div class="dim">暂无规则</div>'}</section>
  <section class="panel scroll"><table><thead><tr><th>规则</th><th>状态</th><th>样本</th><th>胜率</th><th>EV</th><th>盈亏比</th><th>回撤</th><th>总收益</th></tr></thead><tbody>{''.join(trs)}</tbody></table></section>
</main></body></html>"""
    out_dir = BASE_DIR / "stock-lab" / "vibe"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "filters.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    search()
