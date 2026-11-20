"""UsageReport (runtime contract v2): validation at the edge and the idempotency key."""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harness.costs import Provider, UsageKey, UsageReport, idempotency_key

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


def test_model_must_belong_to_the_reported_provider() -> None:
    with pytest.raises(ValidationError, match="gpt-5"):
        UsageReport.model_validate(_payload(provider="anthropic", model="gpt-5"))


def test_unknown_model_is_rejected_at_the_edge() -> None:
    with pytest.raises(ValidationError, match="unknown model"):
        UsageReport.model_validate(_payload(model="claude-imaginary-9"))


@pytest.mark.parametrize("bad", ["-0.01", "NaN", "Infinity"])
def test_sdk_cost_must_be_a_finite_non_negative_number(bad: str) -> None:
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(sdk_reported_cost_usd=bad))


def test_unexpected_fields_are_rejected() -> None:
    # The contract fields are agreed with the adapters; a typo must not pass silently.
    with pytest.raises(ValidationError):
        UsageReport.model_validate(_payload(cached_tokens=5))


def test_report_is_immutable() -> None:
    report = UsageReport.model_validate(_payload())
    with pytest.raises(ValidationError):
        report.input_tokens = 5


def test_idempotency_key_is_run_segment_provider_model() -> None:
    report = UsageReport.model_validate(_payload())
    assert idempotency_key(report) == UsageKey(
        run_id=RUN_ID, segment_id="seg-1", provider=Provider.ANTHROPIC, model="claude-sonnet-4-6"
    )


def test_same_segment_with_other_token_counts_has_the_same_key() -> None:
    first = UsageReport.model_validate(_payload(input_tokens=1))
    again = UsageReport.model_validate(_payload(input_tokens=999))
    assert idempotency_key(first) == idempotency_key(again)


def test_key_differs_by_model_segment_and_run() -> None:
    base = idempotency_key(UsageReport.model_validate(_payload()))
    others = [
        _payload(model="claude-haiku-4-5"),
        _payload(segment_id="seg-2"),
        _payload(run_id=str(uuid4())),
    ]
    assert all(idempotency_key(UsageReport.model_validate(p)) != base for p in others)


def test_snapshot_id_and_alias_share_one_key() -> None:
    # A resend that spells the same model as its dated snapshot must stay a no-op, so the key
    # uses the table's canonical model id.
    alias = UsageReport.model_validate(_payload(model="claude-sonnet-4-5"))
    snapshot = UsageReport.model_validate(_payload(model="claude-sonnet-4-5-20250929"))
    assert snapshot.model == "claude-sonnet-4-5-20250929"
    assert idempotency_key(snapshot) == idempotency_key(alias)
    assert idempotency_key(snapshot).model == "claude-sonnet-4-5"
