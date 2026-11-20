"""Price a usage report from the price table, and cross-check the SDK's own cost."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from harness.costs.prices import (
    PRICE_BOOK,
    PRICE_TABLE,
    PriceBook,
    PriceTable,
    Provider,
    UnpricedUsageError,
)
from harness.costs.usage import MAX_TOKENS, MAX_USD, UsageReport, quantize_usd

logger = logging.getLogger("harness.costs")

_PER_MTOK = Decimal(1_000_000)


class WebSearchNotPricedError(UnpricedUsageError):
    """The report has web search requests, which the table does not price (decision C12)."""

    reason = "web_search"


class CostOutOfRangeError(UnpricedUsageError):
    """The cost does not fit numeric(20, 9). Such usage is far over any cap."""

    reason = "cost_out_of_range"


# An SDK-reported cost that differs from ours by more than the larger of these is logged.
SDK_COST_REL_TOLERANCE = Decimal("0.01")
SDK_COST_ABS_TOLERANCE = Decimal("0.000001")


def _check_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if not 0 <= value <= MAX_TOKENS:
        raise ValueError(f"{name} must be between 0 and {MAX_TOKENS}, got {value}")


def _price_counts(
    table: PriceTable,
    provider: Provider,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> Decimal:
    counts = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }
    for name, value in counts.items():
        _check_count(name, value)
    prices = table.get(provider, model).prices
    with localcontext() as ctx:
        ctx.prec = 60  # exact for any bigint token count and any price
        total = quantize_usd(
            (
                input_tokens * prices.input
                + output_tokens * prices.output
                + cache_read_tokens * prices.cache_read
                + cache_write_tokens * prices.cache_write
            )
            / _PER_MTOK
        )
    if total >= MAX_USD:
        raise CostOutOfRangeError(f"cost {total} USD of model {model!r} does not fit the ledger")
    return total


def price_usage(
    provider: Provider,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    at: datetime | None = None,
    prices: PriceBook = PRICE_BOOK,
) -> Decimal:
    """Price token counts with the table in effect at `at` (default now), as the ledger does.

    sum(tokens x USD per MTok) / 1e6, rounded half up to 9 places. Use it for a running
    estimate inside a segment (pass the result to the caps as `pending_usd`). Raises an
    UnpricedUsageError subclass: an unpriced model, no table in effect, or a cost of 10^11 USD
    or more.
    """
    return _price_counts(
        prices.table_at(at or datetime.now(UTC)),
        provider,
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def cost_usd(report: UsageReport, table: PriceTable = PRICE_TABLE) -> Decimal:
    """The billed cost of one report, priced from `table`.

    Raises an UnpricedUsageError subclass when the report cannot be priced: an unknown,
    tiered or non-first-party model, web search requests, or a cost out of range.
    """
    if report.web_search_requests:
        raise WebSearchNotPricedError(
            f"{report.web_search_requests} web search requests on model {report.model!r} "
            "are not priced (docs/costs.md, decision C12)"
        )
    return _price_counts(
        table,
        report.provider,
        report.model,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cache_read_tokens=report.cache_read_tokens,
        cache_write_tokens=report.cache_write_tokens,
    )


class SdkCostCheck(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_REPORTED = "not_reported"


def check_sdk_cost(
    report: UsageReport,
    our_cost_usd: Decimal,
    *,
    rel_tolerance: Decimal = SDK_COST_REL_TOLERANCE,
    abs_tolerance: Decimal = SDK_COST_ABS_TOLERANCE,
) -> SdkCostCheck:
    """Compare the SDK-reported cost with ours and log a warning on a mismatch.

    The result never changes the billed value; it only flags drift in the price table or in
    the adapter's token mapping.
    """
    sdk = report.sdk_reported_cost_usd
    if sdk is None:
        return SdkCostCheck.NOT_REPORTED
    tolerance = max(abs_tolerance, rel_tolerance * our_cost_usd)
    if abs(sdk - our_cost_usd) <= tolerance:
        return SdkCostCheck.MATCH
    logger.warning(
        "SDK cost mismatch: run_id=%s segment_id=%s provider=%s model=%s "
        "ours_usd=%s sdk_usd=%s tolerance_usd=%s",
        report.run_id,
        report.segment_id,
        report.provider,
        report.model,
        our_cost_usd,
        sdk,
        tolerance,
    )
    return SdkCostCheck.MISMATCH
