"""UsageReport: the runtime contract (v2) between a runtime adapter and the cost ledger.

Each adapter sends one report per (segment, model), where `model` is the exact id the runtime
reported (for the Claude Agent SDK, the `ResultMessage.model_usage` key). Token buckets are
disjoint:

- input_tokens: input tokens billed at the base input rate (not read from or written to cache)
- output_tokens: output tokens, including reasoning/thinking tokens
- cache_read_tokens: input tokens served from the prompt cache
- cache_write_tokens: input tokens written to the prompt cache

The OpenAI Agents SDK reports `input_tokens` with cached tokens included, so the OpenAI adapter
must send `input_tokens - cached_tokens` as input_tokens (docs/costs.md, "Adapter notes").

A report never fails because its model has no price: the ledger records the tokens unpriced
and the caps fail closed. A report also never fails because of its SDK cost, which is only a
cross-check: a bad value becomes None and a warning is logged.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from harness.costs.prices import Provider

logger = logging.getLogger("harness.costs")

# Largest value of a Postgres bigint, the type of the run_costs token columns.
MAX_TOKENS = 2**63 - 1
# Money is stored as numeric(20, 9): nine decimal places, below 10^11 USD.
COST_QUANTUM = Decimal("0.000000001")
MAX_USD = Decimal(10) ** 11

TokenCount = Annotated[StrictInt, Field(ge=0, le=MAX_TOKENS)]


def quantize_usd(amount: Decimal) -> Decimal:
    """Round a USD amount to COST_QUANTUM (the numeric(20, 9) scale), half up."""
    return amount.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def _parse_sdk_cost(value: object) -> tuple[Decimal | None, str | None]:
    """Return (cost, None) for a usable value, or (None, problem) for a bad one."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        # Floats are refused: convert with Decimal(str(x)) in the adapter, before any math.
        return None, f"must be a Decimal, int or decimal string, got {type(value).__name__}"
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None, "is not a decimal number"
    if not amount.is_finite():
        return None, "is not finite"
    if amount < 0:
        return None, "is negative"
    # Round first: a value just under 10^11 can round up to 10^11, which does not fit.
    amount = quantize_usd(amount)
    if amount >= MAX_USD:
        return None, "is out of range"
    return amount, None


class UsageReport(BaseModel):
    """One (segment, model) usage report. Parse adapter output with `model_validate`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    segment_id: Annotated[str, Field(min_length=1, max_length=200)]
    provider: Provider
    model: Annotated[str, Field(min_length=1, max_length=200)]
    input_tokens: TokenCount
    output_tokens: TokenCount
    cache_read_tokens: TokenCount
    cache_write_tokens: TokenCount
    # Optional. Server-side web search calls, which the price table does not price yet
    # (docs/costs.md, decision C12): a report with any is recorded unpriced.
    web_search_requests: TokenCount = 0
    # A cross-check only. The billed cost always comes from the price table.
    sdk_reported_cost_usd: Decimal | None = None

    @model_validator(mode="before")
    @classmethod
    def _clean_sdk_cost(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "sdk_reported_cost_usd" not in data:
            return data
        cost, problem = _parse_sdk_cost(data["sdk_reported_cost_usd"])
        if problem is not None:
            logger.warning(
                "SDK cost ignored, tokens kept: run_id=%s segment_id=%s model=%s: "
                "sdk_reported_cost_usd %s (%r)",
                data.get("run_id"),
                data.get("segment_id"),
                data.get("model"),
                problem,
                data["sdk_reported_cost_usd"],
            )
        return {**data, "sdk_reported_cost_usd": cost}

    @field_validator("segment_id", "model")
    @classmethod
    def _no_blank_or_padded_ids(cls, value: str) -> str:
        # Ids are exact-match keys: " gpt-5" must not silently become "gpt-5" or a new key.
        if not value.strip():
            raise ValueError("must not be blank")
        if value != value.strip():
            raise ValueError("must not have leading or trailing whitespace")
        return value


class UsageKey(NamedTuple):
    """Idempotency key of a usage report. A second report with the same key is a no-op."""

    run_id: UUID
    segment_id: str
    provider: Provider
    model: str


def idempotency_key(report: UsageReport) -> UsageKey:
    """(run_id, segment_id, provider, model), with `model` exactly as the runtime sent it.

    Two ids for one model in one segment (claude-haiku-4-5 and claude-haiku-4-5-20251001) are
    two keys, because both are real usage. A true resend repeats the same id.
    """
    return UsageKey(report.run_id, report.segment_id, report.provider, report.model)
