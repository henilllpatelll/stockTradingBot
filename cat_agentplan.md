# Plan: AI Catalyst News Evaluator Agent

## Context

The trading bot currently requires a human to type `y/n` to confirm every catalyst before entering a trade (`scanner.py:confirm_with_user()`). The goal is to replace this manual gate with a Claude-powered agent that reads the headline(s), considers the stock's gap/RVOL metrics, and makes an automatic go/no-go decision. Manual confirmation is kept as a fallback if the API is unreachable.

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `news_evaluator.py` | **NEW** — AI agent module |
| `config.py` | Add `NewsEvaluatorConfig` dataclass + field on `Config` |
| `run_bot.py` | Replace lines 468–471 with AI evaluation call |
| `trade_logger.py` | Add `catalyst_confidence` + `catalyst_type` fields |
| `requirements.txt` | Add `anthropic>=0.40.0` |
| `.env` | User must add `ANTHROPIC_API_KEY=...` (note only, no code change) |

---

## 1. `news_evaluator.py` (new file)

### Dataclasses

```python
@dataclass
class CatalystEvaluation:
    should_trade: bool          # True if confidence >= threshold
    confidence: float           # 0.0–1.0 raw from Claude
    catalyst_quality: str       # "strong" | "medium" | "weak" | "negative"
    catalyst_type: str          # e.g. "fda_approval", "dilution", …
    reasoning: str              # 1–3 sentence explanation (logged)
    raw_headlines: list[str]
```

### Class: `NewsCatalystAgent`

- `__init__(config: NewsEvaluatorConfig)` — instantiates `anthropic.Anthropic()`, builds the system prompt list (with `cache_control: ephemeral`) and the tool schema once.
- `evaluate(symbol, headlines, rvol, gap_pct, price) → CatalystEvaluation` — main entry point.

### Claude API call pattern

```python
response = client.messages.create(
    model=config.model,                          # "claude-haiku-4-5-20251001"
    max_tokens=config.max_tokens,                # 512
    system=[{                                    # cached system prompt
        "type": "text",
        "text": _SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }],
    tools=[_TOOL_SCHEMA],
    tool_choice={"type": "tool", "name": "catalyst_evaluation"},
    messages=[{"role": "user", "content": user_message}],
    timeout=config.timeout_seconds,              # 15.0
)
tool_block = next(b for b in response.content if b.type == "tool_use")
data = tool_block.input
```

`should_trade` in the returned object is `data["should_trade"] and data["confidence"] >= config.confidence_threshold`.

### Tool schema (forced structured output)

Required fields: `should_trade` (bool), `confidence` (0–1 float), `catalyst_quality` (enum: strong/medium/weak/negative), `catalyst_type` (string from allowed list), `reasoning` (string).

### System prompt (cached — ~650 tokens, paid once per 5-min window)

Covers:
- Strategy context: small-cap momentum, $1–$20, RVOL ≥5x, gap ≥5%, float <20M
- **Strong** catalysts (confidence ≥ 0.80): FDA approval, major contract, earnings beat + raised guidance, M&A offer, short-squeeze
- **Medium** catalysts (confidence 0.55–0.80): earnings inline, product launch, analyst upgrade, partnership
- **Weak/Negative** (should_trade=False): generic PR, recycled news, sector news, dilution, legal/SEC, downgrade
- Rules: specificity required, recency matters, high gap+RVOL lowers bar slightly, lean toward False when uncertain

### Error handling / fallback

Catches `APIConnectionError`, `RateLimitError`, `APIStatusError`, and generic `Exception`. If `config.fallback_to_manual=True` → calls `CatalystChecker.confirm_with_user()`. Otherwise returns `should_trade=False`.

Logs cache token usage at DEBUG level for cost monitoring.

---

## 2. `config.py` changes

Add new dataclass **before** `Config`:

```python
@dataclass
class NewsEvaluatorConfig:
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    confidence_threshold: float = 0.55
    fallback_to_manual: bool = True
    max_tokens: int = 512
    timeout_seconds: float = 15.0
```

