# Per-run cost tracking and spend caps

Status: DEX-24 part 1. Pure Python in `backend/src/harness/costs/`, with an in-memory ledger.
The `run_costs` table below is a PROPOSAL. It becomes a migration after PR#5 (tenancy schema
and RLS, https://github.com/nikhilllmno/vorflux/pull/5) merges, and after the `runs` table
(DEX-14) exists.

Design authority: `docs/architecture.md` sections 5.5 (per-hotel budgets), 6.2 (usage and cost
fields on every runtime event), 7.2 (cost isolation layer), 8.4 (tokens and cost per run),
10.9 (observability and cost) and 12 (M1: basic hard spend caps per run and per hotel per day).

## Flow

```
before a run:      check_run_models(every model the runtime may call)         ─▶ refuse the run
                   check_hotel_unpriced_limit (C16, every hotel)              ─▶ block until next UTC day
                   check_hotel_daily_cap (priced spend; also runs C16)        ─▶ block until next UTC day
before a segment:  check_run_cap + check_hotel_unpriced_limit + check_hotel_daily_cap
during a segment:  the same checks with pending_usd = price_usage(tokens so far)
after a segment:   runtime adapter ──UsageReport──▶ CostLedger.record(report, caps=SpendCaps(...))
                     one row per (run, segment, provider, serving, exact model id), tokens always kept
                     priced from the PriceBook table in effect (never from the SDK)
                     or recorded unpriced (no cost) when it cannot be priced
                     run cap + hotel daily cap checked in the same atomic step
                     RecordResult.stop_run ─▶ stop the run
                     unpriced row ─▶ stop only this run + alert (C11 option a)
```

## Runtime contract v2: `UsageReport`

Each adapter sends one report per (segment, model id). A segment that calls two model ids
sends two reports. Fields, exactly:

| Field | Type | Rule |
|---|---|---|
| `run_id` | UUID | The harness run. Must exist (the ledger finds its org and hotel). |
| `segment_id` | str | Non-blank, no surrounding whitespace, max 200 chars. Stable across resume. |
| `provider` | `"anthropic"` \| `"openai"` | No other value. |
| `serving` | `"first_party"` \| `"bedrock"` \| `"vertex"` \| `"other"` | Required. Who served the request. Only `first_party` is priced; any other value is recorded unpriced with `non_first_party` and the run stops (decision C13). |
| `model` | str | The exact model id the runtime reported, unchanged. Max 200 chars. A model without a price is accepted and recorded unpriced. |
| `input_tokens` | int, 0 to 2^63-1 | Input billed at the base input rate (not a cache read or write). |
| `output_tokens` | int, 0 to 2^63-1 | Output, including reasoning or thinking tokens. |
| `cache_read_tokens` | int, 0 to 2^63-1 | Input served from the prompt cache. |
| `cache_write_tokens` | int, 0 to 2^63-1 | Input written to the prompt cache, at any TTL. |
| `cache_write_1h_tokens` | int, 0 to 2^63-1, or null; optional (default null) | The 1-hour TTL part of `cache_write_tokens` (a subset, not a fifth bucket; more than `cache_write_tokens` is rejected). Null means the split is unknown, and every cache write is priced at the 1-hour rate (decision C1). |
| `web_search_requests` | int, 0 to 2^63-1, optional (default 0) | Server-side web search calls (Claude: `ModelUsage.webSearchRequests`). Any value above 0 is recorded unpriced (decision C12). |
| `sdk_reported_cost_usd` | Decimal, optional | The SDK's own cost (Claude: the delta of `ModelUsage.costUSD`). A cross-check only. Normalised, never rejected (see below). |

Validation is strict: token counts must be real ints (not `1.5`, `"10"`, `true` or null) and
fit a Postgres `bigint`, unknown fields are rejected, and the report is immutable.

`sdk_reported_cost_usd` normalisation. A cross-check field never rejects the report, because
the tokens are already spent:

- Accepted: a `Decimal`, an `int` (not a `bool`) or a decimal string, finite and ≥ 0. It is
  rounded half up to 9 places, the scale of `numeric(20, 9)`, so Postgres never rounds it
  again. The range check (below 10^11) runs after the rounding, so a value that rounds up to
  10^11 becomes null instead of failing the insert.
- Anything else becomes null and logs a warning on `harness.costs` with run_id and
  segment_id: a float (convert with `Decimal(str(x))` in the adapter), a negative value
  (including a float delta such as `-5.5e-17`), NaN, infinity, an unparsable string or an
  out-of-range value. The tokens are kept.

### Adapter notes

