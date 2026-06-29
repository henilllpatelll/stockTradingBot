from datetime import date
from pathlib import Path

from config import Config
from scripts.blind_week_replay import decision_time_for, parse_news_events


def test_parse_news_events_extracts_section_date_time_symbol_and_headline(tmp_path):
    news_file = tmp_path / "news.md"
    news_file.write_text(
        "\n".join(
            [
                "Monday May 11, 2026",
                "06:25:00AM",
                "ATHE",
                "Alterity headline",
                "GLN",
                "Tuesday May 12, 2026",
                "03:15:22PM",
                "TACT",
                "TransAct headline",
                "BZ Wire",
            ]
        ),
        encoding="utf-8",
    )

    events = parse_news_events(news_file)

    assert len(events) == 2
    assert events[0].day == date(2026, 5, 11)
    assert events[0].symbol == "ATHE"
    assert events[0].headline == "Alterity headline"
    assert events[0].published_at.hour == 6


def test_decision_time_waits_until_trading_window_for_early_news(tmp_path):
    news_file = tmp_path / "news.md"
    news_file.write_text(
        "\n".join(["Monday May 11, 2026", "05:00:00AM", "LNZA", "Headline", "GLN"]),
        encoding="utf-8",
    )
    event = parse_news_events(news_file)[0]

    decision_time = decision_time_for(event, Config())

    assert decision_time is not None
    assert decision_time.hour == 7
    assert decision_time.minute == 0


def test_decision_time_skips_after_trading_window(tmp_path):
    news_file = tmp_path / "news.md"
    news_file.write_text(
        "\n".join(["Tuesday May 12, 2026", "03:15:22PM", "TACT", "Headline", "BZ Wire"]),
        encoding="utf-8",
    )
    event = parse_news_events(news_file)[0]

    assert decision_time_for(event, Config()) is None
