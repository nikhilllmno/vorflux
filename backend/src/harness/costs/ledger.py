"""The cost ledger: one immutable row per (run, segment, provider, model).

`CostLedger` is the contract. `InMemoryCostLedger` implements it for tests and local runs. A
Postgres store over the proposed `run_costs` table (docs/costs.md) implements it later with
`INSERT ... ON CONFLICT (run_id, segment_id, provider, model) DO NOTHING` and reuses
`build_cost_row`, so both stores compute the same row.
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

from harness.costs.prices import PRICE_TABLE, PriceTable, Provider
from harness.costs.pricing import check_sdk_cost, cost_usd, quantize_usd
from harness.costs.usage import UsageKey, UsageReport, idempotency_key
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


@dataclass(frozen=True, slots=True)
class CostRow:
    """One ledger row. Field names match the proposed run_costs columns (docs/costs.md)."""

    org_id: UUID
    hotel_id: UUID
    run_id: UUID
    segment_id: str
    provider: Provider
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal
    sdk_reported_cost_usd: Decimal | None
    price_table_version: str
    recorded_at: datetime

    @property
    def key(self) -> UsageKey:
        return UsageKey(self.run_id, self.segment_id, self.provider, self.model)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"expected a timezone-aware datetime, got {moment!r}")
    return moment


def build_cost_row(
    report: UsageReport,
    owner: RunTenant,
    *,
    recorded_at: datetime,
    table: PriceTable = PRICE_TABLE,
) -> CostRow:
    """Price a report and turn it into a row. Every store must build rows with this."""
    return CostRow(
        org_id=owner.org_id,
        hotel_id=owner.hotel_id,
        run_id=report.run_id,
        segment_id=report.segment_id,
        provider=report.provider,
        model=table.get(report.provider, report.model).model,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cache_read_tokens=report.cache_read_tokens,
        cache_write_tokens=report.cache_write_tokens,
        cost_usd=cost_usd(report, table),
        sdk_reported_cost_usd=report.sdk_reported_cost_usd,
        price_table_version=table.version,
        recorded_at=_require_aware(recorded_at),
    )


@runtime_checkable
class CostLedger(Protocol):
    """Records usage once per key and answers spend totals within one tenant's scope.

    Reads take the caller's TenantContext. Rows of another tenant are invisible: they add
    nothing to a total and are not returned, the same as the RLS policy on run_costs.
    Days are UTC calendar days of `recorded_at`.
    """

    def record(self, report: UsageReport) -> RecordStatus: ...

    def run_total(self, tenant: TenantContext, run_id: UUID) -> Decimal: ...

    def hotel_total(self, tenant: TenantContext, *, day: date | None = None) -> Decimal: ...

    def org_total(self, tenant: TenantContext, *, day: date | None = None) -> Decimal: ...

    def run_rows(self, tenant: TenantContext, run_id: UUID) -> list[CostRow]: ...


def _same_usage(row: CostRow, report: UsageReport) -> bool:
    return (
        row.input_tokens == report.input_tokens
        and row.output_tokens == report.output_tokens
        and row.cache_read_tokens == report.cache_read_tokens
        and row.cache_write_tokens == report.cache_write_tokens
        and row.sdk_reported_cost_usd == report.sdk_reported_cost_usd
    )


def _sum(rows: Iterable[CostRow]) -> Decimal:
    return quantize_usd(sum((row.cost_usd for row in rows), Decimal(0)))


class InMemoryCostLedger:
    """A thread-safe CostLedger held in process memory. Not durable."""

    def __init__(
        self,
        runs: RunTenantLookup,
        *,
        table: PriceTable = PRICE_TABLE,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._runs = runs
        self._table = table
        self._clock = clock
        self._rows: dict[UsageKey, CostRow] = {}
        self._lock = threading.Lock()

    def record(self, report: UsageReport) -> RecordStatus:
        """Store the report's row once. A repeat of the same key is a no-op (DUPLICATE)."""
        owner = self._runs.tenant_of(report.run_id)
        key = idempotency_key(report, self._table)
        with self._lock:
            existing = self._rows.get(key)
            if existing is None:
                row = build_cost_row(report, owner, recorded_at=self._clock(), table=self._table)
                self._rows[key] = row
                status = RecordStatus.RECORDED
            else:
                row = existing
                status = RecordStatus.DUPLICATE
        if status is RecordStatus.RECORDED:
            check_sdk_cost(report, row.cost_usd)
        elif not _same_usage(row, report):
            logger.warning(
                "usage report conflicts with the recorded row; kept the first: key=%s "
                "recorded=(%s, %s, %s, %s) resent=(%s, %s, %s, %s)",
                key,
                row.input_tokens,
                row.output_tokens,
                row.cache_read_tokens,
                row.cache_write_tokens,
                report.input_tokens,
                report.output_tokens,
                report.cache_read_tokens,
                report.cache_write_tokens,
            )
        return status

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

    def run_total(self, tenant: TenantContext, run_id: UUID) -> Decimal:
        return _sum(self.run_rows(tenant, run_id))

    def hotel_total(self, tenant: TenantContext, *, day: date | None = None) -> Decimal:
        return _sum(row for row in self._visible(tenant) if _on_day(row, day))

    def org_total(self, tenant: TenantContext, *, day: date | None = None) -> Decimal:
        """Spend of every hotel in the caller's org. A control-plane aggregate for org caps."""
        with self._lock:
            rows = [row for row in self._rows.values() if row.org_id == tenant.org_id]
        return _sum(row for row in rows if _on_day(row, day))


def _on_day(row: CostRow, day: date | None) -> bool:
    return day is None or row.recorded_at.astimezone(UTC).date() == day
