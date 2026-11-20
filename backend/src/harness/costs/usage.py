"""UsageReport: the runtime contract (v2) between a runtime adapter and the cost ledger.

Each adapter sends one report per (segment, model). Token buckets are disjoint:

- input_tokens: input tokens billed at the base input rate (not read from or written to cache)
- output_tokens: output tokens, including reasoning/thinking tokens
- cache_read_tokens: input tokens served from the prompt cache
- cache_write_tokens: input tokens written to the prompt cache

The OpenAI Agents SDK reports `input_tokens` with cached tokens included, so the OpenAI adapter
must send `input_tokens - cached_tokens` as input_tokens (docs/costs.md, "Adapter notes").
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, NamedTuple, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from harness.costs.prices import PRICE_TABLE, PriceTable, Provider

TokenCount = Annotated[StrictInt, Field(ge=0)]


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
    # A cross-check only. The billed cost always comes from the price table.
    sdk_reported_cost_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None

    @field_validator("segment_id", "model")
    @classmethod
    def _no_blank_or_padded_ids(cls, value: str) -> str:
        # Ids are exact-match keys: " gpt-5" must not silently become "gpt-5" or a new key.
        if not value.strip():
            raise ValueError("must not be blank")
        if value != value.strip():
            raise ValueError("must not have leading or trailing whitespace")
        return value

    @model_validator(mode="after")
    def _model_has_a_price(self) -> Self:
        # Raises UnknownModelError (a ValueError), which pydantic reports as a ValidationError.
        PRICE_TABLE.get(self.provider, self.model)
        return self


class UsageKey(NamedTuple):
    """Idempotency key of a usage report. A second report with the same key is a no-op."""

    run_id: UUID
    segment_id: str
    provider: Provider
    model: str


def idempotency_key(report: UsageReport, table: PriceTable = PRICE_TABLE) -> UsageKey:
    """(run_id, segment_id, provider, canonical model id).

    The model id is canonical so that a resend that names the same model by a dated snapshot id
    (for example claude-sonnet-4-5-20250929) is still a duplicate.
    """
    canonical = table.get(report.provider, report.model).model
    return UsageKey(report.run_id, report.segment_id, report.provider, canonical)
