"""One versioned price table for every model the runtimes may call (docs/costs.md, "Prices").

All prices are USD per million tokens (MTok), as Decimal. A model that is not in the table is
an error, never a zero price. The billed cost always comes from this table; a cost that an SDK
reports is only a cross-check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import ClassVar


class Provider(StrEnum):
    """Model providers. The runtimes are claude_agent_sdk (Anthropic) and openai_agents_sdk."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


# Every value of run_costs.unpriced_reason (the CHECK list in docs/costs.md must match).
UNPRICED_REASONS = (
    "unknown_model",
    "tiered_pricing",
    "non_first_party",
    "web_search",
    "cost_out_of_range",
    "no_price_table",
)


class UnpricedUsageError(ValueError):
    """Usage that cannot be priced. Never price it as zero: record it unpriced and stop the run.

    Raise a subclass. Each subclass has a `reason` from UNPRICED_REASONS, which is stored in
    run_costs.unpriced_reason; a code that the SQL CHECK rejects would lose the tokens.
    """

    reason: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "reason", None) not in UNPRICED_REASONS:
            raise TypeError(f"{cls.__name__}.reason must be one of {UNPRICED_REASONS}")

    def __init__(self, *args: object) -> None:
        if type(self) is UnpricedUsageError:
            raise TypeError("raise a subclass of UnpricedUsageError, which has a reason code")
        super().__init__(*args)


class UnknownModelError(UnpricedUsageError):
    """The model has no price in the table."""

    reason = "unknown_model"


class TieredPricingError(UnknownModelError):
    """The model's price depends on each request's prompt length (docs/costs.md, decision C2)."""

    reason = "tiered_pricing"


class NonFirstPartyModelError(UnknownModelError):
    """A Bedrock, Vertex AI, gateway or fine-tuned model id. Only first-party prices are known."""

    reason = "non_first_party"


class NoPriceTableError(UnpricedUsageError):
    """No price table was in effect at the time of the usage."""

    reason = "no_price_table"


def _non_first_party_kind(model: str) -> str | None:
    """Name the kind of a model id that is not a first-party API id, or return None."""
    if model.startswith("ft:"):
        return "fine-tuned"
    if "anthropic." in model or model.endswith(("-v1:0", "-v2:0")):
        return "Amazon Bedrock"
    if "@" in model:
        return "Google Vertex AI"
    if "/" in model or ":" in model:
        return "gateway or provider-prefixed"
    return None


