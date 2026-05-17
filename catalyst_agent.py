"""
Benzinga Pro Catalyst Agent
AI-powered catalyst analysis using Claude Haiku.

Reads ticker_symbols_benpro.md, fetches Alpaca SIP 1-min bars around each
catalyst, and evaluates every event with Claude Haiku for a structured
GO/NO-GO verdict that replaces the rule-based SIGNAL column.

Final section: AI synthesis of top 3 setups, patterns to avoid, and all insights.
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

# UTF-8 output on Windows so symbols like ✓ render correctly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import anthropic
from dotenv import load_dotenv

# Reuse all parsing / bar-fetching / metric / formatting logic from catalyst_analyzer
from catalyst_analyzer import (
    parse_events,
    fetch_bars,
    compute_metrics,
    classify_catalyst,
    NewsEvent,
    ET,
    _fmt,
    _fmt_vol,
    _fmt_time,
    _abbrev_cat,
    INPUT_FILE,
)

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"

# ── Cached system prompt (~650 tokens, billed once per 5-min window) ──────────

_SYSTEM_PROMPT = """You are an elite small-cap momentum trader and catalyst analyst with 15+ years of experience.

STRATEGY CONTEXT:
- Focus: small-cap stocks, $1-$20 price range, RVOL >= 5x, gap >= 5%, float < 20M shares
- Timeframe: intraday momentum plays off pre-market or early-market catalysts
- Risk profile: aggressive but disciplined — quality catalysts only, protect capital first

STRONG catalysts (go_no_go=true, confidence 0.80-1.0):
- FDA approval, positive clinical trial results, major regulatory milestone
- Significant named contract win or major partnership (specific dollar value or Fortune 500 partner)
- Earnings beat with raised guidance AND volume confirmation
- M&A offer at premium or buyout announcement
- Confirmed short squeeze with catalyst trigger
- Circuit breaker halt on upside (momentum continuation candidate)

MEDIUM catalysts (go_no_go=true, confidence 0.55-0.80):
- Earnings beat without guidance raise
- Product launch with quantified addressable market
- Analyst upgrade from reputable firm with meaningful price target raise
- Named contract win (specific customer or dollar amount)
- Resumption after halt — if original catalyst was strong, continuation is tradeable

WEAK / NEGATIVE catalysts (go_no_go=false):
- Generic press releases with no financial specifics or no named counterparty
- Recycled or stale news (previously announced, repackaged)
- Sector or index news affecting many tickers simultaneously
- Dilutive offerings: ATM programs, registered direct placements, public offerings at discount
- Legal issues, SEC investigations, DOJ subpoenas
- Analyst downgrades, price target cuts, removed from coverage
- Clinical hold, FDA rejection, negative trial data
- "Earnings Scheduled For..." (no actual results reported)
- Guidance cuts or lowered outlook

DECISION RULES:
1. Specificity required — vague claims without numbers or named parties score weak
2. A strong price move + high volume spike is market confirmation; lowers the confidence bar slightly
3. Multi-day context matters — if this ticker already had a catalyst yesterday that drove the move, today's news may be follow-on noise
4. When uncertain, default go_no_go=false (capital preservation is the priority)
5. risk_flags must be concrete and specific: "dilution risk", "no revenue yet", "FDA pending decision", "halt history today", "follow-on to yesterday's move", "recycled PR"