Add one field to `Config`:

```python
news_evaluator: NewsEvaluatorConfig = field(default_factory=NewsEvaluatorConfig)
```

---

## 3. `run_bot.py` changes

**Add import** near top (after existing imports):
```python
from news_evaluator import NewsCatalystAgent, CatalystEvaluation
```

**Replace lines 457–471** (catalyst check block):

```python
catalyst_headline = ""
catalyst_confidence = 0.0
catalyst_type = ""
no_catalyst_check = args and getattr(args, "no_catalyst_check", False)
if not no_catalyst_check:
    checker = CatalystChecker(config.scanner, client)
    headlines = checker.fetch_headlines(symbol, config.scanner.news_lookback_hours)
    has_cat, headline = checker.has_catalyst(symbol, headlines)
    if not has_cat:
        print(f"{symbol}: no catalyst — if you cannot explain why it is moving, don't trade.")
        return

    if config.news_evaluator.enabled:
        news_agent = NewsCatalystAgent(config.news_evaluator)
        evaluation = news_agent.evaluate(
            symbol=symbol,
            headlines=headlines,
            rvol=filter_result.rvol,
            gap_pct=filter_result.change_pct,
            price=filter_result.price,
        )
        log.info(
            "%s: AI catalyst eval — should_trade=%s  confidence=%.2f  "
            "quality=%s  type=%s  reasoning=%s",
            symbol, evaluation.should_trade, evaluation.confidence,
            evaluation.catalyst_quality, evaluation.catalyst_type, evaluation.reasoning,
        )
        if not evaluation.should_trade:
            print(
                f"{symbol}: catalyst rejected by AI "
                f"(confidence={evaluation.confidence:.0%}, quality={evaluation.catalyst_quality})\n"
                f"  Reason: {evaluation.reasoning}"
            )
            return
        catalyst_headline = headlines[0] if headlines else ""
        catalyst_confidence = evaluation.confidence
        catalyst_type = evaluation.catalyst_type
        print(
            f"[AI CATALYST APPROVED] {symbol}: {evaluation.catalyst_type} "
            f"(confidence={evaluation.confidence:.0%})\n"
            f"  {evaluation.reasoning}"
        )
    else:
        if not checker.confirm_with_user(symbol, headline):
            print(f"{symbol}: catalyst not confirmed — skipping.")
            return
        catalyst_headline = headline
```

**Update `TradeRecord` construction** at line 557–568, add two new fields:
```python
trade = TradeRecord(
    ...
    catalyst_headline=catalyst_headline,
    catalyst_confidence=catalyst_confidence,   # new
    catalyst_type=catalyst_type,               # new
)
```

---

## 4. `trade_logger.py` changes

**`_FIELDS` list** — append after `catalyst_headline`:
```python
"catalyst_confidence",
"catalyst_type",
```

**`TradeRecord` dataclass** — append after `catalyst_headline: str = ""`:
```python
catalyst_confidence: float = 0.0
catalyst_type: str = ""
```

No changes to `log_entry()` or `log_exit()` — they use `asdict(trade)` which auto-includes the new fields. The existing `trades.csv` will need to be deleted (or re-headered) since it lacks the new columns.

---

## 5. `requirements.txt` changes

Add one line:
```
anthropic>=0.40.0
```

---

## Verification

1. `pip install -r requirements.txt` — confirms `anthropic` installs.
2. Set `ANTHROPIC_API_KEY` in `.env`.
3. Run `python run_bot.py AAPL --skip-filters --no-trading-hours-gate` — watch `logs/bot.log` for the `AI catalyst eval` log line and cache usage stats.
4. Run with `ANTHROPIC_API_KEY=invalid` to verify fallback to manual confirmation fires without crashing.
5. Run with `--no-catalyst-check` to confirm bypass still skips the evaluator entirely.
6. Check `logs/trades.csv` headers include `catalyst_confidence` and `catalyst_type`.
