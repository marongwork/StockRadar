#!/usr/bin/env python3
"""选股雷达 v4：动态主题 + 趋势事件交易法
- 先扫当天强势概念和全市场资金动量，不再固定 5 个主线
- 入池硬约束：50-500 亿市值，非 ST/停牌，有事件催化或资金确认
- 评分：产业趋势 + 事件强度 + 公司卡位/成长 + 资金确认 + 交易位置
- 交易计划：试仓 3%，突破/回踩再加；-7% 止损，+10%/+20% 分批止盈
- 飞书推送：设了 FEISHU_WEBHOOK 就推 TOP5
- 盘前复核：--premarket 查候选开盘、标异动
用法:
  python3 stock_picker.py             # 盘后全流程 + 写库 + 重生成 + (可选)飞书
  python3 stock_picker.py --premarket # 盘前复核候选开盘
  python3 stock_picker.py --print     # 只打印不写库
"""
import os, json, secrets, urllib.request, urllib.parse, re, sqlite3, subprocess, sys
from pathlib import Path
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent_council import apply_candidate_review

ENDPOINT = "https://openapi.iwencai.com/v1/query2data"
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "invest.db"
GEN_PY = SCRIPT_DIR / "generate.py"
STOCK_LAB_PY = SCRIPT_DIR / "generate_stock_lab.py"
P2_PAGES_PY = SCRIPT_DIR / "generate_p2_pages.py"
LARK_CLI = "/home/ubuntu/.npm-global/bin/lark-cli"
TREND_THEMES = {
    "固态电池": ["固态电池", "硫化物", "锂电材料", "正极材料", "电解质"],
    "机器人": ["机器人", "减速器", "伺服", "传感器", "特斯拉 Optimus"],
    "国产存储": ["国产存储", "长鑫", "长江存储", "HBM", "封测"],
    "AI算力": ["AI算力", "CPO", "光模块", "服务器", "液冷", "PCB"],
    "脑机接口": ["脑机接口", "神经调控", "脑科学", "医疗器械"],
}
CORE_WATCHLIST = [
    ("603200", "上海洗霸", "固态电池"), ("002173", "创新医疗", "脑机接口"),
    ("301293", "三博脑科", "脑机接口"), ("300073", "当升科技", "固态电池"),
    ("002472", "双环传动", "机器人"), ("600667", "太极实业", "国产存储"),
]
MIN_CAP = 3e9
IDEAL_CAP = 3e10
MAX_CAP = 8e10
LIMIT_UP_MIN_SCORE = 70
TREND_MIN_SCORE = 70
FALLBACK_TREND_MIN_SCORE = 50
EXECUTABLE_MIN_SCORE = 70
EXECUTABLE_K50_MIN_SCORE = 70
DYNAMIC_THEME_LIMIT = 10
IWENCAI_DAILY_SKILL_LIMIT = int(os.environ.get("IWENCAI_DAILY_SKILL_LIMIT", "420"))
RECOMMEND_COOLDOWN_DAYS = 5
MAX_REPEAT_SIGNALS_IN_COOLDOWN = 2
WEAK_THEME_MIN_TRADES = 3
WEAK_THEME_WIN_RATE = 25.0
WEAK_THEME_AVG_RETURN = 0.0
WEAK_STRATEGY_MIN_TRADES = 8
WEAK_STRATEGY_EXPECTANCY = 0.0
WEAK_STRATEGY_MAX_DRAWDOWN = -30.0
HARD_BLOCK_TAGS = ["⚠️高PE", "⚠️估值异常", "⚠️解禁", "⚠️减持", "⚠️监管", "⚠️舆情利空", "⚠️北交所", "⚠️趋势熔断", "⚠️策略样本不足", "⚠️Agent风险否决"]
WATCH_ONLY_TAGS = ["等转强", "⚠️破位", "⚠️资金流出"]
MANUAL_THEME_COOLDOWN = {}
ANCHOR_THEMES = list(TREND_THEMES)  # 兼容旧函数

def load_key():
    k = os.environ.get("IWENCAI_API_KEY", "").strip()
    if k: return k
    for p in ["~/.zshrc", "/home/ubuntu/.iwencai_api_key",
              "/var/www/mazhi.icu/invest/.iwencai_key", str(SCRIPT_DIR / ".iwencai_key")]:
        try:
            if p.endswith("zshrc") or p.endswith("bashrc"):
                for line in open(os.path.expanduser(p)):
                    m = re.search(r'export\s+IWENCAI_API_KEY=["\']?([^"\'\n]+)', line)
                    if m: return m.group(1).strip()
            else:
                v = open(p).read().strip()
                if v: return v
        except Exception: pass
    return ""

def reserve_iwencai_call(skill):
    """按 skill 预留调用次数，保留约 16% 的官方日额度作为安全余量。"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.execute("""CREATE TABLE IF NOT EXISTS iwencai_daily_usage (
            usage_date TEXT NOT NULL, skill TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, PRIMARY KEY (usage_date, skill)
        )""")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        today = date.today().isoformat()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR IGNORE INTO iwencai_daily_usage(usage_date,skill,calls,updated_at) VALUES(?,?,0,?)",
            (today, skill, now),
        )
        updated = conn.execute(
            """UPDATE iwencai_daily_usage SET calls=calls+1,updated_at=?
               WHERE usage_date=? AND skill=? AND calls<?""",
            (now, today, skill, IWENCAI_DAILY_SKILL_LIMIT),
        ).rowcount
        conn.commit()
        return updated == 1
    except sqlite3.Error as exc:
        if conn:
            conn.rollback()
        print(f"!! 问财额度计数失败，保护性跳过 {skill}: {exc}")
        return False
    finally:
        if conn:
            conn.close()


def iw_query(query, skill="hithink-astock-selector", limit="15", retry=False):
    key = load_key()
    if not key: print("!! 无 IWENCAI_API_KEY"); return []
    if not reserve_iwencai_call(skill):
        print(f"!! {skill} 已达到每日安全上限 {IWENCAI_DAILY_SKILL_LIMIT} 次，跳过")
        return []
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "X-Claw-Call-Type": "retry" if retry else "normal", "X-Claw-Skill-Id": skill.encode("utf-8") if not skill.isascii() else skill,
        "X-Claw-Skill-Version": "1.0.0", "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none", "X-Claw-Trace-Id": secrets.token_hex(32)}
    body = json.dumps({"query": query, "page": "1", "limit": limit,
                       "is_cache": "1", "expand_index": "true"}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return (json.loads(r.read().decode())).get("datas") or []
    except Exception as e:
        print(f"   query 失败: {e}"); return []


def fetch_public_fund_flow_pool(limit=300):
    """东方财富公开行情资金流榜；分钟级估算数据，不代表交易所识别的主力身份。"""
    params = {
        "pn": "1", "pz": str(limit), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f62", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f5,f6,f8,f9,f10,f20,f62,f100,f184",
    }
    url = "http://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            rows = (json.loads(response.read().decode("utf-8", "ignore")).get("data") or {}).get("diff") or []
    except Exception as exc:
        print(f"   东方财富公开资金流接口失败: {exc}")
        return []
    result = []
    for row in rows:
        result.append({
            "股票代码": row.get("f12"), "股票简称": row.get("f14"), "最新价": row.get("f2"),
            "最新涨跌幅": row.get("f3"), "成交量": row.get("f5"), "成交额": row.get("f6"),
            "换手率": row.get("f8"), "市盈率": row.get("f9"), "量比": row.get("f10"),
            "总市值": row.get("f20"), "主力资金净流入": row.get("f62"),
            "所属行业": row.get("f100"), "主力净流入占比": row.get("f184"),
            "资金数据源": "东方财富公开行情估算",
        })
    return result


SECTOR_NOISE_RE = re.compile(
    r"昨日|最近|连板|涨停|跌停|首板|炸板|百元股|破净股|机构重仓|融资融券|沪股通|深股通|"
    r"MSCI|标普|富时|预盈|预增|预亏|转债标的|次新股|ST股|AH股|证金持股|漂亮100|中特估100|"
    r"大盘股|中盘股|小盘股|科技风格|价值风格|成长风格"
)


def fetch_public_sector_fund_flow(limit=30):
    """东方财富概念/行业板块资金榜，不把个股资金榜冒充板块榜。"""
    fields = "f12,f14,f2,f3,f6,f62,f104,f105,f128,f140,f141,f184"
    rows = []
    for board_type, market_filter in (("概念", "m:90+t:3"), ("行业", "m:90+t:2")):
        for order in ("1", "0"):
            params = {
                "pn": "1", "pz": str(limit), "po": order, "np": "1", "fltt": "2", "invt": "2",
                "fid": "f62", "fs": market_filter, "fields": fields,
            }
            url = "http://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/center/boardlist.html",
            })
            try:
                with urllib.request.urlopen(req, timeout=12) as response:
                    diff = (json.loads(response.read().decode("utf-8", "ignore")).get("data") or {}).get("diff") or []
            except Exception as exc:
                print(f"   东方财富{board_type}板块资金接口失败: {exc}")
                continue
            for row in diff:
                name = str(row.get("f14") or "").strip()
                if not name or (board_type == "概念" and SECTOR_NOISE_RE.search(name)):
                    continue
                rows.append({
                    "板块代码": row.get("f12"), "板块名称": name, "板块类型": board_type,
                    "最新价": row.get("f2"), "涨跌幅": row.get("f3"), "成交额": row.get("f6"),
                    "主力资金净流入": row.get("f62"), "主力净流入占比": row.get("f184"),
                    "上涨家数": row.get("f104"), "下跌家数": row.get("f105"),
                    "领涨股": row.get("f128"), "领涨股代码": row.get("f140"),
                    "领涨股涨跌幅": row.get("f141"), "资金数据源": "东方财富板块资金估算",
                })
    # 同名概念/行业只保留净流入更强的一条，最终按资金净流入排序。
    unique = {}
    for row in rows:
        name = row["板块名称"]
        flow = parse_cn_number(row.get("主力资金净流入")) or 0
        old_flow = parse_cn_number((unique.get(name) or {}).get("主力资金净流入")) or float("-inf")
        if name not in unique or flow > old_flow:
            unique[name] = row
    return sorted(unique.values(), key=lambda x: parse_cn_number(x.get("主力资金净流入")) or 0, reverse=True)


def normalize_iwencai_sector_rows(datas, board_type):
    normalized = []
    for row in datas or []:
        name = row.get("指数简称") or row.get("板块名称") or row.get("概念名称") or row.get("名称")
        name = str(name or "").strip()
        if not name or (board_type == "概念" and SECTOR_NOISE_RE.search(name)):
            continue
        main_net = num(row, "主力净买入额", "主力资金净流入")
        if main_net is None:
            outflow = num(row, "主力净流出额", "主力净卖出额")
            main_net = -abs(outflow) if outflow is not None else None
        normalized.append({
            "板块代码": row.get("指数代码") or row.get("板块代码"),
            "板块名称": name, "板块类型": board_type,
            "最新价": num(row, "最新价"), "涨跌幅": num(row, "涨跌幅"),
            "成交额": num(row, "成交额"),
            "主力资金净流入": main_net,
            "主力净流入占比": num(row, "主力净流入占比"),
            "上涨家数": num(row, "上涨家数"), "下跌家数": num(row, "下跌家数"),
            "领涨股": row.get("领涨股") or row.get("领涨股票"),
            "资金数据源": "同花顺问财板块技能",
        })
    return normalized


def is_sector_snapshot_time(moment=None):
    """接收盘前集合竞价和 A 股连续竞价时段快照。"""
    moment = moment or datetime.now()
    if moment.weekday() >= 5:
        return False
    minute = moment.hour * 60 + moment.minute
    return 9 * 60 + 15 <= minute <= 9 * 60 + 25 or 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60


def sector_snapshot_phase(moment=None):
    moment = moment or datetime.now()
    minute = moment.hour * 60 + moment.minute
    return "preopen" if 9 * 60 + 15 <= minute <= 9 * 60 + 25 else "trading"


def save_public_sector_snapshot():
    """轻量保存大板块资金快照；东方财富失败时才消耗问财额度。"""
    if not is_sector_snapshot_time():
        print("当前不在 A 股交易时段，跳过板块分钟快照")
        return False
    rows = fetch_public_sector_fund_flow(limit=40)
    if not rows:
        print("东方财富板块接口不可用，回退问财板块技能")
        results = parallel_iw_queries([
            ("今日概念板块主力资金净流入排名前20，涨跌幅，成交额，领涨股", "hithink-sector-selector", "20"),
            ("今日概念板块主力资金净流出排名前15，涨跌幅，成交额，领涨股", "hithink-sector-selector", "15"),
            ("今日行业板块主力资金净流入排名前15，涨跌幅，成交额，领涨股", "hithink-sector-selector", "15"),
            ("今日行业板块主力资金净流出排名前10，涨跌幅，成交额，领涨股", "hithink-sector-selector", "10"),
        ], max_workers=4)
        rows = (normalize_iwencai_sector_rows(results[0], "概念") + normalize_iwencai_sector_rows(results[1], "概念") +
                normalize_iwencai_sector_rows(results[2], "行业") + normalize_iwencai_sector_rows(results[3], "行业"))
    if not rows:
        print("板块资金接口均未返回数据，跳过快照")
        return False
    def balanced(board_type, inflow_count, outflow_count):
        selected = {}
        for row in rows:
            if row.get("板块类型") != board_type:
                continue
            selected[row.get("板块名称")] = row
        ranked = sorted(selected.values(), key=lambda x: parse_cn_number(x.get("主力资金净流入")) or 0, reverse=True)
        result = []
        seen = set()
        for row in ranked[:inflow_count] + ranked[-outflow_count:]:
            if row.get("板块名称") not in seen:
                result.append(row)
                seen.add(row.get("板块名称"))
        return result
    concepts = balanced("概念", 12, 8)
    industries = balanced("行业", 10, 5)
    combined = sorted(concepts + industries, key=lambda x: abs(parse_cn_number(x.get("主力资金净流入")) or 0), reverse=True)[:25]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS sector_fund_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at TEXT NOT NULL,
        concept_json TEXT NOT NULL,
        industry_json TEXT NOT NULL,
        combined_json TEXT NOT NULL,
        source TEXT NOT NULL,
        phase TEXT NOT NULL DEFAULT 'trading'
    )""")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sector_fund_snapshots)")}
    if "phase" not in cols:
        conn.execute("ALTER TABLE sector_fund_snapshots ADD COLUMN phase TEXT NOT NULL DEFAULT 'trading'")
    snapshot_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    phase = sector_snapshot_phase()
    source = "iwencai_sector_skill" if any("问财" in str(row.get("资金数据源")) for row in rows) else "eastmoney_board_flow"
    conn.execute(
        "INSERT INTO sector_fund_snapshots (snapshot_at,concept_json,industry_json,combined_json,source,phase) VALUES (?,?,?,?,?,?)",
        (snapshot_at, json.dumps(concepts, ensure_ascii=False), json.dumps(industries, ensure_ascii=False),
         json.dumps(combined, ensure_ascii=False), source, phase),
    )
    conn.commit(); conn.close()
    print(f"已写入大板块资金快照 {snapshot_at}：概念{len(concepts)} / 行业{len(industries)}")
    return True

