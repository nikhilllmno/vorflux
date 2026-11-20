"""Basic hard spend caps: allow or deny, deny at or over the cap, and deny unpriced usage.

Where the runner calls them (docs/costs.md, "Spend caps"):

- After each usage report: `CostLedger.record(report, caps=SpendCaps(...))` records the row and
  checks the run cap and the hotel daily cap with it included, in one atomic step. A deny
  (`RecordResult.stop_run`) stops the run.
- Before each segment: `check_run_cap` and `check_hotel_daily_cap`. Before a new run:
  `check_hotel_daily_cap`. Pass the running estimate of the open segment (from `price_usage`)
  as `pending_usd` to check during a segment.

Usage that has no price (unpriced rows) fails closed: every check that sees it denies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from harness.tenancy import TenantContext

if TYPE_CHECKING:
    from harness.costs.ledger import CostLedger


@dataclass(frozen=True, slots=True)
class Spend:
    """Spend in one scope: the priced total and the number of rows that have no price."""

    priced_usd: Decimal
    unpriced_rows: int


@dataclass(frozen=True, slots=True)
class SpendCaps:
    """Caps to check when a report is recorded. None means that cap is not checked."""

    run_usd: Decimal | None = None
    hotel_daily_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CapDecision:
    allowed: bool
    cap_usd: Decimal
    spent_usd: Decimal
    remaining_usd: Decimal
    reason: str
    unpriced_rows: int = 0


def _check_amount(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite, got {value}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")


def check_spend_cap(cap_usd: Decimal, spent_usd: Decimal) -> CapDecision:
    """Allow only while spend is strictly below the cap. A cap of zero allows nothing."""
    _check_amount("cap_usd", cap_usd)
    _check_amount("spent_usd", spent_usd)
    if spent_usd < cap_usd:
        return CapDecision(
            allowed=True,
            cap_usd=cap_usd,
            spent_usd=spent_usd,
            remaining_usd=cap_usd - spent_usd,
            reason=f"spent {spent_usd} USD of the {cap_usd} USD cap",
        )
    return CapDecision(
        allowed=False,
        cap_usd=cap_usd,
        spent_usd=spent_usd,
        remaining_usd=Decimal(0),
        reason=f"spend cap reached: spent {spent_usd} USD, cap {cap_usd} USD",
    )


def check_spend(
    cap_usd: Decimal, spend: Spend, *, pending_usd: Decimal = Decimal(0)
) -> CapDecision:
    """Check `spend` plus `pending_usd` against the cap. Any unpriced row denies (fail closed)."""
    _check_amount("pending_usd", pending_usd)
    decision = check_spend_cap(cap_usd, spend.priced_usd + pending_usd)
    if not spend.unpriced_rows:
        return decision
    return CapDecision(
        allowed=False,
        cap_usd=decision.cap_usd,
        spent_usd=decision.spent_usd,
        remaining_usd=Decimal(0),
        reason=(
            f"{spend.unpriced_rows} unpriced usage row(s): spend is unknown, so the cap denies "
            f"(priced spend {decision.spent_usd} USD, cap {cap_usd} USD)"
        ),
        unpriced_rows=spend.unpriced_rows,
    )


def utc_day(moment: datetime) -> date:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"now must be a timezone-aware datetime, got {moment!r}")
    return moment.astimezone(UTC).date()


def check_run_cap(
    ledger: CostLedger,
    tenant: TenantContext,
    run_id: UUID,
    cap_usd: Decimal,
    *,
    pending_usd: Decimal = Decimal(0),
) -> CapDecision:
    """Per-run cap. `tenant` must be the run's own tenant (rows of others are invisible)."""
    return check_spend(cap_usd, ledger.run_spend(tenant, run_id), pending_usd=pending_usd)


def check_hotel_daily_cap(
    ledger: CostLedger,
    tenant: TenantContext,
    cap_usd: Decimal,
    *,
    now: datetime,
    pending_usd: Decimal = Decimal(0),
) -> CapDecision:
    """Per-hotel cap for the UTC day that contains `now`."""
    spend = ledger.hotel_spend(tenant, day=utc_day(now))
    return check_spend(cap_usd, spend, pending_usd=pending_usd)
