#!/usr/bin/env python3
"""从公开的东方财富板块监控历史补齐指定交易日的分钟资金快照。"""
import argparse
import calendar
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

from stock_picker import SECTOR_NOISE_RE, parallel_iw_queries

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "invest.db"
SOURCE_BASE = "https://dw.web69.cn/api/chart.php"


def fetch_series(board_type, limit=20, trade_date=None):
    params = {"type": board_type, "limit": limit}
    if trade_date:
        params["date"] = trade_date
    url = SOURCE_BASE + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 200 or not payload.get("trade_date"):
        raise RuntimeError(payload.get("msg") or "板块历史接口返回异常")
    return payload


def minute_value(text):
    hour, minute = str(text)[-5:].split(":")
    return int(hour) * 60 + int(minute)


def sample_targets():
    return [571] + list(range(575, 691, 5)) + [781] + list(range(785, 901, 5))


def latest_point(points, target):
    session_start = 570 if target <= 690 else 780
    eligible = [point for point in points if session_start <= minute_value(point.get("time")) <= target]
    if not eligible:
        return None
    point = max(eligible, key=lambda item: minute_value(item.get("time")))
    return point if target - minute_value(point.get("time")) <= 7 else None


def rows_at(series, board_type, target):
    rows = []
    for item in series:
        name = str(item.get("name") or "").strip()
        if not name or (board_type == "概念" and SECTOR_NOISE_RE.search(name)):
            continue
        point = latest_point(item.get("points") or [], target)
        if not point:
            continue
        rows.append({
            "板块代码": item.get("code"), "板块名称": name, "板块类型": board_type,
            "涨跌幅": point.get("pct_chg"),
            "主力资金净流入": float(point.get("main_net_yi") or 0) * 1e8,
            "资金数据源": "web69公开接口（东方财富分钟采集）",
            "原始采样时间": str(point.get("time") or "")[-5:],
        })
    return sorted(rows, key=lambda row: row["主力资金净流入"], reverse=True)


def backfill(trade_date, replace=False):
    concept_payload = fetch_series("concept", trade_date=trade_date)
    industry_payload = fetch_series("industry", trade_date=trade_date)
    source_date = concept_payload["trade_date"]
    if source_date != industry_payload["trade_date"] or source_date != trade_date:
        raise RuntimeError(f"接口交易日为 {source_date}，不是请求的 {trade_date}")
    concepts = concept_payload.get("data") or []
    industries = industry_payload.get("data") or []
    if not concepts and not industries:
        return 0
    conn = sqlite3.connect(DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sector_fund_snapshots)")}
    if "phase" not in cols:
        conn.execute("ALTER TABLE sector_fund_snapshots ADD COLUMN phase TEXT NOT NULL DEFAULT 'trading'")
    if replace:
        conn.execute("DELETE FROM sector_fund_snapshots WHERE substr(snapshot_at,1,10)=?", (trade_date,))
    inserted = 0
    for target in sample_targets():
        concept_rows = rows_at(concepts, "概念", target)
        industry_rows = rows_at(industries, "行业", target)
        if not concept_rows and not industry_rows:
            continue
        combined = sorted(concept_rows + industry_rows, key=lambda row: abs(row["主力资金净流入"]), reverse=True)[:25]
        snapshot_at = f"{trade_date} {target // 60:02d}:{target % 60:02d}"
        conn.execute(
            """INSERT INTO sector_fund_snapshots
               (snapshot_at,concept_json,industry_json,combined_json,source,phase)
               VALUES (?,?,?,?,?,?)""",
            (snapshot_at, json.dumps(concept_rows, ensure_ascii=False), json.dumps(industry_rows, ensure_ascii=False),
             json.dumps(combined, ensure_ascii=False), "web69_eastmoney_history", "trading"),
        )
        inserted += 1
    conn.commit(); conn.close()
    return inserted


def historical_number(row, date_token, *labels):
    for key, value in row.items():
        if date_token in str(key) and any(label in str(key) for label in labels):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def normalize_daily_rows(datas, board_type, trade_date):
    date_token = trade_date.replace("-", "")
    rows = []
    for raw in datas or []:
        name = raw.get("指数简称") or raw.get("板块名称") or raw.get("概念名称")
        name = str(name or "").strip()
        if not name or (board_type == "概念" and SECTOR_NOISE_RE.search(name)):
            continue
        flow = historical_number(raw, date_token, "主力净买入额", "主力资金净流入")
        if flow is None:
            outflow = historical_number(raw, date_token, "主力净流出额", "主力净卖出额")
            flow = -abs(outflow) if outflow is not None else None
        if flow is None:
            continue
        rows.append({
            "板块代码": raw.get("指数代码") or raw.get("板块代码"),
            "板块名称": name, "板块类型": board_type,
            "涨跌幅": historical_number(raw, date_token, "涨跌幅"),
            "主力资金净流入": flow,
            "资金数据源": "同花顺问财历史板块数据（日级）",
        })
    unique = {row["板块名称"]: row for row in rows}
    return sorted(unique.values(), key=lambda row: row["主力资金净流入"], reverse=True)


