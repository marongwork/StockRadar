import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stock_picker
import strategy_backtest
import virtual_trader
import run_daily_backtest
import generate_stock_lab
import market_intraday_monitor
import public_market_scanner
import agent_council


class AgentCouncilTests(unittest.TestCase):
    def test_directional_agent_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(agent_council.AGENT_WEIGHTS.values()), 1.0)

    def test_aligned_agents_produce_buy_consensus(self):
        review = agent_council.evaluate_candidate({
            "theme": "AI算力", "reason": "事件催化+业绩催化+高成长+量能确认",
            "chg": 2.1, "turnover": 6, "fund_grade": "A", "main": 2e8,
            "super_net": 1e8, "pe": 35, "sector_chg": 1.5, "sector_main_net": 5e9,
        })
        self.assertEqual(review["consensus"], "buy")
        self.assertFalse(review["risk_veto"])
        self.assertTrue(review["mandatory_confirmed"])
        self.assertIn("事件驱动", review["matched_strategies"])
        self.assertIn("资金突破", review["matched_strategies"])
        self.assertIn("热点共振", review["matched_strategies"])
        self.assertEqual(len(review["opinions"]), 6)

    def test_unlock_risk_vetoes_otherwise_bullish_candidate(self):
        review = agent_council.evaluate_candidate({
            "theme": "半导体", "reason": "事件催化+业绩催化+⚠️解禁",
            "chg": 2.1, "turnover": 6, "fund_grade": "A", "main": 2e8,
            "pe": 35, "sector_chg": 1.5, "sector_main_net": 5e9,
        })
        self.assertEqual(review["consensus"], "hold")
        self.assertTrue(review["risk_veto"])

    def test_missing_capital_confirmation_cannot_be_voted_into_buy(self):
        review = agent_council.evaluate_candidate({
            "theme": "券商金股", "reason": "券商金股+低估+动量",
            "chg": 1.6, "pe": 16,
        })
        self.assertEqual(review["consensus"], "hold")
        self.assertFalse(review["mandatory_confirmed"])
        self.assertIn("capital", review["degraded_agents"])

    def test_broker_recommendation_is_not_an_event_catalyst(self):
        review = agent_council.evaluate_candidate({
            "theme": "券商金股", "reason": "券商金股+低估+动量",
            "chg": 1.6, "turnover": 6, "fund_grade": "A", "main": 2e8,
            "pe": 16, "sector_chg": 1.0, "sector_main_net": 2e9,
        })
        event = next(item for item in review["opinions"] if item["agent"] == "event")
        self.assertEqual(event["signal"], "hold")
        self.assertFalse(review["mandatory_confirmed"])

    def test_generic_momentum_label_is_not_volume_confirmation(self):
        opinion = agent_council.technical_opinion({"reason": "动量", "chg": 0.0})
        self.assertEqual(opinion.signal, "hold")

    def test_valuation_without_growth_is_not_quality_buy(self):
        opinion = agent_council.quality_opinion({"reason": "低估+估值可接受", "pe": 16})
        self.assertEqual(opinion.signal, "hold")

    def test_overheated_emotion_cycle_is_blocked(self):
        review = agent_council.evaluate_candidate({"reason": "情绪82+⚠️情绪过热减仓"})
        strategy = next(item for item in review["strategy_assessments"] if item["strategy"] == "emotion_cycle")
        self.assertEqual(strategy["status"], "block")

    def test_agent_hold_cannot_receive_executable_levels(self):
        pick = {
            "code": "600000", "name": "测试", "theme": "趋势事件", "score": 95,
            "reason": "资金流入", "price": 10.0, "chg": 2.0, "fund_grade": "A",
            "agent_consensus": "hold", "risk_veto": 0,
        }
        stock_picker.add_levels([pick])
        self.assertIsNone(pick.get("buy_point"))
        self.assertIn("Agent未形成买入共识", pick["reason"])

    def test_agent_review_fields_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invest.db"
            with mock.patch.object(stock_picker, "DB_PATH", db_path):
                pick = {
                    "code": "600000", "name": "测试", "theme": "趋势事件", "score": 80,
                    "reason": "事件催化+业绩催化+量能确认", "price": 10.0, "chg": 2.0,
                    "fund_grade": "A", "main": 2e8, "sector_chg": 1.0, "sector_main_net": 2e9,
                    "cap": 2e10, "pe": 25,
                }
                stock_picker.apply_agent_council(pick)
                stock_picker.save_picks([pick])
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT agent_consensus,agent_reviews_json,risk_veto FROM stock_picks").fetchone()
            conn.close()
            self.assertEqual(row[0], "buy")
            self.assertIn('"version":"council-v1"', row[1])
            self.assertEqual(row[2], 0)

    def test_agent_backtest_keeps_only_buy_consensus_without_veto(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE stock_picks (
                id INTEGER,code TEXT,name TEXT,theme TEXT,reason TEXT,picked_date TEXT,
                picked_at TEXT,run_id TEXT,rank INTEGER,buy_point REAL,
                agent_consensus TEXT,risk_veto INTEGER
            )"""
        )
        conn.executemany("INSERT INTO stock_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
            (1,"600001","通过","趋势","","2026-07-20","2026-07-20 09:45","a",1,10,"buy",0),
            (2,"600002","分歧","趋势","","2026-07-20","2026-07-20 09:45","a",2,10,"hold",0),
            (3,"600003","否决","趋势","","2026-07-20","2026-07-20 09:45","a",3,10,"buy",1),
        ])
        rows = strategy_backtest.load_picks(conn, "agent_council", None, None)
        self.assertEqual([row["code"] for row in rows], ["600001"])


class BeijingExchangeFilterTests(unittest.TestCase):
    def test_all_beijing_code_generations_are_blocked(self):
        for code in ("430047", "830799", "873001", "920106"):
            with self.subTest(code=code):
                self.assertTrue(stock_picker.is_beijing_code(code))
                self.assertTrue(strategy_backtest.is_beijing_code(code))
                self.assertTrue(virtual_trader.is_beijing_code(code))

    def test_shanghai_shenzhen_codes_remain_eligible(self):
        for code in ("600000", "000001", "300750", "688981"):
            with self.subTest(code=code):
                self.assertFalse(stock_picker.is_beijing_code(code))
                self.assertFalse(strategy_backtest.is_beijing_code(code))
                self.assertFalse(virtual_trader.is_beijing_code(code))

    def test_backtest_loader_excludes_beijing_codes(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE stock_picks (
                id INTEGER, code TEXT, name TEXT, theme TEXT, reason TEXT,
                picked_date TEXT, picked_at TEXT, run_id TEXT, rank INTEGER,
                buy_point REAL
            )"""
        )
        rows = [
            (1, "920106", "北交样本", "趋势", "", "2026-07-21", "2026-07-21 09:45", "a", 1, 10.0),
            (2, "688981", "沪市样本", "趋势", "", "2026-07-21", "2026-07-21 09:45", "a", 2, 20.0),
        ]
        conn.executemany("INSERT INTO stock_picks VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        loaded = strategy_backtest.load_picks(conn, "all", None, None)
        self.assertEqual([row["code"] for row in loaded], ["688981"])

    def test_virtual_buy_entrypoint_cannot_buy_beijing_stock(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE stock_picks (id INTEGER PRIMARY KEY, theme TEXT)")
        virtual_trader.ensure_schema(conn)
        pick = {
            "id": 920106, "code": "920106", "name": "北交样本", "score": 99,
            "theme": "趋势事件", "reason": "资金流入", "position_pct": 3,
        }
        bought = virtual_trader.buy(conn, pick, 10.0, "测试")
        self.assertFalse(bought)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM virtual_positions").fetchone()[0], 0)
        reason = conn.execute("SELECT reason FROM virtual_signal_observations").fetchone()[0]
        self.assertIn("禁止交易北交所", reason)


class ExecutableLevelTests(unittest.TestCase):
    def test_fund_unconfirmed_signal_stays_observation(self):
        pick = {
            "code": "600000", "name": "测试", "theme": "趋势事件", "score": 75,
            "reason": "事件催化+资金流入", "price": 10.0, "chg": 2.0,
            "fund_grade": "C",
        }
        stock_picker.add_levels([pick])
        self.assertIsNone(pick.get("buy_point"))
        self.assertIn("资金未确认", pick["reason"])

    def test_high_score_c_grade_cannot_bypass_fund_gate(self):
        pick = {
            "code": "600000", "name": "测试", "theme": "趋势事件", "score": 95,
            "reason": "事件催化+资金流入", "price": 10.0, "chg": 2.0,
            "fund_grade": "C",
        }
        stock_picker.add_levels([pick])
        self.assertIsNone(pick.get("buy_point"))
        self.assertIn("资金未确认", pick["reason"])

    def test_k50_uses_same_70_point_execution_floor(self):
        pick = {
            "code": "688981", "name": "测试", "theme": "科创50", "score": 69,
            "reason": "资金流入", "price": 10.0, "chg": 2.0,
            "fund_grade": "A",
        }
        stock_picker.add_levels([pick])
        self.assertIsNone(pick.get("buy_point"))
        self.assertIn("评分未达买点阈值", pick["reason"])

    def test_strong_funded_signal_gets_levels(self):
        pick = {
            "code": "600000", "name": "测试", "theme": "趋势事件", "score": 75,
            "reason": "事件催化+资金流入", "price": 10.0, "chg": 2.0,
            "fund_grade": "A",
        }
        stock_picker.add_levels([pick])
        self.assertEqual(pick["buy_point"], 10.0)
        self.assertEqual(pick["stop_loss"], 9.3)
        self.assertEqual(pick["target"], 12.0)

    def test_virtual_account_rejects_high_score_c_grade(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        pick = {
            "code": "600000", "name": "测试", "theme": "趋势事件", "score": 95,
            "reason": "事件催化+资金流入", "fund_grade": "C", "buy_point": 10.0,
        }
        quote = {"date": virtual_trader.today_str(), "current": 10.0, "chg_pct": 2.0}
        with mock.patch.object(virtual_trader, "in_entry_window", return_value=True):
            passed, _, reason = virtual_trader.strategy_gate(conn, pick, quote)
        self.assertFalse(passed)
        self.assertIn("缺少资金A/B档确认", reason)


class RiskScanPersistenceTests(unittest.TestCase):
    def test_empty_scan_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invest.db"
            sqlite3.connect(db_path).close()
            with mock.patch.object(stock_picker, "DB_PATH", db_path):
                stock_picker.save_holding_risk_alerts([])
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT alert_count,status FROM risk_scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row, (0, "clear"))


class DailyBacktestCleanupTests(unittest.TestCase):
    def test_only_latest_run_per_strategy_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invest.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE backtest_runs (id INTEGER PRIMARY KEY,run_at TEXT,strategy TEXT)")
            conn.execute("CREATE TABLE backtest_trades (id INTEGER PRIMARY KEY,run_id INTEGER)")
            conn.executemany("INSERT INTO backtest_runs VALUES (?,?,?)", [
                (1, "2026-07-22 11:00:00", "all"),
                (2, "2026-07-22 12:00:00", "all"),
                (3, "2026-07-22 12:00:01", "trend"),
                (4, "2026-07-21 12:00:00", "all"),
            ])
            conn.executemany("INSERT INTO backtest_trades VALUES (?,?)", [(1, 1), (2, 2)])
            conn.commit(); conn.close()
            run_daily_backtest.prune_same_day_runs(db_path, "2026-07-22")
            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT id FROM backtest_runs ORDER BY id").fetchall(), [(2,), (3,), (4,)])
            self.assertEqual(conn.execute("SELECT run_id FROM backtest_trades").fetchall(), [(2,)])
            conn.close()

    def test_empty_backtest_run_replaces_stale_strategy_result(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        strategy_backtest.ensure_schema(conn)
        args = mock.MagicMock(
            start="2026-06-21", end="2026-07-22", max_days=None,
            fee_bps=5.0, slippage_bps=20.0, datalen=180,
        )
        with mock.patch.object(strategy_backtest, "benchmark_returns", return_value=[{"name":"上证指数","return":-5.0}]):
            run_id = strategy_backtest.save_run(conn, "agent_council", args, [])
        row = conn.execute("SELECT strategy,trades,qualified,excess_return,benchmarks_json FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(tuple(row[:4]), ("agent_council", 0, 0, None))
        self.assertIsNone(strategy_backtest.json.loads(row[4])[0]["excess"])

    def test_signal_context_uses_only_latest_run_per_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invest.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE stock_picks (code TEXT,picked_date TEXT,theme TEXT,eval_return_pct REAL)")
            conn.execute("CREATE TABLE backtest_runs (id INTEGER PRIMARY KEY,strategy TEXT,trades INTEGER,expectancy REAL,qualified INTEGER)")
            conn.execute("""CREATE TABLE backtest_trades (
                run_id INTEGER,theme TEXT,strategy TEXT,return_pct REAL,signal_date TEXT
            )""")
            conn.executemany("INSERT INTO backtest_runs VALUES (?,?,?,?,?)", [(1, "trend", 20, -2, 0), (2, "trend", 8, 1, 0)])
            old = [(1, "趋势", "trend", -2.0, "2026-07-20") for _ in range(20)]
            latest = [(2, "趋势", "trend", 1.0, "2026-07-21") for _ in range(8)]
            conn.executemany("INSERT INTO backtest_trades VALUES (?,?,?,?,?)", old + latest)
            conn.commit(); conn.close()
            with mock.patch.object(stock_picker, "DB_PATH", db_path):
                ctx = stock_picker.load_signal_context()
            self.assertNotIn("trend", ctx["weak_strategies"])
            self.assertEqual(ctx["unqualified_strategies"]["trend"]["count"], 8)


class MarketRadarRenderingTests(unittest.TestCase):
    def test_sector_snapshot_only_accepts_trading_session(self):
        from datetime import datetime
        self.assertTrue(stock_picker.is_sector_snapshot_time(datetime(2026, 7, 22, 9, 15)))
        self.assertEqual(stock_picker.sector_snapshot_phase(datetime(2026, 7, 22, 9, 25)), "preopen")
        self.assertTrue(stock_picker.is_sector_snapshot_time(datetime(2026, 7, 22, 9, 30)))
        self.assertTrue(stock_picker.is_sector_snapshot_time(datetime(2026, 7, 22, 15, 0)))
        self.assertFalse(stock_picker.is_sector_snapshot_time(datetime(2026, 7, 22, 12, 45)))
        self.assertFalse(stock_picker.is_sector_snapshot_time(datetime(2026, 7, 22, 15, 1)))

    def test_market_radar_renders_decision_and_lhb_amounts(self):
        snapshot = {
            "snapshot_at": "2026-07-22 09:45",
            "market": [{
                "指数简称": "同花顺全A(沪深京)", "最新涨跌幅": 0.1,
                "上涨家数": 2400, "下跌家数": 2900, "涨停家数": 20,
                "跌停家数": 5, "成交额": 6.5e11, "主力净买入额": 3.7e9,
            }],
            "sectors": [{"股票简称": "测试股", "主力资金流向": 8e8, "涨跌幅": 3.2}],
            "limit": [{"股票简称": "连板股", "连续涨停天数": 3, "涨停封单额": 2e8}],
            "sentiment": [{"指数简称": "电子", "板块热度": 500, "涨跌幅": 1.2}],
            "lhb": [{
                "股票代码": "600000.SH", "股票简称": "榜单股", "上榜日期": "20260722",
                "上榜原因": "日涨幅偏离值达7%", "买入额": 2e8, "卖出额": 1e8,
                "净买入额": 1e8, "营业部名称": "测试营业部", "营业部类型": ["知名游资"],
            }],
        }
        html = generate_stock_lab.market_fund_panel(snapshot)
        self.assertIn("谨慎试错", html)
        self.assertIn("大板块主力净流入 TOP", html)
        self.assertIn("主力净流出 TOP", html)
        self.assertIn("1.0亿", html)
        self.assertIn("日涨幅偏离值达7%", html)
        self.assertIn("知名游资", html)
        self.assertIn("class='pyramid-stock'", html)
        self.assertIn("查看 连板股 详情分析", html)

    def test_stock_board_fields_and_sector_rotation_render(self):
        industry, concepts = stock_picker.board_fields({
            "所属同花顺行业": "半导体",
            "所属概念": "存储芯片、汽车芯片、国产替代",
        })
        self.assertEqual(industry, "半导体")
        self.assertIn("存储芯片", concepts)
        snapshot = {
            "snapshot_at": "2026-07-22 14:30", "market": [], "sectors": [], "limit": [],
            "sector_history": [
                {"snapshot_at": "2026-07-22 14:30", "sectors": [{"板块名称": "机器人", "主力资金净流入": 2e9, "涨跌幅": 2.0}]},
                {"snapshot_at": "2026-07-22 12:45", "sectors": [{"板块名称": "固态电池", "主力资金净流入": 1e9, "涨跌幅": 1.0}]},
            ],
        }
        rendered = generate_stock_lab.market_fund_panel(snapshot)
        self.assertIn("大板块排名变化", rendered)
        self.assertIn("新进", rendered)
        self.assertIn("固态电池 · 掉出TOP", rendered)


class IntradayMarketMonitorTests(unittest.TestCase):
    def test_large_surge_reversal_is_level_three(self):
        quote = {"chg_pct": -1.9, "high_chg_pct": 1.6, "retreat_pct": 3.5}
        event = market_intraday_monitor.detect_event(quote, [])
        self.assertEqual(event[:2], ("surge_reversal", 3))

    def test_normal_intraday_noise_does_not_alert(self):
        quote = {"chg_pct": 0.2, "high_chg_pct": 0.5, "retreat_pct": 0.3}
        self.assertIsNone(market_intraday_monitor.detect_event(quote, []))

    def test_cross_index_context_explains_growth_selloff(self):
        quotes = {
            "sh000688": {"chg_pct": -1.9},
            "sh000001": {"chg_pct": -0.1},
            "sz399006": {"chg_pct": -2.8},
        }
        text = market_intraday_monitor.explain(quotes, {"up": 1500, "down": 3500, "main_net": -2e10})
        self.assertIn("成长风格整体退潮", text)
        self.assertIn("科技成长明显跑输权重", text)
        self.assertIn("风险偏好正在收缩", text)

    def test_iwencai_skill_budget_has_hard_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invest.db"
            sqlite3.connect(db_path).close()
            with mock.patch.object(stock_picker, "DB_PATH", db_path), mock.patch.object(stock_picker, "IWENCAI_DAILY_SKILL_LIMIT", 2):
                self.assertTrue(stock_picker.reserve_iwencai_call("test-skill"))
                self.assertTrue(stock_picker.reserve_iwencai_call("test-skill"))
                self.assertFalse(stock_picker.reserve_iwencai_call("test-skill"))

    def test_recent_market_reversal_blocks_new_virtual_positions(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE market_intraday_alerts (
            id INTEGER PRIMARY KEY, alert_at TEXT, trade_date TEXT, severity INTEGER, title TEXT
        )""")
        now = virtual_trader.dt.datetime.now()
        conn.execute(
            "INSERT INTO market_intraday_alerts VALUES(1,?,?,3,?)",
            (now.strftime("%Y-%m-%d %H:%M:%S"), virtual_trader.today_str(), "冲高后大幅转弱"),
        )
        reason = virtual_trader.intraday_entry_block(conn, now)
        self.assertIn("30分钟内不开新仓", reason)

    def test_public_fund_flow_fields_are_normalized(self):
        payload = {"data": {"diff": [{
            "f12": "600000", "f14": "测试", "f2": 10.2, "f3": 2.1,
            "f5": 1000, "f6": 2000, "f8": 3.2, "f9": 12.0, "f10": 1.4,
            "f20": 2e10, "f62": 8e7, "f184": 4.0,
        }]}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = stock_picker.json.dumps(payload).encode()
        with mock.patch.object(stock_picker.urllib.request, "urlopen", return_value=response):
            rows = stock_picker.fetch_public_fund_flow_pool(1)
        self.assertEqual(rows[0]["股票代码"], "600000")
        self.assertEqual(rows[0]["主力资金净流入"], 8e7)
        self.assertIn("估算", rows[0]["资金数据源"])

    def test_public_scanner_rejects_limit_up_and_accepts_flow_signal(self):
        limit_up = {"最新涨跌幅": 10, "主力资金净流入": 3e8, "主力净流入占比": 12, "量比": 2, "换手率": 5, "资金排名": 1}
        normal = dict(limit_up, 最新涨跌幅=3.2)
        self.assertIsNone(public_market_scanner.detect_signal(limit_up))
        self.assertIsNotNone(public_market_scanner.detect_signal(normal))

    def test_eastmoney_single_quote_contains_daily_change(self):
        payload = {"data": {"f43": 91.6, "f44": 97, "f45": 90, "f46": 96,
                            "f48": 1e9, "f57": "688213", "f58": "测试", "f60": 96.12,
                            "f86": 1784703600, "f170": -4.7}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = virtual_trader.json.dumps(payload).encode()
        with mock.patch.object(virtual_trader.urllib.request, "urlopen", return_value=response):
            quote = virtual_trader.fetch_eastmoney_quote("688213")
        self.assertEqual(quote["current"], 91.6)
        self.assertEqual(quote["chg_pct"], -4.7)
        self.assertIn("东方财富", quote["source"])


if __name__ == "__main__":
    unittest.main()
