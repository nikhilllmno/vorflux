"""CostLedger contract, exercised on the in-memory store.

A Postgres store must pass the same tests later (docs/costs.md, "Ledger").
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from harness.costs import (
    PRICE_TABLE,
    CostLedger,
    InMemoryCostLedger,
    ModelPrice,
    PriceBook,
    PriceTable,
    PricingStatus,
    Provider,
    RecordStatus,
    RunTenant,
    SpendCaps,
    TokenPrices,
    UnknownRunError,
    UnpricedUsageAlert,
    UsageReport,
    check_run_models,
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
    assert ledger.record(report(run)).status is RecordStatus.RECORDED
    assert ledger.run_spend(tenant(ORG_A, HOTEL_A1), run).priced_usd == Decimal("3")


def test_duplicate_report_is_a_no_op(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    ledger.record(report(run))
    assert ledger.record(report(run)).status is RecordStatus.DUPLICATE
    assert len(ledger.run_rows(tenant(ORG_A, HOTEL_A1), run)) == 1
    assert ledger.run_spend(tenant(ORG_A, HOTEL_A1), run).priced_usd == Decimal("3")


def test_resume_that_resends_a_segment_does_not_double_bill(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    ledger.record(report(run, segment_id="seg-1"))
    ledger.record(report(run, segment_id="seg-2", input_tokens=2_000_000))
    # The run pauses for an approval, then resumes and replays its last segment report.
    clock.now += timedelta(hours=1)
    assert ledger.record(report(run, segment_id="seg-2", input_tokens=2_000_000)).status is (
        RecordStatus.DUPLICATE
    )
    ledger.record(report(run, segment_id="seg-3", input_tokens=0, output_tokens=1_000_000))
    assert ledger.run_spend(t, run).priced_usd == Decimal("3") + Decimal("6") + Decimal("15")


def test_conflicting_resend_keeps_the_first_row_and_warns(
    ledger: InMemoryCostLedger,
    runs: StaticRuns,
    report: ReportFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    ledger.record(report(run, input_tokens=1_000_000))
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        status = ledger.record(report(run, input_tokens=5_000_000)).status
    assert status is RecordStatus.DUPLICATE
    assert ledger.run_spend(tenant(ORG_A, HOTEL_A1), run).priced_usd == Decimal("3")
    assert any("conflict" in r.getMessage() for r in caplog.records)


def test_mixed_model_segment_records_one_row_per_model(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    main = report(run, model="claude-sonnet-4-6", input_tokens=1_000_000)  # 3 USD
    helper = report(run, model="claude-haiku-4-5", input_tokens=1_000_000)  # 1 USD
    assert ledger.record(main).status is RecordStatus.RECORDED
    assert ledger.record(helper).status is RecordStatus.RECORDED
    rows = ledger.run_rows(t, run)
    assert sorted(r.model for r in rows) == ["claude-haiku-4-5", "claude-sonnet-4-6"]
    assert {r.segment_id for r in rows} == {"seg-1"}
    assert ledger.run_spend(t, run).priced_usd == Decimal("4")


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
    assert row.model == "claude-sonnet-4-5-20250929"  # the exact id received
    assert row.pricing_status is PricingStatus.PRICED
    assert row.unpriced_reason is None
    assert row.web_search_requests == 0
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


def test_two_ids_for_one_model_in_one_segment_are_both_billed(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    # Review F1: the Claude CLI can key model_usage by claude-haiku-4-5 and by
    # claude-haiku-4-5-20251001 in one segment. Both are real usage.
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    short = report(run, model="claude-haiku-4-5", input_tokens=1_000, output_tokens=100)
    dated = report(run, model="claude-haiku-4-5-20251001", input_tokens=5_000, output_tokens=700)
    assert ledger.record(short).status is RecordStatus.RECORDED
    assert ledger.record(dated).status is RecordStatus.RECORDED
    assert sorted(r.model for r in ledger.run_rows(t, run)) == [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ]
    # 6000 input x 1 + 800 output x 5 USD per MTok
    assert ledger.run_spend(t, run).priced_usd == Decimal("0.01")
    assert ledger.record(dated).status is RecordStatus.DUPLICATE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"model": "claude-imaginary-9"}, "unknown_model"),
        ({"provider": "anthropic", "model": "gpt-5"}, "unknown_model"),
        ({"model": "claude-haiku-5-5"}, "tiered_pricing"),
        ({"model": "anthropic.claude-sonnet-4-5-20250929-v1:0"}, "non_first_party"),
        ({"model": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}, "non_first_party"),
        ({"model": "claude-sonnet-4-5@20250929"}, "non_first_party"),
        ({"provider": "openai", "model": "openai/gpt-5"}, "non_first_party"),
        ({"web_search_requests": 3}, "web_search"),
        ({"model": "claude-fable-5-1", "output_tokens": 10**16}, "cost_out_of_range"),
    ],
)
def test_unpriced_usage_keeps_its_tokens_and_stops_the_run(
    ledger: InMemoryCostLedger,
    runs: StaticRuns,
    report: ReportFactory,
    overrides: dict[str, Any],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Review F2/F5: tokens that were spent are never dropped, and the caps fail closed.
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    usage = report(run, **overrides)
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        result = ledger.record(usage)
    assert result.status is RecordStatus.RECORDED
    assert result.stop_run
    [row] = ledger.run_rows(t, run)
    assert row.pricing_status is PricingStatus.UNPRICED
    assert row.unpriced_reason == reason
    assert row.cost_usd is None
    assert row.model == usage.model
    assert (row.input_tokens, row.output_tokens) == (usage.input_tokens, usage.output_tokens)
    assert ledger.run_spend(t, run).unpriced_rows == 1
    assert ledger.hotel_spend(t).unpriced_rows == 1
    assert any(reason in r.getMessage() for r in caplog.records)
    assert [a.reason for a in ledger.unpriced_models(t)] == [reason]
    # A resend of the same report is still a duplicate, and still says stop.
    again = ledger.record(usage)
    assert again.status is RecordStatus.DUPLICATE
    assert again.stop_run


def test_priced_report_without_caps_does_not_stop_the_run(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    result = ledger.record(report(runs.add(ORG_A, HOTEL_A1)))
    assert not result.stop_run
    assert result.run_cap is None
    assert result.hotel_daily_cap is None
    assert result.row.cost_usd == Decimal("3")


def test_record_checks_both_caps_with_the_new_row_included(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    caps = SpendCaps(run_usd=Decimal("100"), hotel_daily_usd=Decimal("10"))
    first, second = runs.add(ORG_A, HOTEL_A1), runs.add(ORG_A, HOTEL_A1)
    r1 = ledger.record(report(first, input_tokens=2_000_000), caps=caps)  # 6 USD
    assert r1.run_cap is not None and r1.run_cap.allowed
    assert r1.hotel_daily_cap is not None and r1.hotel_daily_cap.allowed
    assert not r1.stop_run
    # Another run of the same hotel reaches the daily cap: that report says stop ...
    r2 = ledger.record(report(second, input_tokens=2_000_000), caps=caps)  # 12 USD for the day
    assert r2.hotel_daily_cap is not None and not r2.hotel_daily_cap.allowed
    assert r2.stop_run
    # ... and so does the next report of the first run.
    r3 = ledger.record(report(first, segment_id="seg-2", input_tokens=1), caps=caps)
    assert r3.stop_run


def test_concurrent_records_see_every_total_exactly_once(
    runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    # Review F7: record and the cap check are one atomic step, so 20 concurrent reports of
    # 1 USD each see the hotel day totals 1..20, each exactly once.
    ledger = InMemoryCostLedger(runs, clock=clock)
    caps = SpendCaps(hotel_daily_usd=Decimal("10"))
    run_ids = [runs.add(ORG_A, HOTEL_A1) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(
                lambda run_id: ledger.record(
                    report(run_id, model="claude-haiku-4-5", input_tokens=1_000_000), caps=caps
                ),
                run_ids,
            )
        )
    seen = sorted(r.hotel_daily_cap.spent_usd for r in results if r.hotel_daily_cap)
    assert seen == [Decimal(n) for n in range(1, 21)]
    assert sum(not r.stop_run for r in results) == 9  # spent 1..9 is below the cap of 10


def _table(version: str, effective: date, input_price: str, *models: str) -> PriceTable:
    p = Decimal(input_price)
    return PriceTable(
        version=version,
        effective_from=effective,
        source_urls={},
        entries=[
            ModelPrice(
                Provider.ANTHROPIC,
                model,
                TokenPrices(input=p, output=p, cache_read=p, cache_write=p),
            )
            for model in models
        ],
    )


def test_price_table_is_chosen_by_effective_date_at_record_time(
    runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    # Review F8: the ledger uses its own price book, never the global table.
    book = PriceBook(
        [
            _table("v1", date(2026, 11, 1), "3", "claude-sonnet-4-6"),
            _table("v2", date(2026, 12, 1), "2", "claude-sonnet-4-6", "claude-new-6"),
        ]
    )
    ledger = InMemoryCostLedger(runs, prices=book, clock=clock)
    run = runs.add(ORG_A, HOTEL_A1)
    t = tenant(ORG_A, HOTEL_A1)
    clock.now = datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)
    ledger.record(report(run, segment_id="old"))
    early = ledger.record(report(run, segment_id="early-new", model="claude-new-6"))
    assert early.row.unpriced_reason == "unknown_model"  # not in v1 yet
    clock.now = datetime(2026, 12, 1, tzinfo=UTC)
    ledger.record(report(run, segment_id="new"))
    ledger.record(report(run, segment_id="late-new", model="claude-new-6"))
    rows = {r.segment_id: r for r in ledger.run_rows(t, run)}
    assert (rows["old"].price_table_version, rows["old"].cost_usd) == ("v1", Decimal("3"))
    assert (rows["new"].price_table_version, rows["new"].cost_usd) == ("v2", Decimal("2"))
    assert rows["late-new"].cost_usd == Decimal("2")


def test_usage_before_the_first_price_table_is_unpriced(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    clock.now = datetime(2026, 11, 19, 23, 0, tzinfo=UTC)  # PRICE_TABLE starts 2026-11-20
    result = ledger.record(report(runs.add(ORG_A, HOTEL_A1)))
    assert result.row.unpriced_reason == "no_price_table"
    assert result.row.price_table_version is None
    assert result.stop_run


def test_price_book_rejects_two_tables_for_one_date() -> None:
    with pytest.raises(ValueError, match="effective"):
        PriceBook(
            [
                _table("a", date(2026, 1, 1), "1", "claude-sonnet-4-6"),
                _table("b", date(2026, 1, 1), "1", "claude-sonnet-4-6"),
            ]
        )


def test_totals_are_scoped_to_the_callers_tenant(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    a1 = runs.add(ORG_A, HOTEL_A1)
    a2 = runs.add(ORG_A, HOTEL_A2)
    b1 = runs.add(ORG_B, HOTEL_B1)
    ledger.record(report(a1))  # 3
    ledger.record(report(a2, input_tokens=2_000_000))  # 6
    ledger.record(report(b1, input_tokens=4_000_000))  # 12

    assert ledger.hotel_spend(tenant(ORG_A, HOTEL_A1)).priced_usd == Decimal("3")
    assert ledger.hotel_spend(tenant(ORG_A, HOTEL_A2)).priced_usd == Decimal("6")
    assert ledger.org_spend(tenant(ORG_A, HOTEL_A1)).priced_usd == Decimal("9")
    assert ledger.org_spend(tenant(ORG_B, HOTEL_B1)).priced_usd == Decimal("12")
    # Another hotel's run reads as empty, not as that hotel's spend.
    assert ledger.run_spend(tenant(ORG_A, HOTEL_A1), a2).priced_usd == Decimal("0")
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

    assert ledger.hotel_spend(t, day=date(2026, 11, 20)).priced_usd == Decimal("3")
    assert ledger.hotel_spend(t, day=date(2026, 11, 21)).priced_usd == Decimal("6")
    assert ledger.org_spend(t, day=date(2026, 11, 21)).priced_usd == Decimal("6")
    assert ledger.hotel_spend(t).priced_usd == Decimal("9")


def test_empty_totals_are_zero(ledger: InMemoryCostLedger) -> None:
    t = tenant(ORG_A, HOTEL_A1)
    assert ledger.run_spend(t, uuid4()).priced_usd == Decimal("0")
    assert ledger.hotel_spend(t).priced_usd == Decimal("0")
    assert ledger.org_spend(t).priced_usd == Decimal("0")


def test_clock_must_be_timezone_aware(runs: StaticRuns, report: ReportFactory) -> None:
    run = runs.add(ORG_A, HOTEL_A1)
    naive = InMemoryCostLedger(runs, clock=lambda: datetime(2026, 11, 20, 10, 0))
    with pytest.raises(ValueError, match="timezone"):
        naive.record(report(run))


def test_unpriced_row_stops_only_its_own_run(
    ledger: InMemoryCostLedger, runs: StaticRuns, report: ReportFactory
) -> None:
    # Re-review N1 (C11 option a): other runs of the hotel, including runs in flight, go on.
    caps = SpendCaps(run_usd=Decimal("100"), hotel_daily_usd=Decimal("10"))
    bad, in_flight = runs.add(ORG_A, HOTEL_A1), runs.add(ORG_A, HOTEL_A1)
    ledger.record(report(in_flight, input_tokens=1_000_000), caps=caps)  # 3 USD
    stopped = ledger.record(report(bad, model="claude-imaginary-9"), caps=caps)
    assert stopped.stop_run
    assert stopped.run_cap is not None and not stopped.run_cap.allowed
    assert stopped.hotel_daily_cap is not None and stopped.hotel_daily_cap.allowed
    going = ledger.record(report(in_flight, segment_id="seg-2"), caps=caps)  # 6 USD
    assert not going.stop_run
    assert going.hotel_daily_cap is not None
    assert going.hotel_daily_cap.spent_usd == Decimal("6")
    assert going.hotel_daily_cap.allowed
    # The daily cap still stops runs on priced spend.
    over = ledger.record(report(in_flight, segment_id="seg-3", input_tokens=2_000_000), caps=caps)
    assert over.stop_run


def test_unpriced_row_raises_one_alert(
    runs: StaticRuns, report: ReportFactory, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    alerts: list[UnpricedUsageAlert] = []
    ledger = InMemoryCostLedger(runs, clock=clock, on_unpriced=alerts.append)
    run = runs.add(ORG_A, HOTEL_A1)
    usage = report(run, model="claude-imaginary-9")
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        ledger.record(usage)
        ledger.record(usage)  # a duplicate does not alert again
        ledger.record(report(run, segment_id="seg-2"))  # priced: no alert
    assert alerts == [
        UnpricedUsageAlert(
            org_id=ORG_A,
            hotel_id=HOTEL_A1,
            run_id=run,
            segment_id="seg-1",
            provider=Provider.ANTHROPIC,
            model="claude-imaginary-9",
            reason="unknown_model",
            recorded_at=clock.now,
        )
    ]
    events = [r for r in caplog.records if "unpriced_usage_alert" in r.getMessage()]
    assert len(events) == 1
    assert events[0].levelno == logging.ERROR


def test_a_failing_alert_hook_does_not_lose_the_row(
    runs: StaticRuns, report: ReportFactory, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(alert: UnpricedUsageAlert) -> None:
        raise RuntimeError("notifier down")

    ledger = InMemoryCostLedger(runs, clock=clock, on_unpriced=broken)
    run = runs.add(ORG_A, HOTEL_A1)
    with caplog.at_level(logging.ERROR, logger="harness.costs"):
        result = ledger.record(report(run, model="claude-imaginary-9"))
    assert result.status is RecordStatus.RECORDED
    assert result.stop_run
    assert len(ledger.run_rows(tenant(ORG_A, HOTEL_A1), run)) == 1
    assert any("alert hook failed" in r.getMessage() for r in caplog.records)


def test_pre_run_check_refuses_an_unpriced_model_until_the_table_covers_it(
    runs: StaticRuns, report: ReportFactory, clock: Clock
) -> None:
    # Re-review N1: the exact model id is refused for the hotel; other models continue; the
    # refusal clears by itself when a price table that prices the id takes effect.
    book = PriceBook(
        [
            _table("v1", date(2026, 11, 1), "3", "claude-sonnet-4-6"),
            _table("v2", date(2026, 12, 1), "2", "claude-sonnet-4-6", "claude-new-6"),
        ]
    )
    ledger = InMemoryCostLedger(runs, prices=book, clock=clock)
    t = tenant(ORG_A, HOTEL_A1)
    clock.now = datetime(2026, 11, 30, 12, tzinfo=UTC)
    ledger.record(report(runs.add(ORG_A, HOTEL_A1), model="claude-new-6"))
    ledger.record(report(runs.add(ORG_A, HOTEL_A1), segment_id="ws", web_search_requests=2))
    new = (Provider.ANTHROPIC, "claude-new-6")
    known = (Provider.ANTHROPIC, "claude-sonnet-4-6")

    check = check_run_models(ledger, t, [new, known], now=clock.now, prices=book)
    assert not check.allowed
    [refused] = check.refused
    assert (refused.provider, refused.model, refused.reason) == (*new, "unknown_model")
    assert refused.seen_unpriced  # this hotel already ran it unpriced
    # The model of the web search row is priced, so it is not refused: only that run stopped.
    assert check_run_models(ledger, t, [known], now=clock.now, prices=book).allowed
    # Another hotel never ran the id, but the table still cannot price it.
    other = check_run_models(ledger, tenant(ORG_A, HOTEL_A2), [new], now=clock.now, prices=book)
    assert not other.allowed
    assert not other.refused[0].seen_unpriced
    # v2 prices the id from 2026-12-01 UTC: the refusal clears with no manual step.
    assert check_run_models(
        ledger, t, [new, known], now=datetime(2026, 12, 1, tzinfo=UTC), prices=book
    ).allowed