def fetch_daily_rows(trade_date):
    cn_date = f"{trade_date[:4]}年{int(trade_date[5:7])}月{int(trade_date[8:10])}日"
    queries = [
        (f"{cn_date}概念板块主力资金净流入排名前20，涨跌幅", "hithink-sector-selector", "20"),
        (f"{cn_date}概念板块主力资金净流出排名前10，涨跌幅", "hithink-sector-selector", "10"),
        (f"{cn_date}行业板块主力资金净流入排名前15，涨跌幅", "hithink-sector-selector", "15"),
        (f"{cn_date}行业板块主力资金净流出排名前10，涨跌幅", "hithink-sector-selector", "10"),
    ]
    result = parallel_iw_queries(queries, max_workers=4)
    concepts = normalize_daily_rows(result[0] + result[1], "概念", trade_date)
    industries = normalize_daily_rows(result[2] + result[3], "行业", trade_date)
    return concepts, industries


def backfill_daily_close(trade_date, replace=False):
    concepts, industries = fetch_daily_rows(trade_date)
    if not concepts and not industries:
        return 0
    combined = sorted(concepts + industries, key=lambda row: abs(row["主力资金净流入"]), reverse=True)[:25]
    conn = sqlite3.connect(DB_PATH)
    if replace:
        conn.execute("DELETE FROM sector_fund_snapshots WHERE substr(snapshot_at,1,10)=?", (trade_date,))
    conn.execute(
        """INSERT INTO sector_fund_snapshots
           (snapshot_at,concept_json,industry_json,combined_json,source,phase)
           VALUES (?,?,?,?,?,?)""",
        (f"{trade_date} 15:00", json.dumps(concepts, ensure_ascii=False), json.dumps(industries, ensure_ascii=False),
         json.dumps(combined, ensure_ascii=False), "iwencai_historical_daily", "daily_close"),
    )
    conn.commit(); conn.close()
    return 1


def supplement_minute_close(trade_date):
    """用问财日级榜补齐分钟源缺失的收盘大额流出板块。"""
    daily_concepts, daily_industries = fetch_daily_rows(trade_date)
    if not daily_concepts and not daily_industries:
        return 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sector_fund_snapshots WHERE substr(snapshot_at,1,10)=? ORDER BY snapshot_at DESC,id DESC LIMIT 1",
        (trade_date,),
    ).fetchone()
    if not row:
        conn.close()
        return 0
    def merge(raw, daily):
        values = {item.get("板块名称"): item for item in json.loads(raw or "[]")}
        for item in daily:
            values[item.get("板块名称")] = item
        return sorted(values.values(), key=lambda item: item.get("主力资金净流入") or 0, reverse=True)
    concepts = merge(row["concept_json"], daily_concepts)
    industries = merge(row["industry_json"], daily_industries)
    combined = sorted(concepts + industries, key=lambda item: abs(item.get("主力资金净流入") or 0), reverse=True)[:25]
    conn.execute(
        "UPDATE sector_fund_snapshots SET concept_json=?,industry_json=?,combined_json=?,source=? WHERE id=?",
        (json.dumps(concepts, ensure_ascii=False), json.dumps(industries, ensure_ascii=False),
         json.dumps(combined, ensure_ascii=False), "web69_minutes+iwencai_daily_close", row["id"]),
    )
    conn.commit(); conn.close()
    return len(daily_concepts) + len(daily_industries)


def month_dates(month):
    year, month_no = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_no)[1]
    end = min(date(year, month_no, last_day), date.today())
    current = date(year, month_no, 1)
    while current <= end:
        if current.weekday() < 5:
            yield current.isoformat()
        current += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="交易日 YYYY-MM-DD")
    parser.add_argument("--month", help="回填月份 YYYY-MM；优先分钟历史，缺失时回填日级收盘")
    parser.add_argument("--replace", action="store_true", help="替换该交易日已有板块快照")
    parser.set_defaults(date=None)
    args = parser.parse_args()
    if args.month:
        for trade_date in month_dates(args.month):
            count = backfill(trade_date, replace=args.replace)
            granularity = "分钟"
            if count == 0:
                count = backfill_daily_close(trade_date, replace=args.replace)
                granularity = "日级"
            else:
                supplement_minute_close(trade_date)
            print(f"{trade_date}: {granularity} {count} 条")
        return
    if not args.date:
        parser.error("--date 或 --month 至少提供一个")
    datetime.strptime(args.date, "%Y-%m-%d")
    count = backfill(args.date, replace=args.replace)
    print(f"已回填 {args.date} 共 {count} 个盘中板块快照")


if __name__ == "__main__":
    main()