- The four token buckets are disjoint. Do not count a token twice.
- Anthropic (Claude Agent SDK): read `ResultMessage.model_usage`, never the combined
  `ResultMessage.usage` (it merges every model, so it cannot be priced per model). Send one
  report per key of `model_usage` that used tokens in the segment, with that key as `model`
  (never `ModelUsage.canonicalModel`). Each value is the segment's delta for that model
  (runtime contract v2, Section 4.3):

  | `ModelUsage` field | `UsageReport` field |
  |---|---|
  | `inputTokens` | `input_tokens` |
  | `outputTokens` | `output_tokens` |
  | `cacheReadInputTokens` | `cache_read_tokens` |
  | `cacheCreationInputTokens` | `cache_write_tokens` |
  | `cacheCreationEphemeral5mInputTokens`, `cacheCreationEphemeral1hInputTokens` | `cache_write_1h_tokens` = `cache_write_1h_from_model_usage(Δ cacheCreationInputTokens, Δ cacheCreationEphemeral5mInputTokens)`; absent when the 5m count is absent (C1). The 1h count is not used |
  | `webSearchRequests` | `web_search_requests` |
  | `costUSD` | `sdk_reported_cost_usd` = `Decimal(str(end)) - Decimal(str(baseline))` |
  | `provider`: `firstParty` → `first_party`; `bedrock` → `bedrock`; `vertex` → `vertex`; absent or anything else → `other` (fail closed) | `serving` |

  A segment that also used a small fast model, or that reported a short id and a dated id,
  sends one report for each.

  The cache-write split. Claude Code CLI 2.1.327 (bundled with `claude-agent-sdk` 0.2.184)
  adds two optional fields to each `modelUsage` entry: `cacheCreationEphemeral5mInputTokens`
  and `cacheCreationEphemeral1hInputTokens`. The Python `ModelUsage` TypedDict does not declare
  them, but the SDK passes `modelUsage` through unchanged, so read them from the dict. Both
  are already inside `cacheCreationInputTokens`. A cache write with no reported lifetime is
  counted in `cacheCreationInputTokens` but in neither TTL field, and the SDK's `costUSD`
  prices it at the 5-minute rate. A direct map of the 1h count would also price those writes
  at the 5-minute rate, so the 1h count is not used. The rule: when
  `cacheCreationEphemeral5mInputTokens` is present, `cache_write_1h_tokens =
  cacheCreationInputTokens - cacheCreationEphemeral5mInputTokens`; when it is absent,
  `cache_write_1h_tokens` is absent, so every cache write uses the 1-hour rate. Take each
  field as a segment delta, the same as `costUSD` (end snapshot minus baseline snapshot of
  that model). Baseline rule (runtime contract v2): a model with no baseline entry has a
  baseline of 0. The adapter leaves `cache_write_1h_tokens` absent (None) when the end entry
  has no 5-minute count (also when neither lifetime count is present), when an existing
  baseline entry has none, or when the result is outside 0 to the `cache_write_tokens` delta.
  Then the segment prices every cache write at the 1-hour rate. An existing baseline entry
  with no 5-minute count (an older CLI wrote it) never counts as 0, because that makes the 5m
  delta too large and under-prices the segment. Call
  `cache_write_1h_from_model_usage(total, five_minute)` with `five_minute=None` in those
  cases; it also returns null when the result is outside 0 to the total. With every write reporting its lifetime, our cost matches
  `costUSD` within the C6 tolerance (tested). Writes with no reported lifetime, or an absent
  5m count, make our cost higher than `costUSD`, and `check_sdk_cost` logs a mismatch for
  that row.
  The `provider` field maps to `serving`. When it is absent, `serving` is `other`: the row is
  recorded unpriced with `non_first_party` and the run stops (fail closed, the same as
  hermes_like_agent PR#3).
- OpenAI (Agents SDK): `input_tokens` INCLUDES cached tokens. Send
  `input_tokens = usage.input_tokens - usage.input_tokens_details.cached_tokens` and
  `cache_read_tokens = cached_tokens`. Send `cache_write_tokens = 0` for the models in the
  table: they have no separate cache-write charge. Send `serving = "first_party"` (the adapter
  calls the OpenAI API directly).
- Do not normalise the model id. Prices resolve dated snapshot ids through explicit aliases
  in the table (for example `claude-haiku-4-5-20251001` → `claude-haiku-4-5`).
- On resume, resend a segment with the same `segment_id` and the same model ids. The ledger
  ignores the repeat.

### Idempotency

The key is `(run_id, segment_id, provider, serving, model)`, with `model` exactly as received.
Two ids for one model in one segment (for example `claude-haiku-4-5` and
`claude-haiku-4-5-20251001`, which Claude Code CLI 2.1.327 reports) are two keys and are both
billed. One model id served first-party and by another provider in one segment is also two
keys, two rows. A second report with the same key is a no-op that returns `DUPLICATE`. If the
resent token counts, 1-hour split or SDK cost differ from the stored row, the first row stays
and a warning that contains "conflict" is logged on the `harness.costs` logger. A resend of a
segment with a changed `serving` is a new key, so it adds a row and no conflict warning. USD
totals and caps stay correct, because only `first_party` is priced and any other `serving`
stops the run. But the token totals of that run count those tokens again. The CLI gives one
`provider` per model entry, so this needs an adapter bug.

## Prices

`harness/costs/prices.py` holds one `PriceTable` for both providers: version `2026-11-20`,
effective from 2026-11-20. Each entry has five Decimal prices in USD per million tokens:
input, output, cache read, cache write (`cache_write`, the 5-minute TTL) and 1-hour cache
write (`cache_write_1h`).

- Anthropic source: https://platform.claude.com/docs/en/about-claude/pricing (checked
  2026-11-20). `cache_write` is the 5-minute TTL rate (1.25x input); `cache_write_1h` is the
  1-hour TTL rate (2x input).
- OpenAI source: https://developers.openai.com/api/docs/pricing, Standard tier (checked
  2026-11-20). Both cache-write prices equal the input price, because these models have "No
  additional cache-write charge" (https://developers.openai.com/api/docs/guides/prompt-caching).