def parallel_iw_queries(tasks, max_workers=8):
    """并行执行多个 iw_query 调用。tasks: list of (query, skill, limit) tuples.
    返回与 tasks 顺序一致的结果列表。"""
    results = [None] * len(tasks)
    def _run(idx, query, skill, limit):
        return idx, iw_query(query, skill=skill, limit=limit)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run, i, q, s, l): i for i, (q, s, l) in enumerate(tasks)}
        for fut in as_completed(futures):
            try:
                idx, data = fut.result()
                results[idx] = data
            except Exception:
                results[futures[fut]] = []
    return [r if r is not None else [] for r in results]

def parse_cn_number(v):
    """Parse iwencai numeric cells, preserving 万/亿 units as yuan-like values."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s in {"-", "--", "None", "nan"}:
        return None
    multiplier = 1.0
    if "%" in s:
        s = s.replace("%", "")
    if "万亿" in s:
        multiplier = 1e12
        s = s.replace("万亿", "")
    elif "亿" in s:
        multiplier = 1e8
        s = s.replace("亿元", "").replace("亿", "")
    elif "万" in s:
        multiplier = 1e4
        s = s.replace("万元", "").replace("万", "")
    s = re.sub(r"[^\d.\-+]", "", s)
    if s in {"", "-", "+", "."}:
        return None
    try:
        return float(s) * multiplier
    except Exception:
        return None

def parse_date_value(v):
    s = str(v or "")
    m = re.search(r"(20\d{2})[-年/]?(\d{1,2})[-月/]?(\d{1,2})", s)
    if not m:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None

def num(item, *kws):
    for k, v in item.items():
        if any(kw in str(k) for kw in kws):
            parsed = parse_cn_number(v)
            if parsed is not None:
                return parsed
    return None

def num_key(item, *kws):
    for k, v in item.items():
        if any(kw in str(k) for kw in kws):
            parsed = parse_cn_number(v)
            if parsed is not None:
                return parsed, str(k)
    return None, ""

def fmt(v):
    if v is None: return "-"
    if abs(v) >= 1e8: return f"{v/1e8:.1f}亿"
    if abs(v) >= 1e4: return f"{v/1e4:.1f}万"
    return f"{v:.2f}" if abs(v) < 1000 else f"{v:.0f}"

def round_pct(v):
    try:
        return round(float(v), 2)
    except Exception:
        return None

def fmt_pct(v, decimals=2):
    pct = round_pct(v)
    return "-" if pct is None else f"{pct:+.{decimals}f}%"

def first_present(*values):
    for v in values:
        if v is not None:
            return v
    return None

def get_hot_sectors():
    """L1: sector-selector 抓当日资金流入+涨幅靠前的概念板块。"""
    datas = iw_query("概念板块，今日涨幅居前，近5日主力资金净流入前20",
                     skill="hithink-sector-selector", limit="30")
    out = []
    for it in datas:
        nm = it.get("指数简称") or it.get("板块名称") or it.get("概念名称") or it.get("名称") or ""
        if nm and nm not in out:
            out.append(nm)
    return out

def compute_theme_weights(themes):
    """自学习：按主题历史已实现盈亏(transactions × stock_picks)调权。无数据默认1.0"""
    w = {t: 1.0 for t in themes}
    try:
        conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
        # 每个主题的候选 code 集合
        theme_codes = {t: set() for t in themes}
        for r in conn.execute("SELECT theme, code FROM stock_picks"):
            if r["theme"] in theme_codes and r["code"]:
                theme_codes[r["theme"]].add(r["code"])
        # transactions 每 code 的 rough realized = Σsell金额 - Σbuy金额
        code_pnl = {}
        for r in conn.execute("SELECT code, action, price, qty FROM transactions"):
            c = r["code"]; amt = (r["price"] or 0) * (r["qty"] or 0)
            if not c: continue
            code_pnl.setdefault(c, 0)
            code_pnl[c] += amt if (r["action"] or "").lower() == "sell" else -amt
        conn.close()
        for t, codes in theme_codes.items():
            pnls = [code_pnl[c] for c in codes if c in code_pnl]
            if pnls:
                avg = sum(pnls) / len(pnls)
                w[t] = max(0.7, min(1.3, 1.0 + avg / 50000))  # 每5万均盈→+0.1, clamp
    except Exception: pass
    return w

def str_field(item, *kws):
    """模糊找含关键词的列，返回字符串值"""
    for k, v in item.items():
        if any(kw in str(k) for kw in kws) and v not in (None, ""):
            return str(v)
    return ""


def normalize_board_text(value, limit=8):
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [str(v).strip() for v in value]
    else:
        text = str(value).replace("['", "").replace("']", "").replace('"', "")
        parts = re.split(r"[,，;；|/、]+", text)
    result = []
    for part in parts:
        part = part.strip(" []'\t\r\n")
        if part and part not in result and part not in {"-", "--"}:
            result.append(part)
    return "、".join(result[:limit])


def board_fields(item, fallback_concept=""):
    industry = ""
    concepts = ""
    for key, value in item.items():
        label = str(key)
        if not industry and any(k in label for k in ("所属同花顺行业", "所属行业", "行业名称")):
            industry = normalize_board_text(value, limit=2)
        if not concepts and any(k in label for k in ("所属概念", "概念板块", "概念名称")):
            concepts = normalize_board_text(value)
    if not concepts and fallback_concept and fallback_concept not in {"全市场强势", "涨停板", "券商金股", "科创50"}:
        concepts = normalize_board_text(fallback_concept)
    return industry, concepts

def is_fresh_report(item, grace_days=7):
    """券商金股只使用仍在有效期内、或刚过期不超过 grace_days 的报告。"""
    end_date = parse_date_value(str_field(item, "截止日期", "结束日期", "有效期至", "截止"))
    if not end_date:
        return True
    return end_date >= date.today() - timedelta(days=grace_days)

def explicit_stock_recommendation(item, name):
    """过滤月度策略里顺带带出的股票，保留摘要明确提及该股的推荐。"""
    text = text_blob(item)
    recommend_words = ["重点推荐", "金股", "推荐标的", "首推", "建议关注", "重点关注", "维持推荐"]
    if not any(w in text for w in recommend_words):
        return False
    if name and name in text:
        return True
    summary = str_field(item, "报告摘要", "摘要", "推荐理由", "投资要点", "核心观点")
    return bool(name and name in summary)

def normalize_code(code):
    m = re.search(r"(\d{6})", str(code or ""))
    return m.group(1) if m else str(code or "").strip()


def is_beijing_code(code):
    """北交所现行92号段及历史4/8号段一律排除。"""
    return normalize_code(code).startswith(("4", "8", "92"))

def append_tags(base, tags, limit=14):
    seen = []
    for t in (base or "").split("+") + list(tags):
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return "+".join(seen[:limit]) or "—"

def remove_reason_tags(reason, prefixes):
    out = []
    for tag in (reason or "").split("+"):
        tag = tag.strip()
        if not tag:
            continue
        if any(tag.startswith(prefix) for prefix in prefixes):
            continue
        out.append(tag)
    return "+".join(out)

def fund_grade(profile):
    """资金可信度：A/B 可参与，C 需验证，D/E 只观察或剔除。"""
    score = 50
    tags = []
    main = profile.get("main_net")
    super_net = profile.get("super_net")
    big_net = profile.get("big_net")
    lobby_net = profile.get("lobby_net")
    seal = profile.get("seal_amount")
    breaks = profile.get("break_count")
    turnover = profile.get("turnover")
    board = profile.get("board_count")

    if main is not None:
        if main > 1e8:
            score += 20; tags.append("主力强流入")
        elif main > 0:
            score += 12; tags.append("主力流入")
        else:
            score -= 18; tags.append("主力流出")
    if super_net is not None:
        if super_net > 5e7:
            score += 14; tags.append("超大单确认")
        elif super_net < 0:
            score -= 14; tags.append("超大单流出")
    if big_net is not None:
        if big_net > 3e7:
            score += 8; tags.append("大单承接")
        elif big_net < 0:
            score -= 8; tags.append("大单流出")
    if lobby_net is not None:
        if lobby_net > 0:
            score += 16; tags.append("龙虎榜机构净买")
        elif lobby_net < 0:
            score -= 14; tags.append("龙虎榜机构净卖")
    if seal is not None:
        if seal >= 3e8:
            score += 14; tags.append("强封单")
        elif seal >= 8e7:
            score += 8; tags.append("有封单")
        elif seal > 0:
            score -= 10; tags.append("封单偏弱")
    if breaks is not None:
        if breaks <= 2:
            score += 8; tags.append("封板稳")
        elif breaks >= 10:
            score -= 18; tags.append("炸板多")
        else:
            score -= 8; tags.append("多次开板")
    if turnover is not None:
        if 3 <= turnover <= 20:
            score += 5; tags.append("换手健康")
        elif turnover > 35:
            score -= 8; tags.append("换手过热")
    if board is not None and board >= 4:
        score -= 8; tags.append("高位连板")

    score = max(0, min(100, round(score)))
    if score >= 82:
        grade = "A"
    elif score >= 68:
        grade = "B"
    elif score >= 52:
        grade = "C"
    elif score >= 35:
        grade = "D"
    else:
        grade = "E"
    return grade, score, "+".join(tags[:8]) or "资金数据不足"

def attach_fund_profile(p, item=None, **overrides):
    item = item or {}
    profile = {
        "main_net": first_present(overrides.get("main_net"), p.get("main"), p.get("main_net"), num(item, "主力资金净流入", "主力净流入", "主力资金", "资金净流入")),
        "super_net": first_present(overrides.get("super_net"), num(item, "超大单净流入", "超大单净额", "超大单资金")),
        "big_net": first_present(overrides.get("big_net"), num(item, "大单净流入", "大单净额", "大单资金")),
        "lobby_net": first_present(overrides.get("lobby_net"), num(item, "机构净买入", "机构买入净额", "机构净额", "龙虎榜净买入")),
        "seal_amount": first_present(overrides.get("seal_amount"), num(item, "封单金额", "封板资金", "封单资金", "涨停封单")),
        "break_count": first_present(overrides.get("break_count"), num(item, "炸板次数", "开板次数")),
        "turnover": first_present(overrides.get("turnover"), num(item, "换手率")),
        "board_count": first_present(overrides.get("board_count"), num(item, "连续涨停天数", "连板数", "几连板", "涨停天数")),
    }
    if profile["seal_amount"] is not None and 0 < profile["seal_amount"] < 10000:
        profile["seal_amount"] *= 1e8
    grade, score, tags = fund_grade(profile)
    p.update(profile)
    p["main"] = profile["main_net"]
    p["fund_grade"] = grade
    p["fund_score"] = score
    p["fund_tags"] = tags
    return p

def apply_fund_score(p):
    grade = p.get("fund_grade")
    p["reason"] = remove_reason_tags(
        p.get("reason", ""),
        ["资金A档", "资金B档", "⚠️资金D档", "⚠️资金E档"],
    )
    if grade == "A":
        p["score"] = round(p.get("score", 0) + 12, 1)
        p["reason"] = append_tags(p.get("reason", ""), ["资金A档"])
    elif grade == "B":
        p["score"] = round(p.get("score", 0) + 6, 1)
        p["reason"] = append_tags(p.get("reason", ""), ["资金B档"])
    elif grade == "D":
        p["score"] = round(p.get("score", 0) - 12, 1)
        p["reason"] = append_tags(p.get("reason", ""), ["⚠️资金D档"])
    elif grade == "E":
        p["score"] = round(p.get("score", 0) - 22, 1)
        p["reason"] = append_tags(p.get("reason", ""), ["⚠️资金E档", "观察不买"])
    return p

def split_market_terms(value):
    text = str(value or "")
    for ch in "[]'\"，,;；|/、()（）":
        text = text.replace(ch, " ")
    bad = {"概念", "同花顺", "今日", "相关", "股份", "有限", "证券"}
    terms = []
    for raw in text.split():
        t = raw.strip()
        if len(t) < 2 or len(t) > 14:
            continue
        if any(b in t for b in bad):
            continue
        terms.append(t)
    return terms

def load_market_fund_context():
    ctx = {"hot_codes": set(), "lhb_codes": set(), "hot_terms": set(), "lhb_names": set(), "emotion_score": None, "weak_market": False, "rotating_sectors": set(), "sentiment_phase": "neutral", "sector_metrics": {}}
    if not DB_PATH.exists():
        return ctx
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM market_fund_snapshots ORDER BY snapshot_at DESC, id DESC LIMIT 3").fetchall()
        if not rows:
            conn.close(); return ctx
        row = rows[0]
        def loads(r, col):
            try:
                return json.loads(r[col] or "[]") if col in r.keys() else []
            except Exception:
                return []
        market = loads(row, "market_json")
        sectors = loads(row, "sectors_json")
        sector_history = []
        table_names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sector_fund_snapshots" in table_names:
            sector_rows = conn.execute(
                "SELECT combined_json FROM sector_fund_snapshots ORDER BY snapshot_at DESC,id DESC LIMIT 3"
            ).fetchall()
            sector_history = [loads(r, "combined_json") for r in sector_rows]
            if sector_history:
                sectors = sector_history[0]
        lhb = loads(row, "lhb_json") or loads(row, "lhb_recent_json")
        if market:
            m = market[0]
            up = num(m, "上涨家数") or 0
            down = num(m, "下跌家数") or 0
            limit_up = num(m, "涨停家数") or 0
            limit_down = num(m, "跌停家数") or 0
            breadth = up / max(1, up + down) * 100
            limit_ratio = limit_up / max(1, limit_up + limit_down) * 100
            emotion = max(0, min(100, round(breadth * 0.55 + limit_ratio * 0.35)))
            ctx["emotion_score"] = emotion
            ctx["weak_market"] = emotion < 45 or down > up * 1.4
            # 情绪周期判断
            if emotion >= 75:
                ctx["sentiment_phase"] = "euphoria"  # 过热，减仓
            elif emotion >= 55:
                ctx["sentiment_phase"] = "warm"  # 偏暖，正常
            elif emotion >= 40:
                ctx["sentiment_phase"] = "neutral"  # 中性
            else:
                ctx["sentiment_phase"] = "cold"  # 冰点，轻仓
        for r in sectors[:25]:
            board_name = str_field(r, "指数简称", "板块名称", "概念名称", "所属概念")
            board_flow = num(r, "主力资金流向", "主力资金净流入")
            if board_name:
                ctx["sector_metrics"][board_name] = {
                    "chg": num(r, "涨跌幅"),
                    "main_net": board_flow,
                }
            if (board_flow or 0) <= 0:
                continue
            code = normalize_code(r.get("股票代码") or r.get("code") or "")
            if code:
                ctx["hot_codes"].add(code)
            for key in ("概念龙头", "所属概念", "行业", "指数简称", "板块名称"):
                ctx["hot_terms"].update(split_market_terms(r.get(key)))
        for r in lhb[:40]:
            code = normalize_code(r.get("股票代码") or r.get("code") or "")
            if code:
                ctx["lhb_codes"].add(code)
            name = r.get("股票简称") or r.get("股票名称")
            if name:
                ctx["lhb_names"].add(str(name))
            for key in ("所属概念", "知名游资营业部", "营业部"):
                ctx["hot_terms"].update(split_market_terms(r.get(key)))
        # 板块轮动检测：连续2次以上出现在资金流入TOP的板块
        if len(sector_history) >= 2:
            prev_sector_terms = set()
            for prev_sectors in sector_history[1:]:
                for r in prev_sectors[:10]:
                    for key in ("板块名称", "指数简称", "所属概念"):
                        prev_sector_terms.update(split_market_terms(r.get(key)))
            # 当前热门板块与历史重叠 = 轮动持续
            ctx["rotating_sectors"] = ctx["hot_terms"] & prev_sector_terms
        conn.close()
    except Exception:
        pass
    return ctx

def apply_market_fund_context(p, ctx):
    if not ctx:
        return p
    code = normalize_code(p.get("code"))
    text = f"{p.get('name','')} {p.get('theme','')} {p.get('industry','')} {p.get('concepts','')} {p.get('reason','')}"
    tags = []
    bonus = 0
    if code in ctx.get("hot_codes", set()):
        bonus += 14; tags.append("大盘主力TOP")
    if code in ctx.get("lhb_codes", set()) or p.get("name") in ctx.get("lhb_names", set()):
        bonus += 8; tags.append("近5日龙虎榜")
    matched_terms = [t for t in ctx.get("hot_terms", set()) if t and t in text]
    if matched_terms:
        bonus += min(10, 4 + len(matched_terms) * 2)
        tags.append("大盘题材共振")
    for board_name, metrics in ctx.get("sector_metrics", {}).items():
        if board_name and board_name in text:
            p["sector_chg"] = metrics.get("chg")
            p["sector_main_net"] = metrics.get("main_net")
            break
    # 板块轮动加分：属于连续多日资金流入的板块
    rotating = [t for t in ctx.get("rotating_sectors", set()) if t and t in text]
    if rotating:
        bonus += 6; tags.append("板块轮动持续")
    if ctx.get("weak_market"):
        if p.get("fund_grade") not in ("A", "B"):
            bonus -= 8; tags.append("⚠️弱市资金不足")
        else:
            tags.append("弱市资金确认")
    # 情绪周期仓位建议
    phase = ctx.get("sentiment_phase", "neutral")
    if phase == "euphoria":
        p["position_hint"] = "试仓减半(1.5%)"
        tags.append("⚠️情绪过热减仓")
    elif phase == "cold":
        p["position_hint"] = "轻仓试错(1.5%)"
        tags.append("冰点轻仓")
    else:
        p["position_hint"] = "标准试仓(3%)"
    if bonus:
        p["score"] = round(p.get("score", 0) + bonus, 1)
    if tags:
        if ctx.get("emotion_score") is not None:
            tags.append(f"情绪{ctx['emotion_score']}")
        p["reason"] = append_tags(p.get("reason", ""), tags)
    return p

def mark_repeat_cooldown(p, ctx):
    """Apply repeat metadata after every merge so cooldown cannot be overwritten.
    新增：渐进式信号衰减，每重复一次扣4分（最多扣12分），而不只是二元剔除。"""
    code = normalize_code(p.get("code"))
    recent = ctx.get("recent_codes", {}).get(code)
    if not recent:
        return p
    count = int(recent.get("count", 1) or 1)
    p["_recent_pick_count"] = count
    # 渐进衰减：重复出现时评分递减
    if count >= 1:
        decay = min(12, count * 4)
        p["score"] = round(p.get("score", 0) - decay, 1)
        p["reason"] = append_tags(p.get("reason", ""), [f"重复衰减-{decay}分"])
    p["reason"] = append_tags(
        p.get("reason", ""),
        [f"⚠️{RECOMMEND_COOLDOWN_DAYS}日内已推{count}次"],
    )
    if count >= MAX_REPEAT_SIGNALS_IN_COOLDOWN:
        p["reason"] = append_tags(p["reason"], ["重复冷却剔除"])
    return p

def text_blob(item):
    return " ".join(str(v) for v in item.values() if v not in (None, ""))

def has_any(text, words):
    return any(w in (text or "") for w in words)

def broad_theme(theme):
    text = theme or ""
    for t in TREND_THEMES:
        if t in text:
            return t
    return "涨停板" if "涨停" in text else text

def is_limit_up_pick(p):
    try:
        chg = p.get("chg")
        if chg is None:
            chg = p.get("chg_pct")
        if chg is not None and float(chg) >= 9.5:
            return True
    except Exception:
        pass
    return "涨停短线" in (p.get("reason") or "") or "涨停板" in (p.get("theme") or "")

def is_qualified_limit_up_pick(p):
    reason = p.get("reason") or ""
    strong_structure = "封板稳" in reason and any(t in reason for t in ("强封单", "有封单"))
    return is_limit_up_pick(p) and strong_structure and p.get("fund_grade") in ("A", "B")

def load_signal_context():
    """Historical guardrails: cooldown repeated codes and penalize weak recent themes."""
    ctx = {"recent_codes": {}, "weak_themes": {}, "weak_strategies": {}, "unqualified_strategies": {}, "today": date.today().isoformat()}
    if not DB_PATH.exists():
        return ctx
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cutoff = (date.today() - timedelta(days=RECOMMEND_COOLDOWN_DAYS)).isoformat()
        for r in conn.execute("""
            SELECT code, MAX(picked_date) last_date, COUNT(*) cnt
            FROM stock_picks
            WHERE picked_date >= ? AND code IS NOT NULL
            GROUP BY code
        """, (cutoff,)):
            ctx["recent_codes"][normalize_code(r["code"])] = {"last_date": r["last_date"], "count": r["cnt"]}

        trades = []
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_trades'").fetchone():
            bt_cutoff = (date.today() - timedelta(days=14)).isoformat()
            trades = [dict(r) for r in conn.execute("""
                SELECT bt.theme, bt.strategy, bt.return_pct
                FROM backtest_trades bt
                JOIN (
                    SELECT strategy, MAX(id) latest_run_id
                    FROM backtest_runs
                    GROUP BY strategy
                ) latest ON latest.latest_run_id=bt.run_id
                WHERE bt.signal_date >= ? AND bt.return_pct IS NOT NULL
            """, (bt_cutoff,))]
        if not trades:
            trades = [dict(r) for r in conn.execute("""
                SELECT theme, eval_return_pct AS return_pct
                FROM stock_picks
                WHERE picked_date >= date('now','-14 day')
                  AND eval_return_pct IS NOT NULL
            """)]
        theme_returns = {}
        for r in trades:
            t = broad_theme(r.get("theme") or r.get("strategy") or "")
            theme_returns.setdefault(t, []).append(float(r["return_pct"]))
        for t, vals in theme_returns.items():
            if len(vals) < WEAK_THEME_MIN_TRADES:
                continue
            win_rate = sum(v > 0 for v in vals) / len(vals) * 100
            avg_ret = sum(vals) / len(vals)
            if win_rate <= WEAK_THEME_WIN_RATE and avg_ret < WEAK_THEME_AVG_RETURN:
                ctx["weak_themes"][t] = {"win_rate": win_rate, "avg_return": avg_ret, "count": len(vals)}
        strategy_returns = {}
        for r in trades:
            strategy = r.get("strategy") or ""
            if strategy:
                strategy_returns.setdefault(strategy, []).append(float(r["return_pct"]))
        for strategy, vals in strategy_returns.items():
            if len(vals) < WEAK_STRATEGY_MIN_TRADES:
                continue
            wins = [v for v in vals if v > 0]
            losses = [abs(v) for v in vals if v < 0]
            win_rate = len(wins) / len(vals) * 100
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.0
            expectancy = len(wins) / len(vals) * avg_win - len(losses) / len(vals) * avg_loss
            payoff = avg_win / avg_loss if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0)
            if expectancy <= WEAK_STRATEGY_EXPECTANCY:
                ctx["weak_strategies"][strategy] = {
                    "win_rate": win_rate,
                    "avg_win": avg_win,
                    "avg_loss": avg_loss,
                    "payoff_ratio": payoff,
                    "expectancy": expectancy,
                    "count": len(vals),
                }
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_runs'").fetchone():
            for r in conn.execute("""
                SELECT br.strategy,br.trades,br.expectancy,br.qualified
                FROM backtest_runs br
                JOIN (SELECT strategy,MAX(id) latest_run_id FROM backtest_runs GROUP BY strategy) latest
                  ON latest.latest_run_id=br.id
            """):
                if int(r["trades"] or 0) < 30:
                    ctx["unqualified_strategies"][r["strategy"]] = {
                        "count": int(r["trades"] or 0), "expectancy": float(r["expectancy"] or 0),
                    }
        conn.close()
    except Exception:
        pass
    for t, note in MANUAL_THEME_COOLDOWN.items():
        ctx["weak_themes"].setdefault(t, {"win_rate": 0.0, "avg_return": -1.0, "count": 0, "note": note})
    return ctx

def apply_trade_discipline(p):
    """Keep the radar aligned with the account rules: no chasing, prefer liquid tech growth."""
    if p.get("_discipline_applied"):
        return p
    penalty = 0
    extra = []
    chg = p.get("chg")
    cap = p.get("cap")
    pe = p.get("pe")
    if chg is not None:
        if chg >= 9.8:
            penalty += 14
            extra.append("⚠️涨停勿追")
        elif chg >= 7:
            penalty += 8
            extra.append("⚠️高位")
        elif chg <= -7:
            penalty += 6
            extra.append("⚠️破位")
    if pe is not None and (pe <= 0 or pe > 90):
        penalty += 10
        extra.append("⚠️估值异常")
    if cap is not None and cap < 5e9:
        penalty += 8
        extra.append("⚠️小市值流动性")
    if is_beijing_code(p.get("code")):
        penalty += 100
        extra.append("⚠️北交所")
    if penalty:
        p["score"] = round(p.get("score", 0) - penalty, 1)
    if extra:
        p["reason"] = append_tags(p.get("reason", ""), extra)
    p["_discipline_applied"] = True
    return p

def apply_history_guardrails(p, ctx):
    """Turn recent failure patterns into score penalties before TOP selection."""
    if p.get("_history_guardrails_applied"):
        return p
    code = normalize_code(p.get("code"))
    reason = p.get("reason") or ""
    theme = broad_theme(p.get("theme"))
    if code in ctx.get("recent_codes", {}):
        before = p.get("reason", "")
        mark_repeat_cooldown(p, ctx)
        p["score"] = round(p.get("score", 0) - 28, 1)
        if before and p.get("reason") == before:
            p["reason"] = before
        reason = p["reason"]
    weak = ctx.get("weak_themes", {}).get(theme)
    if weak:
        p["score"] = round(p.get("score", 0) - 30, 1)
        note = weak.get("note") or f"主题近{weak.get('count', 0)}笔胜率{weak.get('win_rate', 0):.0f}%"
        p["reason"] = append_tags(reason, [f"⚠️弱主题:{note}"])
        reason = p["reason"]
    if is_limit_up_pick(p) and not is_qualified_limit_up_pick(p):
        p["score"] = min(round(p.get("score", 0) - 35, 1), 59)
        p["reason"] = append_tags(reason, ["⚠️弱涨停题材:仅AI算力/脑机接口样本内正期望", "观察不打板"])
        reason = p["reason"]
    if (
        (not is_limit_up_pick(p))
        and p.get("theme") not in {"券商金股", "科创50"}
        and ctx.get("weak_strategies", {}).get("trend")
    ):
        weak_strategy = ctx["weak_strategies"]["trend"]
        p["score"] = min(round(p.get("score", 0) - 35, 1), 59)
        p["reason"] = append_tags(
            reason,
            [f"⚠️趋势熔断:近{weak_strategy.get('count', 0)}笔EV{weak_strategy.get('expectancy', 0):+.2f}%"],
        )
        reason = p["reason"]
    if (
        (not is_limit_up_pick(p))
        and p.get("theme") not in {"券商金股", "科创50"}
        and ctx.get("unqualified_strategies", {}).get("trend")
    ):
        sample = ctx["unqualified_strategies"]["trend"]
        p["score"] = min(p.get("score", 0), 59)
        p["reason"] = append_tags(
            reason,
            [f"⚠️策略样本不足:最新{sample.get('count', 0)}笔/门槛30笔,EV{sample.get('expectancy', 0):+.2f}%"],
        )
        reason = p["reason"]
    if any(tag in reason for tag in HARD_BLOCK_TAGS):
        p["score"] = min(p.get("score", 0), 59)
        p["reason"] = append_tags(reason, ["观察不买"])
    elif any(tag in reason for tag in WATCH_ONLY_TAGS) and not is_limit_up_pick(p):
        p["score"] = round(p.get("score", 0) - 12, 1)
        p["reason"] = append_tags(reason, ["需放量转强"])
    p["_history_guardrails_applied"] = True
    return p

def should_suppress_repeated_observation(p):
    """Do not keep pushing the same stock during cooldown."""
    return int(p.get("_recent_pick_count") or 0) >= MAX_REPEAT_SIGNALS_IN_COOLDOWN

def filter_candidates(candidates, ctx, min_score, limit=None):
    out = []
    for p in candidates:
        if is_beijing_code(p.get("code")):
            continue
        apply_trade_discipline(p)
        apply_history_guardrails(p, ctx)
        if p.get("score", 0) >= min_score:
            out.append(p)
    out.sort(key=lambda x: -x["score"])
    return out[:limit] if limit else out

def score_trend_event_item(item, theme, source):
    code = normalize_code(item.get("股票代码") or item.get("code") or "")
    name = item.get("股票简称") or item.get("股票名称") or item.get("名称") or ""
    if not code or not name or is_beijing_code(code):
        return None
    cap = num(item, "总市值")
    if cap is not None and (cap < MIN_CAP or cap > MAX_CAP):
        return None
    pe = num(item, "市盈率", "PE", "市盈率TTM")
    main = num(item, "主力资金净流入", "主力净流入", "主力资金", "资金净流入")
    chg = num(item, "涨跌幅", "最新涨跌幅")
    rev = num(item, "营收同比", "营业收入同比增长", "营收增长率", "营收增速")
    price = num(item, "收盘价", "最新价", "股价")
    industry, concepts = board_fields(item, theme)
    txt = text_blob(item)
    s = 20.0
    tags = [theme]

    if cap is not None:
        if cap <= IDEAL_CAP:
            s += 20; tags.append("市值30-300亿")
        else:
            s += 10; tags.append("市值300-800亿")
    theme_words = TREND_THEMES.get(theme, [theme])
    if theme and has_any(txt, theme_words):
        s += 12; tags.append("主题匹配")
    if has_any(txt, ["订单", "中标", "合作", "获批", "量产", "扩产", "IPO", "政策", "预增", "扭亏"]):
        s += 18; tags.append("事件催化")
    if has_any(txt, ["前3", "龙头", "核心供应商", "全球", "国内领先", "唯一", "第一"]):
        s += 12; tags.append("卡位")
    if rev is not None:
        if rev >= 50:
            s += 18; tags.append("高成长")
        elif rev >= 20:
            s += 10; tags.append("成长")
    if main is not None:
        if main > 0:
            s += min(20, 8 + main / 5e7); tags.append("资金流入")
        else:
            s -= 10; tags.append("⚠️资金流出")
    if pe is not None:
        if 0 < pe <= 60:
            s += 10; tags.append("估值可接受")
        elif pe > 120:
            s -= 15; tags.append("⚠️高PE")
    if chg is not None:
        if 0 <= chg < 7:
            s += 12; tags.append("可介入")
        elif chg >= 9.8:
            s -= 18; tags.append("⚠️涨停勿追")
        elif chg < -7:
            s -= 8; tags.append("⚠️破位")
    if source == "核心观察池":
        s += 8; tags.append("核心观察")
    p = {"code": code, "name": name, "theme": theme, "score": round(s, 1),
         "weight": 1.0, "cap": cap, "pe": pe, "main": main, "chg": chg,
         "rev": rev, "price": price, "industry": industry, "concepts": concepts,
         "reason": append_tags("", tags)}
    attach_fund_profile(p, item)
    apply_fund_score(p)
    return apply_trade_discipline(p)

def pick_trend_events(volume=False):
    """趋势事件交易法：动态强势概念 + 全市场资金动量 + 核心观察池。（并行版）"""
    candidates = []
    hot_themes = get_hot_sectors()[:DYNAMIC_THEME_LIMIT]
    if not hot_themes:
        hot_themes = list(TREND_THEMES)
    print("动态主题 → " + "、".join(hot_themes[:DYNAMIC_THEME_LIMIT]))

    # 构建所有并行查询任务：主题 + 全市场 + 核心观察池
    tasks = []
    task_meta = []  # (type, theme/code) 与 tasks 索引对应
    for theme in hot_themes:
        q = (f"{theme}，总市值大于30亿小于800亿，非北交所，非ST，非停牌，"
             f"最新涨跌幅大于-3小于9，"
             f"最新股价，最新涨跌幅，总市值，市盈率，所属同花顺行业，所属概念，"
             f"主力资金近5日净流入，超大单净流入，大单净流入，量比，换手率，营收同比，"
             f"近期订单 中标 合作 量产 扩产 政策 业绩预增 机构调研")
        if volume:
            q += "，今日换手率大于3"
        tasks.append((q, "hithink-astock-selector", "50"))
        task_meta.append(("theme", theme))

    market_q = ("全A，非北交所，非ST，非停牌，总市值大于30亿小于800亿，"
                "最新涨跌幅大于0小于8，主力资金今日净流入为正，"
                "换手率大于2，量比大于1，市盈率大于0小于100，"
                "最新股价，最新涨跌幅，总市值，市盈率，所属同花顺行业，所属概念，"
                "主力资金净流入，超大单净流入，大单净流入，量比，换手率，营收同比，"
                "近期订单 中标 合作 量产 扩产 政策 业绩预增 机构调研")
    if volume:
        market_q += "，今日换手率大于3"
    tasks.append((market_q, "hithink-astock-selector", "120"))
    task_meta.append(("market", "全市场强势"))

    for code, name, theme in CORE_WATCHLIST:
        q = (f"{code} {name} 最新股价 最新涨跌幅 总市值 市盈率 主力资金近5日净流入 "
             f"超大单净流入 大单净流入 换手率 营收同比 所属同花顺行业 所属概念 "
             f"近期订单 中标 合作 量产 扩产 政策 业绩预增 机构调研")
        tasks.append((q, "hithink-astock-selector", "1"))
        task_meta.append(("watch", theme))

    # 并行执行所有查询
    results = parallel_iw_queries(tasks, max_workers=10)

    for rows, (mtype, theme) in zip(results, task_meta):
        for it in rows:
            if mtype == "theme":
                p = score_trend_event_item(it, theme, "动态主题")
                if p: candidates.append(p)
            elif mtype == "market":
                p = score_trend_event_item(it, "全市场强势", "全市场资金动量")
                if p:
                    p["score"] = round(p.get("score", 0) - 4, 1)
                    p["reason"] = append_tags(p.get("reason", ""), ["全市场资金动量"])
                    candidates.append(p)
            elif mtype == "watch":
                p = score_trend_event_item(it, theme, "核心观察池")
                if p:
                    p["score"] = round(p.get("score", 0) - 8, 1)
                    candidates.append(p)
    return merge_candidates(candidates)

def score_limit_up_item(item):
    code = normalize_code(item.get("股票代码") or item.get("code") or "")
    name = item.get("股票简称") or item.get("股票名称") or item.get("名称") or ""
    if not code or not name or is_beijing_code(code):
        return None
    cap = num(item, "总市值")
    if cap is not None and (cap < 3e9 or cap > 8e10):
        return None
    price = num(item, "收盘价", "最新价", "股价", "涨停价")
    chg = num(item, "涨跌幅", "最新涨跌幅")
    main = num(item, "主力资金净流入", "主力净流入", "主力资金", "资金净流入")
    seal = num(item, "封单金额", "封板资金", "封单资金", "涨停封单")
    if seal is not None and 0 < seal < 10000:
        seal *= 1e8  # 问财有时返回“亿元”数值但列名带单位，值本身不带“亿”
    boards = num(item, "连续涨停天数", "连板数", "几连板", "涨停天数")
    breaks = num(item, "炸板次数", "开板次数")
    turnover = num(item, "换手率")
    txt = text_blob(item)
    theme = "涨停板"
    for t, words in TREND_THEMES.items():
        if has_any(txt, words):
            theme = f"涨停板/{t}"
            break
    s = 30.0
    tags = ["涨停短线", "次日竞价确认"]
    if boards is not None:
        if 2 <= boards <= 3:
            s += 24; tags.append(f"{int(boards)}连板")
        elif boards == 1:
            s += 12; tags.append("首板")
        elif boards >= 4:
            s -= 10; tags.append("⚠️高位连板")
    if seal is not None:
        if seal >= 3e8:
            s += 22; tags.append("强封单")
        elif seal >= 8e7:
            s += 12; tags.append("有封单")
        else:
            s -= 25; tags.append("⚠️封单弱")
    if breaks is not None:
        if breaks <= 2:
            s += 14; tags.append("封板稳")
        elif breaks >= 10:
            s -= 30; tags.append("⚠️炸板王")
        else:
            s -= 12; tags.append("⚠️多次开板")
    if main is not None:
        if main > 0:
            s += min(18, 8 + main / 8e7); tags.append("资金流入")
        else:
            s -= 10; tags.append("⚠️资金流出")
    if turnover is not None:
        if 5 <= turnover <= 25:
            s += 8; tags.append("换手健康")
        elif turnover > 35:
            s -= 10; tags.append("⚠️换手过热")
    if theme != "涨停板":
        s += 10; tags.append("主线涨停")
    if chg is not None and chg < 9:
        s -= 18; tags.append("⚠️非强涨停")
    industry, concepts = board_fields(item, theme.replace("涨停板/", ""))
    p = {"code": code, "name": name, "theme": theme, "score": round(s, 1),
         "weight": 1.0, "cap": cap, "pe": None, "main": main, "chg": chg,
         "price": price, "industry": industry, "concepts": concepts,
         "reason": append_tags("", tags), "_discipline_applied": True}
    attach_fund_profile(p, item, seal_amount=seal, break_count=breaks, turnover=turnover, board_count=boards)
    apply_fund_score(p)
    return p

def pick_limit_up_boards():
    """涨停板短线池：只找真强封板，最多给短线试错仓候选。"""
    q = ("今日涨停，非北交所，非ST，非停牌，连续涨停天数，封单金额，炸板次数，"
         "换手率，主力资金净流入，超大单净流入，大单净流入，总市值，最新股价，最新涨跌幅，所属概念")
    rows = iw_query(q, skill="hithink-astock-selector", limit="120")
    scored = []
    for it in rows:
        p = score_limit_up_item(it)
        if p:
            scored.append(p)
    return merge_candidates(scored)

def merge_candidates(candidates):
    """Deduplicate cross-pool hits while keeping the strongest score and merged reasons."""
    merged = {}
    for p in candidates:
        p["code"] = normalize_code(p.get("code"))
        if not p["code"] or is_beijing_code(p["code"]):
            continue
        apply_trade_discipline(p)
        old = merged.get(p["code"])
        if not old:
            merged[p["code"]] = p
            continue
        old["theme"] = append_tags(old.get("theme", ""), [p.get("theme", "")], limit=4)
        old["reason"] = append_tags(old.get("reason", ""), (p.get("reason") or "").split("+"))
        for key in ("cap", "pe", "main", "chg", "price", "industry", "concepts", "fund_grade", "fund_score", "fund_tags",
                    "super_net", "big_net", "lobby_net", "seal_amount", "break_count", "turnover", "board_count"):
            if old.get(key) is None and p.get(key) is not None:
                old[key] = p.get(key)
        if p.get("score", 0) > old.get("score", 0):
            old["score"] = p["score"]
    return sorted(merged.values(), key=lambda x: -x["score"])

def pick_theme(theme, weight=1.0, volume=False):
    # 第一性原理多角度筛选：资金面+成长+估值+规模+排雷，iwencai NL 一把筛
    q = (f"{theme}，主力资金近5日净流入为正，市盈率大于0小于60，"
         f"总市值大于50亿，营收同比大于20，所属行业，非ST，非停牌")
    if volume:
        q += "，今日换手率大于3"
    datas = iw_query(q, limit="15")
    if not datas:  # 放宽：去掉成长/资金约束
        datas = iw_query(f"{theme}，市盈率大于0小于80，总市值大于50亿，非ST",
                         limit="15", retry=True)
    scored = []
    for it in datas:
        code = it.get("股票代码") or it.get("code") or ""
        name = it.get("股票简称") or it.get("股票名称") or ""
        if not code: continue
        cap = num(it, "总市值"); pe = num(it, "市盈率", "PE", "市盈率TTM")
        main = num(it, "主力资金净流入", "主力净流入", "主力资金", "资金净流入")
        chg = num(it, "涨跌幅", "最新涨跌幅")
        rev = num(it, "营收同比", "营业收入同比增长", "营收增长率", "营收增速")
        turnover = num(it, "换手率") if volume else None
        sector = str_field(it, "行业")
        # 多角度第一性原理打分：资金/成长/估值/龙头/动量
        s = 0.0; tags = []
        if main is not None and main > 0:       # 资金面：主力净流入
            s += min(30, 15 + main / 1e7); tags.append("资金")
        elif main is not None: s -= 10
        if rev is not None and rev > 30:        # 成长：营收高增
            s += 20; tags.append("成长")
        elif rev is not None and rev > 0: s += 8
        if pe is not None and 0 < pe < 35:      # 估值：便宜
            s += 18; tags.append("低估")
        elif pe is not None and pe < 60: s += 10
        if cap is not None and 1e10 <= cap <= 5e10:   # 成长接力：100-500亿中小盘（你的风格）
            s += 12; tags.append("中小盘")
        elif cap is not None and (5e9 <= cap < 1e10 or 5e10 < cap <= 1.5e11): s += 6
        elif cap is not None and cap > 1.5e11: s -= 10; tags.append("⚠️过大")  # >1500亿成长性弱
        if chg is not None and 0 <= chg < 6:    # 动量：温和上行
            s += 12; tags.append("动量")
        elif chg is not None and 6 <= chg < 10: s += 5
        elif chg is not None and chg >= 10: s -= 5   # 超买警惕
        if chg is not None and chg >= 9.8: tags.append("涨停")  # 涨停(主板10%/创业科创20%)强势信号，但高位/可能买不到
        if turnover is not None and turnover > 3: s += 5
        if len(tags) >= 3: s += 8               # 多角度平衡：强项越多越好
        elif len(tags) >= 2: s += 3
        scored.append({"code": code, "name": name, "theme": theme,
                       "score": round(s * weight, 1), "weight": round(weight, 2),
                       "cap": cap, "pe": pe, "main": main, "chg": chg, "rev": rev,
                       "reason": "+".join(tags[:4]) or "—"})
    scored.sort(key=lambda x: -x["score"])
    return scored

def get_price(code):
    datas = iw_query(f"{code} 最新价", skill="hithink-market-query", limit="1")
    if datas:
        return num(datas[0], "最新价", "最新", "现价", "收盘价", "股价")
    return None

def add_levels(picks):
    """Add executable levels only when the signal is actionable after guardrails."""
    for p in picks:
        reason = p.get("reason") or ""
        is_limit_up = is_limit_up_pick(p)
        executable_min_score = EXECUTABLE_K50_MIN_SCORE if "科创50" in (p.get("theme") or "") else EXECUTABLE_MIN_SCORE
        if p.get("risk_veto"):
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", reason.split("+") + ["⚠️Agent风险否决"])
            continue
        if p.get("agent_consensus") and p.get("agent_consensus") != "buy":
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", reason.split("+") + ["Agent未形成买入共识"])
            continue
        if is_limit_up:
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags(
                "观察不打板",
                ["当天涨停/接近涨停不列为可买", "次日竞价确认", "不板即走"] + reason.split("+"),
            )
            continue
        if p.get("score", 0) < executable_min_score and not is_limit_up:
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", (p.get("reason") or "").split("+") + ["评分未达买点阈值"])
            continue
        if any(tag in reason for tag in HARD_BLOCK_TAGS) or "⚠️弱主题" in reason:
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", reason.split("+"))
            continue
        if (not is_limit_up) and any(tag in reason for tag in WATCH_ONLY_TAGS):
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", reason.split("+"))
            continue
        if p.get("score", 0) < executable_min_score:
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", reason.split("+") + ["评分未达买点阈值"])
            continue
        grade = (p.get("fund_grade") or "").upper()
        if grade not in ("A", "B"):
            p["buy_point"] = p["stop_loss"] = p["target"] = None
            p["reason"] = append_tags("观察不买", reason.split("+") + ["资金未确认"])
            continue
        price = p.get("price") or get_price(p["code"])  # 优先用 insresearch 已取的价，market-query 兜底
        if price and price > 0:
            chg = p.get("chg")
            if chg is not None and chg > 6.5:
                p["buy_point"] = p["stop_loss"] = p["target"] = None
                p["reason"] = append_tags("观察不买", (p.get("reason") or "").split("+") + ["涨幅超过6.5%，等待回踩"])
                continue
            if chg is not None and chg < -2:
                p["buy_point"] = p["stop_loss"] = p["target"] = None
                p["reason"] = append_tags("观察不买", (p.get("reason") or "").split("+") + ["跌幅超过2%，等待转强"])
                continue
            buy = price
            p["buy_point"] = round(buy, 2)
            p["stop_loss"] = round(p["buy_point"] * 0.93, 2)
            p["target"] = round(p["buy_point"] * 1.20, 2)
            p["reason"] = append_tags("试仓3%+涨10卖1/3+涨20卖1/3", (p.get("reason") or "").split("+"))
        else:
            p["buy_point"] = p["stop_loss"] = p["target"] = None

def feishu_chat_id():
    c = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if c: return c
    for p in ["/home/ubuntu/.feishu_chat_id", str(SCRIPT_DIR / ".feishu_chat_id")]:
        try:
            v = open(p).read().strip()
            if v: return v
        except Exception: pass
    return ""

def fetch_kechuang50():
    """科创50(科技基准) 今年以来涨跌幅"""
    d = iw_query("科创50 今年以来涨跌幅", skill="hithink-zhishu-query", limit="1")
    if d:
        v = num(d[0], "年涨跌幅", "涨跌幅")
        if v is not None: return f"科创50今年+{v:.1f}%" if v > 0 else f"科创50今年{v:.1f}%"
    return ""

def stock_quote_url(code):
    c = normalize_code(code)
    market = "sh" if c.startswith(("600", "601", "603", "605", "688", "689")) else "sz"
    return f"https://quote.eastmoney.com/{market}{c}.html"

def stock_lab_url(code):
    return f"https://mazhi.icu/invest/stock-lab/{normalize_code(code)}.html"

def fund_line(p):
    grade = p.get("fund_grade") or "-"
    score = p.get("fund_score")
    parts = [f"资金{grade}档"]
    if score is not None:
        parts.append(f"{score}分")
    if p.get("main") is not None:
        parts.append(f"主力{fmt(p.get('main'))}")
    if p.get("super_net") is not None:
        parts.append(f"超大单{fmt(p.get('super_net'))}")
    if p.get("big_net") is not None:
        parts.append(f"大单{fmt(p.get('big_net'))}")
    if p.get("lobby_net") is not None:
        parts.append(f"龙虎榜机构{fmt(p.get('lobby_net'))}")
    if p.get("seal_amount") is not None:
        parts.append(f"封单{fmt(p.get('seal_amount'))}")
    if p.get("break_count") is not None:
        parts.append(f"炸板{int(p.get('break_count'))}次")
    if p.get("fund_tags"):
        parts.append(str(p.get("fund_tags")))
    return " / ".join(parts)

def load_latest_holdings():
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        latest = conn.execute("SELECT MAX(snapshot_date) FROM holdings").fetchone()[0]
        if not latest:
            conn.close()
            return []
        rows = [dict(r) for r in conn.execute(
            "SELECT code,name,qty,price,cost,sector,note,snapshot_date FROM holdings WHERE snapshot_date=? ORDER BY code",
            (latest,),
        )]
        conn.close()
        return rows
    except Exception:
        return []

def event_date_from_row(row):
    for k, v in row.items():
        if any(word in str(k) for word in ["日期", "变动日期", "解禁日", "公告日"]):
            d = parse_date_value(v)
            if d:
                return d
        d = parse_date_value(k)
        if d:
            return d
    return None

def holding_event_alerts(days_back=3, days_forward=21):
    """Scan current holdings for unlock/reduction/regulatory risks that can move price."""
    alerts = []
    today = date.today()
    holdings = load_latest_holdings()
    seen = set()
    for h in holdings:
        code = normalize_code(h.get("code"))
        name = h.get("name") or code
        if not code:
            continue
        q = (
            f"{code} {name} 近30日 未来30日 限售解禁 解禁数量 解禁市值 "
            "减持计划 股东减持 股权质押 监管函 问询函 立案"
        )
        rows = iw_query(q, skill="hithink-event-query", limit="8")
        for row in rows:
            txt = text_blob(row)
            if not txt:
                continue
            d = event_date_from_row(row)
            if d and not (today - timedelta(days=days_back) <= d <= today + timedelta(days=days_forward)):
                continue
            risk_type = None
            if "解禁" in txt or any("解禁" in str(k) for k in row.keys()):
                risk_type = "解禁抛压"
            elif "减持" in txt:
                risk_type = "减持风险"
            elif any(k in txt for k in ["监管函", "问询函", "警示函", "立案"]):
                risk_type = "监管风险"
            elif "质押" in txt:
                risk_type = "质押风险"
            if not risk_type:
                continue
            shares, _ = num_key(row, "实际解禁股数", "解禁股数", "解禁数量", "减持数量")
            amount, _ = num_key(row, "股份市值", "解禁市值", "减持金额")
            ratio, _ = num_key(row, "占流通股比例", "占总股本比例", "占总市值比例")
            src = str_field(row, "股份来源", "股东名称", "事件类型", "公告标题")
            key = (code, risk_type, d.isoformat() if d else "", round(shares or 0, 0), round(amount or 0, 0))
            if key in seen:
                continue
            seen.add(key)
            impact = "高"
            if risk_type in ("质押风险",) and "解禁" not in txt and "减持" not in txt:
                impact = "中"
            alerts.append({
                "code": code,
                "name": name,
                "type": risk_type,
                "date": d.isoformat() if d else "近期/未来30日",
                "days": (d - today).days if d else None,
                "shares": shares,
                "amount": amount,
                "ratio": ratio,
                "source": src,
                "impact": impact,
            })
    alerts.sort(key=lambda a: (0 if a["impact"] == "高" else 1, abs(a["days"]) if a["days"] is not None else 99))
    return alerts

def push_holding_risk_alert(alerts):
    chat = feishu_chat_id()
    if not alerts or not chat or not os.path.exists(LARK_CLI):
        return
    lines = [f"🚨 持仓风险预警（{datetime.now():%m-%d %H:%M}）"]
    for a in alerts[:8]:
        day_text = ""
        if a["days"] is not None:
            if a["days"] < 0:
                day_text = f"已发生{abs(a['days'])}天"
            elif a["days"] == 0:
                day_text = "今天"
            else:
                day_text = f"{a['days']}天后"
        parts = [
            f"{a['name']}({a['code']}) {a['type']} {a['date']} {day_text}".strip(),
        ]
        if a.get("shares"):
            parts.append(f"数量{fmt(a['shares'])}股")
        if a.get("amount"):
            parts.append(f"市值{fmt(a['amount'])}")
        if a.get("ratio") is not None:
            parts.append(f"比例{a['ratio']:.3f}%")
        if a.get("source"):
            parts.append(str(a["source"])[:28])
        lines.append(" - " + "，".join(parts))
    lines.append("动作：先降仓/设硬止损/不加仓，等抛压释放后再评估。")
    try:
        r = subprocess.run([LARK_CLI, "im", "+messages-send", "--as", "bot",
                            "--chat-id", chat, "--text", "\n".join(lines)],
                           capture_output=True, text=True, timeout=20)
        if '"ok": true' in (r.stdout or "") or '"ok":true' in (r.stdout or ""):
            print(f"已推持仓风险预警 → {chat}")
        else:
            print(f"持仓风险推送可能失败: {(r.stdout or r.stderr)[:200]}")
    except Exception as e:
        print(f"持仓风险推送失败: {e}")

def save_holding_risk_alerts(alerts):
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS holding_risk_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_at TEXT,
        code TEXT,
        name TEXT,
        risk_type TEXT,
        event_date TEXT,
        days INTEGER,
        shares REAL,
        amount REAL,
        ratio REAL,
        source TEXT,
        impact TEXT,
        note TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS risk_scan_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_at TEXT NOT NULL,
        alert_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        note TEXT
    )""")
    scan_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    for a in alerts:
        note = []
        if a.get("shares"): note.append(f"数量{fmt(a['shares'])}股")
        if a.get("amount"): note.append(f"市值{fmt(a['amount'])}")
        if a.get("ratio") is not None: note.append(f"比例{a['ratio']:.3f}%")
        if a.get("source"): note.append(str(a["source"])[:40])
        conn.execute("""INSERT INTO holding_risk_alerts
            (scan_at,code,name,risk_type,event_date,days,shares,amount,ratio,source,impact,note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_at, a.get("code"), a.get("name"), a.get("type"), a.get("date"),
             a.get("days"), a.get("shares"), a.get("amount"), a.get("ratio"),
             a.get("source"), a.get("impact"), "，".join(note)))
    conn.execute(
        "INSERT INTO risk_scan_runs (scan_at,alert_count,status,note) VALUES (?,?,?,?)",
        (scan_at, len(alerts), "alert" if alerts else "clear",
         f"发现 {len(alerts)} 条近30日重大事件" if alerts else "未发现近30日重大事件"),
    )
    conn.commit()
    conn.close()

def regen_risk_page():
    if not P2_PAGES_PY.exists():
        return
    try:
        r = subprocess.run(
            ["python3", str(P2_PAGES_PY), str(SCRIPT_DIR), "--risk-only"],
            capture_output=True, text=True, timeout=120,
        )
        print("risk-page:", (r.stdout or r.stderr).strip().split("\n")[-1] if (r.stdout or r.stderr).strip() else "done")
    except Exception as e:
        print(f"风险页面刷新失败: {e}")

def run_holding_risk_alert(push=True):
    alerts = holding_event_alerts()
    save_holding_risk_alerts(alerts)
    if not alerts:
        print("持仓风险扫描：暂无近30日重大事件")
        regen_risk_page()
        return []
    print(f"持仓风险扫描：{len(alerts)} 条")
    for a in alerts:
        detail = []
        if a.get("shares"): detail.append(f"数量{fmt(a['shares'])}股")
        if a.get("amount"): detail.append(f"市值{fmt(a['amount'])}")
        if a.get("ratio") is not None: detail.append(f"比例{a['ratio']:.3f}%")
        print(f"  {a['name']}({a['code']}) {a['type']} {a['date']} {' '.join(detail)}")
    if push:
        push_holding_risk_alert(alerts)
    regen_risk_page()
    return alerts

def push_feishu(top):
    """经 lark-cli 以 bot 身份推 TOP5 到你和 bot 的私聊"""
    chat = feishu_chat_id()
    if not chat or not os.path.exists(LARK_CLI):
        return
    actionable = [p for p in top if p.get("buy_point") and not is_limit_up_pick(p)]
    observation = [p for p in top if p not in actionable]
    if not actionable and not observation:
        print("飞书跳过：本批次没有候选，只更新网站")
        return
    kc = fetch_kechuang50()
    now = datetime.now()
    phase = "盘后复盘生成" if now.strftime("%H:%M") >= "15:00" else "盘中扫描"
    if actionable:
        lines = [f"🎯 选股雷达 可执行 TOP{min(5,len(actionable))}（{now:%m-%d %H:%M} · {phase}）"]
    else:
        lines = [f"🎯 选股雷达：暂无可执行买点（{now:%m-%d %H:%M} · {phase}）"]
    if kc: lines.append(f"📈 科技基准 {kc}")
    lines.append("规则：涨停/接近涨停只进观察池，不给买点。")
    for i, p in enumerate(actionable[:5], 1):
        if p.get("buy_point"):
            lv = f"试仓3% 买¥{p.get('buy_point')}/止¥{p.get('stop_loss')}/10%止盈/目¥{p.get('target')}"
        else:
            lv = "观察不买：等待放量转强/风险解除"
        chg = p.get("chg")
        chg_s = fmt_pct(chg)
        cap_s = fmt(p.get("cap"))
        lines.append(
            f"{i}. {p['name']}({p['code']}) {p['theme']} 评分{p['score']} "
            f"涨跌{chg_s} 市值{cap_s} [{p.get('reason','')}] {lv}\n"
            f"   资金: {fund_line(p)}\n"
            f"   分析: {stock_lab_url(p['code'])}"
        )
    if observation:
        lines.append(f"👀 观察池（不作为买入）：")
        for i, p in enumerate(observation[:3], 1):
            if is_limit_up_pick(p):
                lv = "涨停/接近涨停，等次日竞价与承接确认"
            else:
                lv = "等待放量转强/风险解除"
            chg = p.get("chg")
            chg_s = fmt_pct(chg)
            lines.append(
                f"{i}. {p['name']}({p['code']}) {p['theme']} 评分{p['score']} "
                f"涨跌{chg_s} [{p.get('reason','')}] {lv}\n"
                f"   资金: {fund_line(p)}\n"
                f"   分析: {stock_lab_url(p['code'])}"
            )
    if observation:
        lines.append(f"已过滤观察票 {len(observation)} 只，详见网站。")
    lines.append("⚠️ 仅候选，非投资建议")
    try:
        r = subprocess.run([LARK_CLI, "im", "+messages-send", "--as", "bot",
                            "--chat-id", chat, "--text", "\n".join(lines)],
                           capture_output=True, text=True, timeout=20)
        if '"ok": true' in (r.stdout or "") or '"ok":true' in (r.stdout or ""):
            print(f"已推飞书 TOP5 → {chat}")
        else:
            print(f"飞书推送可能失败: {(r.stdout or r.stderr)[:200]}")
    except Exception as e:
        print(f"飞书推送失败: {e}")

def enrich_pick(p):
    """多技能综合分析：insresearch(机构面) + event(催化/排雷)，调分并更新理由"""
    code = p["code"]; bonus = 0; tags = []
    # 资金面：主力/超大单/大单/龙虎榜/封板，用于过滤“逻辑好但资金不认”的票
    try:
        money = iw_query(
            f"{code} 今日主力资金净流入 超大单净流入 大单净流入 换手率 量比 龙虎榜 机构净买入额 买入营业部 卖出营业部 封单金额 炸板次数 连续涨停天数",
            skill="hithink-astock-selector",
            limit="1",
        )
        if money:
            before_grade = p.get("fund_grade")
            attach_fund_profile(p, money[0])
            if p.get("fund_grade") != before_grade:
                apply_fund_score(p)
            if p.get("fund_grade") in ("A", "B"):
                tags.append(f"资金{p.get('fund_grade')}档")
            elif p.get("fund_grade") in ("D", "E"):
                tags.append(f"⚠️资金{p.get('fund_grade')}档")
    except Exception:
        pass
    # 机构面：研报评级 / 券商金股
    try:
        ins = iw_query(f"{code} 机构投资评级 买入家数 券商金股", skill="hithink-insresearch-query", limit="1")
        if ins:
            buy_n = num(ins[0], "买入家数", "买入评级", "买入数", "买入")
            if buy_n and buy_n >= 5: bonus += 12; tags.append("机构看好")
            elif buy_n and buy_n >= 1: bonus += 5
            if any("金股" in str(v) for v in ins[0].values()): bonus += 8; tags.append("券商金股")
    except Exception: pass
    # 事件面：业绩预告/解禁/质押/监管/调研 → 催化 + 排雷（惩罚，不硬剔除避免误杀）
    try:
        ev = iw_query(f"{code} 近期业绩预告 限售解禁 股权质押 监管函 机构调研", skill="hithink-event-query", limit="1")
        if ev:
            txt = " ".join(str(v) for v in ev[0].values())
            if any(k in txt for k in ["预增", "预盈", "扭亏", "续盈"]): bonus += 10; tags.append("业绩催化")
            if any(k in txt for k in ["机构调研", "调研"]): bonus += 5; tags.append("获调研")
            if any(k in txt for k in ["监管函", "问询函", "警示函", "立案"]): bonus -= 20; tags.append("⚠️监管")
            if "解禁" in txt: bonus -= 8; tags.append("⚠️解禁")
            if "质押" in txt: bonus -= 6; tags.append("⚠️质押")
    except Exception: pass
    # 龙虎榜：机构净买卖（比评级更直接的聪明资金信号）
    try:
        lb = iw_query(f"{code} 龙虎榜 机构净买入额", skill="hithink-event-query", limit="1")
        if lb:
            inst = num(lb[0], "机构净买入", "机构买入净额", "机构净额", "机构买入")
            if inst is not None:
                p["lobby_net"] = inst
                attach_fund_profile(p, {"机构净买入额": inst})
            if inst is not None and inst > 0: bonus += 10; tags.append("龙虎榜机构买")
            elif inst is not None and inst < 0: bonus -= 8; tags.append("⚠️龙虎榜机构卖")
    except Exception: pass
    # 舆情：news-search 利好/利空
    try:
        ns = iw_query(f"{p.get('name','')} 利好 利空", skill="news-search", limit="3")
        if ns:
            txt = " ".join(str(v) for d in ns for v in d.values())
            if any(k in txt for k in ["利好", "增长", "突破", "合作", "中标", "收购", "获批", "超预期"]): bonus += 6; tags.append("舆情利好")
            if any(k in txt for k in ["利空", "亏损", "下滑", "被查", "处罚", "减持", "爆雷", "退市", "警示"]): bonus -= 8; tags.append("⚠️舆情利空")
    except Exception: pass
    p["score"] = round(p["score"] + bonus, 1)
    if tags:
        p["reason"] = append_tags(p.get("reason", ""), tags)


def apply_agent_council(p):
    """Run independent specialist reviews and feed consensus back into ranking."""
    if p.get("_agent_review_applied"):
        return
    review = apply_candidate_review(p)
    consensus = review["consensus"]
    if review["risk_veto"]:
        p["score"] = round(p.get("score", 0) - 16, 1)
        p["reason"] = append_tags(p.get("reason", ""), ["⚠️Agent风险否决"] + review["risk_flags"])
    elif consensus == "buy":
        p["score"] = round(p.get("score", 0) + 6, 1)
        p["reason"] = append_tags(p.get("reason", ""), [f"Agent共识买入{review['confidence']:.0%}"])
    elif consensus == "sell":
        p["score"] = round(p.get("score", 0) - 12, 1)
        p["reason"] = append_tags(p.get("reason", ""), ["Agent共识偏空"])
    else:
        p["score"] = round(p.get("score", 0) - 4, 1)
        p["reason"] = append_tags(p.get("reason", ""), [f"Agent分歧观察{review['disagreement']:.0f}"])
    p["_agent_review_applied"] = True

def ensure_stock_picks_schema():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, theme TEXT,
        score REAL, rank INTEGER, weight REAL, cap REAL, pe REAL, main_net REAL,
        chg_pct REAL, buy_point REAL, stop_loss REAL, target REAL, premarket_note TEXT,
        fund_grade TEXT, fund_score REAL, fund_tags TEXT, super_net REAL, big_net REAL,
        lobby_net REAL, seal_amount REAL, break_count REAL, turnover REAL, board_count REAL,
        reason TEXT, picked_at TEXT)""")
    # 兼容旧表：补缺失列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_picks)")}
    for col, decl in [("weight","REAL"),("buy_point","REAL"),("stop_loss","REAL"),
                      ("target","REAL"),("premarket_note","TEXT"),("reason","TEXT"),
	                      ("run_id","TEXT"),("picked_date","TEXT"),("picked_time","TEXT"),("eval_date","TEXT"),
	                      ("eval_price","REAL"),("eval_return_pct","REAL"),("eval_status","TEXT"),
	                      ("eval_note","TEXT"),("fund_grade","TEXT"),("fund_score","REAL"),
	                      ("fund_tags","TEXT"),("super_net","REAL"),("big_net","REAL"),
	                      ("lobby_net","REAL"),("seal_amount","REAL"),("break_count","REAL"),
	                      ("turnover","REAL"),("board_count","REAL"),("industry","TEXT"),
                          ("concepts","TEXT"),("sector_chg","REAL"),("sector_main_net","REAL"),
                          ("agent_consensus","TEXT"),("agent_confidence","REAL"),
                          ("agent_disagreement","REAL"),("agent_reviews_json","TEXT"),
                          ("risk_level","TEXT"),("risk_veto","INTEGER"),("risk_reasons","TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE stock_picks ADD COLUMN {col} {decl}")
    conn.execute("UPDATE stock_picks SET picked_date=substr(picked_at,1,10) WHERE picked_date IS NULL AND picked_at IS NOT NULL")
    conn.execute("UPDATE stock_picks SET picked_time=substr(picked_at,12,5) WHERE picked_time IS NULL AND picked_at IS NOT NULL")
    conn.execute("UPDATE stock_picks SET run_id=replace(substr(picked_at,1,16),' ','-') WHERE run_id IS NULL AND picked_at IS NOT NULL")
    conn.commit()
    return conn

def save_picks(picks):
    picks = [p for p in picks if not is_beijing_code(p.get("code"))]
    conn = ensure_stock_picks_schema()
    today = datetime.now().strftime('%Y-%m-%d %H:%M')
    picked_date = datetime.now().strftime('%Y-%m-%d')
    picked_time = datetime.now().strftime('%H:%M')
    run_id = datetime.now().strftime('%Y%m%d-%H%M')
    conn.execute("DELETE FROM stock_picks WHERE run_id=?", (run_id,))
    for p in picks:
        conn.execute("""INSERT INTO stock_picks
            (code,name,theme,score,rank,weight,cap,pe,main_net,chg_pct,
             buy_point,stop_loss,target,reason,picked_at,run_id,picked_date,picked_time,
             fund_grade,fund_score,fund_tags,super_net,big_net,lobby_net,seal_amount,break_count,turnover,board_count,
             industry,concepts,sector_chg,sector_main_net,agent_consensus,agent_confidence,
             agent_disagreement,agent_reviews_json,risk_level,risk_veto,risk_reasons)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["code"], p["name"], p["theme"], p["score"], p.get("rank"), p.get("weight"),
             p.get("cap"), p.get("pe"), p.get("main"), round_pct(p.get("chg")),
             p.get("buy_point"), p.get("stop_loss"), p.get("target"), p.get("reason",""),
             today, run_id, picked_date, picked_time, p.get("fund_grade"), p.get("fund_score"),
             p.get("fund_tags"), p.get("super_net"), p.get("big_net"), p.get("lobby_net"),
             p.get("seal_amount"), p.get("break_count"), p.get("turnover"), p.get("board_count"),
             p.get("industry"), p.get("concepts"), p.get("sector_chg"), p.get("sector_main_net"),
             p.get("agent_consensus"), p.get("agent_confidence"), p.get("agent_disagreement"),
             p.get("agent_reviews_json"), p.get("risk_level"), p.get("risk_veto"), p.get("risk_reasons")))
    conn.commit(); conn.close()
    print(f"已写入 {len(picks)} 条候选，批次 {run_id}")

def save_market_fund_snapshot():
    """大盘资金雷达：市场宽度、板块主力、涨停情绪、龙虎榜游资痕迹。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS market_fund_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at TEXT,
        market_json TEXT,
        sectors_json TEXT,
        limit_json TEXT,
        lhb_json TEXT
    )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_fund_snapshots)")}
    if "sentiment_json" not in cols:
        conn.execute("ALTER TABLE market_fund_snapshots ADD COLUMN sentiment_json TEXT")
    if "lhb_recent_json" not in cols:
        conn.execute("ALTER TABLE market_fund_snapshots ADD COLUMN lhb_recent_json TEXT")
    snapshot_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    queries = {
        "market": ("同花顺全A 上证指数 创业板指 科创50 今日涨跌幅 最新价 上涨家数 下跌家数 涨停家数 跌停家数 沪深两市成交额 主力资金净流入", "hithink-zhishu-query", "8"),
        "sectors": ("概念板块 今日主力资金净流入排名前15 涨跌幅 成交额 龙头股", "hithink-sector-selector", "15"),
        "limit": ("今日涨停 非ST 连续涨停天数 封单金额 炸板次数 所属概念 主力资金净流入 总市值", "hithink-astock-selector", "30"),
        "lhb": ("今日龙虎榜 机构净买入额 营业部 买入金额 卖出金额 所属概念", "hithink-event-query", "20"),
        "lhb_recent": ("近5日龙虎榜 机构净买入额 知名游资营业部 买入金额 卖出金额 所属概念 上榜日期", "hithink-event-query", "30"),
        "sentiment": ("今日市场情绪 赚钱效应 炸板率 连板高度 昨日涨停表现 涨停溢价 跌停家数 涨停家数 上涨家数 下跌家数", "市场情绪偏离分析", "10"),
    }
    # 并行执行所有市场查询
    keys = list(queries.keys())
    tasks = [queries[k] for k in keys]
    results = parallel_iw_queries(tasks, max_workers=6)
    data = dict(zip(keys, results))
    conn.execute(
        "INSERT INTO market_fund_snapshots (snapshot_at, market_json, sectors_json, limit_json, lhb_json, sentiment_json, lhb_recent_json) VALUES (?,?,?,?,?,?,?)",
        (
            snapshot_at,
            json.dumps(data["market"], ensure_ascii=False),
            json.dumps(data["sectors"], ensure_ascii=False),
            json.dumps(data["limit"], ensure_ascii=False),
            json.dumps(data["lhb"], ensure_ascii=False),
            json.dumps(data["sentiment"], ensure_ascii=False),
            json.dumps(data["lhb_recent"], ensure_ascii=False),
        ),
    )
    conn.commit(); conn.close()
    print(f"已写入大盘资金快照 {snapshot_at}")

