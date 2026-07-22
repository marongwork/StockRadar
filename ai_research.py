#!/usr/bin/env python3
"""AI research report generator for stock radar signals.

The first version is intentionally conservative: it creates durable report
records in invest.db and renders static HTML pages. It uses the same GLM
Chat Completions endpoint configured for Vibe Trading, but avoids the Vibe
agent tool loop so a single-stock analysis cannot accidentally launch a
long-running backtest.
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "invest.db"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"


def norm_code(code):
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            code TEXT,
            name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt TEXT,
            report_id INTEGER,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT,
            title TEXT,
            report_html TEXT NOT NULL,
            report_text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'glm',
            model TEXT,
            job_id INTEGER,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_reports_code_time ON ai_reports(code, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_status_time ON ai_jobs(status, created_at DESC)")
    conn.commit()


def load_env_file(path):
    values = {}
    p = Path(path)
    if not p.exists():
        return values
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except PermissionError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def llm_config():
    env_files = [BASE_DIR / ".ai_env"]
    env_file = os.environ.get("VIBE_ENV_FILE")
    if env_file:
        env_files.insert(0, Path(env_file))
    env_files.append(Path("/etc/vibe-trading/vibe-trading.env"))
    file_env = {}
    for path in env_files:
        file_env.update(load_env_file(path))
    key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("ZHIPU_API_KEY")
        or os.environ.get("BIGMODEL_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or file_env.get("DEEPSEEK_API_KEY")
        or file_env.get("ZHIPU_API_KEY")
        or file_env.get("BIGMODEL_API_KEY")
        or file_env.get("OPENAI_API_KEY")
        or file_env.get("LLM_API_KEY")
    )
    base_url = (
        os.environ.get("AI_BASE_URL")
        or file_env.get("DEEPSEEK_BASE_URL")
        or file_env.get("ZHIPU_BASE_URL")
        or file_env.get("OPENAI_BASE_URL")
        or file_env.get("LLM_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = (
        os.environ.get("AI_MODEL")
        or file_env.get("DEEPSEEK_MODEL")
        or file_env.get("ZHIPU_MODEL")
        or file_env.get("OPENAI_MODEL")
        or file_env.get("LLM_MODEL")
        or DEFAULT_MODEL
    )
    return key, base_url.rstrip("/"), model


def compact_row(row, max_len=220):
    parts = []
    for key in row.keys():
        value = row[key]
        if value in (None, "", "--", "-"):
            continue
        text = str(value)
        if len(text) > max_len:
            text = text[: max_len - 3] + "..."
        parts.append(f"{key}={text}")
    return "；".join(parts)


def latest_pick_context(conn, code):
    if "stock_picks" not in table_names(conn):
        return "暂无选股雷达历史信号。"
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT id, picked_at, picked_date, picked_time, run_id, rank, code, name, theme,
                      score, chg_pct, cap, pe, main_net, buy_point, stop_loss, target,
                      reason, eval_status, eval_return_pct
               FROM stock_picks
               WHERE code=?
               ORDER BY picked_at DESC, id DESC
               LIMIT 12""",
            (code,),
        )
    ]
    if not rows:
        return "暂无该股票在选股雷达中的历史信号。"
    lines = []
    for row in rows:
        lines.append("- " + compact_row(row))
    return "\n".join(lines)


def risk_context(conn, code, name):
    tables = table_names(conn)
    chunks = []
    for table in ("holding_risk_alerts", "stock_risk_alerts", "risk_alerts"):
        if table not in tables:
            continue
        try:
            rows = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM {table} WHERE code=? OR name LIKE ? ORDER BY id DESC LIMIT 8",
                    (code, f"%{name}%"),
                )
            ]
        except Exception:
            rows = []
        if rows:
            chunks.append(f"{table}:")
            chunks.extend("- " + compact_row(r) for r in rows)
    return "\n".join(chunks) if chunks else "暂无本地风险表记录。"


def build_prompt(conn, code, name):
    pick_ctx = latest_pick_context(conn, code)
    risk_ctx = risk_context(conn, code, name)
    return f"""你是A股投研助手。请为 {name}({code}) 生成一份可复盘的单股深度分析。

硬性要求：
1. 只输出研究分析，不要调用工具，不要运行回测，不要生成代码。
2. 不要给保证性结论，不要承诺收益；必须写清楚失效条件和风险。
3. 重点补足选股雷达容易漏掉的信息：公告、限售解禁、减持、监管、业绩预告、板块强弱、资金持续性。
4. 交易计划必须有明确点位表达方式：关注位、买入触发、止损位、第一止盈、第二止盈、仓位上限；若数据不足，写“需以最新行情复核”。
5. 输出中文，使用 Markdown 标题和列表。

本地选股雷达历史信号：
{pick_ctx}

本地风险记录：
{risk_ctx}

请按以下结构输出：
# 结论
# 股市行情与技术面
# 基本面与估值
# 公告与重大风险
# 相关板块与产业链
# 新闻与催化
# 执行计划
# 失效条件
# 后续跟踪清单
"""


