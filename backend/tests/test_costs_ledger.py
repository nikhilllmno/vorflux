"""CostLedger contract, exercised on the in-memory store.

A Postgres store must pass the same tests later (docs/costs.md, "Ledger").
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harness.costs import (
    PRICE_TABLE,
    CostLedger,
    InMemoryCostLedger,
    RecordStatus,
    RunTenant,
    UnknownRunError,
    UsageReport,
)
from harness.tenancy import TenantContext

ORG_A = UUID("0000000a-0000-4000-8000-000000000000")
ORG_B = UUID("0000000b-0000-4000-8000-000000000000")
HOTEL_A1 = UUID("000000a1-0000-4000-8000-000000000000")
HOTEL_A2 = UUID("000000a2-0000-4000-8000-000000000000")
HOTEL_B1 = UUID("000000b1-0000-4000-8000-000000000000")


def tenant(org_id: UUID, hotel_id: UUID) -> TenantContext:
    return TenantContext(org_id=org_id, hotel_id=hotel_id, actor_id=uuid4())


class StaticRuns:
    """Run -> tenant lookup backed by a dict (the runs table in production)."""

    def __init__(self) -> None:
        self.runs: dict[UUID, RunTenant] = {}

    def add(self, org_id: UUID, hotel_id: UUID) -> UUID:
        run_id = uuid4()
        self.runs[run_id] = RunTenant(org_id=org_id, hotel_id=hotel_id)
        return run_id

    def tenant_of(self, run_id: UUID) -> RunTenant:
        try:
            return self.runs[run_id]
        except KeyError:
            raise UnknownRunError(run_id) from None


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def runs() -> StaticRuns:
    return StaticRuns()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(runs: StaticRuns, clock: Clock) -> InMemoryCostLedger:
    return InMemoryCostLedger(runs, clock=clock)


ReportFactory = Callable[..., UsageReport]


@pytest.fixture
def report() -> ReportFactory:
    def make(run_id: UUID, **overrides: Any) -> UsageReport:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "segment_id": "seg-1",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1_000_000,  # 3 USD
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        payload.update(overrides)
        return UsageReport.model_validate(payload)

    return make


def test_in_memory_ledger_implements_the_protocol(ledger: InMemoryCostLedger) -> None:
    assert isinstance(ledger, CostLedger)


def test_first_report_is_recorded(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    assert ledger.record(report(run)) is RecordStatus.RECORDED
    assert ledger.run_total(tenant(ORG_A, HOTEL_A1), run) == Decimal("3")


def test_duplicate_report_is_a_no_op(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    ledger.record(report(run))
    assert ledger.record(report(run)) is RecordStatus.DUPLICATE
    assert len(ledger.run_rows(tenant(ORG_A, HOTEL_A1), run)) == 1
    assert ledger.run_total(tenant(ORG_A, HOTEL_A1), run) == Decimal("3")


def test_resume_that_resends_a_segment_does_not_double_bill(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    ledger.record(report(run, segment_id="seg-1"))
    ledger.record(report(run, segment_id="seg-2", input_tokens=2_000_000))
    # The run pauses for an approval, then resumes and replays its last segment report.
    clock.now += timedelta(hours=1)
    assert ledger.record(report(run, segment_id="seg-2", input_tokens=2_000_000)) is (
        RecordStatus.DUPLICATE
    )
    ledger.record(report(run, segment_id="seg-3", input_tokens=0, output_tokens=1_000_000))
    assert ledger.run_total(t, run) == Decimal("3") + Decimal("6") + Decimal("15")


def test_conflicting_resend_keeps_the_first_row_and_warns(
    ledger: InMemoryCostLedger,
    runs: StaticRuns,
    report: ReportFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    ledger.record(report(run, input_tokens=1_000_000))
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        status = ledger.record(report(run, input_tokens=5_000_000))
    assert status is RecordStatus.DUPLICATE
    assert ledger.run_total(tenant(ORG_A, HOTEL_A1), run) == Decimal("3")
    assert any("conflict" in r.getMessage() for r in caplog.records)


def test_mixed_model_segment_records_one_row_per_model(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    main = report(run, model="claude-sonnet-4-6", input_tokens=1_000_000)  # 3 USD
    helper = report(run, model="claude-haiku-4-5", input_tokens=1_000_000)  # 1 USD
    assert ledger.record(main) is RecordStatus.RECORDED
    assert ledger.record(helper) is RecordStatus.RECORDED
    rows = ledger.run_rows(t, run)
    assert sorted(r.model for r in rows) == ["claude-haiku-4-5", "claude-sonnet-4-6"]
    assert {r.segment_id for r in rows} == {"seg-1"}
    assert ledger.run_total(t, run) == Decimal("4")


def test_row_carries_tenant_cost_tokens_and_table_version(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    ledger.record(
        report(
            run,
            model="claude-sonnet-4-5-20250929",
            output_tokens=10,
            cache_read_tokens=20,
            cache_write_tokens=30,
            sdk_reported_cost_usd="3.01",
        )
    )
    [row] = ledger.run_rows(tenant(ORG_A, HOTEL_A1), run)
    assert (row.org_id, row.hotel_id, row.run_id) == (ORG_A, HOTEL_A1, run)
    assert row.provider.value == "anthropic"
    assert row.model == "claude-sonnet-4-5"  # canonical id, so the unique key is stable
    assert (row.input_tokens, row.output_tokens) == (1_000_000, 10)
    assert (row.cache_read_tokens, row.cache_write_tokens) == (20, 30)
    assert row.sdk_reported_cost_usd == Decimal("3.01")
    assert row.price_table_version == PRICE_TABLE.version
    assert row.recorded_at == clock.now
    assert row.cost_usd == Decimal("3.000268500")


def test_report_for_an_unknown_run_is_refused(
    ledger: InMemoryCostLedger, report: ReportFactory
) -> None:
    with pytest.raises(UnknownRunError):
        ledger.record(report(uuid4()))


def test_unknown_model_never_reaches_the_ledger(runs: StaticRuns, report: ReportFactory) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    with pytest.raises(ValidationError, match="unknown model"):
        report(run, model="claude-imaginary-9")


def test_totals_are_scoped_to_the_callers_tenant(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    a1 = runs.add(ORG_A, HOTEL_A1)
    a2 = runs.add(ORG_A, HOTEL_A2)
    b1 = runs.add(ORG_B, HOTEL_B1)
    ledger.record(report(a1))  # 3
    ledger.record(report(a2, input_tokens=2_000_000))  # 6
    ledger.record(report(b1, input_tokens=4_000_000))  # 12

    assert ledger.hotel_total(tenant(ORG_A, HOTEL_A1)) == Decimal("3")
    assert ledger.hotel_total(tenant(ORG_A, HOTEL_A2)) == Decimal("6")
    assert ledger.org_total(tenant(ORG_A, HOTEL_A1)) == Decimal("9")
    assert ledger.org_total(tenant(ORG_B, HOTEL_B1)) == Decimal("12")
    # Another hotel's run reads as empty, not as that hotel's spend.
    assert ledger.run_total(tenant(ORG_A, HOTEL_A1), a2) == Decimal("0")
    assert ledger.run_rows(tenant(ORG_B, HOTEL_B1), a1) == []


def test_daily_totals_use_the_utc_day_of_recording(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    clock.now = datetime(2026, 11, 20, 23, 59, 59, tzinfo=UTC)
    ledger.record(report(run, segment_id="late"))
    clock.now = datetime(2026, 11, 21, 0, 0, 0, tzinfo=UTC)
    ledger.record(report(run, segment_id="early", input_tokens=2_000_000))

    assert ledger.hotel_total(t, day=date(2026, 11, 20)) == Decimal("3")
    assert ledger.hotel_total(t, day=date(2026, 11, 21)) == Decimal("6")
    assert ledger.org_total(t, day=date(2026, 11, 21)) == Decimal("6")
    assert ledger.hotel_total(t) == Decimal("9")


def test_empty_totals_are_zero(ledger: InMemoryCostLedger) -> None:
    t = tenant(ORG_A, HOTEL_A1)
    assert ledger.run_total(t, uuid4()) == Decimal("0")
    assert ledger.hotel_total(t) == Decimal("0")
    assert ledger.org_total(t) == Decimal("0")


def test_clock_must_be_timezone_aware(runs: StaticRuns, report: ReportFactory) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    naive = InMemoryCostLedger(runs, clock=lambda: datetime(2026, 11, 20, 10, 0))
    with pytest.raises(ValueError, match="timezone"):
        naive.record(report(run))
