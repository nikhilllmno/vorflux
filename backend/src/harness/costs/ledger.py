"""The cost ledger: one immutable row per (run, segment, provider, exact model id).

`CostLedger` is the contract. `InMemoryCostLedger` implements it for tests and local runs. A
Postgres store over the proposed `run_costs` table (docs/costs.md) implements it later and
reuses `build_cost_row`, so both stores compute the same row.

Usage is always recorded. Usage that cannot be priced gets `pricing_status = unpriced` and no
cost. Decision C11 (option a): that run stops, an alert is raised (`on_unpriced` hook and an
ERROR log event), and the pre-run check refuses that exact model id until a price table prices
it. The hotel daily cap counts priced spend only, so other runs and models continue.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from harness.costs.caps import (
    CapDecision,
    Spend,
    SpendCaps,
    check_hotel_spend,
    check_run_spend,
    utc_day,
)
from harness.costs.prices import PRICE_BOOK, PriceBook, Provider, UnpricedUsageError
from harness.costs.pricing import check_sdk_cost, cost_usd
from harness.costs.usage import UsageKey, UsageReport, idempotency_key, quantize_usd
from harness.tenancy import TenantContext

logger = logging.getLogger("harness.costs")


@dataclass(frozen=True, slots=True)
class RunTenant:
    """The org and hotel that own a run, as stored on the run itself."""

    org_id: UUID
    hotel_id: UUID


class UnknownRunError(LookupError):
    """The run does not exist, so its usage has no tenant and is not recorded."""

    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"unknown run {run_id}; usage is not recorded")
        self.run_id = run_id


class RunTenantLookup(Protocol):
    """Finds the tenant of a run (the runs table in production). Raises UnknownRunError."""

    def tenant_of(self, run_id: UUID) -> RunTenant: ...


class RecordStatus(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"


class PricingStatus(StrEnum):
    PRICED = "priced"
    UNPRICED = "unpriced"


@dataclass(frozen=True, slots=True)
class CostRow:
    """One ledger row. Field names match the proposed run_costs columns (docs/costs.md)."""

    org_id: UUID
    hotel_id: UUID
    run_id: UUID
    segment_id: str
    provider: Provider
    model: str  # the exact id the runtime reported
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    web_search_requests: int
    pricing_status: PricingStatus
    unpriced_reason: str | None  # set only when unpriced
    cost_usd: Decimal | None  # None only when unpriced
    sdk_reported_cost_usd: Decimal | None
    price_table_version: str | None  # None only when no table was in effect
    recorded_at: datetime

    @property
    def key(self) -> UsageKey:
        return UsageKey(self.run_id, self.segment_id, self.provider, self.model)


@dataclass(frozen=True, slots=True)
class UnpricedUsageAlert:
    """Raised once per new unpriced row. A hook for notifications (DEX-21) and paging."""

    org_id: UUID
    hotel_id: UUID
    run_id: UUID
    segment_id: str
    provider: Provider
    model: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class UnpricedModel:
    """An exact model id that a hotel ran unpriced, for one reason."""

    provider: Provider
    model: str
    reason: str
    rows: int
    last_recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RecordResult:
    """What `record` did, and the cap decisions taken in the same atomic step."""

    status: RecordStatus
    row: CostRow  # the stored row (the first one, for a duplicate)
    run_spend: Spend
    hotel_day_spend: Spend
    run_cap: CapDecision | None
    hotel_daily_cap: CapDecision | None

    @property
    def stop_run(self) -> bool:
        """True when the run must stop: unpriced usage in this run, or a cap at its limit."""
        if self.run_spend.unpriced_rows:
            return True
        return any(d is not None and not d.allowed for d in (self.run_cap, self.hotel_daily_cap))


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_cost_row(
    report: UsageReport,
    owner: RunTenant,
    *,
    recorded_at: datetime,
    prices: PriceBook = PRICE_BOOK,
) -> CostRow:
    """Price a report with the table in effect at `recorded_at` and turn it into a row.

    Never raises for a pricing problem: the row is unpriced and carries the reason.
    Every store must build rows with this.
    """
    version: str | None = None
    cost: Decimal | None = None
    reason: str | None = None
    try:
        table = prices.table_at(recorded_at)
        version = table.version
        cost = cost_usd(report, table)
    except UnpricedUsageError as exc:
        reason = exc.reason
        logger.warning(
            "usage recorded unpriced, the run must stop: reason=%s run_id=%s segment_id=%s "
            "provider=%s model=%s: %s",
            reason,
            report.run_id,
            report.segment_id,
            report.provider,
            report.model,
            exc,
        )
    return CostRow(
        org_id=owner.org_id,
        hotel_id=owner.hotel_id,
        run_id=report.run_id,
        segment_id=report.segment_id,
        provider=report.provider,
        model=report.model,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cache_read_tokens=report.cache_read_tokens,
        cache_write_tokens=report.cache_write_tokens,
        web_search_requests=report.web_search_requests,
        pricing_status=PricingStatus.PRICED if cost is not None else PricingStatus.UNPRICED,
        unpriced_reason=reason,
        cost_usd=cost,
        sdk_reported_cost_usd=report.sdk_reported_cost_usd,
        price_table_version=version,
        recorded_at=recorded_at,
    )


@runtime_checkable
class CostLedger(Protocol):
    """Records usage once per key and answers spend within one tenant's scope.

    Reads take the caller's TenantContext. Rows of another tenant are invisible: they add
    nothing to a total and are not returned, the same as the RLS policy on run_costs.
    Days are UTC calendar days of `recorded_at`.
    """

    def record(self, report: UsageReport, *, caps: SpendCaps | None = None) -> RecordResult:
        """Store the row once, then check the caps with it included, in one atomic step."""
        ...

    def run_spend(self, tenant: TenantContext, run_id: UUID) -> Spend: ...

    def hotel_spend(self, tenant: TenantContext, *, day: date | None = None) -> Spend: ...

    def org_spend(self, tenant: TenantContext, *, day: date | None = None) -> Spend: ...

    def run_rows(self, tenant: TenantContext, run_id: UUID) -> list[CostRow]: ...

    def unpriced_models(self, tenant: TenantContext) -> list[UnpricedModel]:
        """Exact model ids the hotel ran unpriced, by (provider, model, reason)."""
        ...


def _same_usage(row: CostRow, report: UsageReport) -> bool:
    return (
        row.input_tokens == report.input_tokens
        and row.output_tokens == report.output_tokens
        and row.cache_read_tokens == report.cache_read_tokens
        and row.cache_write_tokens == report.cache_write_tokens
        and row.web_search_requests == report.web_search_requests
        and row.sdk_reported_cost_usd == report.sdk_reported_cost_usd
    )


def _spend(rows: Iterable[CostRow]) -> Spend:
    priced = Decimal(0)
    unpriced = 0
    for row in rows:
        if row.cost_usd is None:
            unpriced += 1
        else:
            priced += row.cost_usd
    return Spend(priced_usd=quantize_usd(priced), unpriced_rows=unpriced)


def _on_day(row: CostRow, day: date | None) -> bool:
    return day is None or row.recorded_at.astimezone(UTC).date() == day


class InMemoryCostLedger:
    """A thread-safe CostLedger held in process memory. Not durable."""

    def __init__(
        self,
        runs: RunTenantLookup,
        *,
        prices: PriceBook = PRICE_BOOK,
        clock: Callable[[], datetime] = utc_now,
        on_unpriced: Callable[[UnpricedUsageAlert], None] | None = None,
    ) -> None:
        self._runs = runs
        self._prices = prices
        self._clock = clock
        self._on_unpriced = on_unpriced
        self._rows: dict[UsageKey, CostRow] = {}
        self._lock = threading.Lock()

    def record(self, report: UsageReport, *, caps: SpendCaps | None = None) -> RecordResult:
        """Store the report's row once. A repeat of the same key is a no-op (DUPLICATE).

        The insert, the run and hotel-day totals, and the cap decisions happen under one lock,
        so concurrent reports each see a distinct, complete total.
        """
        owner = self._runs.tenant_of(report.run_id)
        key = idempotency_key(report)
        now = self._clock()
        day = utc_day(now)
        with self._lock:
            existing = self._rows.get(key)
            if existing is None:
                row = build_cost_row(report, owner, recorded_at=now, prices=self._prices)
                self._rows[key] = row
                status = RecordStatus.RECORDED
            else:
                row = existing
                status = RecordStatus.DUPLICATE
            run_spend = _spend(r for r in self._rows.values() if r.run_id == report.run_id)
            hotel_day_spend = _spend(
                r
                for r in self._rows.values()
                if r.org_id == owner.org_id and r.hotel_id == owner.hotel_id and _on_day(r, day)
            )
        if status is RecordStatus.RECORDED:
            if row.cost_usd is not None:
                check_sdk_cost(report, row.cost_usd)
            else:
                self._alert(row)
        elif not _same_usage(row, report):
            logger.warning(
                "usage report conflicts with the recorded row; kept the first: key=%s "
                "recorded=(%s, %s, %s, %s, %s) resent=(%s, %s, %s, %s, %s)",
                key,
                row.input_tokens,
                row.output_tokens,
                row.cache_read_tokens,
                row.cache_write_tokens,
                row.web_search_requests,
                report.input_tokens,
                report.output_tokens,
                report.cache_read_tokens,
                report.cache_write_tokens,
                report.web_search_requests,
            )
        caps = caps or SpendCaps()
        return RecordResult(
            status=status,
            row=row,
            run_spend=run_spend,
            hotel_day_spend=hotel_day_spend,
            run_cap=None if caps.run_usd is None else check_run_spend(caps.run_usd, run_spend),
            hotel_daily_cap=(
                None
                if caps.hotel_daily_usd is None
                else check_hotel_spend(caps.hotel_daily_usd, hotel_day_spend)
            ),
        )

    def _alert(self, row: CostRow) -> None:
        alert = UnpricedUsageAlert(
            org_id=row.org_id,
            hotel_id=row.hotel_id,
            run_id=row.run_id,
            segment_id=row.segment_id,
            provider=row.provider,
            model=row.model,
            reason=row.unpriced_reason or "",
            recorded_at=row.recorded_at,
        )
        logger.error(
            "unpriced_usage_alert: run stopped; model refused for this hotel until priced: "
            "reason=%s org_id=%s hotel_id=%s run_id=%s segment_id=%s provider=%s model=%s",
            alert.reason,
            alert.org_id,
            alert.hotel_id,
            alert.run_id,
            alert.segment_id,
            alert.provider,
            alert.model,
        )
        if self._on_unpriced is None:
            return
        try:
            self._on_unpriced(alert)
        except Exception:
            # The row is already stored and the run already stops; never lose either.
            logger.exception("unpriced usage alert hook failed: run_id=%s", alert.run_id)

    def _visible(self, tenant: TenantContext) -> list[CostRow]:
        with self._lock:
            return [
                row
                for row in self._rows.values()
                if row.org_id == tenant.org_id and row.hotel_id == tenant.hotel_id
            ]

    def run_rows(self, tenant: TenantContext, run_id: UUID) -> list[CostRow]:
        rows = [row for row in self._visible(tenant) if row.run_id == run_id]
        return sorted(rows, key=lambda r: (r.recorded_at, r.segment_id, r.provider, r.model))

    def unpriced_models(self, tenant: TenantContext) -> list[UnpricedModel]:
        found: dict[tuple[Provider, str, str], list[CostRow]] = {}
        for row in self._visible(tenant):
            if row.unpriced_reason is not None:
                found.setdefault((row.provider, row.model, row.unpriced_reason), []).append(row)
        return [
            UnpricedModel(
                provider=provider,
                model=model,
                reason=reason,
                rows=len(rows),
                last_recorded_at=max(r.recorded_at for r in rows),
            )
            for (provider, model, reason), rows in sorted(found.items())
        ]

    def run_spend(self, tenant: TenantContext, run_id: UUID) -> Spend:
        return _spend(self.run_rows(tenant, run_id))

    def hotel_spend(self, tenant: TenantContext, *, day: date | None = None) -> Spend:
        return _spend(row for row in self._visible(tenant) if _on_day(row, day))

    def org_spend(self, tenant: TenantContext, *, day: date | None = None) -> Spend:
        """Spend of every hotel in the caller's org. A control-plane aggregate for org caps."""
        with self._lock:
            rows = [row for row in self._rows.values() if row.org_id == tenant.org_id]
        return _spend(row for row in rows if _on_day(row, day))
