"""Basic hard spend caps: allow or deny, deny at or over the cap, and deny unpriced usage.

Where the runner calls them (docs/costs.md, "Spend caps"):

- After each usage report: `CostLedger.record(report, caps=SpendCaps(...))` records the row and
  checks the run cap and the hotel daily cap with it included, in one atomic step. A deny
  (`RecordResult.stop_run`) stops the run.
- Before each segment: `check_run_cap` and `check_hotel_daily_cap`. Before a new run:
  `check_hotel_daily_cap`. Pass the running estimate of the open segment (from `price_usage`)
  as `pending_usd` to check during a segment.

Unpriced usage (decision C11, option a): the run that has it stops (its run cap denies), an
alert is raised, and `check_run_models` refuses that exact model id at the pre-run check until
the price table in effect prices it. The hotel daily cap counts priced spend only, so the
hotel's other runs and models continue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from harness.costs.prices import PRICE_BOOK, PriceBook, Provider, UnpricedUsageError
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


def check_run_spend(
    cap_usd: Decimal, spend: Spend, *, pending_usd: Decimal = Decimal(0)
) -> CapDecision:
    """Check a run's `spend` plus `pending_usd`. Any unpriced row of the run denies."""
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
            f"{spend.unpriced_rows} unpriced usage row(s) in this run: its spend is unknown, so "
            f"the run stops (priced spend {decision.spent_usd} USD, cap {cap_usd} USD)"
        ),
        unpriced_rows=spend.unpriced_rows,
    )


def check_hotel_spend(
    cap_usd: Decimal, spend: Spend, *, pending_usd: Decimal = Decimal(0)
) -> CapDecision:
    """Check a hotel's priced spend plus `pending_usd`. Unpriced rows are not counted (C11)."""
    _check_amount("pending_usd", pending_usd)
    return check_spend_cap(cap_usd, spend.priced_usd + pending_usd)


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
    return check_run_spend(cap_usd, ledger.run_spend(tenant, run_id), pending_usd=pending_usd)


def check_hotel_daily_cap(
    ledger: CostLedger,
    tenant: TenantContext,
    cap_usd: Decimal,
    *,
    now: datetime,
    pending_usd: Decimal = Decimal(0),
) -> CapDecision:
    """Per-hotel cap for the UTC day that contains `now`, on priced spend only."""
    spend = ledger.hotel_spend(tenant, day=utc_day(now))
    return check_hotel_spend(cap_usd, spend, pending_usd=pending_usd)


@dataclass(frozen=True, slots=True)
class RefusedModel:
    provider: Provider
    model: str
    reason: str  # an unpriced_reason code
    seen_unpriced: bool  # this hotel already has unpriced usage of this exact id


@dataclass(frozen=True, slots=True)
class ModelCheck:
    refused: tuple[RefusedModel, ...]

    @property
    def allowed(self) -> bool:
        return not self.refused


def check_run_models(
    ledger: CostLedger,
    tenant: TenantContext,
    models: Iterable[tuple[Provider, str]],
    *,
    now: datetime,
    prices: PriceBook = PRICE_BOOK,
) -> ModelCheck:
    """Pre-run check: refuse every exact model id that the table in effect at `now` cannot price.

    Pass every model the run may call (run, fallback, small fast and subagent models). An id
    the hotel already ran unpriced is marked `seen_unpriced`. The refusal clears by itself when
    a price table that prices the id takes effect; nothing else needs to be cleared.
    """
    seen = {(u.provider, u.model) for u in ledger.unpriced_models(tenant)}
    refused: list[RefusedModel] = []
    for provider, model in dict.fromkeys(models):
        try:
            prices.table_at(now).get(provider, model)
        except UnpricedUsageError as exc:
            refused.append(RefusedModel(provider, model, exc.reason, (provider, model) in seen))
    return ModelCheck(refused=tuple(refused))
