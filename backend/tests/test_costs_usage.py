"""UsageReport (runtime contract v2): validation at the edge and the idempotency key."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harness.costs import (
    Provider,
    Serving,
    UsageKey,
    UsageReport,
    cache_write_1h_from_model_usage,
    idempotency_key,
)

RUN_ID = UUID("00000000-0000-4000-8000-000000000001")


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": str(RUN_ID),
        "segment_id": "seg-1",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "serving": "first_party",
    }
    payload.update(overrides)
    return payload


def test_valid_report_parses_from_json_like_input() -> None:
    report = UsageReport.model_validate(_payload(sdk_reported_cost_usd="0.006"))
    assert report.run_id == RUN_ID
    assert report.provider is Provider.ANTHROPIC
    assert report.sdk_reported_cost_usd == Decimal("0.006")


def test_sdk_cost_is_optional() -> None:
    assert UsageReport.model_validate(_payload()).sdk_reported_cost_usd is None


@pytest.mark.parametrize(
    "field", ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
)
@pytest.mark.parametrize("bad", [-1, 1.5, True, "10", None])
def test_token_counts_must_be_non_negative_ints(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(**{field: bad}))


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError, match="provider"):
        UsageReport.model_validate(_payload(provider="google"))


@pytest.mark.parametrize("model", ["", "   "])
def test_model_must_not_be_blank(model: str) -> None:
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(model=model))


def test_segment_id_must_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(segment_id=" "))


def test_unpriced_model_still_parses_so_its_tokens_are_kept() -> None:
    # Pricing happens in the ledger, which stores the tokens with no cost and makes the caps
    # deny. Rejecting the report here would drop tokens that were already spent (review F2).
    for provider, model in [("anthropic", "claude-imaginary-9"), ("anthropic", "gpt-5")]:
        report = UsageReport.model_validate(_payload(provider=provider, model=model))
        assert report.model == model


def test_token_counts_fit_a_postgres_bigint() -> None:
    UsageReport.model_validate(_payload(output_tokens=2**63 - 1))
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(output_tokens=2**63))


@pytest.mark.parametrize(
    "bad",
    [
        "-0.01",
        "-5.5E-17",
        "NaN",
        "Infinity",
        "abc",
        0.3,
        -1,
        True,
        [1],
        "100000000000",
        # Re-review N2: rounds up to 10^11, which numeric(20, 9) cannot hold.
        "99999999999.9999999995",
    ],
)
def test_bad_sdk_cost_becomes_null_and_keeps_the_tokens(
    bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    # The SDK cost is a cross-check only: a bad value must never reject the token counts.
    # Floats are refused too, so binary noise (0.30000000000000004) never reaches the ledger.
    with caplog.at_level(logging.WARNING, logger="harness.costs"):
        report = UsageReport.model_validate(_payload(sdk_reported_cost_usd=bad))
    assert report.sdk_reported_cost_usd is None
    assert report.input_tokens == 1000
    [record] = caplog.records
    assert str(RUN_ID) in record.getMessage()
    assert "seg-1" in record.getMessage()


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("0.123456789123", "0.123456789"),  # numeric(20, 9) keeps nine places
        ("0.0000000005", "0.000000001"),  # half up
        (Decimal("1.5"), "1.500000000"),
        (2, "2.000000000"),
    ],
)
def test_sdk_cost_is_quantized_to_nine_places(given: object, stored: str) -> None:
    report = UsageReport.model_validate(_payload(sdk_reported_cost_usd=given))
    assert report.sdk_reported_cost_usd == Decimal(stored)
    assert report.sdk_reported_cost_usd.as_tuple().exponent == -9


def test_unexpected_fields_are_rejected() -> None:
    # The contract fields are agreed with the adapters; a typo must not pass silently.
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(cached_tokens=5))


def test_report_is_immutable() -> None:
    report = UsageReport.model_validate(_payload())
    with pytest.raises(ValidationError):
        report.input_tokens = 5


def test_idempotency_key_is_run_segment_provider_serving_model() -> None:
    report = UsageReport.model_validate(_payload())
    assert UsageKey._fields == ("run_id", "segment_id", "provider", "serving", "model")
    assert idempotency_key(report) == UsageKey(
        run_id=RUN_ID,
        segment_id="seg-1",
        provider=Provider.ANTHROPIC,
        serving=Serving.FIRST_PARTY,
        model="claude-sonnet-4-6",
    )


def test_same_segment_with_other_token_counts_has_the_same_key() -> None:
    first = UsageReport.model_validate(_payload(input_tokens=1))
    again = UsageReport.model_validate(_payload(input_tokens=999))
    assert idempotency_key(first) == idempotency_key(again)


def test_key_differs_by_model_serving_segment_and_run() -> None:
    base = idempotency_key(UsageReport.model_validate(_payload()))
    others = [
        _payload(model="claude-haiku-4-5"),
        _payload(serving="bedrock"),
        _payload(segment_id="seg-2"),
        _payload(run_id=str(uuid4())),
    ]
    assert all(idempotency_key(UsageReport.model_validate(p)) != base for p in others)


def test_key_uses_the_exact_model_id_that_was_sent() -> None:
    # One segment can report a model under two ids (the Claude CLI keys model_usage by raw id,
    # for example claude-haiku-4-5 and claude-haiku-4-5-20251001). Both are real usage, so they
    # must be two keys (review F1). A true resend repeats the same raw id.
    short = UsageReport.model_validate(_payload(model="claude-haiku-4-5"))
    dated = UsageReport.model_validate(_payload(model="claude-haiku-4-5-20251001"))
    assert idempotency_key(dated).model == "claude-haiku-4-5-20251001"
    assert idempotency_key(dated) != idempotency_key(short)


def test_largest_sdk_cost_that_fits_numeric_20_9_is_kept() -> None:
    report = UsageReport.model_validate(_payload(sdk_reported_cost_usd="99999999999.9999999994"))
    assert report.sdk_reported_cost_usd == Decimal("99999999999.999999999")


# Alignment with runtime contract v2 (hermes_like_agent PR #3) ----------------------------------


def test_usage_report_has_exactly_the_contract_fields() -> None:
    # The field set the contract copies. A change here is a contract change.
    assert set(UsageReport.model_fields) == {
        "run_id",
        "segment_id",
        "provider",
        "serving",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_write_1h_tokens",
        "web_search_requests",
        "sdk_reported_cost_usd",
    }


def test_serving_is_required() -> None:
    payload = _payload()
    del payload["serving"]
    with pytest.raises(ValidationError, match="serving"):
        UsageReport.model_validate(payload)


@pytest.mark.parametrize("serving", ["first_party", "bedrock", "vertex", "other"])
def test_serving_accepts_the_four_values(serving: str) -> None:
    report = UsageReport.model_validate(_payload(serving=serving))
    assert report.serving is Serving(serving)


@pytest.mark.parametrize("serving", ["firstParty", "gateway", "", "FIRST_PARTY", None])
def test_serving_rejects_other_values(serving: object) -> None:
    with pytest.raises(ValidationError, match="serving"):
        UsageReport.model_validate(_payload(serving=serving))


def test_cache_write_1h_tokens_is_optional_and_unknown_by_default() -> None:
    assert UsageReport.model_validate(_payload()).cache_write_1h_tokens is None
    report = UsageReport.model_validate(_payload(cache_write_tokens=100, cache_write_1h_tokens=40))
    assert report.cache_write_1h_tokens == 40


@pytest.mark.parametrize("bad", [-1, 1.5, True, "10", 2**63])
def test_cache_write_1h_tokens_must_be_a_token_count(bad: object) -> None:
    with pytest.raises(ValidationError, match="cache_write_1h_tokens"):
        UsageReport.model_validate(_payload(cache_write_tokens=100, cache_write_1h_tokens=bad))


def test_cache_write_1h_tokens_is_a_subset_of_cache_write_tokens() -> None:
    UsageReport.model_validate(_payload(cache_write_tokens=100, cache_write_1h_tokens=100))
    with pytest.raises(ValidationError, match="cache_write_1h_tokens"):
        UsageReport.model_validate(_payload(cache_write_tokens=100, cache_write_1h_tokens=101))


# Claude model_usage cache-write split (Hermes_Like_Bot round 4, item 1) ------------------------
# Claude CLI 2.1.327 reports cacheCreationEphemeral5mInputTokens and
# cacheCreationEphemeral1hInputTokens per model. Both are already inside
# cacheCreationInputTokens, and a write whose TTL the API did not report is in neither.


def test_a_complete_split_maps_the_1h_count() -> None:
    assert cache_write_1h_from_model_usage(1_000_000, 600_000, 400_000) == 400_000
    assert cache_write_1h_from_model_usage(10, 10, 0) == 0
    assert cache_write_1h_from_model_usage(10, 0, 10) == 10
    assert cache_write_1h_from_model_usage(0, 0, 0) == 0


def test_writes_with_no_reported_ttl_count_as_1h() -> None:
    # 200k writes are in neither field: price them at the 1-hour rate (safe side, C1).
    assert cache_write_1h_from_model_usage(1_000_000, 500_000, 300_000) == 500_000


@pytest.mark.parametrize(("five_minute", "one_hour"), [(None, 0), (0, None), (None, None)])
def test_a_missing_split_field_gives_null(five_minute: int | None, one_hour: int | None) -> None:
    assert cache_write_1h_from_model_usage(1000, five_minute, one_hour) is None


@pytest.mark.parametrize(
    ("total", "five_minute", "one_hour"),
    [(1000, 600, 401), (1000, -1, 0), (1000, 0, -1), (-1, 0, 0), (1000, 1001, 0)],
)
def test_an_inconsistent_split_gives_null(total: int, five_minute: int, one_hour: int) -> None:
    # The adapter computes segment deltas; a delta that does not fit the total is not trusted.
    assert cache_write_1h_from_model_usage(total, five_minute, one_hour) is None


def test_the_mapped_value_is_a_valid_report_field() -> None:
    one_hour = cache_write_1h_from_model_usage(1000, 250, 750)
    report = UsageReport.model_validate(
        _payload(cache_write_tokens=1000, cache_write_1h_tokens=one_hour)
    )
    assert report.cache_write_1h_tokens == 750
