"""Price table contract: typed Decimal prices, a version, and a loud failure for unknown models."""

import re
from dataclasses import fields
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from harness.costs import (
    PRICE_TABLE,
    UNPRICED_REASONS,
    CostRow,
    ModelPrice,
    NonFirstPartyModelError,
    NonFirstPartyServingError,
    PriceTable,
    Provider,
    Serving,
    TieredPricingError,
    TokenPrices,
    UnknownModelError,
    UnpricedUsageError,
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


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        (Provider.ANTHROPIC, "anthropic.claude-sonnet-4-5-20250929-v1:0"),  # Bedrock
        (Provider.ANTHROPIC, "us.anthropic.claude-haiku-4-5-20251001-v1:0"),  # Bedrock region
        (Provider.ANTHROPIC, "global.anthropic.claude-opus-4-5-20251101-v1:0"),
        (Provider.ANTHROPIC, "claude-sonnet-4-5@20250929"),  # Vertex AI
        (Provider.OPENAI, "openai/gpt-5"),  # a gateway route
        (Provider.OPENAI, "ft:gpt-4o-mini:acme::abc123"),  # fine-tuned
    ],
)
def test_non_first_party_ids_are_refused_explicitly(provider: Provider, model: str) -> None:
    # Review F5: the table holds first-party API prices only.
    with pytest.raises(NonFirstPartyModelError, match="first-party"):
        PRICE_TABLE.get(provider, model)
    assert issubclass(NonFirstPartyModelError, UnknownModelError)


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
        prices=TokenPrices(
            input=one, output=one, cache_read=one, cache_write=one, cache_write_1h=one
        ),
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
        TokenPrices(input=1.5, output=one, cache_read=one, cache_write=one, cache_write_1h=one)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        TokenPrices(
            input=Decimal("-1"), output=one, cache_read=one, cache_write=one, cache_write_1h=one
        )


def _all_subclasses(cls: type[UnpricedUsageError]) -> list[type[UnpricedUsageError]]:
    found: list[type[UnpricedUsageError]] = []
    for sub in cls.__subclasses__():
        found += [sub, *_all_subclasses(sub)]
    return found


def test_every_unpriced_reason_is_allowed_by_the_proposed_sql() -> None:
    # Re-review N3: a reason code that the run_costs CHECK rejects would lose the tokens.
    doc = (Path(__file__).parents[2] / "docs" / "costs.md").read_text(encoding="utf-8")
    match = re.search(r"unpriced_reason text check \(unpriced_reason in \((.*?)\)\)", doc, re.S)
    assert match is not None
    sql_codes = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert sql_codes == set(UNPRICED_REASONS)
    reasons = {sub.reason for sub in _all_subclasses(UnpricedUsageError)}
    assert reasons == set(UNPRICED_REASONS)


def _proposed_sql() -> str:
    doc = (Path(__file__).parents[2] / "docs" / "costs.md").read_text(encoding="utf-8")
    match = re.search(r"### SQL\n\n```sql\n(.*?)```", doc, re.S)
    assert match is not None
    return match.group(1)


def test_serving_values_and_their_reason_code_match_the_proposed_sql() -> None:
    sql = _proposed_sql()
    match = re.search(r"serving text not null check \(serving in \((.*?)\)\)", sql, re.S)
    assert match is not None
    assert set(re.findall(r"'([a-z_]+)'", match.group(1))) == {s.value for s in Serving}
    # Only first-party serving is priced; the others are recorded with this code.
    assert NonFirstPartyServingError.reason == "non_first_party"
    assert NonFirstPartyServingError.reason in UNPRICED_REASONS
    assert "serving = 'first_party' or pricing_status = 'unpriced'" in sql


def test_every_cost_row_field_is_a_proposed_run_costs_column() -> None:
    sql = _proposed_sql()
    body = sql[sql.index("create table public.run_costs (") : sql.index("\n);")]
    columns = set(
        re.findall(r"^  ([a-z_0-9]+) (?:uuid|text|bigint|numeric|timestamptz)", body, re.M)
    )
    assert columns == {f.name for f in fields(CostRow)} | {"id"}


def test_unpriced_usage_error_needs_a_specific_reason() -> None:
    with pytest.raises(TypeError, match="subclass"):
        UnpricedUsageError("no reason")
    with pytest.raises(TypeError, match="reason"):

        class _NoCode(UnpricedUsageError):
            reason = "unpriced"
