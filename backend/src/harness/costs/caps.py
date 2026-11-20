"""Basic hard spend caps: allow or deny, deny at or over the cap, and deny unpriced usage.

Where the runner calls them (docs/costs.md, "Spend caps"):

- After each usage report: `CostLedger.record(report, caps=SpendCaps(...))` records the row and
  checks the run cap and the hotel daily cap with it included, in one atomic step. A deny
  (`RecordResult.stop_run`) stops the run.
- Before each segment: `check_run_cap` and `check_hotel_daily_cap`. Before a new run:
  `check_hotel_daily_cap`. Pass the running estimate of the open segment (from `price_usage`)
  as `pending_usd` to check during a segment. For a hotel with no daily cap, pass
  `cap_usd=None` (or call `check_hotel_unpriced_limit` directly): the C16 limit still applies.

Unpriced usage (decision C11, option a): the run that has it stops (its run cap denies), an
alert is raised, and `check_run_models` refuses that exact model id at the pre-run check until
the price table in effect prices it. The hotel daily cap counts priced spend only, so the
hotel's other runs and models continue, until UNPRICED_RUNS_HOTEL_DAILY_LIMIT runs of the hotel
were stopped by a model-pricing gap (UNPRICED_LIMIT_REASONS) in one UTC day (decision C16):
then `check_hotel_unpriced_limit` (also inside `check_hotel_daily_cap`) denies new runs and new
segments for the rest of that UTC day with UNPRICED_LIMIT.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from harness.costs.prices import (
    PRICE_BOOK,
    PriceBook,
    Provider,
    TieredPricingError,
    UnknownModelError,
    UnpricedUsageError,
)
from harness.tenancy import TenantContext

if TYPE_CHECKING:
    from harness.costs.ledger import CostLedger

# Failure codes of runtime contract v2 that cost checks give. A USD cap deny is
# BUDGET_EXHAUSTED, which the orchestrator owns.
USAGE_UNPRICED = "USAGE_UNPRICED"  # a run stopped because it has unpriced usage
CONFIG_REJECTED = "CONFIG_REJECTED"  # the pre-run model check refused a model id
UNPRICED_LIMIT = "UNPRICED_LIMIT"  # the hotel reached the C16 unpriced-run limit for the day

# Decision C16: runs of one hotel stopped by a model-pricing gap in one UTC day before
# check_hotel_unpriced_limit denies the hotel for the rest of that day.
UNPRICED_RUNS_HOTEL_DAILY_LIMIT = 3
# The unpriced reasons that C16 counts: gaps in the price table, which the pre-run model check
# cannot catch for an id it was not given. web_search, non_first_party, cost_out_of_range and
# no_price_table are not counted: each one already stops its own run.
UNPRICED_LIMIT_REASONS: frozenset[str] = frozenset(
    {UnknownModelError.reason, TieredPricingError.reason}
)


@dataclass(frozen=True, slots=True)
class Spend:
    """Spend in one scope: the priced total, the rows that have no price, and the distinct
    runs that have a row with a reason in UNPRICED_LIMIT_REASONS (decision C16)."""

    priced_usd: Decimal
    unpriced_rows: int
    pricing_gap_runs: int = 0


@dataclass(frozen=True, slots=True)
class SpendCaps:
    """Caps to check when a report is recorded. None means that cap is not checked."""

    run_usd: Decimal | None = None
    hotel_daily_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CapDecision:
    """A cap check. `cap_usd` and `remaining_usd` are None only for a hotel with no daily cap.

    `failure_code` is USAGE_UNPRICED (the run has unpriced usage), UNPRICED_LIMIT (decision
    C16) or None. A deny with None is a USD cap deny: BUDGET_EXHAUSTED, from the orchestrator.
    """

    allowed: bool
    cap_usd: Decimal | None
    spent_usd: Decimal
    remaining_usd: Decimal | None
    reason: str
    unpriced_rows: int = 0
    failure_code: str | None = None


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
        failure_code=USAGE_UNPRICED,
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


@dataclass(frozen=True, slots=True)
class UnpricedLimitDecision:
    """Decision C16 for one hotel and UTC day."""

    allowed: bool
    pricing_gap_runs: int
    limit: int
    reason: str

    @property
    def failure_code(self) -> str | None:
        """UNPRICED_LIMIT when denied, else None."""
        return None if self.allowed else UNPRICED_LIMIT


def check_hotel_unpriced_limit(
    ledger: CostLedger, tenant: TenantContext, *, now: datetime
) -> UnpricedLimitDecision:
    """Decision C16: deny a hotel for the rest of the UTC day that contains `now` once
    UNPRICED_RUNS_HOTEL_DAILY_LIMIT of its runs were stopped by a model-pricing gap.

    Needs no cap, so it applies to every hotel. Call it before a run and before each segment
    (`check_hotel_daily_cap` calls it too). `record` does not apply it, so a run in flight stops
    at its next pre-segment call.
    """
    runs = ledger.hotel_spend(tenant, day=utc_day(now)).pricing_gap_runs
    limit = UNPRICED_RUNS_HOTEL_DAILY_LIMIT
    if runs < limit:
        return UnpricedLimitDecision(
            allowed=True,
            pricing_gap_runs=runs,
            limit=limit,
            reason=f"{runs} runs of this hotel stopped on a price gap today (C16 limit {limit})",
        )
    return UnpricedLimitDecision(
        allowed=False,
        pricing_gap_runs=runs,
        limit=limit,
        reason=(
            f"{runs} runs of this hotel stopped on unpriced usage of a model with no usable "
            f"price today (C16 limit {limit}): no new runs or segments until the next UTC day "
            "(decision C16, UNPRICED_LIMIT)"
        ),
    )


def check_hotel_daily_cap(
    ledger: CostLedger,
    tenant: TenantContext,
    cap_usd: Decimal | None,
    *,
    now: datetime,
    pending_usd: Decimal = Decimal(0),
) -> CapDecision:
    """Per-hotel cap for the UTC day that contains `now`, on priced spend only, plus the C16
    limit (`check_hotel_unpriced_limit`). `cap_usd=None` (no daily cap) checks only C16.

    When both deny, the C16 deny (UNPRICED_LIMIT) is returned.
    """
    _check_amount("pending_usd", pending_usd)
    spend = ledger.hotel_spend(tenant, day=utc_day(now))
    limit = check_hotel_unpriced_limit(ledger, tenant, now=now)
    if cap_usd is None:
        decision = CapDecision(
            allowed=True,
            cap_usd=None,
            spent_usd=spend.priced_usd + pending_usd,
            remaining_usd=None,
            reason=f"no hotel daily cap; spent {spend.priced_usd + pending_usd} USD today",
        )
    else:
        decision = check_hotel_spend(cap_usd, spend, pending_usd=pending_usd)
    if limit.allowed:
        return decision
    return CapDecision(
        allowed=False,
        cap_usd=decision.cap_usd,
        spent_usd=decision.spent_usd,
        remaining_usd=None if cap_usd is None else Decimal(0),
        reason=limit.reason,
        unpriced_rows=spend.unpriced_rows,
        failure_code=UNPRICED_LIMIT,
    )


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

    @property
    def failure_code(self) -> str | None:
        """CONFIG_REJECTED when a model is refused, else None."""
        return CONFIG_REJECTED if self.refused else None


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
