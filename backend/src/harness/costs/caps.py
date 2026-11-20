"""Basic hard spend caps: allow or deny, and deny at or over the cap.

Where the runner calls them (docs/costs.md, "Spend caps"):

- per-run cap: `check_run_cap` before each segment and after each recorded usage report; a
  deny stops the run.
- per-hotel daily cap: `check_hotel_daily_cap` before a new run starts; a deny blocks new runs
  for the hotel until the next UTC day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from harness.costs.ledger import CostLedger
from harness.tenancy import TenantContext


@dataclass(frozen=True, slots=True)
class CapDecision:
    allowed: bool
    cap_usd: Decimal
    spent_usd: Decimal
    remaining_usd: Decimal
    reason: str


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


def check_run_cap(
    ledger: CostLedger, tenant: TenantContext, run_id: UUID, cap_usd: Decimal
) -> CapDecision:
    """Per-run cap. `tenant` must be the run's own tenant (rows of others are invisible)."""
    return check_spend_cap(cap_usd, ledger.run_total(tenant, run_id))


def check_hotel_daily_cap(
    ledger: CostLedger, tenant: TenantContext, cap_usd: Decimal, *, now: datetime
) -> CapDecision:
    """Per-hotel cap for the UTC day that contains `now`."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now must be a timezone-aware datetime, got {now!r}")
    day = now.astimezone(UTC).date()
    return check_spend_cap(cap_usd, ledger.hotel_total(tenant, day=day))
