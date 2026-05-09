from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

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
]


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


class TradeLogger:
    """Appends entries to a CSV and updates them with exit data."""

    def __init__(self, log_dir: str = "logs") -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(exist_ok=True)
        self._csv = self._dir / "trades.csv"
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
            rows = list(csv.DictReader(fh))

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

        print(f"\n{'─' * 44}")
        print("  Paper Trade Summary")
        print(f"{'─' * 44}")
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
            print(f"  {'─'*6}  {'─'*7}  {'─'*7}  {'─'*9}  {'─'*20}")
            for r in closed[-10:]:
                print(
                    f"  {r['symbol']:<6}  ${float(r['entry_price']):>6.2f}  "
                    f"${float(r['exit_price']):>6.2f}  "
                    f"${float(r['pnl']):>+8.2f}  {r.get('exit_reason', '')}"
                )

        print(f"{'─' * 44}\n")
