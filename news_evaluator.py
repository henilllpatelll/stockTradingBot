from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List

import anthropic

from config import NewsEvaluatorConfig
from strategy import PatternContext

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a professional day trader evaluating premarket setups using the Warrior Trading method.

STRATEGY RULES (all must apply for a trade):
1. Relative volume (RVOL) ≥ 5x — crowd is watching this stock
2. Already up ≥ 10% on the day — momentum is real
3. Clear catalyst: earnings beat, FDA approval, major contract, buyout rumor, short squeeze catalyst, PR from credible source
4. Price $1–$20 — sweet spot for volatility and crowd participation
5. Low float < 20M shares — limited supply creates explosive moves

CATALYST QUALITY RUBRIC:
- strong (confidence 0.80–1.00): earnings beat, FDA approval, merger/acquisition, major contract, biotech catalyst — news that creates sustained crowd participation
- medium (confidence 0.55–0.79): PR release, executive buy, industry news, secondary sector catalyst
- weak (confidence 0.30–0.54): vague PR, unverified rumor, low-credibility source
- negative (confidence 0.00–0.29): dilution/offering, reverse split, SEC investigation, fake/misleading headline

RISK FLAGS (any of these = should_trade: false):
- "dilution_risk": offering, ATM program, shelf registration items 3.01/3.02
- "extended_premarket": stock already up >50% premarket with no pullback — chasing
- "no_catalyst": moving on no identifiable news — dangerous and short-lived
- "low_credibility_source": press release from unknown PR firm, unverified social media
- "halt_risk": stock previously halted today — unpredictable

SETUP QUALITY:
- A+: Strong catalyst + confirmed chart pattern + RVOL > 8x + price < $10
- A: Strong/medium catalyst + confirmed pattern + RVOL 5–8x
- B: Medium catalyst + pattern + RVOL 5x — proceed with caution, reduce size
- skip: Weak/negative catalyst OR no pattern

MARKET REGIME AWARENESS:
- hot market: lower confidence threshold acceptable, float < 10M is ideal
- cold market: only A+ setups, float < 5M, no extended premarket setups

