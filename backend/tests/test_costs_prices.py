"""Price table contract: typed Decimal prices, a version, and a loud failure for unknown models."""

from datetime import date
from decimal import Decimal

import pytest

from harness.costs import (
    PRICE_TABLE,
    ModelPrice,
    PriceTable,
    Provider,
    TieredPricingError,
    TokenPrices,
    UnknownModelError,
)


def test_table_has_a_version_and_an_effective_date() -> None:
    assert PRICE_TABLE.version
    assert isinstance(PRICE_TABLE.effective_from, date)
    assert PRICE_TABLE.source_urls[Provider.ANTHROPIC].startswith("https://")
    assert PRICE_TABLE.source_urls[Provider.OPENAI].startswith("https://")


def test_table_prices_both_providers() -> None:
    providers = {entry.provider for entry in PRICE_TABLE.entries()}
    assert providers == {Provider.ANTHROPIC, Provider.OPENAI}


def test_every_price_is_a_non_negative_decimal() -> None:
    for entry in PRICE_TABLE.entries():
        for value in (
            entry.prices.input,
            entry.prices.output,
            entry.prices.cache_read,
            entry.prices.cache_write,
        ):
            assert isinstance(value, Decimal)
            assert value >= 0
        # A model that is free to call would be a data-entry error, not a price.
        assert entry.prices.input > 0
        assert entry.prices.output > 0


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        # Anthropic, https://platform.claude.com/docs/en/about-claude/pricing (2026-11-20)
        (Provider.ANTHROPIC, "claude-sonnet-4-6", ("3", "15", "0.30", "3.75")),
        (Provider.ANTHROPIC, "claude-opus-5-5", ("4", "20", "0.20", "5")),
        (Provider.ANTHROPIC, "claude-haiku-4-5", ("1", "5", "0.10", "1.25")),
        # OpenAI, https://developers.openai.com/api/docs/pricing (2026-11-20)
        (Provider.OPENAI, "gpt-5", ("1.25", "10", "0.125", "1.25")),
        (Provider.OPENAI, "gpt-5-mini", ("0.25", "2", "0.025", "0.25")),
    ],
)
def test_spot_prices_match_the_published_rates(
    provider: Provider, model: str, expected: tuple[str, str, str, str]
) -> None:
    prices = PRICE_TABLE.get(provider, model).prices
    got = (prices.input, prices.output, prices.cache_read, prices.cache_write)
    assert got == tuple(Decimal(v) for v in expected)


def test_dated_snapshot_id_resolves_to_the_same_price_as_its_alias() -> None:
    alias = PRICE_TABLE.get(Provider.ANTHROPIC, "claude-sonnet-4-5")
    snapshot = PRICE_TABLE.get(Provider.ANTHROPIC, "claude-sonnet-4-5-20250929")
    assert snapshot.prices == alias.prices
    assert snapshot.model == "claude-sonnet-4-5"


def test_unknown_model_raises_a_clear_error_and_is_never_priced_as_zero() -> None:
    with pytest.raises(UnknownModelError, match="claude-imaginary-9"):
        PRICE_TABLE.get(Provider.ANTHROPIC, "claude-imaginary-9")


def test_model_under_the_wrong_provider_is_unknown() -> None:
    with pytest.raises(UnknownModelError, match="openai"):
        PRICE_TABLE.get(Provider.ANTHROPIC, "gpt-5")


def test_model_ids_are_exact_matches() -> None:
    with pytest.raises(UnknownModelError):
        PRICE_TABLE.get(Provider.OPENAI, "GPT-5")
    with pytest.raises(UnknownModelError):
        PRICE_TABLE.get(Provider.OPENAI, " gpt-5")


@pytest.mark.parametrize(
    ("provider", "model"),
    [(Provider.ANTHROPIC, "claude-haiku-5-5"), (Provider.OPENAI, "gpt-6.2-sol")],
)
def test_models_with_prompt_length_tiers_are_refused(provider: Provider, model: str) -> None:
    # Their price depends on each request's prompt length, which a per-segment usage report
    # does not carry (docs/costs.md, decision C2).
    with pytest.raises(TieredPricingError, match="prompt-length"):
        PRICE_TABLE.get(provider, model)


def test_tiered_pricing_error_is_an_unknown_model_error() -> None:
    assert issubclass(TieredPricingError, UnknownModelError)


def test_is_priced_answers_without_raising() -> None:
    assert PRICE_TABLE.is_priced(Provider.OPENAI, "gpt-5")
    assert not PRICE_TABLE.is_priced(Provider.OPENAI, "gpt-6.2-sol")
    assert not PRICE_TABLE.is_priced(Provider.ANTHROPIC, "gpt-5")


def _entry(model: str, *, aliases: tuple[str, ...] = ()) -> ModelPrice:
    one = Decimal("1")
    return ModelPrice(
        provider=Provider.OPENAI,
        model=model,
        prices=TokenPrices(input=one, output=one, cache_read=one, cache_write=one),
        aliases=aliases,
    )


def test_table_rejects_a_duplicate_model_id() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        PriceTable(
            version="t",
            effective_from=date(2026, 1, 1),
            source_urls={},
            entries=[_entry("m"), _entry("n", aliases=("m",))],
        )


def test_token_prices_reject_floats_and_negatives() -> None:
    one = Decimal("1")
    with pytest.raises(TypeError):
        TokenPrices(input=1.5, output=one, cache_read=one, cache_write=one)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        TokenPrices(input=Decimal("-1"), output=one, cache_read=one, cache_write=one)
