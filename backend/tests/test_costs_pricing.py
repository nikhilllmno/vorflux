"""cost_usd: one price table for both providers, exact Decimal math, SDK cost as a cross-check."""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from harness.costs import (
    COST_QUANTUM,
    PRICE_TABLE,
    ModelPrice,
    NonFirstPartyModelError,
    NonFirstPartyServingError,
    NoPriceTableError,
    PriceBook,
    PriceTable,
    Provider,
    SdkCostCheck,
    TieredPricingError,
    TokenPrices,
    UnknownModelError,
    UsageReport,
    cache_write_1h_from_model_usage,
    check_priced,
    check_sdk_cost,
    cost_usd,
    non_first_party_kind,
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
        "serving": "first_party",
    }
    payload.update(overrides)
    return UsageReport.model_validate(payload)


def test_each_token_bucket_uses_its_own_price() -> None:
    # claude-sonnet-4-6: input 3, output 15, cache read 0.30, cache write 3.75 (5-minute TTL)
    # and 6 (1-hour TTL) USD per MTok.
    report = _report(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        cache_write_1h_tokens=0,
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
                prices=TokenPrices(
                    input=p, output=p, cache_read=p, cache_write=p, cache_write_1h=p
                ),
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
        "cache_write_1h_tokens": 1_000,
    }
    priced = price_usage(Provider.ANTHROPIC, "claude-sonnet-4-6", **counts)
    # 1000*3 + 2000*15 + 3000*0.30 + 3000*3.75 + 1000*6 = 51150 USD per MTok-token
    assert priced == cost_usd(_report(**counts)) == Decimal("0.05115")


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
        prices = TokenPrices(input=p, output=p, cache_read=p, cache_write=p, cache_write_1h=p)
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


# 1-hour cache writes (decision C1) ------------------------------------------------------------


def test_cache_writes_are_split_between_the_5_minute_and_1_hour_rates() -> None:
    # claude-sonnet-4-6: 600k at 3.75 + 400k at 6 = 2.25 + 2.40 USD.
    report = _report(cache_write_tokens=1_000_000, cache_write_1h_tokens=400_000)
    assert cost_usd(report) == Decimal("4.65")


def test_unknown_split_prices_every_anthropic_cache_write_at_the_1_hour_rate() -> None:
    # The safe side: over-price rather than under-price when the TTL is not known.
    assert cost_usd(_report(cache_write_tokens=1_000_000)) == Decimal("6")
    assert price_usage(
        Provider.ANTHROPIC,
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=1_000_000,
    ) == Decimal("6")


def test_openai_cache_writes_cost_the_input_price_with_or_without_a_split() -> None:
    # gpt-5: no separate cache-write charge, so both rates are the input price (1.25).
    unknown = _report(provider="openai", model="gpt-5", cache_write_tokens=1_000_000)
    split = _report(
        provider="openai", model="gpt-5", cache_write_tokens=1_000_000, cache_write_1h_tokens=1
    )
    assert cost_usd(unknown) == cost_usd(split) == Decimal("1.25")


def test_every_anthropic_1_hour_write_price_is_twice_the_input_price() -> None:
    for entry in PRICE_TABLE.entries():
        p = entry.prices
        if entry.provider is Provider.ANTHROPIC:
            assert p.cache_write_1h == 2 * p.input, entry.model
            assert p.cache_write == Decimal("1.25") * p.input, entry.model
        else:
            assert p.cache_write == p.cache_write_1h == p.input, entry.model


# Serving provider ------------------------------------------------------------------------------


@pytest.mark.parametrize("serving", ["bedrock", "vertex", "other"])
def test_only_first_party_serving_is_priced(serving: str) -> None:
    with pytest.raises(NonFirstPartyServingError) as caught:
        cost_usd(_report(serving=serving, input_tokens=1_000))
    assert caught.value.reason == "non_first_party"
    assert cost_usd(_report(serving="first_party", input_tokens=1_000_000)) == Decimal("3")


