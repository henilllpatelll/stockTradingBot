"""Tests for trade_logger.py — CSV write/read, entry/exit, stats."""
import csv
import pytest
from pathlib import Path
from trade_logger import TradeLogger, TradeRecord, PerformanceTracker


def _make_record(**kwargs) -> TradeRecord:
    defaults = dict(
        symbol="AAPL",
        entry_time="2024-01-15T09:30:00+00:00",
        entry_price=10.00,
        shares=50,
        initial_stop=9.50,
        trail_pct=0.0,
        rvol=7.5,
        change_pct=15.0,
        setup_name="premarket_high_break",
        catalyst_headline="AAPL earnings beat",
        catalyst_type="earnings",
    )
    defaults.update(kwargs)
    return TradeRecord(**defaults)


class TestTradeLogger:
    def test_csv_created_on_init(self, tmp_path):
        logger = TradeLogger(str(tmp_path))
        assert (tmp_path / "trades_catalyst.csv").exists()

    def test_csv_has_header(self, tmp_path):
        TradeLogger(str(tmp_path))
        with open(tmp_path / "trades_catalyst.csv") as f:
            header = f.readline().strip()
        assert "symbol" in header
        assert "entry_price" in header

    def test_log_entry_writes_row(self, tmp_path):
        logger = TradeLogger(str(tmp_path))
        trade = _make_record(symbol="TSLA", entry_price=12.50, shares=40)
        logger.log_entry(trade)
        with open(tmp_path / "trades_catalyst.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "TSLA"
        assert float(rows[0]["entry_price"]) == pytest.approx(12.50)
        assert int(rows[0]["shares"]) == 40

    def test_log_exit_updates_correct_row(self, tmp_path):
        logger = TradeLogger(str(tmp_path))
        trade = _make_record(symbol="GME", entry_price=8.00)
        logger.log_entry(trade)

        trade.exit_time = "2024-01-15T10:00:00+00:00"
        trade.exit_price = 9.00
        trade.exit_reason = "red_candle_exit"
        trade.pnl = 50.0
        trade.pnl_pct = 12.5
        logger.log_exit(trade)

        with open(tmp_path / "trades_catalyst.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["exit_reason"] == "red_candle_exit"
        assert float(rows[0]["pnl"]) == pytest.approx(50.0)

    def test_log_exit_only_updates_most_recent_open(self, tmp_path):
        logger = TradeLogger(str(tmp_path))
        # First closed trade
        t1 = _make_record(symbol="AMC")
        logger.log_entry(t1)
        t1.exit_time = "2024-01-15T09:45:00+00:00"
        t1.exit_price = 10.50
        t1.exit_reason = "stop_loss"
        t1.pnl = -25.0
        t1.pnl_pct = -5.0
        logger.log_exit(t1)

        # Second open trade (same symbol)
        t2 = _make_record(symbol="AMC", entry_price=11.0)
        logger.log_entry(t2)

        # Exit the second trade
        t2.exit_time = "2024-01-15T10:15:00+00:00"
        t2.exit_price = 12.0
        t2.exit_reason = "red_candle_exit"
        t2.pnl = 50.0
        t2.pnl_pct = 9.09
        logger.log_exit(t2)

        with open(tmp_path / "trades_catalyst.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        # First row should still have stop_loss, second should have red_candle_exit
        assert rows[0]["exit_reason"] == "stop_loss"
        assert rows[1]["exit_reason"] == "red_candle_exit"

    def test_print_summary_empty(self, tmp_path, capsys):
        logger = TradeLogger(str(tmp_path))
        logger.print_summary()
        out = capsys.readouterr().out
        assert "No trades" in out

    def test_print_summary_with_closed_trades(self, tmp_path, capsys):
        logger = TradeLogger(str(tmp_path))
        trade = _make_record()
        logger.log_entry(trade)
        trade.exit_time = "2024-01-15T10:00:00+00:00"
        trade.exit_price = 11.00
        trade.exit_reason = "red_candle_exit"
        trade.pnl = 50.0
        trade.pnl_pct = 10.0
        logger.log_exit(trade)
        logger.print_summary()
        out = capsys.readouterr().out
        assert "Win rate" in out
        assert "Total PnL" in out


class TestPerformanceTracker:
    def test_get_stats_no_csv(self, tmp_path):
        tracker = PerformanceTracker(str(tmp_path))
        assert tracker.get_stats() == {}

    def test_get_stats_empty_csv(self, tmp_path):
        TradeLogger(str(tmp_path))  # creates header-only CSV
        tracker = PerformanceTracker(str(tmp_path))
        assert tracker.get_stats() == {}

    def _write_closed_trades(self, tmp_path, trades: list[dict]) -> None:
        logger = TradeLogger(str(tmp_path))
        for t in trades:
            record = _make_record(**{k: v for k, v in t.items()
                                     if k not in ("exit_time", "exit_price", "exit_reason", "pnl", "pnl_pct")})
            logger.log_entry(record)
            record.exit_time = "2024-01-15T10:00:00+00:00"
            record.exit_price = t.get("exit_price", 10.0)
            record.exit_reason = t.get("exit_reason", "red_candle_exit")
            record.pnl = t.get("pnl", 0.0)
            record.pnl_pct = t.get("pnl_pct", 0.0)
            logger.log_exit(record)

    def test_get_stats_overall(self, tmp_path):
        self._write_closed_trades(tmp_path, [
            {"pnl": 50.0, "exit_price": 11.0, "setup_name": "premarket_high_break"},
            {"pnl": -25.0, "exit_price": 9.50, "setup_name": "premarket_high_break"},
            {"pnl": 75.0, "exit_price": 11.5, "setup_name": "pm_flag_break"},
        ])
        stats = PerformanceTracker(str(tmp_path)).get_stats()
        assert stats["overall"]["trades"] == 3
        assert stats["overall"]["wins"] == 2
        assert stats["overall"]["losses"] == 1
        assert stats["overall"]["win_rate"] == pytest.approx(2 / 3, abs=0.001)
        assert stats["overall"]["avg_pnl"] == pytest.approx((50.0 - 25.0 + 75.0) / 3, abs=0.01)

    def test_get_stats_by_setup(self, tmp_path):
        self._write_closed_trades(tmp_path, [
            {"pnl": 50.0, "exit_price": 11.0, "setup_name": "premarket_high_break"},
            {"pnl": -25.0, "exit_price": 9.50, "setup_name": "pm_flag_break"},
        ])
        stats = PerformanceTracker(str(tmp_path)).get_stats()
        assert "premarket_high_break" in stats["by_setup"]
        assert stats["by_setup"]["premarket_high_break"]["wins"] == 1

    def test_adjusted_threshold_no_data(self, tmp_path):
        tracker = PerformanceTracker(str(tmp_path))
        assert tracker.adjusted_threshold("premarket_high_break", base=0.65) == 0.65

    def test_adjusted_threshold_high_win_rate(self, tmp_path):
        # 5 wins out of 5 (win_rate=1.0 >= 0.65) → lower threshold
        self._write_closed_trades(tmp_path, [
            {"pnl": 50.0, "exit_price": 11.0, "setup_name": "premarket_high_break"}
        ] * 5)
        tracker = PerformanceTracker(str(tmp_path))
        result = tracker.adjusted_threshold("premarket_high_break", base=0.65)
        assert result < 0.65

    def test_adjusted_threshold_low_win_rate(self, tmp_path):
        # 1 win, 5 losses → win_rate ~0.17 < 0.45 → raise threshold
        trades = [{"pnl": 50.0, "exit_price": 11.0, "setup_name": "premarket_high_break"}]
        trades += [{"pnl": -20.0, "exit_price": 9.8, "setup_name": "premarket_high_break"}] * 5
        self._write_closed_trades(tmp_path, trades)
        tracker = PerformanceTracker(str(tmp_path))
        result = tracker.adjusted_threshold("premarket_high_break", base=0.65)
        assert result > 0.65
