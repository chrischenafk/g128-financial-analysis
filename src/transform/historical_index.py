"""Local, persistent store of per-period channel + SKU facts (the history index).

Transform layer — file 3 of 3. This is the **store and its interface**, not the
trend analysis. The build order (PROJECT_CONTEXT §8) defers real trailing-trend
work to a later Level-2 step, and with only three periods on hand there isn't
enough data for meaningful 3/6-month averages yet. So this module implements:
schema, idempotent write, trailing-period read, and graceful
insufficient-history handling — the contract the analysis layer will later call.
It deliberately does NOT implement trailing-average business logic, seasonal
detection, or outlier scoring (those land in the analysis layer once data has
accumulated). A single thin ``trailing_mean`` convenience is included; nothing
heavier.

The store is an **optimization**: the workbooks in ``data/raw/`` remain the
rebuildable source of truth, so this DB is always safe to wipe and rebuild. That
is why writes are idempotent by ``(marketplace, period)`` — re-running a month,
or the same current period arriving in both the MoM and YoY file, must never
double-count (the AGENTS.md determinism guarantee, applied to the store).

Portability is a hard constraint: this migrates to the company SQL Server later.
We use SQLAlchemy Core so the backend swaps via connection string, and only
plain, translatable column types (INTEGER, DECIMAL/Numeric, VARCHAR, DATE,
TIMESTAMP) — no SQLite-only features, no JSON columns. Money is stored as
``Numeric`` so it round-trips as ``Decimal``, not lossy float.

A period is stored by its month identity as ``period_start`` / ``period_end``
DATEs (the month's first and last day), derived from ``period_parser.Period`` —
no parallel period type.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    and_,
    create_engine,
    delete,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from src import config
from src.ingest.period_parser import Period
from src.transform.normalize_tiktok import KEY_COLUMN
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Quantization templates for the Numeric columns (keep money at 2 dp, pct at 4).
_MONEY_Q = Decimal("0.01")
_PCT_Q = Decimal("0.0001")

# ─────────────────────────────────────────────────────────────────────────────
# Schema — two core tables + one optional channel long table. Bounded VARCHAR
# (String(n)) rather than unbounded Text, because SQL Server prefers bounded
# VARCHAR; everything here translates cleanly to SQL Server types.
# ─────────────────────────────────────────────────────────────────────────────
metadata = MetaData()

run_history = Table(
    "run_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("marketplace", String(64), nullable=False),
    Column("source_file", String(255), nullable=False),
    Column("ingested_at", DateTime, nullable=False),
    Column("package_schema_version", String(32), nullable=False),
)

sku_period_metrics = Table(
    "sku_period_metrics",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("marketplace", String(64), nullable=False),
    Column("marketplace_sku", String(128), nullable=False),
    Column("theme", String(128), nullable=True),
    Column("units", Integer, nullable=False),
    Column("gross", Numeric(12, 2), nullable=False),
    Column("profit", Numeric(12, 2), nullable=False),
    Column("profit_margin_pct", Numeric(7, 4), nullable=True),
    Column("ad_cost", Numeric(12, 2), nullable=False),
)

# Channel P&L stored long/narrow (one row per line item) rather than wide: the
# Summary has many line items and the set can shift between periods, so a long
# table is portable and schema-stable (no column churn when a line item appears).
channel_period_metrics = Table(
    "channel_period_metrics",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("marketplace", String(64), nullable=False),
    Column("line_item", String(128), nullable=False),
    Column("value", Numeric(14, 2), nullable=True),
)


# ─────────────────────────────────────────────────────────────────────────────
# Return type for trailing-period lookups
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TrailingPeriods:
    """Up to ``requested`` periods before a reference period, oldest→newest.

    ``sufficient`` tells the caller whether the full window was available —
    insufficient history is a normal result, not an error.
    """

    periods: list[Period]
    requested: int

    @property
    def available(self) -> int:
        return len(self.periods)

    @property
    def sufficient(self) -> bool:
        return self.available >= self.requested


# ─────────────────────────────────────────────────────────────────────────────
# Engine resolution (accepts an Engine, a URL string, or None → config default)
# ─────────────────────────────────────────────────────────────────────────────
def _default_url() -> str:
    # as_posix() so a Windows path becomes a valid sqlite URL (forward slashes).
    return f"sqlite:///{config.HISTORY_DB.as_posix()}"


def _engine_from_url(url: str) -> Engine:
    # In-memory SQLite needs a StaticPool so every connection sees the SAME DB
    # (otherwise the pool hands out fresh, empty in-memory databases). Tests rely
    # on this to share one in-memory DB across init/record/read calls.
    if url == "sqlite://" or ":memory:" in url:
        return create_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    return create_engine(url)


def _resolve_engine(engine: Engine | str | None) -> Engine:
    if isinstance(engine, Engine):
        return engine
    if isinstance(engine, str):
        return _engine_from_url(engine)
    if engine is None:
        return _engine_from_url(_default_url())
    raise TypeError(f"engine must be an Engine, a URL string, or None; got {type(engine)!r}.")


# ─────────────────────────────────────────────────────────────────────────────
# Period <-> date helpers
# ─────────────────────────────────────────────────────────────────────────────
def _period_bounds(period: Period) -> tuple[date, date]:
    """(first day, last day) of the period's month."""
    start = date(period.year, period.month, 1)
    last_day = calendar.monthrange(period.year, period.month)[1]
    return start, date(period.year, period.month, last_day)


