#!/usr/bin/env python3
"""Deterministic multi-agent review for A-share radar candidates.

The contract follows the useful part of daily_stock_analysis: specialists emit
independent opinions, disagreement stays visible, and risk can veto a buy.  It
uses facts already collected by the radar, so it adds no external API calls.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class AgentOpinion:
    agent: str
    label: str
    signal: str
    confidence: float
    evidence: tuple[str, ...]


SIGNAL_VALUE = {"buy": 1.0, "hold": 0.0, "sell": -1.0}
AGENT_WEIGHTS = {
    "technical": 0.24,
    "capital": 0.30,
    "event": 0.20,
    "sector": 0.15,
    "quality": 0.11,
}
HARD_RISK_WORDS = (
    "⚠️监管", "立案", "处罚", "业绩预亏", "业绩变脸", "⚠️减持",
    "⚠️解禁", "⚠️舆情利空", "⚠️策略样本不足", "⚠️趋势熔断",
)
SOFT_RISK_WORDS = (
    "⚠️质押", "⚠️高PE", "⚠️估值异常", "⚠️资金流出", "⚠️龙虎榜机构卖",
    "⚠️破位", "涨幅超过6.5%", "跌幅超过2%",
)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _opinion(agent: str, label: str, signal: str, confidence: float, evidence: Iterable[str]) -> AgentOpinion:
    facts = tuple(dict.fromkeys(str(item) for item in evidence if item))[:4]
    return AgentOpinion(agent, label, signal, round(max(0.0, min(1.0, confidence)), 2), facts)


def technical_opinion(pick: dict[str, Any]) -> AgentOpinion:
    reason = str(pick.get("reason") or "")
    chg = _number(pick.get("chg"))
    turnover = _number(pick.get("turnover"))
    evidence: list[str] = []
    if "涨停短线" in reason or "涨停板" in str(pick.get("theme") or "") or (chg is not None and chg >= 9.5):
        return _opinion("technical", "技术", "hold", 0.96, ["当日涨停不可追买", "等待次日竞价承接"])
    if "⚠️破位" in reason or (chg is not None and chg < -2):
        return _opinion("technical", "技术", "sell", 0.78, ["价格弱于可执行区间", f"当日涨跌 {chg:+.2f}%" if chg is not None else "破位"])
    score = 0
    if chg is not None and 0.2 <= chg <= 5.0:
        score += 2; evidence.append(f"温和走强 {chg:+.2f}%")
    elif chg is not None and -1.0 <= chg < 0.2:
        score += 1; evidence.append(f"价格位置可控 {chg:+.2f}%")
    if turnover is not None and 2 <= turnover <= 15:
        score += 1; evidence.append(f"换手率 {turnover:.1f}%")
    if any(word in reason for word in ("量能确认", "放量", "相对强势")):
        score += 1; evidence.append("量价结构确认")
    return _opinion("technical", "技术", "buy" if score >= 2 else "hold", 0.55 + score * 0.08, evidence or ["技术证据不足"])


def capital_opinion(pick: dict[str, Any]) -> AgentOpinion:
    grade = str(pick.get("fund_grade") or "").upper()
    main = _number(pick.get("main" if "main" in pick else "main_net"))
    super_net = _number(pick.get("super_net"))
    lobby = _number(pick.get("lobby_net"))
    evidence = [f"资金 {grade or '-'} 档"]
    positives = sum(value is not None and value > 0 for value in (main, super_net, lobby))
    negatives = sum(value is not None and value < 0 for value in (main, super_net, lobby))
    if main is not None: evidence.append(f"主力净额 {main / 1e8:+.2f}亿")
    if lobby is not None: evidence.append(f"龙虎榜机构 {lobby / 1e8:+.2f}亿")
    if grade in ("A", "B") and positives >= negatives:
        return _opinion("capital", "资金", "buy", 0.88 if grade == "A" else 0.74, evidence)
    if grade in ("D", "E") or negatives > positives:
        return _opinion("capital", "资金", "sell", 0.86 if grade == "E" else 0.72, evidence)
    return _opinion("capital", "资金", "hold", 0.6, evidence + ["资金未形成同向确认"])


def event_opinion(pick: dict[str, Any]) -> AgentOpinion:
    reason = str(pick.get("reason") or "")
    strong_positive_words = ("事件催化", "业绩催化", "中标", "合作", "获批", "舆情利好")
    weak_positive_words = ("机构看好", "券商金股")
    negative_words = HARD_RISK_WORDS + ("⚠️质押", "⚠️龙虎榜机构卖")
    positives = [word for word in strong_positive_words if word in reason]
    opinions = [word for word in weak_positive_words if word in reason]
    negatives = [word for word in negative_words if word in reason]
    if negatives:
        return _opinion("event", "事件", "sell", min(0.96, 0.68 + 0.07 * len(negatives)), negatives)
    if positives:
        return _opinion("event", "事件", "buy", min(0.9, 0.58 + 0.07 * len(positives)), positives)
    if opinions:
        return _opinion("event", "事件", "hold", 0.62, opinions + ["机构观点不是独立事件催化"])
    return _opinion("event", "事件", "hold", 0.55, ["缺少可核验的新事件"])


def sector_opinion(pick: dict[str, Any]) -> AgentOpinion:
    chg = _number(pick.get("sector_chg"))
    flow = _number(pick.get("sector_main_net"))
    reason = str(pick.get("reason") or "")
    evidence: list[str] = []
    if chg is not None: evidence.append(f"板块涨跌 {chg:+.2f}%")
    if flow is not None: evidence.append(f"板块主力 {flow / 1e8:+.2f}亿")
    if chg is not None and chg > 0 and flow is not None and flow > 0:
        return _opinion("sector", "板块", "buy", 0.82, evidence + ["板块量价共振"])
    if (chg is not None and chg < -1) or (flow is not None and flow < 0) or "弱主题" in reason:
        return _opinion("sector", "板块", "sell", 0.72, evidence or ["主题历史表现偏弱"])
    return _opinion("sector", "板块", "hold", 0.56, evidence or ["板块证据不足"])


def quality_opinion(pick: dict[str, Any]) -> AgentOpinion:
    pe = _number(pick.get("pe"))
    reason = str(pick.get("reason") or "")
    evidence: list[str] = []
    if pe is not None: evidence.append(f"PE {pe:.1f}")
    growth_facts = [word for word in ("高成长", "成长", "盈利", "业绩催化") if word in reason]
    valuation_facts = [word for word in ("低估", "估值可接受") if word in reason]
    if pe is not None and (pe <= 0 or pe > 100):
        return _opinion("quality", "质量", "sell", 0.74, evidence + ["估值或盈利质量异常"])
    if growth_facts and pe is not None and 0 < pe <= 60:
        return _opinion("quality", "质量", "buy", 0.7 + min(0.12, len(growth_facts) * 0.04), evidence + growth_facts[:2] + ["成长与估值相互验证"])
    if valuation_facts:
        return _opinion("quality", "质量", "hold", 0.58, evidence + valuation_facts + ["估值观点缺少成长数据验证"])
    return _opinion("quality", "质量", "hold", 0.5, evidence or ["基本面验证不足"])


def risk_opinion(pick: dict[str, Any]) -> tuple[AgentOpinion, str, bool, list[str]]:
    reason = str(pick.get("reason") or "")
    chg = _number(pick.get("chg"))
    hard = [word for word in HARD_RISK_WORDS if word in reason]
    soft = [word for word in SOFT_RISK_WORDS if word in reason]
    if "涨停短线" in reason or "涨停板" in str(pick.get("theme") or "") or (chg is not None and chg >= 9.5):
        soft.append("当日涨停不可成交")
    hard = list(dict.fromkeys(hard))
    soft = list(dict.fromkeys(soft))
    flags = hard + soft
    if hard:
        return _opinion("risk", "风险", "sell", min(0.99, 0.82 + 0.03 * len(hard)), hard), "high", True, flags
    if len(soft) >= 2:
        return _opinion("risk", "风险", "sell", 0.78, soft), "medium", True, flags
    if soft:
        return _opinion("risk", "风险", "hold", 0.66, soft), "medium", False, flags
    return _opinion("risk", "风险", "hold", 0.6, ["未发现已采集的硬风险"]), "low", False, []


def strategy_assessments(pick: dict[str, Any], opinions: list[AgentOpinion]) -> list[dict[str, Any]]:
    """Evaluate adapted daily_stock_analysis strategies on the same evidence."""
    by_agent = {item.agent: item for item in opinions}
    reason = str(pick.get("reason") or "")
    theme = str(pick.get("theme") or "")
    chg = _number(pick.get("chg"))
    emotion_match = re.search(r"情绪(\d{1,3})", reason)
    emotion = int(emotion_match.group(1)) if emotion_match else None

    def item(key: str, label: str, status: str, evidence: str) -> dict[str, Any]:
        return {"strategy": key, "label": label, "status": status, "evidence": evidence}

    event_status = "pass" if by_agent["event"].signal == "buy" else ("block" if by_agent["event"].signal == "sell" else "watch")
    capital_status = "pass" if by_agent["capital"].signal == "buy" and by_agent["technical"].signal == "buy" else ("block" if by_agent["capital"].signal == "sell" else "watch")
    theme_status = "pass" if by_agent["sector"].signal == "buy" and by_agent["event"].signal == "buy" else ("block" if by_agent["sector"].signal == "sell" else "watch")
    quality_status = "pass" if by_agent["quality"].signal == "buy" else ("block" if by_agent["quality"].signal == "sell" else "watch")
    is_limit = "涨停短线" in reason or "涨停板" in theme or (chg is not None and chg >= 9.5)
    pullback_status = "pass" if by_agent["capital"].signal == "buy" and chg is not None and -1 <= chg <= 1 and by_agent["technical"].signal != "sell" else "watch"
    overheated = "情绪过热" in reason or (emotion is not None and emotion > 75)
    emotion_status = "block" if overheated or (emotion is not None and emotion < 35) else ("pass" if emotion is not None and 45 <= emotion <= 70 else "watch")
    return [
        item("event_driven", "事件驱动", event_status, "；".join(by_agent["event"].evidence)),
        item("volume_breakout", "资金突破", capital_status, "技术与资金必须同向"),
        item("hot_theme", "热点共振", theme_status, "板块资金与事件共同确认"),
        item("growth_quality", "成长质量", quality_status, "；".join(by_agent["quality"].evidence)),
        item("dragon_head", "涨停龙头", "watch" if is_limit else "inactive", "涨停当天只观察，次日竞价确认"),
        item("shrink_pullback", "缩量回踩", pullback_status, "资金确认且涨跌处于 -1% 至 +1%"),
        item("emotion_cycle", "情绪周期", emotion_status, f"市场情绪 {emotion}" if emotion is not None else "缺少情绪分值"),
    ]


def evaluate_candidate(pick: dict[str, Any]) -> dict[str, Any]:
    opinions = [
        technical_opinion(pick), capital_opinion(pick), event_opinion(pick),
        sector_opinion(pick), quality_opinion(pick),
    ]
    risk, risk_level, veto_buy, risk_flags = risk_opinion(pick)
    strategies = strategy_assessments(pick, opinions)
    directional_weight = sum(AGENT_WEIGHTS[item.agent] for item in opinions if item.signal != "hold")
    weighted_score = sum(AGENT_WEIGHTS[item.agent] * SIGNAL_VALUE[item.signal] * item.confidence for item in opinions)
    disagreement = 0.0 if directional_weight == 0 else max(0.0, 1 - abs(weighted_score) / directional_weight) * 100
    opinion_map = {item.agent: item for item in opinions}
    bullish = sum(item.signal == "buy" for item in opinions)
    bearish = sum(item.signal == "sell" for item in opinions)
    mandatory_confirmed = (
        opinion_map["capital"].signal == "buy"
        and opinion_map["technical"].signal == "buy"
        and opinion_map["event"].signal == "buy"
    )
    degraded_agents = [
        item.agent for item in opinions
        if item.evidence and any(word in " ".join(item.evidence) for word in ("不足", "未形成", "缺少"))
    ]
    if weighted_score >= 0.28 and bullish >= 3 and mandatory_confirmed:
        pre_risk = "buy"
    elif weighted_score <= -0.18 and bearish >= 1:
        pre_risk = "sell"
    else:
        pre_risk = "hold"
    consensus = "hold" if veto_buy and pre_risk == "buy" else pre_risk
    confidence = min(0.96, abs(weighted_score) + (0.18 if consensus != "hold" else 0.42))
    payload = {
        "version": "council-v1",
        "consensus": consensus,
        "pre_risk_consensus": pre_risk,
        "confidence": round(confidence, 2),
        "disagreement": round(disagreement, 1),
        "weighted_score": round(weighted_score, 3),
        "risk_level": risk_level,
        "risk_veto": veto_buy,
        "risk_flags": risk_flags,
        "mandatory_confirmed": mandatory_confirmed,
        "degraded_agents": degraded_agents,
        "data_quality": "complete" if not degraded_agents else "partial",
        "matched_strategies": [item["label"] for item in strategies if item["status"] == "pass"],
        "strategy_assessments": strategies,
        "opinions": [asdict(item) for item in opinions + [risk]],
    }
    return payload


def apply_candidate_review(pick: dict[str, Any]) -> dict[str, Any]:
    review = evaluate_candidate(pick)
    pick["agent_consensus"] = review["consensus"]
    pick["agent_confidence"] = review["confidence"]
    pick["agent_disagreement"] = review["disagreement"]
    pick["risk_level"] = review["risk_level"]
    pick["risk_veto"] = int(review["risk_veto"])
    pick["risk_reasons"] = "+".join(review["risk_flags"])
    pick["agent_reviews_json"] = json.dumps(review, ensure_ascii=False, separators=(",", ":"))
    return review
