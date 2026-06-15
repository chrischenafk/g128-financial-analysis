"""Tests for src/transform/historical_index.py.

Synthetic data against a temp-file SQLite (via init_db with an override URL) — no
real company files. Covers the store's contract: write→read roundtrip with types
intact, idempotency by (marketplace, period), the MoM/YoY same-current-period
collapse, trailing-window ordering and the n limit, and graceful insufficient
history.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.ingest.period_parser import Period
from src.transform.historical_index import (
    channel_period_metrics,
    get_channel_history,
    get_sku_history,
    get_trailing_periods,
    init_db,
    record_period,
    run_history,
    sku_period_metrics,
    trailing_mean,
)

MKT = "TikTok Shop"


@pytest.fixture()
def engine(tmp_path: Path):
    # A temp-file SQLite so the same DB is shared across calls in the test.
    return init_db(f"sqlite:///{(tmp_path / 'history.sqlite').as_posix()}")


def _metrics(rows: list[dict]) -> pd.DataFrame:
    base = {"Theme": "ThemeA", "units": 1, "gross": 10.0, "profit": 2.0,
            "profit_margin_pct": 20.0, "ad_spend": 1.0}
    return pd.DataFrame([{"Marketplace SKU": "SKU", **base, **r} for r in rows])


def _count(engine, table) -> int:
    with engine.connect() as conn:
        return len(conn.execute(table.select()).all())


def test_write_read_roundtrip_types_intact(engine) -> None:
    period = Period(2026, 4)
    sku = _metrics(
        [{"Marketplace SKU": "FG-A", "units": 5, "gross": 100.00, "profit": 23.71,
          "profit_margin_pct": 23.7100, "ad_spend": 12.50}]
    )
    channel = {"Total Gross Sale": 32033.09, "Total Profit": 7595.09,
               "Total Refund": -704.91}
    record_period(period, MKT, "file_2026.xlsm", sku, channel,
                  ingested_at=datetime(2026, 5, 1, 9, 0, 0), engine=engine)

    # run_history row intact, dates derived to month bounds.
    with engine.connect() as conn:
        rh = conn.execute(run_history.select()).all()
    assert len(rh) == 1
    assert rh[0].period_start == date(2026, 4, 1)
    assert rh[0].period_end == date(2026, 4, 30)
    assert rh[0].source_file == "file_2026.xlsm"

    # SKU row: money came back as Decimal, ad_cost stored signed-negative.
    hist = get_sku_history(MKT, "FG-A", Period(2026, 5), n=3, engine=engine)
    assert len(hist) == 1
    row = hist.iloc[0]
    assert isinstance(row["gross"], Decimal)
    assert row["gross"] == Decimal("100.00")
    assert row["profit"] == Decimal("23.71")
    assert row["profit_margin_pct"] == Decimal("23.7100")
    assert row["ad_cost"] == Decimal("-12.50")
    assert int(row["units"]) == 5

    # Channel line stored long, sign preserved.
    ch = get_channel_history(MKT, Period(2026, 5), n=3, engine=engine)
    refund = ch[ch["line_item"] == "Total Refund"].iloc[0]
    assert refund["value"] == Decimal("-704.91")


def test_idempotent_rewrite_no_duplicates(engine) -> None:
    period = Period(2026, 4)
    sku = _metrics([{"Marketplace SKU": "FG-A"}, {"Marketplace SKU": "FG-B"}])
    channel = {"Total Gross Sale": 100.0}

    record_period(period, MKT, "v1.xlsm", sku, channel, engine=engine)
    record_period(period, MKT, "v1.xlsm", sku, channel, engine=engine)  # re-run

    assert _count(engine, run_history) == 1
    assert _count(engine, sku_period_metrics) == 2
    assert _count(engine, channel_period_metrics) == 1


def test_same_current_period_from_two_files_collapses(engine) -> None:
    # The MoM/YoY anchor case: the same current period arrives in two different
    # source files. The second write replaces the first — one period's rows.
    period = Period(2026, 4)
    record_period(period, MKT, "MoM_2026_03_vs_2026_04.xlsm",
                  _metrics([{"Marketplace SKU": "FG-A"}]), {"Total Gross Sale": 1.0},
                  engine=engine)
    record_period(period, MKT, "YoY_2025_04_vs_2026_04.xlsm",
                  _metrics([{"Marketplace SKU": "FG-A"}]), {"Total Gross Sale": 1.0},
                  engine=engine)

    assert _count(engine, run_history) == 1
    with engine.connect() as conn:
        rh = conn.execute(run_history.select()).all()
    # The surviving row reflects the most recent write.
    assert rh[0].source_file == "YoY_2025_04_vs_2026_04.xlsm"


def test_trailing_periods_order_and_limit(engine) -> None:
    for p in [Period(2025, 12), Period(2026, 1), Period(2026, 2), Period(2026, 3)]:
        record_period(p, MKT, f"f_{p}.xlsm", _metrics([{}]), {}, engine=engine)

    # n=2 before April → Feb, Mar (oldest→newest), not Dec/Jan.
    tp = get_trailing_periods(MKT, Period(2026, 4), n=2, engine=engine)
    assert tp.periods == [Period(2026, 2), Period(2026, 3)]
    assert tp.sufficient is True
    assert tp.available == 2

    # All four, ordered oldest→newest.
    tp_all = get_trailing_periods(MKT, Period(2026, 4), n=10, engine=engine)
    assert tp_all.periods == [Period(2025, 12), Period(2026, 1), Period(2026, 2), Period(2026, 3)]


def test_insufficient_history_is_graceful(engine) -> None:
    for p in [Period(2026, 2), Period(2026, 3)]:
        record_period(p, MKT, f"f_{p}.xlsm", _metrics([{}]), {}, engine=engine)

    tp = get_trailing_periods(MKT, Period(2026, 4), n=6, engine=engine)
    assert tp.available == 2
    assert tp.requested == 6
    assert tp.sufficient is False             # clear not-enough indicator
    assert tp.periods == [Period(2026, 2), Period(2026, 3)]  # returns what exists, no raise


def test_get_sku_history_only_before_current_and_ordered(engine) -> None:
    for p, gross in [(Period(2026, 1), 10.0), (Period(2026, 2), 20.0), (Period(2026, 3), 30.0)]:
        record_period(p, MKT, "f.xlsm",
                      _metrics([{"Marketplace SKU": "FG-A", "gross": gross}]), {},
                      engine=engine)

    hist = get_sku_history(MKT, "FG-A", Period(2026, 3), n=5, engine=engine)
    # Strictly before March → Jan, Feb, oldest→newest.
    assert list(hist["gross"]) == [Decimal("10.00"), Decimal("20.00")]
    assert list(hist["period_start"]) == [date(2026, 1, 1), date(2026, 2, 1)]


def test_get_sku_history_empty_for_unknown_sku(engine) -> None:
    record_period(Period(2026, 2), MKT, "f.xlsm", _metrics([{"Marketplace SKU": "FG-A"}]),
                  {}, engine=engine)
    hist = get_sku_history(MKT, "NOPE", Period(2026, 3), n=3, engine=engine)
    assert hist.empty
    assert list(hist.columns) == ["period_start", "period_end", "units", "gross",
                                  "profit", "profit_margin_pct", "ad_cost"]


def test_channel_history_filter_by_line_item(engine) -> None:
    for p, gross in [(Period(2026, 1), 10.0), (Period(2026, 2), 20.0)]:
        record_period(p, MKT, "f.xlsm", _metrics([{}]),
                      {"Total Gross Sale": gross, "Total Profit": gross / 2}, engine=engine)

    ch = get_channel_history(MKT, Period(2026, 3), n=5, line_item="Total Gross Sale",
                             engine=engine)
    assert list(ch["line_item"].unique()) == ["Total Gross Sale"]
    assert list(ch["value"]) == [Decimal("10.00"), Decimal("20.00")]


def test_trailing_mean_ignores_none_and_handles_empty() -> None:
    assert trailing_mean([Decimal("10.00"), None, Decimal("20.00")]) == pytest.approx(15.0)
    assert trailing_mean([]) is None
    assert trailing_mean([None, None]) is None


def test_record_period_missing_column_raises(engine) -> None:
    bad = pd.DataFrame({"Marketplace SKU": ["FG-A"], "units": [1]})  # no gross/profit/...
    with pytest.raises(ValueError, match="missing required column"):
        record_period(Period(2026, 4), MKT, "f.xlsm", bad, {}, engine=engine)
