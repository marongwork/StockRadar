#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库隔离同步脚本 —— 把个人库(invest.db)的【选股表】同步到付费平台库(stock.db)。

架构隔离原则：
- 个人库 invest.db：全量数据(含个人持仓/基金/账户等私密表)，仅 my.mazhi.icu 访问
- 付费库 stock.db：仅选股服务相关表，供 stock.mazhi.icu 的用户使用
- 同步方向：invest.db → stock.db（单向，付费平台永远无法触达个人私密表）

只同步以下"选股服务"表（付费用户可见）：
  stock_picks / backtest_runs / backtest_trades / ai_reports /
  vibe_alphas / vibe_alpha_bench_runs / vibe_strategy_reviews / strategy_filter_candidates

绝不同步（个人私密）：
  account / holdings / funds / transactions / monthly_pnl /
  asset_milestones / longterm_goals / watchlist / holding_risk_alerts

用法：python3 sync_stock_db.py
建议加入 crontab：*/10 * * * * 定时同步（选股更新后 10 分钟内同步到付费平台）
"""
import sqlite3
import shutil
import os
import sys
from datetime import datetime
from pathlib import Path

# 路径配置
PERSONAL_DB = "/var/www/mazhi.icu/invest/invest.db"          # 个人库（源，全量）
STOCK_DB = "/var/www/stock.mazhi.icu/data/stock.db"          # 付费库（目标，仅选股表）
STOCK_DB_DIR = os.path.dirname(STOCK_DB)

# 白名单：允许同步到付费平台的表（仅选股服务相关）
SYNC_TABLES = [
    "stock_picks",
    "backtest_runs",
    "backtest_trades",
    "ai_reports",
    "ai_jobs",
    "vibe_alphas",
    "vibe_alpha_bench_runs",
    "vibe_strategy_reviews",
    "strategy_filter_candidates",
]

# 黑名单：绝不同步（个人私密）—— 这里列出是为了自检，防止误同步
NEVER_SYNC = [
    "account", "holdings", "funds", "transactions", "monthly_pnl",
    "asset_milestones", "longterm_goals", "watchlist", "holding_risk_alerts",
]


def sync():
    if not os.path.exists(PERSONAL_DB):
        print(f"❌ 个人库不存在: {PERSONAL_DB}")
        return False

    # 自检：确认白名单与黑名单无交集
    leak = set(SYNC_TABLES) & set(NEVER_SYNC)
    if leak:
        print(f"❌ 严重错误：白名单与黑名单交集 {leak}，同步中止！")
        return False

    os.makedirs(STOCK_DB_DIR, exist_ok=True)

    # 先复制整个个人库到临时文件，再从临时库提取白名单表（避免读写锁冲突）
    tmp = STOCK_DB + ".tmp"
    shutil.copy2(PERSONAL_DB, tmp)

    # 连接临时库（读源表结构+数据）和目标库（重建）
    src = sqlite3.connect(tmp)
    src.row_factory = sqlite3.Row

    # 重建目标库：先备份旧 stock.db，再创建新的
    backup_target()
    dst = sqlite3.connect(STOCK_DB)

    synced = 0
    for table in SYNC_TABLES:
        # 检查源库是否有该表
        cols = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchall()
        if not cols:
            continue
        # 取建表语句
        schema = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        # 取索引
        indexes = [r[0] for r in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)
        ).fetchall()]
        # 取数据
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        col_names = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 1").description] if rows else \
                   [c[1] for c in src.execute(f"PRAGMA table_info({table})").fetchall()]

        # 在目标库重建表（DROP IF EXISTS 保证幂等）
        dst.execute(f"DROP TABLE IF EXISTS {table}")
        dst.execute(schema)
        for idx_sql in indexes:
            dst.execute(idx_sql)
        if rows:
            placeholders = ",".join("?" * len(col_names))
            dst.executemany(
                f"INSERT INTO {table} ({','.join(col_names)}) VALUES ({placeholders})",
                [tuple(r) for r in rows]
            )
        synced += 1
        print(f"  ✓ {table}: {len(rows)} 行")

    dst.commit()
    dst.close()
    src.close()
    os.remove(tmp)

    # 安全校验：确认目标库不含任何个人私密表
    verify = sqlite3.connect(STOCK_DB)
    target_tables = [r[0] for r in verify.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    verify.close()
    leaked = [t for t in target_tables if t in NEVER_SYNC]
    if leaked:
        print(f"❌ 安全校验失败：付费库含私密表 {leaked}！已回滚。")
        rollback_target()
        return False

    print(f"\n✅ 同步完成: {synced} 个选股表 → {STOCK_DB}")
    print(f"   安全校验: 付费库不含任何个人私密表 ✓")
    return True


def backup_target():
    """同步前备份旧的 stock.db（保留最近一份）"""
    if os.path.exists(STOCK_DB):
        bak = STOCK_DB + f".bak.{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(STOCK_DB, bak)
        # 只保留最近 3 份备份
        baks = sorted(Path(STOCK_DB_DIR).glob("stock.db.bak.*"))
        for old in baks[:-3]:
            os.remove(old)


def rollback_target():
    """出错时回滚到最近备份"""
    baks = sorted(Path(STOCK_DB_DIR).glob("stock.db.bak.*"))
    if baks:
        shutil.copy2(baks[-1], STOCK_DB)
        print(f"   已回滚到 {baks[-1].name}")


if __name__ == "__main__":
    print(f"=== 数据库隔离同步 {datetime.now():%Y-%m-%d %H:%M} ===")
    print(f"源(个人库): {PERSONAL_DB}")
    print(f"目标(付费库): {STOCK_DB}")
    print(f"同步表({len(SYNC_TABLES)}): {', '.join(SYNC_TABLES)}")
    print(f"绝不同步({len(NEVER_SYNC)}): {', '.join(NEVER_SYNC)}")
    print()
    ok = sync()
    sys.exit(0 if ok else 1)
