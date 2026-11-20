"""cost_usd: one price table for both providers, exact Decimal math, SDK cost as a cross-check."""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from harness.costs import (
    COST_QUANTUM,
    ModelPrice,
    NoPriceTableError,
    PriceBook,
    PriceTable,
    Provider,
    SdkCostCheck,
    TieredPricingError,
    TokenPrices,
    UnknownModelError,
    UsageReport,
    check_priced,
    check_sdk_cost,
    cost_usd,
    price_usage,
)

RUN_ID = UUID("00000000-0000-4000-8000-000000000002")


def _report(**overrides: Any) -> UsageReport:
    payload: dict[str, Any] = {
        "run_id": RUN_ID,
        "segment_id": "seg-1",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    payload.update(overrides)
    return UsageReport.model_validate(payload)


def test_each_token_bucket_uses_its_own_price() -> None:
    # claude-sonnet-4-6: input 3, output 15, cache read 0.30, cache write 3.75 USD per MTok.
    report = _report(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost_usd(report) == Decimal("22.05")


def test_openai_is_priced_from_the_same_table() -> None:
    # gpt-5: input 1.25, output 10, cached input 0.125 USD per MTok.
    report = _report(
        provider="openai",
        model="gpt-5",
        input_tokens=2_000,
        output_tokens=500,
        cache_read_tokens=10_000,
    )
    # 2000*1.25 + 500*10 + 10000*0.125 = 2500 + 5000 + 1250 = 8750 USD per MTok-token
    assert cost_usd(report) == Decimal("0.00875")


def test_zero_usage_costs_zero() -> None:
    assert cost_usd(_report()) == Decimal("0")


def test_result_is_a_decimal_quantized_to_the_cost_quantum() -> None:
    cost = cost_usd(_report(input_tokens=1))
    assert isinstance(cost, Decimal)
    assert cost.as_tuple().exponent == COST_QUANTUM.as_tuple().exponent
    assert cost == Decimal("0.000003000")


def test_cheapest_single_token_is_exact_not_rounded_to_zero() -> None:
    # gpt-5-nano cached input is 0.005 USD per MTok: one token is 0.000000005 USD.
    report = _report(provider="openai", model="gpt-5-nano", cache_read_tokens=1)
    assert cost_usd(report) == Decimal("0.000000005")


def test_decimal_math_has_no_float_drift() -> None:
    # In float, 0.1 + 0.2 != 0.3. Ten one-token reports must sum to exactly ten times one.
    one = cost_usd(_report(provider="openai", model="gpt-5", input_tokens=1))
    ten = cost_usd(_report(provider="openai", model="gpt-5", input_tokens=10))
    assert sum([one] * 10, Decimal(0)) == ten == Decimal("0.0000125")


def _tiny_table(input_price: str) -> PriceTable:
    p = Decimal(input_price)
    return PriceTable(
        version="test",
        effective_from=date(2026, 1, 1),
        source_urls={},
        entries=[
            ModelPrice(
                provider=Provider.ANTHROPIC,
                model="claude-sonnet-4-6",
                prices=TokenPrices(input=p, output=p, cache_read=p, cache_write=p),
            )
        ],
    )


@pytest.mark.parametrize(
    ("price_per_mtok", "tokens", "expected"),
    [
        ("0.0005", 1, "0.000000001"),  # 0.0000000005 -> half rounds up
        ("0.0004", 1, "0.000000000"),  # 0.0000000004 -> rounds down
        ("0.0015", 1, "0.000000002"),  # 0.0000000015 -> half rounds up
        ("0.0025", 1, "0.000000003"),  # half-up, not banker's (half-even would give ...2)
    ],
)
def test_sub_quantum_amounts_round_half_up(price_per_mtok: str, tokens: int, expected: str) -> None:
    report = _report(input_tokens=tokens)
    assert cost_usd(report, table=_tiny_table(price_per_mtok)) == Decimal(expected)


def test_large_usage_keeps_full_precision() -> None:
    report = _report(input_tokens=987_654_321, output_tokens=123_456_789)
    # 987654321*3 + 123456789*15 = 2962962963 + 1851851835 = 4814814798 -> / 1e6
    assert cost_usd(report) == Decimal("4814.814798")


def test_cost_of_an_unpriced_model_raises_instead_of_zero() -> None:
    with pytest.raises(UnknownModelError, match="claude-imaginary-9"):
        cost_usd(_report(model="claude-imaginary-9", input_tokens=1))


# Pricing during a segment and before a run ---------------------------------------------------


def test_price_usage_prices_raw_counts_like_a_report() -> None:
    # The orchestrator prices running usage inside a segment to stop a run early (review F4).
    counts = {
        "input_tokens": 1_000,
        "output_tokens": 2_000,
        "cache_read_tokens": 3_000,
        "cache_write_tokens": 4_000,
    }
    priced = price_usage(Provider.ANTHROPIC, "claude-sonnet-4-6", **counts)
    assert priced == cost_usd(_report(**counts)) == Decimal("0.0489")


@pytest.mark.parametrize(
    ("bad", "error"), [(-1, ValueError), (1.5, TypeError), (True, TypeError), (2**63, ValueError)]
)
def test_price_usage_refuses_bad_counts(bad: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        price_usage(
            Provider.OPENAI,
            "gpt-5",
            input_tokens=bad,  # type: ignore[arg-type]
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )


def test_check_priced_lets_the_orchestrator_refuse_a_model_before_the_run() -> None:
    assert check_priced(Provider.ANTHROPIC, "claude-haiku-4-5-20251001").model == (
        "claude-haiku-4-5"
    )
    with pytest.raises(UnknownModelError, match="haiku"):
        check_priced(Provider.ANTHROPIC, "haiku")
    with pytest.raises(TieredPricingError):
        check_priced(Provider.OPENAI, "gpt-5.5")


# SDK cost cross-check -------------------------------------------------------------------------


def test_sdk_cost_within_tolerance_is_a_match(caplog: pytest.LogCaptureFixture) -> None:
    report = _report(input_tokens=1_000_000, sdk_reported_cost_usd="3.0001")
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        assert check_sdk_cost(report, cost_usd(report)) is SdkCostCheck.MATCH
    assert not caplog.records


def test_sdk_cost_mismatch_is_logged_but_never_billed(caplog: pytest.LogCaptureFixture) -> None:
    report = _report(input_tokens=1_000_000, sdk_reported_cost_usd="4.50")
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        assert check_sdk_cost(report, cost_usd(report)) is SdkCostCheck.MISMATCH
    # The billed value stays the price-table value.
    assert cost_usd(report) == Decimal("3")
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    for expected in (str(RUN_ID), "seg-1", "claude-sonnet-4-6", "4.50", "3.000000000"):
        assert expected in message


def test_missing_sdk_cost_is_not_checked(caplog: pytest.LogCaptureFixture) -> None:
    report = _report(input_tokens=10)
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        assert check_sdk_cost(report, cost_usd(report)) is SdkCostCheck.NOT_REPORTED
    assert not caplog.records


def test_tiny_absolute_differences_are_not_mismatches() -> None:
    # One token of drift on a tiny segment is below the absolute tolerance.
    report = _report(input_tokens=1, sdk_reported_cost_usd="0.000004")
    assert check_sdk_cost(report, cost_usd(report)) is SdkCostCheck.MATCH


def test_sdk_cost_of_zero_against_real_usage_is_a_mismatch() -> None:
    report = _report(input_tokens=1_000_000, sdk_reported_cost_usd="0")
    assert check_sdk_cost(report, cost_usd(report)) is SdkCostCheck.MISMATCH


def _book() -> PriceBook:
    def table(version: str, effective: date, price: str) -> PriceTable:
        p = Decimal(price)
        prices = TokenPrices(input=p, output=p, cache_read=p, cache_write=p)
        return PriceTable(
            version=version,
            effective_from=effective,
            source_urls={},
            entries=[ModelPrice(Provider.ANTHROPIC, "claude-sonnet-4-6", prices)],
        )

    return PriceBook([table("v1", date(2026, 11, 1), "3"), table("v2", date(2026, 12, 1), "2")])


def test_price_usage_picks_the_table_by_date_like_the_ledger() -> None:
    # Re-review N4: a running estimate and the final row use the same table.
    counts = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    book = _book()
    old = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)
    new = datetime(2026, 12, 1, tzinfo=UTC)
    assert price_usage(Provider.ANTHROPIC, "claude-sonnet-4-6", at=old, prices=book, **counts) == 3
    assert price_usage(Provider.ANTHROPIC, "claude-sonnet-4-6", at=new, prices=book, **counts) == 2
    with pytest.raises(NoPriceTableError):
        price_usage(
            Provider.ANTHROPIC,
            "claude-sonnet-4-6",
            at=datetime(2026, 10, 31, tzinfo=UTC),
            prices=book,
            **counts,
        )
    assert check_priced(Provider.ANTHROPIC, "claude-sonnet-4-6", at=new, prices=book).model == (
        "claude-sonnet-4-6"
    )