`PRICE_BOOK` (a `PriceBook`) holds every table by `effective_from`. The ledger prices each
report with `PRICE_BOOK.table_at(recorded_at)`: the latest table whose `effective_from` is on
or before the UTC date of the report (decision C15). Two tables with one `effective_from` are
refused. A price change is a new table with a later date; never edit a table that rows
reference. Each row stores `price_table_version`.

`price_usage` and `check_run_models` choose the table the same way: `prices.table_at(...)`,
where `prices` defaults to `PRICE_BOOK`. `price_usage` takes `at`, which defaults to now
(UTC); `check_run_models` takes `now`. So a running estimate, the pre-run check and the final
row use the same table. Before the first table, `price_usage` raises `NoPriceTableError` and
`check_run_models` refuses every model with `no_price_table`.

Rules. A model that cannot be priced is never priced as zero. `PriceTable.get` checks, in
order:

1. An exact id or alias in the table: priced.
2. A model whose price depends on each request's prompt length: `TieredPricingError`. The
   report carries per-segment totals, not per-request prompt lengths, so the tier cannot be
   chosen. These are `claude-haiku-5-5` (over 100,000 input tokens costs 5x) and the OpenAI
   models with a 272K short-context tier (`gpt-5.4`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`).
   See decision C2.
3. A non-first-party id: `NonFirstPartyModelError` (decision C13). An id that starts with
   `ft:` (fine-tuned), contains `anthropic.` or ends with `-v1:0`/`-v2:0` (Amazon Bedrock),
   contains `@` (Google Vertex AI), or contains `/` or `:` (a gateway or provider prefix).
   Their prices differ from the first-party API. `non_first_party_kind(model)` is this check.

Why `:` is refused. No first-party Anthropic or OpenAI API id contains `:`, `/` or `@`. A `:`
appears only in OpenAI fine-tune ids (`ft:gpt-4o-mini-2024-07-18:org::id`), Bedrock version
suffixes (`anthropic.claude-sonnet-4-6-v1:0`) and gateway prefixes (`vendor:claude-haiku-4-5`,
`gpt-5:free`). A test passes every id and alias in the table, and every default model pin of
runtime contract v2 (`claude-haiku-4-5` and the Sonnet, Opus and OpenAI ids in the table),
through the check. `PriceTable` refuses to build with an id that the check flags, so pricing
such an id needs a change to the rule first.

Serving. A first-party id can still be served by Bedrock, Vertex AI or a gateway (for example
when the CLI picks a provider from a settings file). The report's `serving` field says who
served it. A report with `serving` other than `first_party` is recorded unpriced with
`non_first_party` (`NonFirstPartyServingError`), whatever its model id; this check runs first.
4. Anything else: `UnknownModelError`.

Not modelled (decision C8): batch, priority, flex and fast modes; Anthropic
`inference_geo: "us"` (1.1x); OpenAI regional processing (+10%); web search requests (C12);
Bedrock, Vertex AI and gateway prices (C13).

### Cost formula

```
one_hour = cache_write_1h_tokens if known, else cache_write_tokens      (decision C1)
cost_usd = (input_tokens × input + output_tokens × output
            + cache_read_tokens × cache_read
            + (cache_write_tokens − one_hour) × cache_write
            + one_hour × cache_write_1h) / 1,000,000
```

When the split is unknown, every cache write is priced at the 1-hour rate. That over-prices
5-minute writes by 0.75x input, which is the safe side for a cap. The Claude adapter sends
the split from `model_usage` (see "Adapter notes"); it is null only when the 5-minute count
is absent. For OpenAI both rates are the input price, so the split changes nothing.

The math is exact Decimal. The result is rounded half up to 9 decimal places
(`COST_QUANTUM = 0.000000001`), which fits `numeric(20, 9)`. Totals are sums of rounded rows.
A cost of 10^11 USD or more does not fit and raises `CostOutOfRangeError`.

`price_usage(provider, model, *, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, cache_write_1h_tokens=None, at=None, prices=PRICE_BOOK)` applies the
formula (first-party serving) to raw counts with the
table in effect at `at`. `cost_usd(report, table)` applies it to a report with a given table
(the ledger passes `table_at(recorded_at)`). Use `price_usage` for a running estimate inside a
segment (see "Spend caps").

### Unpriced usage

Usage is always recorded. When a report cannot be priced, its row gets
`pricing_status = 'unpriced'`, `cost_usd = null` and an `unpriced_reason`. The built default
is decision C11 option (a); see "Unpriced usage: what happens" under "Spend caps". Reason codes (each is the `reason` of an `UnpricedUsageError` subclass;
`UNPRICED_REASONS` in `prices.py` lists them, a subclass with another code fails at import, the
base class cannot be raised, and a test checks that the SQL `unpriced_reason` list matches;
another test checks that the SQL `serving` list matches `Serving`):

| `unpriced_reason` | Cause |
|---|---|
| `unknown_model` | The id is not in the table (`UnknownModelError`). |
| `tiered_pricing` | The model has prompt-length tiers (`TieredPricingError`, C2). |
| `non_first_party` | A Bedrock, Vertex AI, gateway or fine-tuned id (`NonFirstPartyModelError`), or `serving` other than `first_party` (`NonFirstPartyServingError`), C13. |
| `web_search` | `web_search_requests > 0` (`WebSearchNotPricedError`, C12). |
| `cost_out_of_range` | The cost is 10^11 USD or more (`CostOutOfRangeError`). |
| `no_price_table` | No table was in effect at `recorded_at` (`NoPriceTableError`). `price_table_version` is null. |

Call `check_run_models(ledger, tenant, models, now=...)` before a run with every
`(provider, model)` the runtime may call (the run model, fallback model, small fast model and
subagent models). It returns `ModelCheck(refused=...)`; `allowed` is true when nothing is
refused. Each `RefusedModel` has `provider`, `model`, the `reason` code and `seen_unpriced`
(true when this hotel already has unpriced usage of that exact id, from
`CostLedger.unpriced_models(tenant)`). A known-unpriced model is refused before any token is
spent. `check_priced(provider, model, *, at=None, prices=PRICE_BOOK)` is a helper that
checks one model against the table only; the pre-run check is `check_run_models`.
`ModelCheck.failure_code` is `CONFIG_REJECTED` when a model is refused (see "Failure codes").

### SDK cost cross-check

`check_sdk_cost` compares `sdk_reported_cost_usd` with our cost, for priced rows only. If the
difference is larger than `max(0.000001 USD, 1% of our cost)`, it logs a warning on
`harness.costs` with run_id, segment_id, provider, model, both costs and the tolerance. It
never changes the billed value.

### Claude run budget

The per-run cap is computed by the harness from the ledger and the price book, for both
runtimes. The Claude Agent SDK's own budget option uses the SDK's estimate, so it is at most a
backstop set to the same cap. It is never the authority.

## Ledger

`CostLedger` (protocol) has:

- `record(report, *, caps=None) -> RecordResult`. It finds the run's org and hotel through
  `RunTenantLookup.tenant_of(run_id)`. An unknown run raises `UnknownRunError` and nothing is
  stored. In one atomic step it inserts the row (or finds the existing one), computes the
  run's spend and the hotel's spend for the UTC day, and checks the caps in `SpendCaps(run_usd,
  hotel_daily_usd)`. `RecordResult` has `status` (`recorded` | `duplicate`), `row` (the
  stored row; the first one for a duplicate), `run_spend`, `hotel_day_spend`, `run_cap`,
  `hotel_daily_cap`, `stop_run` and `failure_code`. `stop_run` is true when the run has an
  unpriced row or a checked cap denies. `failure_code` is `USAGE_UNPRICED` when the run has
  an unpriced row, else null. For a new unpriced row it raises the alert (C11) after the lock is
  released.
- `run_spend(tenant, run_id)`, `run_rows(tenant, run_id)`
- `unpriced_models(tenant)`: one `UnpricedModel(provider, model, reason, rows,
  last_recorded_at)` per exact id and reason that the hotel ran unpriced
- `hotel_spend(tenant, day=None)`, `org_spend(tenant, day=None)`

A `Spend` is `priced_usd` (the sum of priced rows), `unpriced_rows` (the count of rows
without a cost) and `pricing_gap_runs` (the count of distinct runs that have a row whose
reason is in `UNPRICED_LIMIT_REASONS`, decision C16). Every read takes an explicit
`TenantContext`. Rows of another tenant are
invisible (they add nothing and are not returned), the same as RLS. Days are UTC calendar
days of `recorded_at`. `org_spend` covers every hotel in the caller's org; it is a
control-plane aggregate for an org-level cap.

Every store builds rows with `build_cost_row(report, owner, recorded_at=..., prices=...)`, so
all stores compute the same values. `InMemoryCostLedger` implements the protocol for tests and
local runs with one lock around the insert, the sums and the cap decisions.

### Postgres store (later)

The Postgres store must make record-and-check atomic per hotel, the same as the lock above
(decision C14). In one transaction:

1. `select pg_advisory_xact_lock(hashtextextended('run_costs:' || $org_id || ':' || $hotel_id, 0));`
   This serialises reports of one hotel. Other hotels do not wait.
2. `insert into public.run_costs (...) values (...) on conflict (run_id, segment_id, provider,
   serving, model) do nothing returning *;` If no row returns, select the existing row (a
   duplicate).
3. Sum the run and the hotel's UTC day:
   `select coalesce(sum(cost_usd), 0), count(*) filter (where pricing_status = 'unpriced'),
   count(distinct run_id) filter (where unpriced_reason in ('unknown_model',
   'tiered_pricing')) from public.run_costs where ...`. Under READ COMMITTED each statement takes a new snapshot, so after the lock it
   sees every report of the hotel that committed before.
4. Commit, then return the cap decisions.
5. Raise the unpriced alert (C11) only after the commit succeeds, and only when step 2's
   insert returned a row that is unpriced. A rolled-back transaction or a duplicate insert
   (no row returned) never raises an alert.

Set `recorded_at` from the same clock time that picked the price table, not from the column
default. Read-only checks (before a run or a segment) need no lock.

## Spend caps

`check_spend_cap(cap_usd, spent_usd)` allows only while `spent < cap`. At or over the cap it
denies. A cap of 0 denies everything. A negative, non-finite or float amount is an error.
`check_run_spend(cap_usd, spend, pending_usd=0)` checks `spend.priced_usd + pending_usd` and
denies whenever the run has an unpriced row (reason contains "unpriced", remaining 0,
`unpriced_rows` set). `check_hotel_spend(cap_usd, spend, pending_usd=0)` checks
`spend.priced_usd + pending_usd` only: the hotel daily cap counts priced spend only.
`check_hotel_unpriced_limit` applies the unpriced-run limit of decision C16 and needs no cap
(see below). `check_hotel_daily_cap(ledger, tenant, cap_usd, now=...)` calls it too, and accepts
`cap_usd=None` for a hotel with no daily cap: then it checks only C16, and `cap_usd` and
`remaining_usd` of the result are null.

Every `CapDecision` has `failure_code`: `USAGE_UNPRICED` when the run cap denies because the
run has unpriced usage, `UNPRICED_LIMIT` when the C16 limit denies (it wins when the USD cap
also denies), and null otherwise. A deny with a null code is a USD cap deny.

- After each report: `record(report, caps=SpendCaps(run_usd=..., hotel_daily_usd=...))`. If
  `stop_run` is true, the run stops. A duplicate adds nothing, so a resume cannot push a run
  over its cap.
- Before each segment: `check_run_cap(ledger, tenant, run_id, cap)`,
  `check_hotel_unpriced_limit(ledger, tenant, now=...)` and, when the hotel has a daily cap,
  `check_hotel_daily_cap(ledger, tenant, cap, now=...)`. Before a new run:
  `check_hotel_unpriced_limit` and `check_hotel_daily_cap`. Calling only
  `check_hotel_daily_cap` (with `cap_usd=None` when there is no cap) also covers C16. A deny of
  either blocks new runs for that hotel until the next UTC day.
- During a segment: the same checks with `pending_usd=price_usage(...)` for the tokens the
  open segment has used so far, when the runtime streams usage. A deny stops the run before
  the final report.

### Unpriced usage: what happens

Decision C11. Built default: option (a). When a report is recorded unpriced:

- That run stops at that report (`stop_run`), and its run cap denies from then on, also on
  later days. The run cap need not be set for `stop_run`.
- An alert is raised once per new row (not for a duplicate): an ERROR log event
  `unpriced_usage_alert` on `harness.costs`, and the optional hook
  `InMemoryCostLedger(..., on_unpriced=callback)` gets an `UnpricedUsageAlert(org_id,
  hotel_id, run_id, segment_id, provider, model, reason, recorded_at)`. The alert text
  (`UnpricedUsageAlert.summary`, also in the log event) depends on the reason. For
  `unknown_model` and `tiered_pricing` (`refuses_model` is true) it is "run stopped; model
  refused until priced" (refused for every hotel, see below). For `web_search`, `non_first_party`,
  `cost_out_of_range` and `no_price_table` it is "only this run stopped". The hook is called
  outside the lock. If it raises, the error is logged and the row and `stop_run` are kept.
  DEX-21 notifications connect to this hook.
- The pre-run check (`check_run_models`) refuses that exact model id for every hotel until it
  is priced, because the price table is global: it refuses the id while the price table in
  effect cannot price it. For the hotel that hit it, `seen_unpriced` is
  true.
- The hotel daily cap counts priced spend only. Other runs of the hotel, including runs in
  flight, and other models continue. Other hotels are not blocked; only that model id is
  refused for them too.
- It clears by itself: when a price table that prices the id takes effect (a new
  `effective_from`), `check_run_models` allows it again. No manual step. The old rows stay
  unpriced (rows are immutable).
- Web search (C12) and `serving` other than `first_party` (C13) are exceptions to the
  refusal: the model id is priced, so it is not refused. Only the run that had that usage
  stops.

Limit of the model refusal. `check_run_models` refuses only the ids that the orchestrator
passes to it. An id that the runtime calls but the orchestrator did not pass (an undeclared
model) is admitted on each new run, and each such run stops after one segment, with no USD
bound for that segment. The adapter model pins in runtime contract v2
(https://github.com/nikhilllmno/hermes_like_agent/pull/3, `RunConfig.model_pins`) close this on
the Claude side: the orchestrator passes every pinned id to `check_run_models`.

Unpriced-run limit per hotel (decision C16). Default (chosen by Ember on 2026-11-20 after review round 4; the owner has not answered). When 3 or more
runs of one hotel (`UNPRICED_RUNS_HOTEL_DAILY_LIMIT`) were stopped by a model-pricing gap in
one UTC day, `check_hotel_unpriced_limit` denies that hotel for the rest of the UTC day with
`UNPRICED_LIMIT` (the reason names C16 and contains "unpriced"). A model-pricing gap is an
unpriced row whose reason is in `UNPRICED_LIMIT_REASONS`: `unknown_model` or `tiered_pricing`.
These are the gaps that `check_run_models` cannot catch for an id it was not given.
`web_search`, `non_first_party`, `cost_out_of_range` and `no_price_table` do not count: each
one already stops its own run, and C12 and C13 stay as they are. The count is distinct
`run_id`s (`Spend.pricing_gap_runs`), so several gap rows in one run count once, and a run with
a web search row and a gap row counts once. It clears at the next UTC day. The check needs no
cap, so it applies to a hotel with no daily cap; `check_hotel_daily_cap` calls it too. `record`
does not apply the limit, so its `hotel_daily_cap` stays a USD decision; the orchestrator calls
the check before each segment, so a run in flight in that hotel stops at its next segment
boundary. Other hotels are not affected. The alternatives that still wait for the owner are in
the C16 row of "Owner decisions".

Options for C11, and how each one clears:

| Option | Behaviour | How it clears |
|---|---|---|
| (a) built default | Stop only that run, alert, refuse that exact model id at the pre-run check; the hotel daily cap counts priced spend only | By itself, when the price table in effect prices the id |
| (b) | Stop the run and block the whole hotel (new runs and runs in flight) | Only when an owner or admin clears it with a separate append-only acknowledgement record (cost rows are immutable); needs a new table and an API |
| (c) | Fail open: price unknown usage at an estimate (for example the most expensive known price) and continue | Nothing to clear; not recommended, because it bills wrong amounts and hides the problem |

Worst-case overshoot (decision C14):

- Per run: one segment, because usage is known only at the end of a segment. With in-segment
  estimates, the usage between two estimates.
- Per hotel per day: the segments that are in flight in that hotel's concurrent runs when the
  cap is crossed. The atomic record gives each report a distinct total, so the first report
  that reaches the cap stops its run, and every later report and segment check denies.
- An unpriced row cannot be bounded in USD; that is why it stops the run at once. With C16,
  a hotel has at most 3 unbounded segments per UTC day from model-pricing gaps (plus the
  segments in flight when the third run stops). Runs stopped for the other reasons are not
  limited per hotel: each one stops after its own segment.

### Failure codes (runtime contract v2)

| Situation | Code | Who gives it |
|---|---|---|
| `check_run_models` refuses a model (`ModelCheck.failure_code`) | `CONFIG_REJECTED` | DEX-24 |
| `record` stores or finds an unpriced row in the run (`RecordResult.failure_code`) | `USAGE_UNPRICED` | DEX-24 |
| `price_usage` raises an `UnpricedUsageError` during a segment | `USAGE_UNPRICED` | orchestrator |
| `check_run_cap` denies because the run has unpriced usage (`CapDecision.failure_code`) | `USAGE_UNPRICED` | DEX-24 |
| The C16 limit denies: `check_hotel_unpriced_limit`, or `check_hotel_daily_cap` (`failure_code`) | `UNPRICED_LIMIT` | DEX-24 |
| A USD cap deny: `stop_run` with no unpriced row, `check_run_cap` or `check_hotel_daily_cap` with a null `failure_code` | `BUDGET_EXHAUSTED` | orchestrator |

`USAGE_UNPRICED`, `CONFIG_REJECTED` and `UNPRICED_LIMIT` are constants in `harness.costs`.
`UNPRICED_LIMIT` is new in this PR and is not yet in runtime contract v2
(https://github.com/nikhilllmno/hermes_like_agent/pull/3); the contract's failure-code table
must add it. DEX-24 gives no code for a USD cap deny, because the contract names that code and
the orchestrator owns it.

Wiring the caps into the orchestrator, and where the cap values live, is later work (DEX-14
and DEX-23).

## PROPOSED `run_costs` table

Becomes a migration after PR#5 merges. Do not apply it before then. It follows PR#5's
conventions: forced RLS, deny by default, select-only for `authenticated`, and inserts only through
the service role (no update, delete or truncate grant, so the ledger stays append-only).

### Columns (for the usage dashboard)

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `uuid` | no | Primary key, `gen_random_uuid()` |
| `org_id` | `uuid` | no | Tenant |
| `hotel_id` | `uuid` | no | Tenant; FK `(org_id, hotel_id)` → `hotels (org_id, id)` |
| `run_id` | `uuid` | no | Composite FK `(org_id, hotel_id, run_id)` → `runs (org_id, hotel_id, id)` once DEX-14 adds `runs` |
| `segment_id` | `text` | no | From the report |
| `provider` | `text` | no | `'anthropic'` or `'openai'` |
| `serving` | `text` | no | `'first_party'`, `'bedrock'`, `'vertex'` or `'other'`; not `first_party` only when unpriced |
| `model` | `text` | no | The exact model id the runtime reported (not normalised) |
| `input_tokens` | `bigint` | no | ≥ 0 |
| `output_tokens` | `bigint` | no | ≥ 0 |
| `cache_read_tokens` | `bigint` | no | ≥ 0 |
| `cache_write_tokens` | `bigint` | no | ≥ 0 |
| `cache_write_1h_tokens` | `bigint` | yes | 0 to `cache_write_tokens`; null when the TTL split is unknown (C1) |
| `web_search_requests` | `bigint` | no | ≥ 0, default 0 |
| `pricing_status` | `text` | no | `'priced'` or `'unpriced'` |
| `unpriced_reason` | `text` | yes | A reason code; set only when unpriced |
| `cost_usd` | `numeric(20,9)` | yes | Billed cost from the price table, ≥ 0, not NaN; null only when unpriced |
| `sdk_reported_cost_usd` | `numeric(20,9)` | yes | Cross-check only, never billed; ≥ 0, not NaN; 9 places from Python |
| `price_table_version` | `text` | yes | For example `2026-11-20`; null only for `no_price_table` |
| `recorded_at` | `timestamptz` | no | Picks the price table. Day boundary for caps is UTC. |

Unique key: `(run_id, segment_id, provider, serving, model)`, with the exact model id. The runtime is
not stored; join `runs`.

### SQL

```sql
-- PROPOSED. Becomes a migration after PR#5 (tenancy schema + RLS) merges, and after DEX-14
-- adds public.runs. Design: docs/costs.md.

create table public.run_costs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  hotel_id uuid not null,
  run_id uuid not null,  -- composite FK to public.runs below, after DEX-14
  segment_id text not null check (length(segment_id) between 1 and 200),
  provider text not null check (provider in ('anthropic', 'openai')),
  serving text not null check (serving in ('first_party', 'bedrock', 'vertex', 'other')),
  model text not null check (length(model) between 1 and 200),
  input_tokens bigint not null check (input_tokens >= 0),
  output_tokens bigint not null check (output_tokens >= 0),
  cache_read_tokens bigint not null check (cache_read_tokens >= 0),
  cache_write_tokens bigint not null check (cache_write_tokens >= 0),
  -- The 1-hour TTL part of cache_write_tokens; null when the split is unknown (decision C1).
  cache_write_1h_tokens bigint,
  web_search_requests bigint not null default 0 check (web_search_requests >= 0),
  pricing_status text not null check (pricing_status in ('priced', 'unpriced')),
  unpriced_reason text check (unpriced_reason in (
    'unknown_model', 'tiered_pricing', 'non_first_party', 'web_search',
    'cost_out_of_range', 'no_price_table')),
  -- Postgres orders numeric NaN above every number, so `>= 0` alone accepts it.
  -- Null only for unpriced usage (decision C11): never priced as zero.
  cost_usd numeric(20, 9) check (cost_usd >= 0 and cost_usd <> 'NaN'::numeric),
  -- Already rounded to 9 places in Python, so Postgres does not round it again.
  sdk_reported_cost_usd numeric(20, 9)
    check (sdk_reported_cost_usd >= 0 and sdk_reported_cost_usd <> 'NaN'::numeric),
  price_table_version text check (length(price_table_version) > 0),
  recorded_at timestamptz not null default now(),
  constraint run_costs_usage_key unique (run_id, segment_id, provider, serving, model),
  constraint run_costs_cache_write_1h_within_total check (
    cache_write_1h_tokens >= 0 and cache_write_1h_tokens <= cache_write_tokens
  ),
  constraint run_costs_pricing_consistent check (
    (pricing_status = 'priced' and cost_usd is not null and unpriced_reason is null
      and price_table_version is not null)
    or (pricing_status = 'unpriced' and cost_usd is null and unpriced_reason is not null
      and (price_table_version is null) = (unpriced_reason = 'no_price_table'))
  ),
  -- Only first-party usage is priced (decision C13).
  constraint run_costs_serving_priced check (serving = 'first_party' or pricing_status = 'unpriced'),
  constraint run_costs_hotel_fk foreign key (org_id, hotel_id)
    references public.hotels (org_id, id) on delete cascade
  -- After DEX-14: a cost row must belong to the same tenant as its run. Needs
  -- `unique (org_id, hotel_id, id)` on public.runs.
  -- constraint run_costs_run_fk foreign key (org_id, hotel_id, run_id)
  --   references public.runs (org_id, hotel_id, id) on delete cascade
);

