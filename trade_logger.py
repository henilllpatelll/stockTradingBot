from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_FIELDS = [
    "symbol",
    "entry_time",
    "entry_price",
    "shares",
    "initial_stop",
    "trail_pct",
    "exit_time",
    "exit_price",
    "exit_reason",
    "pnl",
    "pnl_pct",
    "rvol",
    "change_pct",
    "setup_name",
    "catalyst_headline",
    "catalyst_type",
    "strategy",
]


def _safe_float(val: str) -> Optional[float]:
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


@dataclass
class TradeRecord:
    symbol: str
    entry_time: str
    entry_price: float
    shares: int
    initial_stop: float
    trail_pct: float
    rvol: float
    change_pct: float
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    setup_name: str = ""
    catalyst_headline: str = ""
    catalyst_type: str = ""
    strategy: str = "catalyst"


class TradeLogger:
    """Appends entries to a CSV and updates them with exit data."""

    def __init__(self, log_dir: str = "logs", strategy: str = "catalyst") -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(exist_ok=True)
        self._csv = self._dir / f"trades_{strategy}.csv"
        if not self._csv.exists():
            with open(self._csv, "w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=_FIELDS).writeheader()

    def log_entry(self, trade: TradeRecord) -> None:
        with open(self._csv, "a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=_FIELDS).writerow(asdict(trade))
        logger.info(
            "ENTRY  %-6s  $%.2f × %d  stop=$%.2f",
            trade.symbol,
            trade.entry_price,
            trade.shares,
            trade.initial_stop,
        )

    def log_exit(self, trade: TradeRecord) -> None:
        rows = []
        with open(self._csv, newline="") as fh:
            rows = list(csv.DictReader(fh, restval=""))

        # Update the most-recent open record for this symbol
        for row in reversed(rows):
            if row["symbol"] == trade.symbol and not row.get("exit_time"):
                row.update(
                    {
                        "exit_time": trade.exit_time,
                        "exit_price": trade.exit_price,
                        "exit_reason": trade.exit_reason,
                        "pnl": trade.pnl,
                        "pnl_pct": trade.pnl_pct,
                    }
                )
                break

        with open(self._csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        pnl_str = (
            f"${trade.pnl:+.2f} ({trade.pnl_pct:+.1f}%)"
            if trade.pnl is not None
            else "N/A"
        )
        logger.info(
            "EXIT   %-6s  $%.2f  reason=%-22s  PnL=%s",
            trade.symbol,
            trade.exit_price or 0,
            trade.exit_reason or "",
            pnl_str,
        )

    def print_summary(self) -> None:
        if not self._csv.exists():
            print("No trades logged yet.")
            return

        with open(self._csv, newline="") as fh:
            rows = list(csv.DictReader(fh))

        closed = [r for r in rows if r.get("exit_price")]
        open_trades = [r for r in rows if not r.get("exit_price")]

        if not rows:
            print("No trades logged yet.")
            return

        print(f"\n{'-' * 44}")
        print("  Paper Trade Summary")
        print(f"{'-' * 44}")
        print(f"  Total trades  : {len(rows)}")
        print(f"  Open          : {len(open_trades)}")
        print(f"  Closed        : {len(closed)}")

        if closed:
            pnls = [float(r["pnl"]) for r in closed if r.get("pnl")]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            win_rate = len(wins) / len(pnls) * 100 if pnls else 0
            print(f"  Win rate      : {win_rate:.0f}%  ({len(wins)}W / {len(losses)}L)")
            print(f"  Total PnL     : ${sum(pnls):+.2f}")
            print(f"  Average PnL   : ${sum(pnls) / len(pnls):+.2f}" if pnls else "")
            if wins:
                print(f"  Average win   : ${sum(wins) / len(wins):+.2f}")
            if losses:
                print(f"  Average loss  : ${sum(losses) / len(losses):+.2f}")
            print()
            print(f"  {'Symbol':<6}  {'Entry':>7}  {'Exit':>7}  {'PnL':>9}  Reason")
            print(f"  {'-'*6}  {'-'*7}  {'-'*7}  {'-'*9}  {'-'*20}")
            for r in closed[-10:]:
                print(
                    f"  {r['symbol']:<6}  ${float(r['entry_price']):>6.2f}  "
                    f"${float(r['exit_price']):>6.2f}  "
                    f"${float(r['pnl']):>+8.2f}  {r.get('exit_reason', '')}"
                )

        print(f"{'-' * 44}\n")


class PerformanceTracker:
    """Reads the trade CSV and computes win-rate stats for the learning feedback loop."""

    _MIN_SAMPLE = 5    # trades needed before adjusting a threshold
    _ADJUST_STEP = 0.07

    def __init__(self, log_dir: str = "logs", strategy: str = "catalyst") -> None:
        self._csv = Path(log_dir) / f"trades_{strategy}.csv"

    def get_stats(self) -> Dict:
        if not self._csv.exists():
            return {}
        with open(self._csv, newline="") as fh:
            rows = [r for r in csv.DictReader(fh, restval="")
                    if r.get("exit_price") and r.get("pnl")]
        if not rows:
            return {}

        def _agg(key_field: str) -> Dict:
            groups: Dict[str, List[float]] = {}
            for r in rows:
                key = r.get(key_field) or "unknown"
                pnl = _safe_float(r.get("pnl", ""))
                if pnl is None:
                    continue
                groups.setdefault(key, []).append(pnl)
            out: Dict = {}
            for k, pnls in groups.items():
                wins = sum(1 for p in pnls if p > 0)
                out[k] = {
                    "trades": len(pnls),
                    "wins": wins,
                    "losses": len(pnls) - wins,
                    "win_rate": wins / len(pnls),
                    "avg_pnl": sum(pnls) / len(pnls),
                }
            return out

        all_pnls = [_safe_float(r.get("pnl", "")) for r in rows]
        all_pnls = [p for p in all_pnls if p is not None]
        wins_all = sum(1 for p in all_pnls if p > 0)
        return {
            "overall": {
                "trades": len(all_pnls),
                "wins": wins_all,
                "losses": len(all_pnls) - wins_all,
                "win_rate": wins_all / len(all_pnls) if all_pnls else 0.0,
                "avg_pnl": sum(all_pnls) / len(all_pnls) if all_pnls else 0.0,
            },
            "by_setup": _agg("setup_name"),
            "by_catalyst": _agg("catalyst_type"),
        }

    def adjusted_threshold(self, setup_name: str, base: float) -> float:
        """Raise/lower confidence threshold based on this setup's live win rate."""
        stats = self.get_stats()
        s = stats.get("by_setup", {}).get(setup_name, {})
        if not s or s["trades"] < self._MIN_SAMPLE:
            return base
        wr = s["win_rate"]
        if wr >= 0.65:
            return max(base - self._ADJUST_STEP, 0.40)
        if wr < 0.45:
            return min(base + self._ADJUST_STEP, 0.92)
        return base