@dataclass(frozen=True, slots=True)
class TokenPrices:
    """USD per MTok for each disjoint token bucket of a usage report."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} price must be a Decimal, got {type(value).__name__}")
            if not value.is_finite():
                raise ValueError(f"{name} price must be finite, got {value}")
            if value < 0:
                raise ValueError(f"{name} price must not be negative, got {value}")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """The price of one model. `model` is the canonical id; `aliases` are other exact ids."""

    provider: Provider
    model: str
    prices: TokenPrices
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Tiered:
    provider: Provider
    model: str
    rule: str


class PriceTable:
    """An immutable, versioned lookup from (provider, exact model id) to a price."""

    def __init__(
        self,
        *,
        version: str,
        effective_from: date,
        source_urls: Mapping[Provider, str],
        entries: Iterable[ModelPrice],
        tiered: Iterable[tuple[Provider, str, str]] = (),
    ) -> None:
        if not version.strip():
            raise ValueError("price table version must not be blank")
        self.version = version
        self.effective_from = effective_from
        self.source_urls: Mapping[Provider, str] = MappingProxyType(dict(source_urls))
        self._entries = tuple(entries)
        self._by_id: dict[tuple[Provider, str], ModelPrice] = {}
        self._tiered: dict[tuple[Provider, str], _Tiered] = {}
        for entry in self._entries:
            for model_id in (entry.model, *entry.aliases):
                self._claim((entry.provider, model_id))
                self._by_id[(entry.provider, model_id)] = entry
        for provider, model, rule in tiered:
            self._claim((provider, model))
            self._tiered[(provider, model)] = _Tiered(provider, model, rule)

    def _claim(self, key: tuple[Provider, str]) -> None:
        if key in self._by_id or key in self._tiered:
            raise ValueError(f"duplicate model id in price table: {key[0]}/{key[1]}")

    def entries(self) -> tuple[ModelPrice, ...]:
        return self._entries

    def get(self, provider: Provider, model: str) -> ModelPrice:
        """Return the price for an exact model id, or raise UnknownModelError."""
        key = (provider, model)
        if key in self._by_id:
            return self._by_id[key]
        if key in self._tiered:
            tier = self._tiered[key]
            raise TieredPricingError(
                f"model {model!r} ({provider}) has prompt-length pricing tiers ({tier.rule}); "
                "a usage report does not carry per-request prompt length, so it cannot be "
                "priced (docs/costs.md, decision C2)"
            )
        kind = _non_first_party_kind(model)
        if kind is not None:
            raise NonFirstPartyModelError(
                f"model {model!r} ({provider}) looks like a {kind} id; the price table holds "
                "first-party API prices only (docs/costs.md, decision C13)"
            )
        owners = sorted(
            str(p) for (p, m) in (*self._by_id, *self._tiered) if m == model and p != provider
        )
        hint = f"; it is listed under provider {', '.join(owners)}" if owners else ""
        raise UnknownModelError(
            f"unknown model {model!r} for provider {provider} in price table "
            f"{self.version}{hint}; add it to harness/costs/prices.py before use"
        )

    def is_priced(self, provider: Provider, model: str) -> bool:
        return (provider, model) in self._by_id


class PriceBook:
    """Price tables over time. The table for a moment is the latest one whose `effective_from`
    (a UTC date) is on or before that moment's UTC date."""

    def __init__(self, tables: Iterable[PriceTable]) -> None:
        ordered = sorted(tables, key=lambda t: t.effective_from)
        if not ordered:
            raise ValueError("a price book needs at least one price table")
        for earlier, later in pairwise(ordered):
            if earlier.effective_from == later.effective_from:
                raise ValueError(f"two price tables are effective on {later.effective_from}")
        if len({t.version for t in ordered}) != len(ordered):
            raise ValueError("price table versions must be unique")
        self._tables = tuple(ordered)

    def tables(self) -> tuple[PriceTable, ...]:
        return self._tables

    @property
    def latest(self) -> PriceTable:
        return self._tables[-1]

    def table_at(self, moment: datetime) -> PriceTable:
        """Return the table in effect at `moment` (timezone-aware), or raise NoPriceTableError."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError(f"expected a timezone-aware datetime, got {moment!r}")
        day = moment.astimezone(UTC).date()
        for table in reversed(self._tables):
            if table.effective_from <= day:
                return table
        raise NoPriceTableError(
            f"no price table is in effect on {day}; "
            f"the first starts {self._tables[0].effective_from}"
        )


def check_priced(
    provider: Provider,
    model: str,
    *,
    at: datetime | None = None,
    prices: PriceBook | None = None,
) -> ModelPrice:
    """Raise an UnpricedUsageError subclass unless the model has a price at `at` (default now).

    The table is chosen by date the same way the ledger chooses it. The orchestrator calls this
    (through `check_run_models`) before a run for every model the runtime may call: the run's
    model, a fallback model, the small fast model and subagent models.
    """
    book = prices or PRICE_BOOK
    return book.table_at(at or datetime.now(UTC)).get(provider, model)


# --- Default table ---------------------------------------------------------------------------

_D = Decimal


def _anthropic(model: str, inp: str, out: str, read: str, write: str, *aliases: str) -> ModelPrice:
    return ModelPrice(
        Provider.ANTHROPIC,
        model,
        TokenPrices(input=_D(inp), output=_D(out), cache_read=_D(read), cache_write=_D(write)),
        aliases,
    )


def _openai(model: str, inp: str, out: str, cached: str) -> ModelPrice:
    # These models have "No additional cache-write charge": a token written to the cache is
    # billed as ordinary input (https://developers.openai.com/api/docs/guides/prompt-caching).
    return ModelPrice(
        Provider.OPENAI,
        model,
        TokenPrices(input=_D(inp), output=_D(out), cache_read=_D(cached), cache_write=_D(inp)),
    )


ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"

# Anthropic: source ANTHROPIC_PRICING_URL, checked 2026-11-20. Standard global routing.
# Columns: input, output, cache read (hits and refreshes), cache write at the 5-minute TTL.
# Not modelled: 1-hour cache writes (2x input), batch (-50%), fast mode, inference_geo="us"
# (1.1x). Retired models (Opus 4.1, Opus 4, Sonnet 4) and Mythos are left out on purpose.
_ANTHROPIC = (
    _anthropic("claude-fable-5-2", "8", "40", "0.20", "10"),
    _anthropic("claude-fable-5-1", "10", "50", "0.25", "12.50"),
    _anthropic("claude-fable-5", "10", "50", "1", "12.50"),
    _anthropic("claude-opus-5-5", "4", "20", "0.20", "5"),
    _anthropic("claude-opus-5", "5", "25", "0.50", "6.25"),
    _anthropic("claude-opus-4-8", "5", "25", "0.50", "6.25"),
    _anthropic("claude-opus-4-7", "5", "25", "0.50", "6.25"),
    _anthropic("claude-opus-4-6", "5", "25", "0.50", "6.25"),
    _anthropic("claude-opus-4-5", "5", "25", "0.50", "6.25", "claude-opus-4-5-20251101"),
    _anthropic("claude-sonnet-5-5", "2", "10", "0.10", "2.50"),
    _anthropic("claude-sonnet-5", "2", "10", "0.20", "2.50"),
    _anthropic("claude-sonnet-4-6", "3", "15", "0.30", "3.75"),
    _anthropic("claude-sonnet-4-5", "3", "15", "0.30", "3.75", "claude-sonnet-4-5-20250929"),
    _anthropic("claude-haiku-4-5", "1", "5", "0.10", "1.25", "claude-haiku-4-5-20251001"),
)

# OpenAI: source OPENAI_PRICING_URL, Standard tier, checked 2026-11-20.
# Columns: input, output, cached input. Not modelled: regional processing (+10%), priority,
# flex, and batch. Pro models (gpt-5-pro, gpt-5.2-pro, gpt-5.4-pro, gpt-5.5-pro, o3-pro) are
# left out on purpose.
_OPENAI = (
    _openai("gpt-5.4-mini", "0.75", "4.50", "0.075"),
    _openai("gpt-5.4-nano", "0.20", "1.25", "0.02"),
    _openai("gpt-5.2", "1.75", "14.00", "0.175"),
    _openai("gpt-5.1", "1.25", "10.00", "0.125"),
    _openai("gpt-5", "1.25", "10.00", "0.125"),
    _openai("gpt-5-mini", "0.25", "2.00", "0.025"),
    _openai("gpt-5-nano", "0.05", "0.40", "0.005"),
    _openai("gpt-4.1", "2.00", "8.00", "0.50"),
    _openai("gpt-4.1-mini", "0.40", "1.60", "0.10"),
    _openai("gpt-4o", "2.50", "10.00", "1.25"),
    _openai("gpt-4o-mini", "0.15", "0.60", "0.075"),
    _openai("o3", "2.00", "8.00", "0.50"),
)

# Prompt-length tiers: the price of these models changes with each request's prompt length,
# which a per-segment usage report does not carry. They are refused (docs/costs.md, C2).
_ANTHROPIC_TIER_RULE = "prompts over 100,000 input tokens cost 5x"
_OPENAI_TIER_RULE = "short context is up to 272K input tokens, long context costs more"
_TIERED = (
    (Provider.ANTHROPIC, "claude-haiku-5-5", _ANTHROPIC_TIER_RULE),
    *(
        (Provider.OPENAI, model, _OPENAI_TIER_RULE)
        for model in (
            "gpt-6.2-astra",
            "gpt-6.2-sol",
            "gpt-6.1-sol",
            "gpt-6.1-terra",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
        )
    ),
)

PRICE_TABLE = PriceTable(
    version="2026-11-20",
    effective_from=date(2026, 11, 20),
    source_urls={Provider.ANTHROPIC: ANTHROPIC_PRICING_URL, Provider.OPENAI: OPENAI_PRICING_URL},
    entries=(*_ANTHROPIC, *_OPENAI),
    tiered=_TIERED,
)

# Add a new table here (with a later effective_from) when prices change. Never edit a table
# that rows already reference: each row stores price_table_version.
PRICE_BOOK = PriceBook([PRICE_TABLE])

__all__ = [
    "PRICE_BOOK",
    "PRICE_TABLE",
    "UNPRICED_REASONS",
    "ModelPrice",
    "NoPriceTableError",
    "NonFirstPartyModelError",
    "PriceBook",
    "PriceTable",
    "Provider",
    "TieredPricingError",
    "TokenPrices",
    "UnknownModelError",
    "UnpricedUsageError",
    "check_priced",
]
