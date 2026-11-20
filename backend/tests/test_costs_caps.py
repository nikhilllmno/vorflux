"""Basic hard spend caps (architecture 5.5 and 12, M1): deny at or over the cap."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from harness.costs import (
    UNPRICED_LIMIT,
    UNPRICED_LIMIT_REASONS,
    UNPRICED_REASONS,
    UNPRICED_RUNS_HOTEL_DAILY_LIMIT,
    USAGE_UNPRICED,
    CapDecision,
    InMemoryCostLedger,
    Provider,
    RunTenant,
    SpendCaps,
    UnknownRunError,
    UnpricedLimitDecision,
    UsageReport,
    check_hotel_daily_cap,
    check_hotel_unpriced_limit,
    check_run_cap,
    check_spend_cap,
    price_usage,
)
from harness.tenancy import TenantContext

ORG = UUID("0000000a-0000-4000-8000-000000000000")
HOTEL = UUID("000000a1-0000-4000-8000-000000000000")
OTHER_HOTEL = UUID("000000a2-0000-4000-8000-000000000000")


@pytest.mark.parametrize(
    ("cap", "spent", "allowed"),
    [
        ("10", "0", True),
        ("10", "9.999999999", True),
        ("10", "10", False),  # at the cap: deny
        ("10", "10.000000001", False),
        ("0", "0", False),  # a zero cap allows nothing
    ],
)
def test_deny_at_or_over_the_cap(cap: str, spent: str, allowed: bool) -> None:
    decision = check_spend_cap(Decimal(cap), Decimal(spent))
    assert isinstance(decision, CapDecision)
    assert decision.allowed is allowed


def test_decision_reports_remaining_budget_and_reason() -> None:
    allow = check_spend_cap(Decimal("10"), Decimal("2.5"))
    assert allow.remaining_usd == Decimal("7.5")
    deny = check_spend_cap(Decimal("10"), Decimal("12"))
    assert deny.remaining_usd == Decimal("0")
    assert "10" in deny.reason and "12" in deny.reason


@pytest.mark.parametrize(("cap", "spent"), [("-1", "0"), ("1", "-1")])
def test_negative_amounts_are_invalid(cap: str, spent: str) -> None:
    with pytest.raises(ValueError, match="negative"):
        check_spend_cap(Decimal(cap), Decimal(spent))


def test_float_amounts_are_refused() -> None:
    with pytest.raises(TypeError):
        check_spend_cap(10.0, Decimal("1"))  # type: ignore[arg-type]


def test_non_finite_amounts_are_invalid() -> None:
    with pytest.raises(ValueError, match="finite"):
        check_spend_cap(Decimal("Infinity"), Decimal("0"))


# With the ledger ------------------------------------------------------------------------------


class Runs:
    def __init__(self) -> None:
        self.runs: dict[UUID, RunTenant] = {}

    def add(self, hotel_id: UUID = HOTEL) -> UUID:
        run_id = uuid4()
        self.runs[run_id] = RunTenant(org_id=ORG, hotel_id=hotel_id)
        return run_id

    def tenant_of(self, run_id: UUID) -> RunTenant:
        try:
            return self.runs[run_id]
        except KeyError:
            raise UnknownRunError(run_id) from None


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _usage(run_id: UUID, segment_id: str, usd: int) -> UsageReport:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "segment_id": segment_id,
        "provider": "openai",
        "model": "gpt-5",  # 10 USD per MTok output
        "input_tokens": 0,
        "output_tokens": usd * 100_000,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "serving": "first_party",
    }
    return UsageReport.model_validate(payload)


def _tenant(hotel_id: UUID = HOTEL) -> TenantContext:
    return TenantContext(org_id=ORG, hotel_id=hotel_id, actor_id=uuid4())


def test_run_cap_stops_the_run_after_the_report_that_reaches_it() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    run = runs.add()
    cap = Decimal("5")
    t = _tenant()

    assert check_run_cap(ledger, t, run, cap).allowed  # before segment 1
    ledger.record(_usage(run, "seg-1", 3))
    assert check_run_cap(ledger, t, run, cap).allowed  # after report: 3 < 5, continue
    assert check_run_cap(ledger, t, run, cap).allowed  # before segment 2
    ledger.record(_usage(run, "seg-2", 2))
    decision = check_run_cap(ledger, t, run, cap)  # after report: 5 >= 5, stop
    assert not decision.allowed
    assert decision.spent_usd == Decimal("5")


def test_duplicate_report_does_not_push_a_run_over_its_cap() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    run = runs.add()
    ledger.record(_usage(run, "seg-1", 3))
    ledger.record(_usage(run, "seg-1", 3))  # resume resend
    assert check_run_cap(ledger, _tenant(), run, Decimal("5")).allowed


def test_hotel_daily_cap_blocks_new_runs_until_the_next_utc_day() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    cap = Decimal("10")
    t = _tenant()

    first = runs.add()
    assert check_hotel_daily_cap(ledger, t, cap, now=clock()).allowed
    ledger.record(_usage(first, "seg-1", 10))

    # Same day: a new run for this hotel is blocked.
    clock.now = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    assert not check_hotel_daily_cap(ledger, t, cap, now=clock()).allowed
    # Another hotel in the same org has its own daily cap.
    assert check_hotel_daily_cap(ledger, _tenant(OTHER_HOTEL), cap, now=clock()).allowed

    # Next UTC day: allowed again.
    clock.now += timedelta(minutes=1)
    assert check_hotel_daily_cap(ledger, t, cap, now=clock()).allowed


def test_daily_cap_needs_an_aware_time() -> None:
    ledger = InMemoryCostLedger(Runs(), clock=Clock())
    with pytest.raises(ValueError, match="timezone"):
        check_hotel_daily_cap(ledger, _tenant(), Decimal("1"), now=datetime(2026, 11, 20))


def _unpriced(run_id: UUID) -> UsageReport:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "segment_id": "seg-x",
        "provider": "anthropic",
        "model": "haiku",  # a CLI alias, not a priced id
        "input_tokens": 10,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "serving": "first_party",
    }
    return UsageReport.model_validate(payload)


def test_unpriced_usage_denies_its_run_cap_but_not_the_hotel_daily_cap() -> None:
    # Re-review N1 (owner decision C11, option a): the run with unpriced usage stops; the
    # hotel daily cap counts priced spend only, so the hotel's other runs continue.
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    run, other = runs.add(), runs.add()
    ledger.record(_unpriced(run))
    ledger.record(_usage(other, "seg-1", 3))
    t = _tenant()
    run_cap = check_run_cap(ledger, t, run, Decimal("1000"))
    assert not run_cap.allowed
    assert run_cap.unpriced_rows == 1
    assert "unpriced" in run_cap.reason
    assert check_run_cap(ledger, t, other, Decimal("1000")).allowed
    daily_cap = check_hotel_daily_cap(ledger, t, Decimal("1000"), now=clock())
    assert daily_cap.allowed
    assert daily_cap.spent_usd == Decimal("3")
    assert check_hotel_daily_cap(ledger, t, Decimal("3"), now=clock()).allowed is False
    # The run itself stays stopped on later days.
    clock.now += timedelta(days=1)
    assert not check_run_cap(ledger, t, run, Decimal("1000")).allowed


def test_running_estimate_inside_a_segment_counts_toward_the_run_cap() -> None:
    # Review F4: the orchestrator prices usage seen so far in the open segment with
    # price_usage and passes it as pending_usd, so it can stop before the final report.
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    run = runs.add()
    ledger.record(_usage(run, "seg-1", 3))
    t = _tenant()
    in_flight = price_usage(
        Provider.OPENAI,
        "gpt-5",
        input_tokens=0,
        output_tokens=200_000,  # 2 USD
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert check_run_cap(ledger, t, run, Decimal("6"), pending_usd=in_flight).allowed
    decision = check_run_cap(ledger, t, run, Decimal("5"), pending_usd=in_flight)
    assert not decision.allowed
    assert decision.spent_usd == Decimal("5")
    assert not check_hotel_daily_cap(
        ledger, t, Decimal("5"), now=clock(), pending_usd=in_flight
    ).allowed


def test_pending_spend_must_be_a_non_negative_decimal() -> None:
    ledger = InMemoryCostLedger(Runs(), clock=Clock())
    with pytest.raises(ValueError, match="negative"):
        check_run_cap(ledger, _tenant(), uuid4(), Decimal("1"), pending_usd=Decimal("-1"))


# Decision C16: a hotel with repeated unpriced runs stops for the day --------------------------


def test_unpriced_run_limit_is_three() -> None:
    assert UNPRICED_RUNS_HOTEL_DAILY_LIMIT == 3


def test_hotel_with_too_many_unpriced_runs_is_denied_new_runs_until_the_next_utc_day() -> None:
    # Hermes_Like_Bot follow-up: an id the orchestrator did not pass to check_run_models is
    # admitted on every new run. After the limit, the hotel stops for the rest of the UTC day.
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    t, cap = _tenant(), Decimal("1000")
    first = runs.add()
    ledger.record(_unpriced(first))
    two_rows = UsageReport.model_validate({**_unpriced(first).model_dump(), "segment_id": "seg-y"})
    ledger.record(two_rows)  # a second unpriced row in the same run counts once
    ledger.record(_unpriced(runs.add()))
    assert ledger.hotel_spend(t, day=clock().date()).pricing_gap_runs == 2
    assert check_hotel_daily_cap(ledger, t, cap, now=clock()).allowed

    in_flight = runs.add()
    ledger.record(_usage(in_flight, "seg-1", 1))
    ledger.record(_unpriced(runs.add()))
    denied = check_hotel_daily_cap(ledger, t, cap, now=clock())
    assert not denied.allowed
    assert "unpriced" in denied.reason
    assert denied.unpriced_rows == 4
    # record() keeps judging the in-flight run on USD only; the orchestrator's next
    # pre-segment call to check_hotel_daily_cap stops it.
    result = ledger.record(_usage(in_flight, "seg-2", 1))
    assert result.hotel_daily_cap is None or result.hotel_daily_cap.allowed
    # Another hotel is not affected.
    assert check_hotel_daily_cap(ledger, _tenant(OTHER_HOTEL), cap, now=clock()).allowed
    # The next UTC day starts clean.
    tomorrow = clock() + timedelta(days=1)
    assert check_hotel_daily_cap(ledger, t, cap, now=tomorrow.replace(hour=0)).allowed


def test_record_hotel_daily_cap_ignores_the_unpriced_run_limit() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    for _ in range(UNPRICED_RUNS_HOTEL_DAILY_LIMIT):
        ledger.record(_unpriced(runs.add()))
    caps = SpendCaps(hotel_daily_usd=Decimal("1000"))
    result = ledger.record(_usage(runs.add(), "seg-1", 1), caps=caps)
    assert result.hotel_daily_cap is not None and result.hotel_daily_cap.allowed
    assert not result.stop_run


# Round 4: C16 counts only model-pricing gaps, works without a daily cap, has its own code -----


def _gap(run_id: UUID, segment_id: str = "seg-x", **overrides: Any) -> UsageReport:
    return UsageReport.model_validate(
        {**_unpriced(run_id).model_dump(), "segment_id": segment_id, **overrides}
    )


def test_c16_counts_only_unknown_model_and_tiered_pricing() -> None:
    assert UNPRICED_LIMIT_REASONS == frozenset({"unknown_model", "tiered_pricing"})
    assert UNPRICED_LIMIT_REASONS <= set(UNPRICED_REASONS)


def test_unpriced_limit_code_is_exported() -> None:
    assert UNPRICED_LIMIT == "UNPRICED_LIMIT"


def test_unpriced_limit_check_needs_no_cap() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    t = _tenant()
    allowed = check_hotel_unpriced_limit(ledger, t, now=clock())
    assert isinstance(allowed, UnpricedLimitDecision)
    assert allowed.allowed
    assert allowed.failure_code is None
    assert allowed.limit == UNPRICED_RUNS_HOTEL_DAILY_LIMIT
    for _ in range(UNPRICED_RUNS_HOTEL_DAILY_LIMIT):
        ledger.record(_unpriced(runs.add()))
    denied = check_hotel_unpriced_limit(ledger, t, now=clock())
    assert not denied.allowed
    assert denied.pricing_gap_runs == 3
    assert denied.failure_code == UNPRICED_LIMIT
    assert "C16" in denied.reason and "unpriced" in denied.reason
    tomorrow = (clock() + timedelta(days=1)).replace(hour=0)
    assert check_hotel_unpriced_limit(ledger, t, now=tomorrow).allowed


def test_hotel_daily_cap_of_none_applies_only_the_unpriced_limit() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    t = _tenant()
    ledger.record(_usage(runs.add(), "seg-1", 50))
    no_cap = check_hotel_daily_cap(ledger, t, None, now=clock())
    assert no_cap.allowed
    assert no_cap.cap_usd is None and no_cap.remaining_usd is None
    assert no_cap.spent_usd == Decimal("50")
    assert no_cap.failure_code is None
    for _ in range(UNPRICED_RUNS_HOTEL_DAILY_LIMIT):
        ledger.record(_unpriced(runs.add()))
    denied = check_hotel_daily_cap(ledger, t, None, now=clock())
    assert not denied.allowed
    assert denied.failure_code == UNPRICED_LIMIT
    assert "C16" in denied.reason


def test_c16_deny_has_its_own_code_and_a_usd_deny_has_none() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    t = _tenant()
    ledger.record(_usage(runs.add(), "seg-1", 5))
    usd = check_hotel_daily_cap(ledger, t, Decimal("5"), now=clock())
    assert not usd.allowed
    assert usd.failure_code is None  # the orchestrator maps a USD deny to BUDGET_EXHAUSTED
    for _ in range(UNPRICED_RUNS_HOTEL_DAILY_LIMIT):
        ledger.record(_unpriced(runs.add()))
    both = check_hotel_daily_cap(ledger, t, Decimal("5"), now=clock())
    assert not both.allowed
    assert both.failure_code == UNPRICED_LIMIT  # C16 is named first when both deny


def test_run_cap_deny_for_unpriced_usage_gives_usage_unpriced() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    run, other = runs.add(), runs.add()
    ledger.record(_unpriced(run))
    ledger.record(_usage(other, "seg-1", 5))
    assert check_run_cap(ledger, _tenant(), run, Decimal("1000")).failure_code == USAGE_UNPRICED
    assert check_run_cap(ledger, _tenant(), other, Decimal("5")).failure_code is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"model": "claude-haiku-4-5", "web_search_requests": 1}, "web_search"),
        ({"model": "claude-haiku-4-5", "serving": "bedrock"}, "non_first_party"),
        ({"model": "claude-haiku-4-5", "serving": "vertex"}, "non_first_party"),
    ],
)
def test_runs_stopped_for_other_reasons_do_not_block_the_hotel(
    overrides: dict[str, Any], reason: str
) -> None:
    # Owner decision (round 4): web_search, non_first_party, cost_out_of_range and
    # no_price_table each stop their own run already; C16 does not count them.
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    t = _tenant()
    stopped = [runs.add() for _ in range(UNPRICED_RUNS_HOTEL_DAILY_LIMIT)]
    for run in stopped:
        result = ledger.record(_gap(run, **overrides))
        assert result.row.unpriced_reason == reason
        assert result.stop_run
    spend = ledger.hotel_spend(t, day=clock().date())
    assert spend.unpriced_rows == 3
    assert spend.pricing_gap_runs == 0
    assert check_hotel_unpriced_limit(ledger, t, now=clock()).allowed
    assert check_hotel_daily_cap(ledger, t, Decimal("1000"), now=clock()).allowed
    assert check_hotel_daily_cap(ledger, t, None, now=clock()).allowed
    assert all(not check_run_cap(ledger, t, run, Decimal("1000")).allowed for run in stopped)


def test_three_tiered_pricing_runs_block_the_hotel() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    for _ in range(UNPRICED_RUNS_HOTEL_DAILY_LIMIT):
        row = ledger.record(_gap(runs.add(), model="claude-haiku-5-5")).row
        assert row.unpriced_reason == "tiered_pricing"
    assert not check_hotel_unpriced_limit(ledger, _tenant(), now=clock()).allowed


def test_only_gap_runs_count_toward_the_limit_in_a_mixed_day() -> None:
    runs, clock = Runs(), Clock()
    ledger = InMemoryCostLedger(runs, clock=clock)
    t = _tenant()
    ledger.record(_unpriced(runs.add()))
    ledger.record(_unpriced(runs.add()))
    ledger.record(_gap(runs.add(), model="claude-haiku-4-5", web_search_requests=2))
    # A run that has a web search row and a gap row counts once, as a gap run.
    mixed = runs.add()
    ledger.record(_gap(mixed, "seg-a", model="claude-haiku-4-5", serving="other"))
    assert check_hotel_unpriced_limit(ledger, t, now=clock()).allowed
    ledger.record(_gap(mixed, "seg-b"))
    decision = check_hotel_unpriced_limit(ledger, t, now=clock())
    assert decision.pricing_gap_runs == 3
    assert not decision.allowed