def push_premarket(notes):
    chat = feishu_chat_id()
    if not chat or not os.path.exists(LARK_CLI): return
    alerts = [(c, v) for c, v in notes.items() if v.startswith("⚠️")]
    lines = [f"📋 盘前复核（{datetime.now():%H:%M}）"]
    if alerts:
        lines.append("⚠️ 异动:")
        for c, v in alerts: lines.append(f"  {c} {v}")
    else:
        lines.append("候选开盘平稳，无异动 ✓")
    lines.append("详见网站「选股」tab")
    try:
        subprocess.run([LARK_CLI, "im", "+messages-send", "--as", "bot", "--chat-id", chat,
                        "--text", "\n".join(lines)], capture_output=True, text=True, timeout=20)
        print(f"已推盘前复核 → {chat}")
    except Exception as e:
        print(f"盘前推送失败: {e}")

def premarket():
    conn = ensure_stock_picks_schema(); conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT run_id FROM stock_picks WHERE run_id IS NOT NULL ORDER BY picked_at DESC, id DESC LIMIT 1").fetchone()
    run_id = run["run_id"] if run else None
    if not run_id:
        print("无选股批次可复核"); conn.close(); return
    picks = [dict(r) for r in conn.execute("SELECT code,name FROM stock_picks WHERE run_id=? ORDER BY rank", (run_id,))]
    n = 0; notes = {}
    for p in picks:
        datas = iw_query(f"{p['code']} 今日开盘 涨跌幅", skill="hithink-market-query", limit="1")
        chg = num(datas[0], "涨跌幅") if datas else None
        if chg is None: continue
        if chg >= 5: note = f"⚠️高开{chg:.1f}%"
        elif chg <= -5: note = f"⚠️低开{chg:.1f}%"
        else: note = f"开盘{chg:+.1f}%"
        conn.execute("UPDATE stock_picks SET premarket_note=? WHERE run_id=? AND code=?", (note, run_id, p["code"]))
        notes[f"{p['name']}({p['code']})"] = note
        n += 1
    conn.commit(); conn.close()
    print(f"盘前复核 {n}/{len(picks)} 只")
    push_premarket(notes)