def _period_from_start(start: date) -> Period:
    return Period(start.year, start.month)


# ─────────────────────────────────────────────────────────────────────────────
# Value coercion (NaN/None → SQL NULL; floats → quantized Decimal)
# ─────────────────────────────────────────────────────────────────────────────
def _decimal_or_none(value: object, quantum: Decimal) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(value)).quantize(quantum)


def _int_or_none(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


# ─────────────────────────────────────────────────────────────────────────────
# Schema creation
# ─────────────────────────────────────────────────────────────────────────────
def init_db(engine: Engine | str | None = None) -> Engine:
    """Create the tables if absent (idempotent) and return the engine.

    Returning the engine lets a caller (and tests, esp. in-memory) reuse the same
    one across ``record_period`` / retrieval calls.
    """
    eng = _resolve_engine(engine)
    metadata.create_all(eng)
    logger.info("History store initialized at %s.", eng.url)
    return eng


# ─────────────────────────────────────────────────────────────────────────────
# Write — idempotent by (marketplace, period)
# ─────────────────────────────────────────────────────────────────────────────
def record_period(
    period: Period,
    marketplace: str,
    source_file: str,
    sku_metrics: pd.DataFrame,
    channel: dict[str, float | None] | None = None,
    *,
    package_schema_version: str = config.PACKAGE_SCHEMA_VERSION,
    ingested_at: datetime | None = None,
    engine: Engine | str | None = None,
) -> None:
    """Write one period's channel + active-SKU facts for ``marketplace``.

    Idempotent by ``(marketplace, period)``: existing rows for that key are
    deleted then re-inserted in one transaction, so re-running a month — or the
    same current period arriving via both the MoM and YoY file — yields exactly
    one set of rows. This module does NOT recompute metrics; it persists the
    already-computed ``sku_metrics`` DataFrame (from ``sku_metrics.py``) and the
    ``channel`` line-item dict (from ``normalize_tiktok.summary[period]``).

    ``ad_cost`` is stored signed-negative (``-ad_spend``), matching the
    costs-are-negative convention used everywhere upstream.

    Raises:
        ValueError: if a required SKU-metrics column is missing.
    """
    required = {KEY_COLUMN, "units", "gross", "profit", "profit_margin_pct", "ad_spend"}
    missing = required - set(sku_metrics.columns)
    if missing:
        raise ValueError(
            f"record_period: sku_metrics is missing required column(s) "
            f"{sorted(missing)}. Present: {list(sku_metrics.columns)}."
        )

    eng = _resolve_engine(engine)
    start, end = _period_bounds(period)
    stamped_at = ingested_at if ingested_at is not None else datetime.now()
    has_theme = "Theme" in sku_metrics.columns

    sku_rows = [
        {
            "period_start": start,
            "period_end": end,
            "marketplace": marketplace,
            "marketplace_sku": str(row[KEY_COLUMN]),
            "theme": (str(row["Theme"]) if has_theme and not pd.isna(row["Theme"]) else None),
            "units": _int_or_none(row["units"]),
            "gross": _decimal_or_none(row["gross"], _MONEY_Q),
            "profit": _decimal_or_none(row["profit"], _MONEY_Q),
            "profit_margin_pct": _decimal_or_none(row["profit_margin_pct"], _PCT_Q),
            # +0.0 avoids storing a -0.00 when ad_spend is 0.
            "ad_cost": _decimal_or_none(-float(row["ad_spend"]) + 0.0, _MONEY_Q),
        }
        for _, row in sku_metrics.iterrows()
    ]

    channel_rows = [
        {
            "period_start": start,
            "period_end": end,
            "marketplace": marketplace,
            "line_item": line_item,
            "value": _decimal_or_none(value, _MONEY_Q),
        }
        for line_item, value in (channel or {}).items()
    ]

    with eng.begin() as conn:
        # Delete-then-insert = the idempotency guarantee for this (marketplace, period).
        for table in (run_history, sku_period_metrics, channel_period_metrics):
            conn.execute(
                delete(table).where(
                    and_(table.c.marketplace == marketplace, table.c.period_start == start)
                )
            )
        conn.execute(
            run_history.insert().values(
                period_start=start,
                period_end=end,
                marketplace=marketplace,
                source_file=source_file,
                ingested_at=stamped_at,
                package_schema_version=package_schema_version,
            )
        )
        if sku_rows:
            conn.execute(sku_period_metrics.insert(), sku_rows)
        if channel_rows:
            conn.execute(channel_period_metrics.insert(), channel_rows)

    logger.info(
        "Recorded %s (%s): %d SKU row(s), %d channel line(s) from %r "
        "(replaced any existing rows for this period).",
        period,
        marketplace,
        len(sku_rows),
        len(channel_rows),
        source_file,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Read — trailing windows (retrieval returns plain data; no interpretation here)
# ─────────────────────────────────────────────────────────────────────────────
def get_trailing_periods(
    marketplace: str,
    current_period: Period,
    n: int,
    engine: Engine | str | None = None,
) -> TrailingPeriods:
    """Up to ``n`` recorded periods strictly before ``current_period``, oldest→newest.

    Insufficient history (fewer than ``n``) is a normal result — see
    ``TrailingPeriods.sufficient`` — not an exception.
    """
    eng = _resolve_engine(engine)
    start, _ = _period_bounds(current_period)
    with eng.connect() as conn:
        rows = conn.execute(
            select(run_history.c.period_start)
            .where(
                and_(
                    run_history.c.marketplace == marketplace,
                    run_history.c.period_start < start,
                )
            )
            .order_by(run_history.c.period_start.desc())
            .limit(n)
        ).all()

    periods = [_period_from_start(r.period_start) for r in rows][::-1]  # oldest→newest
    result = TrailingPeriods(periods=periods, requested=n)
    logger.info(
        "Trailing history for %s before %s: %d of %d requested period(s) available%s.",
        marketplace,
        current_period,
        result.available,
        n,
        "" if result.sufficient else " (insufficient)",
    )
    return result


def get_sku_history(
    marketplace: str,
    marketplace_sku: str,
    current_period: Period,
    n: int,
    engine: Engine | str | None = None,
) -> pd.DataFrame:
    """Trailing metric rows for one SKU, oldest→newest (up to ``n`` periods).

    Returns a DataFrame (possibly empty) with columns ``period_start``,
    ``period_end``, ``units``, ``gross``, ``profit``, ``profit_margin_pct``,
    ``ad_cost``. Money values come back as ``Decimal``.
    """
    eng = _resolve_engine(engine)
    start, _ = _period_bounds(current_period)
    cols = (
        sku_period_metrics.c.period_start,
        sku_period_metrics.c.period_end,
        sku_period_metrics.c.units,
        sku_period_metrics.c.gross,
        sku_period_metrics.c.profit,
        sku_period_metrics.c.profit_margin_pct,
        sku_period_metrics.c.ad_cost,
    )
    with eng.connect() as conn:
        rows = conn.execute(
            select(*cols)
            .where(
                and_(
                    sku_period_metrics.c.marketplace == marketplace,
                    sku_period_metrics.c.marketplace_sku == marketplace_sku,
                    sku_period_metrics.c.period_start < start,
                )
            )
            .order_by(sku_period_metrics.c.period_start.desc())
            .limit(n)
        ).all()

    column_names = ["period_start", "period_end", "units", "gross", "profit",
                    "profit_margin_pct", "ad_cost"]
    return pd.DataFrame(rows[::-1], columns=column_names)


def get_channel_history(
    marketplace: str,
    current_period: Period,
    n: int,
    line_item: str | None = None,
    engine: Engine | str | None = None,
) -> pd.DataFrame:
    """Trailing channel line-item rows, oldest→newest, over up to ``n`` periods.

    Optionally filter to a single ``line_item``. Returns a DataFrame with
    ``period_start``, ``period_end``, ``line_item``, ``value`` (``value`` as
    ``Decimal``).
    """
    eng = _resolve_engine(engine)
    trailing = get_trailing_periods(marketplace, current_period, n, engine=eng)
    starts = [_period_bounds(p)[0] for p in trailing.periods]
    column_names = ["period_start", "period_end", "line_item", "value"]
    if not starts:
        return pd.DataFrame(columns=column_names)

    query = (
        select(
            channel_period_metrics.c.period_start,
            channel_period_metrics.c.period_end,
            channel_period_metrics.c.line_item,
            channel_period_metrics.c.value,
        )
        .where(
            and_(
                channel_period_metrics.c.marketplace == marketplace,
                channel_period_metrics.c.period_start.in_(starts),
            )
        )
        .order_by(
            channel_period_metrics.c.period_start.asc(),
            channel_period_metrics.c.line_item.asc(),
        )
    )
    if line_item is not None:
        query = query.where(channel_period_metrics.c.line_item == line_item)

    with eng.connect() as conn:
        rows = conn.execute(query).all()
    return pd.DataFrame(rows, columns=column_names)


# ─────────────────────────────────────────────────────────────────────────────
# Thin convenience (no seasonal / outlier logic — that's the analysis layer)
# ─────────────────────────────────────────────────────────────────────────────
def trailing_mean(values: pd.Series | list) -> float | None:
    """Mean of a trailing series, ignoring NaN/None; ``None`` if nothing to average.

    A deliberately thin helper. All real trend interpretation (weighting,
    seasonality, outliers) belongs to the later analysis layer.
    """
    series = pd.Series(list(values), dtype="float64").dropna()
    return float(series.mean()) if len(series) else None