# Model id check (decision C13) ---------------------------------------------------------------

# The pins of runtime contract v2 (ClaudeModelPins): the small fast and haiku models default to
# claude-haiku-4-5; sonnet, opus and subagent models are priced ids from the plan.
PINNED_IDS = [
    (Provider.ANTHROPIC, "claude-haiku-4-5"),
    (Provider.ANTHROPIC, "claude-haiku-4-5-20251001"),
    *(
        (e.provider, model_id)
        for e in PRICE_TABLE.entries()
        if e.provider is Provider.OPENAI or "sonnet" in e.model or "opus" in e.model
        for model_id in (e.model, *e.aliases)
    ),
]
PRICED_IDS = [(e.provider, i) for e in PRICE_TABLE.entries() for i in (e.model, *e.aliases)]


@pytest.mark.parametrize(("provider", "model"), PINNED_IDS + PRICED_IDS)
def test_every_priced_and_pinned_id_passes_the_id_check(provider: Provider, model: str) -> None:
    assert non_first_party_kind(model) is None
    assert ":" not in model and "/" not in model
    assert check_priced(provider, model).provider is provider


@pytest.mark.parametrize(
    "model",
    [
        "ft:gpt-4o-mini-2024-07-18:acme::abc123",  # OpenAI fine-tune
        "openai/gpt-5",  # gateway
        "anthropic/claude-sonnet-4-6",  # gateway
        "vendor:claude-haiku-4-5",  # provider prefix (contract C31)
        "gpt-5:free",  # gateway variant suffix
        "anthropic.claude-sonnet-4-6-v1:0",  # Amazon Bedrock
        "claude-sonnet-4-5@20250929",  # Google Vertex AI
    ],
)
def test_non_first_party_id_forms_are_refused(model: str) -> None:
    assert non_first_party_kind(model) is not None
    with pytest.raises(NonFirstPartyModelError):
        check_priced(Provider.ANTHROPIC, model)


def test_a_price_table_refuses_an_id_that_the_id_check_would_flag() -> None:
    # If a priced id ever needs ':' or '/', the rule must change first (docs/costs.md, C13).
    one = Decimal(1)
    prices = TokenPrices(input=one, output=one, cache_read=one, cache_write=one, cache_write_1h=one)
    with pytest.raises(ValueError, match="id check"):
        PriceTable(
            version="t",
            effective_from=date(2026, 1, 1),
            source_urls={},
            entries=[ModelPrice(Provider.OPENAI, "gpt-x:1", prices)],
        )


# N4: the default table is the one in effect now -----------------------------------------------


def test_check_priced_and_price_usage_default_to_the_table_in_effect_now() -> None:
    def table(version: str, effective: date) -> PriceTable:
        p = Decimal(1)
        return PriceTable(
            version=version,
            effective_from=effective,
            source_urls={},
            entries=[
                ModelPrice(
                    Provider.OPENAI,
                    "gpt-5",
                    TokenPrices(input=p, output=p, cache_read=p, cache_write=p, cache_write_1h=p),
                )
            ],
        )

    counts = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    future = PriceBook([table("far", date(2999, 1, 1))])
    with pytest.raises(NoPriceTableError):
        check_priced(Provider.OPENAI, "gpt-5", prices=future)
    with pytest.raises(NoPriceTableError):
        price_usage(Provider.OPENAI, "gpt-5", prices=future, **counts)
    current = PriceBook([table("old", date(2020, 1, 1)), table("far", date(2999, 1, 1))])
    assert price_usage(Provider.OPENAI, "gpt-5", prices=current, **counts) == 1
    # Without `prices`, the default book's table in effect now (2026-11-20 table) is used.
    assert price_usage(Provider.OPENAI, "gpt-5", **counts) == Decimal("1.25")


# Claude model_usage split (Hermes_Like_Bot round 4, item 1) -----------------------------------


