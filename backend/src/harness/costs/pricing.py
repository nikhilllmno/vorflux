"""Price a usage report from the price table, and cross-check the SDK's own cost."""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

from harness.costs.prices import PRICE_TABLE, PriceTable
from harness.costs.usage import UsageReport

logger = logging.getLogger("harness.costs")

# Costs are stored as numeric(20, 9): nine decimal places of a US dollar.
COST_QUANTUM = Decimal("0.000000001")
_PER_MTOK = Decimal(1_000_000)

# An SDK-reported cost that differs from ours by more than the larger of these is logged.
SDK_COST_REL_TOLERANCE = Decimal("0.01")
SDK_COST_ABS_TOLERANCE = Decimal("0.000001")


def quantize_usd(amount: Decimal) -> Decimal:
    """Round a USD amount to COST_QUANTUM, half up."""
    return amount.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def cost_usd(report: UsageReport, table: PriceTable = PRICE_TABLE) -> Decimal:
    """The billed cost of one report: sum(tokens x USD per MTok) / 1e6, rounded half up.

    Raises UnknownModelError if the table has no price for the model.
    """
    prices = table.get(report.provider, report.model).prices
    with localcontext() as ctx:
        ctx.prec = 60  # exact for any realistic token count and price
        total = (
            report.input_tokens * prices.input
            + report.output_tokens * prices.output
            + report.cache_read_tokens * prices.cache_read
            + report.cache_write_tokens * prices.cache_write
        ) / _PER_MTOK
        return quantize_usd(total)


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