def chat_completion(prompt, retries=1):
    key, base_url, model = llm_config()
    if not key:
        raise RuntimeError("missing API key: set ZHIPU_API_KEY/BIGMODEL_API_KEY/OPENAI_API_KEY or /etc/vibe-trading/vibe-trading.env")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨的A股研究助手，输出可执行、可复盘、风险优先的分析。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.environ.get("AI_MAX_TOKENS", "1800")),
    }
    if model.startswith("deepseek-v4"):
        payload["thinking"] = {"type": os.environ.get("AI_THINKING", "enabled")}
        payload["reasoning_effort"] = os.environ.get("AI_REASONING_EFFORT", "high")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=int(os.environ.get("AI_TIMEOUT", "180"))) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"], model
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"chat completion failed: {last_error}")


def markdown_to_html(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    in_ul = False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            close_ul()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            close_ul()
            level = len(heading.group(1)) + 2
            out.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
            continue
        item = re.match(r"^[-*]\s+(.+)$", stripped)
        if item:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{html.escape(item.group(1))}</li>")
            continue
        close_ul()
        out.append(f"<p>{html.escape(stripped)}</p>")
    close_ul()
    return "\n".join(out)


def save_report(conn, code, name, text, model, job_id=None):
    created_at = now_text()
    title = f"{name}({code}) AI深度分析"
    html_body = markdown_to_html(text)
    cur = conn.execute(
        """INSERT INTO ai_reports(code, name, title, report_html, report_text, source, model, job_id, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (code, name, title, html_body, text, "glm", model, job_id, created_at),
    )
    report_id = cur.lastrowid
    if job_id:
        conn.execute(
            "UPDATE ai_jobs SET status='done', report_id=?, finished_at=? WHERE id=?",
            (report_id, created_at, job_id),
        )
    conn.commit()
    return report_id


def create_job(conn, code, name, prompt):
    created_at = now_text()
    cur = conn.execute(
        "INSERT INTO ai_jobs(job_type, code, name, status, prompt, created_at) VALUES(?,?,?,?,?,?)",
        ("stock_deep_research", code, name, "running", prompt, created_at),
    )
    job_id = cur.lastrowid
    conn.execute("UPDATE ai_jobs SET started_at=? WHERE id=?", (created_at, job_id))
    conn.commit()
    return job_id


def analyze(args):
    code = norm_code(args.code)
    if not code:
        raise SystemExit("ERROR: --code is required")
    name = args.name or code
    conn = connect(Path(args.db) if args.db else DB_PATH)
    ensure_schema(conn)
    prompt = build_prompt(conn, code, name)
    job_id = create_job(conn, code, name, prompt)
    try:
        text, model = chat_completion(prompt)
        report_id = save_report(conn, code, name, text, model, job_id)
    except Exception as exc:
        conn.execute(
            "UPDATE ai_jobs SET status='failed', error=?, finished_at=? WHERE id=?",
            (str(exc), now_text(), job_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()
    print(f"AI report saved: id={report_id} code={code} name={name}")


def latest_from_db(args):
    conn = connect(Path(args.db) if args.db else DB_PATH)
    ensure_schema(conn)
    if "stock_picks" not in table_names(conn):
        raise SystemExit("ERROR: stock_picks table not found")
    limit = max(1, int(args.limit))
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT code, name, MAX(picked_at) picked_at, MAX(score) score
               FROM stock_picks
               WHERE code IS NOT NULL
               GROUP BY code, name
               ORDER BY picked_at DESC, score DESC
               LIMIT ?""",
            (limit,),
        )
    ]
    conn.close()
    for row in rows:
        sub = argparse.Namespace(code=row["code"], name=row["name"], db=args.db)
        analyze(sub)


def main():
    parser = argparse.ArgumentParser(description="Generate AI research reports for stock radar signals")
    parser.add_argument("--db", default=str(DB_PATH), help="path to invest.db")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure")
    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("--code", required=True)
    p_analyze.add_argument("--name")
    p_latest = sub.add_parser("latest")
    p_latest.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.cmd == "ensure":
        conn = connect(Path(args.db))
        ensure_schema(conn)
        conn.close()
        print("AI research schema ready")
    elif args.cmd == "analyze":
        analyze(args)
    elif args.cmd == "latest":
        latest_from_db(args)


if __name__ == "__main__":
    main()