def _from_model_usage(entry: dict[str, Any], model: str) -> UsageReport:
    """The documented Claude mapping (docs/costs.md, "Adapter notes"), one segment from zero."""
    return _report(
        model=model,
        input_tokens=entry["inputTokens"],
        output_tokens=entry["outputTokens"],
        cache_read_tokens=entry["cacheReadInputTokens"],
        cache_write_tokens=entry["cacheCreationInputTokens"],
        cache_write_1h_tokens=cache_write_1h_from_model_usage(
            entry["cacheCreationInputTokens"],
            entry.get("cacheCreationEphemeral5mInputTokens"),
            entry.get("cacheCreationEphemeral1hInputTokens"),
        ),
        web_search_requests=entry["webSearchRequests"],
        sdk_reported_cost_usd=Decimal(str(entry["costUSD"])),
    )


# claude-haiku-4-5: input 1, output 5, cache read 0.10, 5-minute write 1.25, 1-hour write 2.
# The SDK's costUSD for this entry: (1000 + 10000 + 1000 + 750000 + 800000) / 1e6 = 1.562.
MODEL_USAGE_WITH_SPLIT: dict[str, Any] = {
    "inputTokens": 1000,
    "outputTokens": 2000,
    "cacheReadInputTokens": 10_000,
    "cacheCreationInputTokens": 1_000_000,
    "cacheCreationEphemeral5mInputTokens": 600_000,
    "cacheCreationEphemeral1hInputTokens": 400_000,
    "webSearchRequests": 0,
    "costUSD": 1.562,
}


def test_model_usage_split_prices_each_ttl_at_its_rate_and_matches_the_sdk_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _from_model_usage(MODEL_USAGE_WITH_SPLIT, "claude-haiku-4-5")
    assert report.cache_write_1h_tokens == 400_000
    ours = cost_usd(report, PRICE_TABLE)
    # 600k writes at 1.25x input and 400k at 2x input, not 1M at 2x.
    assert ours == Decimal("1.562")
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        assert check_sdk_cost(report, ours) is SdkCostCheck.MATCH  # within the C6 tolerance
    assert not caplog.records


def test_model_usage_split_matches_the_sdk_cost_on_sonnet() -> None:
    # claude-sonnet-4-6: input 3, output 15, cache read 0.30, 5-minute write 3.75, 1-hour 6.
    entry = {**MODEL_USAGE_WITH_SPLIT, "costUSD": 4.686}
    report = _from_model_usage(entry, "claude-sonnet-4-6")
    ours = cost_usd(report, PRICE_TABLE)
    assert ours == Decimal("4.686")
    assert check_sdk_cost(report, ours) is SdkCostCheck.MATCH


def test_model_usage_without_a_split_prices_every_write_at_2x() -> None:
    # An older CLI reports no split: every cache write is priced at the 1-hour rate (C1), so
    # the SDK cost (5-minute rate) differs and the cross-check flags it.
    entry = {
        k: v
        for k, v in MODEL_USAGE_WITH_SPLIT.items()
        if not k.startswith("cacheCreationEphemeral")
    }
    entry["costUSD"] = 1.262
    report = _from_model_usage(entry, "claude-haiku-4-5")
    assert report.cache_write_1h_tokens is None
    ours = cost_usd(report, PRICE_TABLE)
    assert ours == Decimal("2.012")
    assert check_sdk_cost(report, ours) is SdkCostCheck.MISMATCH


def test_model_usage_writes_with_no_reported_ttl_are_priced_at_2x() -> None:
    # 200k writes are in neither TTL field; the SDK prices them at 5 minutes, we at 1 hour.
    entry = {
        **MODEL_USAGE_WITH_SPLIT,
        "cacheCreationEphemeral5mInputTokens": 400_000,
        "cacheCreationEphemeral1hInputTokens": 400_000,
    }
    report = _from_model_usage(entry, "claude-haiku-4-5")
    assert report.cache_write_1h_tokens == 600_000
    assert cost_usd(report, PRICE_TABLE) == Decimal("1.712")