Output ONLY via the catalyst_evaluation tool — no prose outside the tool call."""

# ── Forced tool schema ────────────────────────────────────────────────────────

_TOOL_SCHEMA = {
    "name": "catalyst_evaluation",
    "description": "Structured go/no-go evaluation of a news catalyst for a small-cap momentum trade",
    "input_schema": {
        "type": "object",
        "properties": {
            "go_no_go": {
                "type": "boolean",
                "description": "True = trade the catalyst, False = avoid",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence level 0.0 to 1.0 (0 = certain avoid, 1 = certain go)",
            },
            "catalyst_quality": {
                "type": "string",
                "enum": ["strong", "medium", "weak", "negative"],
                "description": "Overall catalyst quality assessment",
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of specific risk factors (e.g. 'dilution risk', 'FDA pending', 'halt history')",
            },
            "reasoning": {
                "type": "string",
                "description": "1-3 sentence reasoning explaining the go/no-go decision",
            },
        },
        "required": ["go_no_go", "confidence", "catalyst_quality", "risk_flags", "reasoning"],
    },
}


# ── Evaluation dataclass ──────────────────────────────────────────────────────

@dataclass
class CatalystEvaluation:
    go_no_go: bool
    confidence: float
    catalyst_quality: str
    risk_flags: list
    reasoning: str


# ── AI client ─────────────────────────────────────────────────────────────────

_ai_client = None


def _get_client() -> anthropic.Anthropic:
    global _ai_client
    if _ai_client is None:
        _ai_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _ai_client


# ── Per-event evaluation ──────────────────────────────────────────────────────

def _fmt_m(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"{'+' if v >= 0 else ''}{v:.1f}%"


def _fmt_v(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"{v:.1f}x"


def evaluate_catalyst(
    event: NewsEvent,
    cat_type: str,
    metrics: dict,
    is_premarket: bool,
    ticker_context: list[dict],
) -> CatalystEvaluation:
    """Call Claude Haiku to evaluate a single catalyst with forced tool_use."""
    client = _get_client()
    timing = "PRE-MARKET" if is_premarket else "INTRADAY"
    dt_et = event.dt_utc.astimezone(ET)

    metrics_lines = [
        f"  T+1m price move : {_fmt_m(metrics.get('move_1m'))}",
        f"  T+5m price move : {_fmt_m(metrics.get('move_5m'))}",
        f"  Volume spike vs 5-bar avg: {_fmt_v(metrics.get('vol_spike'))}",
    ]
    if is_premarket:
        metrics_lines += [
            f"  Move to 9:30 open : {_fmt_m(metrics.get('move_to_open'))}",
            f"  Intraday high (news→open): {_fmt_m(metrics.get('high_to_open'))}",
            f"  Intraday low  (news→open): {_fmt_m(metrics.get('low_to_open'))}",
        ]
    else:
        metrics_lines.append(f"  T+10m price move : {_fmt_m(metrics.get('move_10m'))}")

    ctx_block = ""
    if ticker_context:
        ctx_lines = "\n".join(
            f"  - [{c['date_label'][:3]} {c['time_et']}] {c['headline'][:70]} ({c['cat_type']})"
            for c in ticker_context
        )
        ctx_block = f"\nOther events for {event.symbol} this week (multi-day context):\n{ctx_lines}"

    user_message = (
        f"Evaluate this catalyst for {event.symbol}:\n\n"
        f"Headline : {event.headline}\n"
        f"Source   : {event.source or 'Unknown'}\n"
        f"Time     : {dt_et.strftime('%A %B %d, %Y %I:%M %p ET')} ({timing})\n"
        f"Rule-based catalyst type: {cat_type}\n\n"
        f"Price / volume metrics (Alpaca SIP 1-min bars):\n"
        + "\n".join(metrics_lines)
        + ctx_block
        + "\n\nProvide your structured evaluation via the catalyst_evaluation tool."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "catalyst_evaluation"},
            messages=[{"role": "user", "content": user_message}],
        )
        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            raise ValueError("No tool_use block in response")
        data = tool_block.input
        return CatalystEvaluation(
            go_no_go=bool(data.get("go_no_go", False)),
            confidence=float(data.get("confidence", 0.0)),
            catalyst_quality=data.get("catalyst_quality", "weak"),
            risk_flags=data.get("risk_flags", []),
            reasoning=data.get("reasoning", ""),
        )
    except Exception as exc:
        return CatalystEvaluation(
            go_no_go=False,
            confidence=0.0,
            catalyst_quality="weak",
            risk_flags=["api_error"],
            reasoning=f"Claude API error: {exc}",
        )


# ── Final synthesis call ──────────────────────────────────────────────────────

def synthesize_insights(all_rows: list[dict]) -> str:
    """One Haiku call: top 3 setups, patterns to avoid next week, all insights."""
    client = _get_client()

    lines = []
    for row in all_rows:
        e = row["event"]
        ev: CatalystEvaluation = row["evaluation"]
        dt_et = e.dt_utc.astimezone(ET)
        flag = "GO   " if ev.go_no_go else "SKIP "
        lines.append(
            f"  {e.date_label[:3]} {dt_et.strftime('%I:%M%p')} "
            f"{e.symbol:<6} [{row['cat_type'][:18]:<18}] "
            f"{flag} {ev.confidence:.0%} {ev.catalyst_quality:<8} | "
            f"{e.headline[:65]}"
        )

    summary = "\n".join(lines)

    user_message = (
        f"You just evaluated {len(all_rows)} catalyst events from Benzinga Pro "
        f"for the week of May 11-15, 2026. Here is the full evaluation log:\n\n"
        f"{summary}\n\n"
        "Based on these evaluations provide:\n\n"
        "1. TOP 3 ACTIONABLE SETUPS: The 3 best trades this week — "
        "symbol, catalyst, why it was the best setup, what made it high-confidence, "
        "and the ideal entry criteria.\n\n"
        "2. PATTERNS TO AVOID NEXT WEEK: Specific patterns, catalyst types, or "
        "situations observed this week that should be filtered out or faded next week. "
        "Be concrete — name tickers and catalyst types.\n\n"
        "3. ALL INSIGHTS: Every pattern, observation, and takeaway a small-cap momentum "
        "trader should extract from this week's catalyst data. Include statistical "
        "tendencies, timing observations, multi-day storylines, and any edge you spotted.\n\n"
        "Be specific, concrete, and actionable. Reference actual tickers and events."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1800,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_message}],
        )
        text_parts = [b.text for b in response.content if hasattr(b, "text")]
        return "\n".join(text_parts)
    except Exception as exc:
        return f"[Synthesis failed: {exc}]"


# ── Display helpers ───────────────────────────────────────────────────────────

_QUAL_ICON = {
    "strong":   "+++",
    "medium":   "++-",
    "weak":     "+--",
    "negative": "---",
}


def _verdict_str(ev: CatalystEvaluation) -> str:
    icon = "GO  " if ev.go_no_go else "SKIP"
    qual = _QUAL_ICON.get(ev.catalyst_quality, "?  ")
    return f"{icon} {ev.confidence:>3.0%} [{qual}]"


# ── Table output ──────────────────────────────────────────────────────────────

def _print_table(results: list[dict], no_data: list[dict]):
    W = 145
    print()
    print("=" * W)
    print("BENZINGA CATALYST ANALYSIS (AI-POWERED — Claude Haiku)   May 11-15, 2026")
    print("=" * W)

    by_day: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for row in results:
        label = row["event"].date_label
        if label not in by_day:
            order.append(label)
        by_day[label].append(row)

    hdr = (
        f"{'TIME(ET)':<10} {'TICKER':<7} {'CATALYST TYPE':<22}"
        f"{'T+1m':>7} {'T+5m':>7}"
        f"{'TO OPEN':>9} {'HIGH':>8} {'LOW':>8}"
        f"{'VOL':>7}  {'AI VERDICT':<16}  REASONING"
    )

    for label in order:
        print()
        print(f"-- {label} " + "-" * max(0, W - len(label) - 4))
        print(hdr)
        for row in by_day[label]:
            e = row["event"]
            ev: CatalystEvaluation = row["evaluation"]
            reasoning_short = (ev.reasoning[:60] + "...") if len(ev.reasoning) > 60 else ev.reasoning
            print(
                f"{_fmt_time(e.dt_utc):<10} {e.symbol:<7} "
                f"{_abbrev_cat(row['cat_type'], row['is_premarket']):<22}"
                f"{_fmt(row.get('move_1m')):>7} "
                f"{_fmt(row.get('move_5m')):>7}"
                f"{_fmt(row.get('move_to_open')):>9} "
                f"{_fmt(row.get('high_to_open')):>8} "
                f"{_fmt(row.get('low_to_open')):>8}"
                f"{_fmt_vol(row.get('vol_spike')):>7}  "
                f"{_verdict_str(ev):<16}  {reasoning_short}"
            )

    if no_data:
        print()
        print(f"-- [NO DATA] " + "-" * max(0, W - 14))
        for row in no_data:
            e = row["event"]
            ev: CatalystEvaluation = row["evaluation"]
            print(f"  {e.symbol:<7} {_fmt_time(e.dt_utc)}  {e.headline[:80]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def get_analysis_data():
    import math
    from dataclasses import asdict
    events = parse_events(INPUT_FILE)
    ticker_events = defaultdict(list)
    for ev in events:
        ticker_events[ev.symbol].append(ev)

    results = []
    no_data_events = []
    raw_all_rows = []

    for event in events:
        df_1m, is_premarket = fetch_bars(event)
        m = compute_metrics(event, df_1m, is_premarket)
        cat_type = classify_catalyst(event.headline)

        ctx = [
            {
                "date_label": other.date_label,
                "time_et": other.dt_utc.astimezone(ET).strftime("%I:%M %p"),
                "headline": other.headline,
                "cat_type": classify_catalyst(other.headline),
            }
            for other in ticker_events[event.symbol]
            if other is not event
        ]

        if df_1m.empty and cat_type not in ("HALT", "RESUME"):
            evaluation = CatalystEvaluation(
                go_no_go=False, confidence=0.0, catalyst_quality="weak",
                risk_flags=["no_price_data"], reasoning="No Alpaca bar data for this window."
            )
        else:
            evaluation = evaluate_catalyst(event, cat_type, m, is_premarket, ctx)
            time.sleep(0.1)
            raw_all_rows.append(dict(event=event, is_premarket=is_premarket, cat_type=cat_type, evaluation=evaluation, **m))
            
        candles = []
        if not df_1m.empty:
            for _, row_data in df_1m.iterrows():
                candles.append({
                    "timestamp": row_data["timestamp"].isoformat() if hasattr(row_data["timestamp"], "isoformat") else str(row_data["timestamp"]),
                    "open": float(row_data["open"]),
                    "high": float(row_data["high"]),
                    "low": float(row_data["low"]),
                    "close": float(row_data["close"]),
                    "volume": float(row_data["volume"]),
                })

        clean_m = {}
        for k, v in m.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                clean_m[k] = None
            else:
                clean_m[k] = v

        row_json = dict(
            event={
                "dt_utc": event.dt_utc.isoformat(),
                "symbol": event.symbol,
                "headline": event.headline,
                "source": event.source,
                "date_label": event.date_label,
            },
            is_premarket=is_premarket,
            cat_type=cat_type,
            evaluation=asdict(evaluation),
            candles=candles,
            **clean_m,
        )

        if df_1m.empty and cat_type not in ("HALT", "RESUME"):
            no_data_events.append(row_json)
        else:
            results.append(row_json)

    synthesis = synthesize_insights(raw_all_rows)
    return {"results": results, "no_data_events": no_data_events, "synthesis": synthesis}

def run():
    print()
    print("Parsing news events...")
    events = parse_events(INPUT_FILE)
    print(f"  {len(events)} events found.")
    print()

    # Pre-build per-ticker event lookup for multi-day context injection
    ticker_events: dict[str, list] = defaultdict(list)
    for ev in events:
        ticker_events[ev.symbol].append(ev)

    all_rows: list[dict] = []
    no_data_rows: list[dict] = []

    print(f"Fetching bars and evaluating with {MODEL}...")
    print()

    for idx, event in enumerate(events, 1):
        print(
            f"  [{idx:3d}/{len(events)}] {event.symbol:<6} {_fmt_time(event.dt_utc)}",
            end="  ",
            flush=True,
        )

        df_1m, is_premarket = fetch_bars(event)
        m = compute_metrics(event, df_1m, is_premarket)
        cat_type = classify_catalyst(event.headline)

        # Multi-day context: all other events for this ticker this week
        ctx = [
            {
                "date_label": other.date_label,
                "time_et": other.dt_utc.astimezone(ET).strftime("%I:%M %p"),
                "headline": other.headline,
                "cat_type": classify_catalyst(other.headline),
            }
            for other in ticker_events[event.symbol]
            if other is not event
        ]

        # Skip AI call for events with zero price data (after-hours gaps, etc.)
        if df_1m.empty and cat_type not in ("HALT", "RESUME"):
            evaluation = CatalystEvaluation(
                go_no_go=False,
                confidence=0.0,
                catalyst_quality="weak",
                risk_flags=["no_price_data"],
                reasoning="No Alpaca bar data for this window.",
            )
            print(f"{cat_type:<22}  NO DATA")
            no_data_rows.append(dict(event=event, is_premarket=is_premarket, cat_type=cat_type, evaluation=evaluation, **m))
            continue

        evaluation = evaluate_catalyst(event, cat_type, m, is_premarket, ctx)
        print(f"{cat_type:<22}  {_verdict_str(evaluation)}")
        time.sleep(0.1)  # gentle API pacing

        all_rows.append(dict(event=event, is_premarket=is_premarket, cat_type=cat_type, evaluation=evaluation, **m))

    _print_table(all_rows, no_data_rows)

    print()
    print("Generating AI synthesis (final Haiku call)...")
    synthesis = synthesize_insights(all_rows)

    W = 145
    print()
    print("=" * W)
    print("AI SYNTHESIS -- Claude Haiku")
    print("=" * W)
    print(synthesis)
    print()


if __name__ == "__main__":
    run()