comment on table public.run_costs is
  'Token usage and billed cost per (run, segment, provider, serving, exact model id). '
  'Unpriced rows have no cost and stop the run. Append-only. docs/costs.md.';

-- Daily hotel and org totals for caps and the usage dashboard.
create index run_costs_hotel_day_idx on public.run_costs (org_id, hotel_id, recorded_at);

-- Unpriced rows stop their run and refuse the model (decision C11); find them fast.
create index run_costs_unpriced_idx on public.run_costs (org_id, hotel_id, recorded_at)
  where pricing_status = 'unpriced';

-- Rows are immutable. Deletes happen only through the hotel (or run) cascade. The trigger is a
-- second guard for the table owner, which keeps its privileges.
create function private.prevent_run_costs_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'run_costs rows are immutable'
    using errcode = 'check_violation';
end;
$$;
revoke all on function private.prevent_run_costs_update() from public;

create trigger run_costs_no_update before update on public.run_costs
  for each row execute function private.prevent_run_costs_update();

alter table public.run_costs enable row level security;
alter table public.run_costs force row level security;

-- Append-only even for the control plane: no UPDATE, DELETE or TRUNCATE grant. FK cascades
-- from hotels (and runs) still remove rows, because cascades do not check the caller's grants.
revoke all on table public.run_costs from public, anon, authenticated, service_role;
grant select, insert on table public.run_costs to service_role;
grant select on table public.run_costs to authenticated;