def backtest_picks(days=7):
    """回测已保存批次：默认评估 7 天前及更早、尚未评估的候选。"""
    conn = ensure_stock_picks_schema(); conn.row_factory = sqlite3.Row
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = [dict(r) for r in conn.execute("""
        SELECT * FROM stock_picks
        WHERE picked_date IS NOT NULL
          AND picked_date <= ?
          AND eval_date IS NULL
          AND buy_point IS NOT NULL
        ORDER BY picked_date, run_id, rank
    """, (cutoff,))]
    if not rows:
        print(f"没有需要回测的候选（picked_date <= {cutoff} 且未回测）")
        conn.close(); return
    ok = 0
    for p in rows:
        px = get_price(p["code"])
        if not px or not p.get("buy_point"):
            continue
        ret = (px / p["buy_point"] - 1) * 100
        is_short = "涨停短线" in (p.get("reason") or "")
        win_line = 5 if is_short else 10
        stop_line = -5 if is_short else -7
        if ret >= win_line:
            status = "success"; note = f"达成 {win_line}% 目标"
        elif ret <= stop_line:
            status = "stop"; note = f"触发 {abs(stop_line)}% 风险"
        else:
            status = "neutral"; note = "未达目标/止损"
        conn.execute("""UPDATE stock_picks
            SET eval_date=?, eval_price=?, eval_return_pct=?, eval_status=?, eval_note=?
            WHERE id=?""",
            (date.today().isoformat(), round(px, 2), round(ret, 2), status, note, p["id"]))
        ok += 1
        print(f"{p['picked_date']} {p['name']}({p['code']}) 买{p['buy_point']}→{px:.2f} {ret:+.2f}% {status}")
    conn.commit()
    stats = conn.execute("""
        SELECT eval_status, COUNT(*) c, AVG(eval_return_pct) avg_ret
        FROM stock_picks WHERE eval_date IS NOT NULL GROUP BY eval_status
    """).fetchall()
    print(f"已回测 {ok}/{len(rows)} 条")
    for r in stats:
        print(f"  {r['eval_status']}: {r['c']} 条，平均 {r['avg_ret'] or 0:.2f}%")
    conn.close()