Your role: quickly assess whether the catalyst + setup creates sustainable crowd participation for a Warrior-style breakout trade. Think: "Is this the most obvious stock right now? Will the crowd pile in?"
"""

_TOOL_SCHEMA = {
    "name": "trade_assessment",
    "description": "Structured trade go/no-go decision for a Warrior Trading premarket setup",
    "input_schema": {
        "type": "object",
        "properties": {
            "should_trade": {
                "type": "boolean",
                "description": "True only if catalyst is strong/medium AND setup quality is A+ or A"
            },
            "confidence": {
                "type": "number",
                "description": "0.0–1.0 confidence in the catalyst quality"
            },
            "catalyst_quality": {
                "type": "string",
                "enum": ["strong", "medium", "weak", "negative"],
                "description": "Quality tier of the news catalyst"
            },
            "catalyst_type": {
                "type": "string",
                "description": "e.g. earnings, FDA, merger, PR, offering, short_squeeze, sector_move, unknown"
            },
            "setup_quality": {
                "type": "string",
                "enum": ["A+", "A", "B", "skip"],
                "description": "Overall setup quality combining catalyst + pattern + RVOL"
            },
            "reasoning": {
                "type": "string",
                "description": "2–3 sentence explanation of the decision, citing specific catalyst details"
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Active risk flags from the rubric"
            },
            "entry_price_target": {
                "type": "number",
                "description": "Suggested entry price (0 if should_trade is false)"
            },
            "stop_suggestion": {
                "type": "number",
                "description": "Suggested stop price (0 if should_trade is false)"
            },
        },
        "required": ["should_trade", "confidence", "catalyst_quality", "catalyst_type",
                     "setup_quality", "reasoning", "risk_flags", "entry_price_target", "stop_suggestion"],
    },
}


@dataclass
class TradeDecision:
    should_trade: bool
    confidence: float
    catalyst_quality: str
    catalyst_type: str
    setup_quality: str
    reasoning: str
    risk_flags: List[str] = field(default_factory=list)
    entry_price_target: float = 0.0
    stop_suggestion: float = 0.0

    @classmethod
    def reject(cls, reason: str = "api_error") -> "TradeDecision":
        return cls(
            should_trade=False, confidence=0.0, catalyst_quality="weak",
            catalyst_type="unknown", setup_quality="skip",
            reasoning=reason, risk_flags=[reason],
        )


class TradingAI:
    def __init__(self, config: NewsEvaluatorConfig) -> None:
        self._cfg = config
        self._client = anthropic.Anthropic()

    def assess(
        self,
        symbol: str,
        headlines: List[str],
        filter_data: dict,
        setup_name: str = "",
        pattern_ctx: Optional[PatternContext] = None,
    ) -> TradeDecision:
        if not headlines and not setup_name:
            return TradeDecision.reject("no_catalyst")

        # Effective threshold based on setup type and market regime
        threshold = self._cfg.setup_confidence_overrides.get(setup_name, self._cfg.confidence_threshold)
        if self._cfg.market_regime == "cold":
            threshold = min(threshold + 0.10, 0.95)

        user_msg = self._build_user_message(symbol, headlines, filter_data, setup_name, pattern_ctx)

        t0 = time.monotonic()
        try:
            resp = self._client.messages.create(
                model=self._cfg.model,
                max_tokens=self._cfg.max_tokens,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "trade_assessment"},
                messages=[{"role": "user", "content": user_msg}],
                timeout=self._cfg.timeout_seconds,
            )
        except Exception as exc:
            logger.error("%s: TradingAI API error: %s", symbol, exc)
            return TradeDecision.reject("api_error")

        elapsed = time.monotonic() - t0
        logger.debug("%s: TradingAI response in %.2fs", symbol, elapsed)

        # Extract tool use block
        for block in resp.content:
            if block.type == "tool_use" and block.name == "trade_assessment":
                inp = block.input
                decision = TradeDecision(
                    should_trade=bool(inp.get("should_trade", False)),
                    confidence=float(inp.get("confidence", 0.0)),
                    catalyst_quality=str(inp.get("catalyst_quality", "weak")),
                    catalyst_type=str(inp.get("catalyst_type", "unknown")),
                    setup_quality=str(inp.get("setup_quality", "skip")),
                    reasoning=str(inp.get("reasoning", "")),
                    risk_flags=list(inp.get("risk_flags", [])),
                    entry_price_target=float(inp.get("entry_price_target", 0.0)),
                    stop_suggestion=float(inp.get("stop_suggestion", 0.0)),
                )
                # Apply confidence threshold — AI may say should_trade=True but confidence is too low
                if decision.should_trade and decision.confidence < threshold:
                    decision.should_trade = False
                    decision.reasoning += f" (confidence {decision.confidence:.0%} below threshold {threshold:.0%})"
                # Also require setup quality A or better
                if decision.should_trade and decision.setup_quality not in ("A+", "A"):
                    decision.should_trade = False
                return decision

        logger.warning("%s: TradingAI returned no tool_use block", symbol)
        return TradeDecision.reject("no_tool_response")

    def _build_user_message(
        self,
        symbol: str,
        headlines: List[str],
        filter_data: dict,
        setup_name: str,
        pattern_ctx: Optional[PatternContext],
    ) -> str:
        headline_block = "\n".join(f"  - {h}" for h in headlines[:5]) if headlines else "  (none)"
        pattern_block = ""
        if pattern_ctx:
            pattern_block = (
                f"\nChart Pattern:\n"
                f"  Active setup   : {pattern_ctx.setup_name}\n"
                f"  All setups     : {', '.join(pattern_ctx.active_setups) or 'none'}\n"
                f"  PM high        : ${pattern_ctx.pm_high:.2f}\n"
                f"  PM low         : ${pattern_ctx.pm_low:.2f}\n"
                f"  VWAP           : ${pattern_ctx.vwap:.2f}\n"
                f"  Price vs VWAP  : {pattern_ctx.price_vs_vwap_pct:+.1f}%\n"
                f"  Price vs PM hi : {pattern_ctx.price_vs_pm_high_pct:+.1f}%\n"
            )
        market_regime_note = (
            "Market is HOT — crowd participation is elevated."
            if self._cfg.market_regime == "hot"
            else "Market is COLD — be highly selective, A+ setups only."
        )
        return (
            f"Symbol: {symbol}\n"
            f"Price: ${filter_data.get('price', 0):.2f}  "
            f"Gap: {filter_data.get('gap_pct', 0):+.1f}%  "
            f"RVOL: {filter_data.get('rvol', 0):.1f}x  "
            f"Float: {filter_data.get('float_shares', 0) / 1e6:.1f}M shares\n"
            f"Setup pattern: {setup_name or 'not confirmed'}\n"
            f"{pattern_block}"
            f"\nNews headlines:\n{headline_block}\n"
            f"\nMarket regime: {market_regime_note}\n"
            f"\nAssess this setup and return your trade_assessment."
        )