-- Cost is billing data, like plans: org-wide or hotel owners and admins only (decision C7).
-- No insert, update, or delete policy: only the control plane (service role) writes.
create policy run_costs_select_owners_admins on public.run_costs
  for select to authenticated
  using (
    private.has_hotel_role(org_id, hotel_id, array['owner', 'admin']::public.hotel_role[])
  );
```

The migration also needs the PR#5 catalog and isolation tests extended to `run_costs`: RLS
forced, no write policy, a two-org read test, service role cannot update or delete, `NaN`
costs are rejected, an inconsistent priced or unpriced row is rejected, a priced row with
`serving` other than `first_party` is rejected, a `cache_write_1h_tokens` above
`cache_write_tokens` fails `run_costs_cache_write_1h_within_total`, the same model in one
segment with two `serving` values is two rows, and a row whose run belongs to another hotel is
rejected.

## Owner decisions (each with a safe default)

| # | Decision | Safe default (built) | Options |
|---|---|---|---|
| C1 | Cache-write TTL to price | Optional `cache_write_1h_tokens`: a known split prices 5-minute writes at 1.25x input and 1-hour writes at 2x input; an unknown split (null) prices every cache write at the 1-hour rate (over-price, the safe side). Claude, from `model_usage` (segment deltas): when `cacheCreationEphemeral5mInputTokens` is present, `cache_write_1h_tokens = cacheCreationInputTokens - cacheCreationEphemeral5mInputTokens`; when it is absent, `cache_write_1h_tokens` is absent, so every cache write uses the 1-hour rate. Cache writes with no reported lifetime are in `cacheCreationInputTokens` but in neither TTL field, so they get the 1-hour rate; a direct map of `cacheCreationEphemeral1hInputTokens` would give them the 5-minute rate, so the 1h count is not used. OpenAI: both rates equal the input price | Price an unknown split at the 5-minute rate (under-prices 1-hour writes); map the 1h count directly (under-prices writes with no reported lifetime); require the split from every adapter |
| C2 | Models with prompt-length tiers (`claude-haiku-5-5`, `gpt-5.4`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`) | Refuse before the run (`check_run_models` refuses it with `tiered_pricing`; `check_priced` raises `TieredPricingError`); if one still runs, its usage is recorded unpriced and the run stops (C11) | B: price at the higher tier (over-bill); C: price the short tier and cap prompt length in the model policy (DEX-23); D: contract v3 splits reports per tier |
| C3 | Claude Agent SDK small fast model | Pin it to `claude-haiku-4-5` in the adapter: set `ANTHROPIC_SMALL_FAST_MODEL` (it has priority) and `ANTHROPIC_DEFAULT_HAIKU_MODEL`, because Haiku 5.5 is refused by C2. The orchestrator passes it to `check_run_models` before the run (runtime contract v2 `model_pins`) | Accept Haiku 5.5 after C2 changes |
| C4 | Model ids | Key and store the exact id the runtime reported; resolve the price through explicit aliases only. An unknown snapshot id is recorded unpriced (the run stops) until it is added | Strip date suffixes automatically (rejected: a new snapshot can have a new price, and two ids in one segment would collapse to one key) |
| C5 | Day boundary for the hotel daily cap | UTC day | The hotel's time zone, once `hotels` stores it |
| C6 | SDK cost tolerance | Log above max(1%, 0.000001 USD) | Tighter tolerance, or alert instead of log |
| C7 | Who reads `run_costs` | Owners and admins of the hotel or org (billing data) | Every member who can read the hotel (`private.can_read_hotel`) |
| C8 | Pricing modes | Standard first-party only: no batch, flex, priority, fast or regional uplift; web search (C12) and Bedrock, Vertex AI or gateway prices (C13) are not modelled. Cache-write TTL is modelled (C1) | Add a pricing-mode field in contract v3 |
| C9 | Explicit `pydantic` dependency | Not added now: it comes through FastAPI and pydantic-settings, and PR#5 edits `pyproject.toml`/`uv.lock` | Add `pydantic>=2.14,<3` after PR#5 merges |
| C10 | `docs/backlog.md` | Not edited (PR#4 edits it) | Link this PR from the DEX-24 row after PR#4 merges |
| C11 | Usage that cannot be priced | Option (a): record the tokens with `pricing_status = 'unpriced'` and no cost; stop only that run; raise an alert (`unpriced_usage_alert` log event and the `on_unpriced` hook for DEX-21); refuse that exact model id for every hotel until it is priced, because the price table is global: the pre-run check (`check_run_models`) refuses it until the price table in effect prices it (clears by itself). The hotel daily cap counts priced spend only (see "Unpriced usage: what happens") | (b): block the whole hotel until an owner or admin clears it with a separate append-only acknowledgement record; (c): fail open, price at an estimate (not recommended: wrong bills) |
| C12 | Web search requests | Recorded unpriced, so only the run that uses web search stops and an alert is raised (C11); the model is not refused, because its tokens are priced | Price them at the published rate (about 10 USD per 1,000 searches) in a later table |
| C13 | Non-first-party ids and serving (Bedrock, Vertex AI, gateway prefixes, `ft:`, or `serving` other than `first_party`) | Ids refused (`NonFirstPartyModelError`), recorded unpriced if they still run; a report with `serving` other than `first_party` is recorded unpriced (`NonFirstPartyServingError`) and its run stops, but the id is not refused. `:` stays refused, because no priced id has one (see "Prices") | Add provider-specific tables when a hotel needs them |
| C14 | Concurrency of the hotel daily cap | Record, sum and check in one atomic step (a lock in memory; a per-hotel `pg_advisory_xact_lock` in Postgres) and check after each report. Worst case: the segments in flight when the cap is crossed | Reserve an estimated budget before each segment (stricter, more complex) |
| C15 | Price changes | One table per `effective_from` date (UTC); each report uses the table in effect at `recorded_at` and stores its version | Price by the run's start date |
| C16 | Repeated runs stopped by a model-pricing gap in one hotel | Default (chosen by Ember on 2026-11-20 after review round 4; the owner has not answered): when 3 or more runs of a hotel are stopped by unpriced usage with reason `unknown_model` or `tiered_pricing` (`UNPRICED_LIMIT_REASONS`) in one UTC day, `check_hotel_unpriced_limit` denies that hotel for the rest of the UTC day with `UNPRICED_LIMIT`; it needs no daily cap, and `check_hotel_daily_cap` calls it. `web_search`, `non_first_party`, `cost_out_of_range` and `no_price_table` do not count, because each one already stops its own run (C12, C13 unchanged). `UNPRICED_RUNS_HOTEL_DAILY_LIMIT = 3` | Option (a) as review comment r4639844361 wrote it: count only `unknown_model` on an id that was not passed to `check_run_models`; count every unpriced reason; count `unknown_model` only; no limit; a different count; alert only |