def regen():
    if not GEN_PY.exists(): return
    print("重生成网站...")
    procs = []
    procs.append(subprocess.Popen(["python3", str(GEN_PY), str(SCRIPT_DIR)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    if STOCK_LAB_PY.exists():
        procs.append(subprocess.Popen(["python3", str(STOCK_LAB_PY), str(SCRIPT_DIR)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    for p in procs:
        try:
            out, err = p.communicate(timeout=180)
            label = "regen" if p is procs[0] else "stock-lab"
            print(f"{label}:", (out or err).strip().split("\n")[-1] if (out or err).strip() else "done")
        except subprocess.TimeoutExpired:
            p.kill()
            print(f"regen timeout, killed")

def pick_gold():
    """候选池：insresearch 券商金股（机构推荐）+ 行情，绕开额度受限的 astock-selector/market-query。
    返回按多角度打分排序的候选（已过滤科技圈/中小盘/估值）。"""
    datas = iw_query("券商金股，所属同花顺行业，所属概念，总市值，市盈率，最新涨跌幅，最新股价，报告摘要，开始日期，截止日期，研究机构",
                     skill="hithink-insresearch-query", limit="200")
    scored = []
    best_by_code = {}
    for it in datas:
        code = normalize_code(it.get("股票代码") or ""); name = it.get("股票简称") or ""
        if not code: continue
        sector = str_field(it, "行业"); cap = num(it, "总市值")
        pe = num(it, "市盈率", "PE"); chg = round_pct(num(it, "涨跌幅")); price = num(it, "收盘价", "最新价", "股价")
        if not is_fresh_report(it):
            continue
        if not explicit_stock_recommendation(it, name):
            continue
        s = 8.0; tags = ["券商金股"]   # 只有明确个股推荐才保留此标签
        org = str_field(it, "研究机构", "机构")
        if org:
            tags.append(org[:8])
        if pe is not None and 0 < pe < 35: s += 18; tags.append("低估")
        elif pe is not None and pe < 60: s += 10
        if cap is not None and 1e10 <= cap <= 5e10: s += 12; tags.append("中小盘")
        elif cap is not None and (5e9 <= cap < 1e10 or 5e10 < cap <= 1.5e11): s += 6
        elif cap is not None and cap > 1.5e11: s -= 10; tags.append("⚠️过大")
        if chg is not None and 0 <= chg < 6: s += 12; tags.append("动量")
        elif chg is not None and 6 <= chg < 10: s += 5
        elif chg is not None and chg >= 10: s -= 5
        if chg is not None and chg >= 9.8: tags.append("涨停")
        if len([t for t in tags if not t.startswith("⚠️")]) >= 3: s += 8
        industry, concepts = board_fields(it)
        pick = {"code": code, "name": name, "theme": "券商金股", "score": round(s, 1),
                "cap": cap, "pe": pe, "main": None, "chg": chg, "price": price,
                "industry": industry, "concepts": concepts, "reason": "+".join(tags[:5])}
        attach_fund_profile(pick, it)
        apply_fund_score(pick)
        old = best_by_code.get(code)
        if (not old) or pick["score"] > old["score"]:
            best_by_code[code] = pick
    scored = sorted(best_by_code.values(), key=lambda x: -x["score"])
    return scored

def pick_contrarian():
    """超跌反弹池：市场情绪偏离分析(近6月超跌+基本面稳健)，找被市场错杀的反弹机会。"""
    datas = iw_query("近6个月跌幅大于20%，营收同比正增长，扣非净利润为正，所属同花顺行业，总市值，市盈率，最新股价",
                     skill="市场情绪偏离分析", limit="100")
    scored = []
    for it in datas:
        code = normalize_code(it.get("股票代码") or ""); name = it.get("股票简称") or ""
        if not code: continue
        sector = str_field(it, "行业"); cap = num(it, "总市值")
        pe = num(it, "市盈率", "PE"); price = num(it, "收盘价", "最新价", "股价")
        drop = num(it, "涨跌幅")   # 近6月跌幅(负数)
        rev = num(it, "营业收入同比增长", "营收同比", "营收增长")
        profit = num(it, "扣非归母净利润", "扣非净利润", "归母净利润")
        s = 8.0; tags = ["超跌反弹"]
        if drop is not None and drop <= -30: s += 12; tags.append("深度超跌")
        elif drop is not None and drop <= -20: s += 8; tags.append("超跌")
        if rev is not None and rev > 20: s += 12; tags.append("成长")
        elif rev is not None and rev > 0: s += 6
        if profit is not None and profit > 0: s += 5; tags.append("盈利")
        if pe is not None and 0 < pe < 35: s += 18; tags.append("低估")
        elif pe is not None and pe < 60: s += 10
        if cap is not None and 1e10 <= cap <= 5e10: s += 12; tags.append("中小盘")
        elif cap is not None and (5e9 <= cap < 1e10 or 5e10 < cap <= 1.5e11): s += 6
        elif cap is not None and cap > 1.5e11: s -= 10; tags.append("⚠️过大")
        if len([t for t in tags if not t.startswith("⚠️")]) >= 3: s += 8
        pick = {"code": code, "name": name, "theme": "超跌反弹", "score": round(s, 1),
                "cap": cap, "pe": pe, "main": None, "chg": drop, "price": price,
                "reason": "+".join(tags[:5])}
        attach_fund_profile(pick, it)
        apply_fund_score(pick)
        scored.append(pick)
    scored.sort(key=lambda x: -x["score"])
    return scored


def pick_relative_strength():
    """市场转弱时寻找仍有资金承接、尚未涨停且可成交的相对强势股。"""
    datas = fetch_public_fund_flow_pool(limit=300)
    scored = []
    for it in datas:
        code = normalize_code(it.get("股票代码") or "")
        name = it.get("股票简称") or ""
        if not code or is_beijing_code(code):
            continue
        chg = num(it, "最新涨跌幅", "涨跌幅")
        main = num(it, "主力资金净流入", "主力净买入")
        cap = num(it, "总市值")
        pe = num(it, "市盈率", "PE")
        price = num(it, "最新价", "收盘价", "股价")
        volume_ratio = num(it, "量比")
        turnover = num(it, "换手率")
        if chg is None or not (-1 <= chg < 6.5) or main is None or main <= 0:
            continue
        if cap is not None and not (5e9 <= cap <= 8e10):
            continue
        if pe is not None and not (0 < pe < 100):
            continue
        score = 45.0
        tags = ["弱市相对强势", "未涨停可成交", "东财资金流估算"]
        if main >= 1e8:
            score += 18; tags.append("资金强承接")
        elif main >= 3e7:
            score += 12
        else:
            score += 6
        if 0 <= chg <= 3.5:
            score += 12; tags.append("位置可控")
        elif chg < 0:
            score += 5; tags.append("抗跌承接")
        if cap is not None and 5e9 <= cap <= 5e10:
            score += 8; tags.append("中小市值")
        if pe is not None and 0 < pe <= 50:
            score += 7; tags.append("估值可接受")
        if volume_ratio is not None and 1.0 <= volume_ratio <= 3.0:
            score += 6; tags.append("量能确认")
        if turnover is not None and 2 <= turnover <= 15:
            score += 4; tags.append("换手健康")
        industry, concepts = board_fields(it)
        pick = {
            "code": code, "name": name, "theme": "弱市相对强势", "score": round(score, 1),
            "cap": cap, "pe": pe, "main": main, "chg": chg, "price": price,
            "industry": industry, "concepts": concepts, "reason": "+".join(tags), "turnover": turnover,
        }
        attach_fund_profile(pick, it)
        apply_fund_score(pick)
        scored.append(pick)
    return sorted(scored, key=lambda x: -x["score"])

def pick_kechuang50():
    """科创50成分股池(科技龙头，今年涨65%的主力)，绕开astock/market。"""
    datas = iw_query("科创50成分股，所属同花顺行业，所属概念，总市值，市盈率，最新涨跌幅，最新股价",
                     skill="hithink-zhishu-query", limit="50")
    scored = []
    for it in datas:
        code = normalize_code(it.get("股票代码") or ""); name = it.get("股票简称") or ""
        if not code: continue
        cap = num(it, "总市值"); pe = num(it, "市盈率", "PE")
        chg = num(it, "涨跌幅"); price = num(it, "收盘价", "最新价", "股价")
        s = 10.0; tags = ["科创50成分"]   # baseline: 科创50龙头(今年+65%主力)
        if pe is not None and 0 < pe < 35: s += 18; tags.append("低估")
        elif pe is not None and pe < 60: s += 10
        elif pe is not None and pe <= 0: s -= 10; tags.append("⚠️亏损")
        if cap is not None and 1e10 <= cap <= 5e10: s += 12; tags.append("中小盘")
        elif cap is not None and (5e9 <= cap < 1e10 or 5e10 < cap <= 1.5e11): s += 6
        elif cap is not None and cap > 1.5e11: s -= 10; tags.append("⚠️过大")
        if chg is not None and 0 <= chg < 6: s += 12; tags.append("动量")
        elif chg is not None and 6 <= chg < 10: s += 5
        elif chg is not None and chg >= 10: s -= 5
        if chg is not None and chg >= 9.8: tags.append("涨停")
        if len([t for t in tags if not t.startswith("⚠️")]) >= 3: s += 8
        industry, concepts = board_fields(it)
        pick = {"code": code, "name": name, "theme": "科创50", "score": round(s, 1),
                "cap": cap, "pe": pe, "main": None, "chg": chg, "price": price,
                "industry": industry, "concepts": concepts, "reason": "+".join(tags[:5])}
        attach_fund_profile(pick, it)
        apply_fund_score(pick)
        scored.append(pick)
    scored.sort(key=lambda x: -x["score"])
    return scored

def main():
    args = sys.argv[1:]
    if "--sector-snapshot" in args:
        if save_public_sector_snapshot() and "--no-generate" not in args:
            subprocess.run(["python3", str(STOCK_LAB_PY), str(SCRIPT_DIR)], check=False)
        return
    if "--backtest" in args:
        days = 7
        for a in args:
            if a.startswith("--days="):
                try: days = int(a.split("=", 1)[1])
                except Exception: pass
        print(f"=== 选股回测 {datetime.now():%Y-%m-%d %H:%M} | horizon {days}d ===")
        backtest_picks(days=days)
        regen()
        return
    if "--risk-alert" in args:
        run_holding_risk_alert(push=("--no-push" not in args))
        return
    if "--premarket" in args:
        print(f"=== 盘前复核 {datetime.now():%H:%M} ===")
        run_holding_risk_alert(push=("--no-push" not in args))
        premarket()
        regen()
        return
    print_only = "--print" in args
    reversal_rescan = "--market-reversal" in args
    volume_focus = "--volume" in args or reversal_rescan
    no_push = "--no-push" in args
    mode_label = " [市场反转重选]" if reversal_rescan else (" [交易量侧重]" if volume_focus else "")
    print(f"=== 趋势事件雷达 {datetime.now():%Y-%m-%d %H:%M} ==={mode_label}")
    signal_ctx = load_signal_context()
    if signal_ctx.get("weak_themes"):
        weak = ", ".join(signal_ctx["weak_themes"].keys())
        print(f"历史防线：弱主题降权/观察 → {weak}")
    if signal_ctx.get("weak_strategies"):
        weak = ", ".join(
            f"{k} EV{v.get('expectancy', 0):+.2f}%"
            for k, v in signal_ctx["weak_strategies"].items()
        )
        print(f"策略熔断：回测期望值为负 → {weak}")
    market_ctx = load_market_fund_context()
    if market_ctx.get("emotion_score") is not None:
        print(
            f"大盘资金上下文：情绪{market_ctx['emotion_score']}，"
            f"主力TOP {len(market_ctx.get('hot_codes', []))} 只，"
            f"龙虎榜 {len(market_ctx.get('lhb_codes', []))} 只"
        )
    trend_candidates = pick_trend_events(volume_focus)
    fill_candidates = pick_gold() + pick_kechuang50()
    if reversal_rescan:
        fill_candidates += pick_relative_strength()
    limit_candidates = pick_limit_up_boards()
    print(
        f"动态主题 + 全市场池 → {len(trend_candidates)} 只；"
        f"非涨停补位池 → {len(fill_candidates)} 只；"
        f"涨停短线池 → {len(limit_candidates)} 只，做事件/舆情排雷..."
    )
    for p in trend_candidates:
        apply_trade_discipline(p)
        apply_history_guardrails(p, signal_ctx)
    for p in fill_candidates:
        apply_trade_discipline(p)
        apply_history_guardrails(p, signal_ctx)
    for p in limit_candidates:
        apply_history_guardrails(p, signal_ctx)
    trend_top = filter_candidates(trend_candidates + fill_candidates, signal_ctx, FALLBACK_TREND_MIN_SCORE, limit=12)
    trend_top = [p for p in trend_top if not is_limit_up_pick(p)]
    limit_top = [] if reversal_rescan else filter_candidates(limit_candidates, signal_ctx, LIMIT_UP_MIN_SCORE, limit=3)
    top = merge_candidates(trend_top + limit_top)
    if not top:
        fallback_source = trend_candidates + fill_candidates
        if not reversal_rescan:
            fallback_source += limit_candidates
        fallback_pool = merge_candidates(fallback_source)
        top = sorted(fallback_pool, key=lambda x: -x.get("score", 0))[:7]
        for p in top:
            p["reason"] = append_tags("观察不买", (p.get("reason") or "").split("+") + ["保底观察池"])
    # 并行深度研究（每只票5个API调用，12只并行）
    enrich_pool = top[:12]
    print(f"并行深度研究 {len(enrich_pool)} 只候选...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(enrich_pick, enrich_pool))
    # 串行应用上下文（轻量操作）
    for p in enrich_pool:
        apply_market_fund_context(p, market_ctx)
        apply_trade_discipline(p)
        apply_history_guardrails(p, signal_ctx)
        apply_agent_council(p)
    top = sorted(top, key=lambda x: -x.get("score", 0))
    non_limit = sorted([p for p in top if not is_limit_up_pick(p)], key=lambda x: -x["score"])[:5]
    limit_watch = sorted([p for p in top if is_limit_up_pick(p)], key=lambda x: -x["score"])[:2]
    top = (non_limit + limit_watch)[:7]
    add_levels(top)
    if reversal_rescan:
        for p in top:
            p["reason"] = append_tags("市场反转后重选", (p.get("reason") or "").split("+"))
    suppressed = [p for p in top if should_suppress_repeated_observation(p)]
    if suppressed:
        names = ", ".join(f"{p['name']}({p['code']})" for p in suppressed)
        print(f"重复观察冷却剔除：{names}")
    top = [p for p in top if not should_suppress_repeated_observation(p)]
    for i, s in enumerate(top, 1): s["rank"] = i
    print("=== 最终 TOP 5 ===")
    for s in top:
        lv = f"买{s.get('buy_point')}/止{s.get('stop_loss')}/目{s.get('target')}" if s.get("buy_point") else ""
        print(f"  {s['rank']}. {s['code']} {s['name']}({s['theme']}) 评分{s['score']} [{s.get('reason','')}] {lv}")
    if not print_only:
        save_picks(top)
        save_market_fund_snapshot()
        if not no_push: push_feishu(top)
        regen()

if __name__ == "__main__":
    main()
