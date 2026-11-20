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
before a run:      check_priced(model) for every model the runtime may call   ─▶ refuse the run
                   check_hotel_daily_cap                                       ─▶ block until next UTC day
before a segment:  check_run_cap + check_hotel_daily_cap
during a segment:  the same checks with pending_usd = price_usage(tokens so far)
after a segment:   runtime adapter ──UsageReport──▶ CostLedger.record(report, caps=SpendCaps(...))
                     one row per (run, segment, provider, exact model id), tokens always kept
                     priced from the PriceBook table in effect (never from the SDK)
                     or recorded unpriced (no cost) when it cannot be priced
                     run cap + hotel daily cap checked in the same atomic step
                     RecordResult.stop_run ─▶ stop the run
```

## Runtime contract v2: `UsageReport`

Each adapter sends one report per (segment, model id). A segment that calls two model ids
sends two reports. Fields, exactly:

| Field | Type | Rule |
|---|---|---|
| `run_id` | UUID | The harness run. Must exist (the ledger finds its org and hotel). |
| `segment_id` | str | Non-blank, no surrounding whitespace, max 200 chars. Stable across resume. |
| `provider` | `"anthropic"` \| `"openai"` | No other value. |
| `model` | str | The exact model id the runtime reported, unchanged. Max 200 chars. A model without a price is accepted and recorded unpriced. |
| `input_tokens` | int, 0 to 2^63-1 | Input billed at the base input rate (not a cache read or write). |
| `output_tokens` | int, 0 to 2^63-1 | Output, including reasoning or thinking tokens. |
| `cache_read_tokens` | int, 0 to 2^63-1 | Input served from the prompt cache. |
| `cache_write_tokens` | int, 0 to 2^63-1 | Input written to the prompt cache. |
| `web_search_requests` | int, 0 to 2^63-1, optional (default 0) | Server-side web search calls (Anthropic `server_tool_use.web_search_requests`). Optional extension of v2. Any value above 0 is recorded unpriced (decision C12). |
| `sdk_reported_cost_usd` | Decimal, optional | The SDK's own cost. A cross-check only. Normalised, never rejected (see below). |

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
- Anthropic (Claude Agent SDK): the usage fields map one to one: `input_tokens`,
  `output_tokens`, `cache_read_input_tokens` → `cache_read_tokens`,
  `cache_creation_input_tokens` → `cache_write_tokens`,
  `server_tool_use.web_search_requests` → `web_search_requests`. Send one report per key of
  the result message's per-model usage, with that key as `model`. A segment that also used a
  small fast model, or that reported a short id and a dated id, sends one report for each.
- OpenAI (Agents SDK): `input_tokens` INCLUDES cached tokens. Send
  `input_tokens = usage.input_tokens - usage.input_tokens_details.cached_tokens` and
  `cache_read_tokens = cached_tokens`. Send `cache_write_tokens = 0` for the models in the
  table: they have no separate cache-write charge.
- Do not normalise the model id. Prices resolve dated snapshot ids through explicit aliases
  in the table (for example `claude-haiku-4-5-20251001` → `claude-haiku-4-5`).
- On resume, resend a segment with the same `segment_id` and the same model ids. The ledger
  ignores the repeat.

### Idempotency

The key is `(run_id, segment_id, provider, model)`, with `model` exactly as received. Two ids
for one model in one segment (for example `claude-haiku-4-5` and `claude-haiku-4-5-20251001`,
which Claude Code CLI 2.1.327 reports) are two keys and are both billed. A second report with
the same key is a no-op that returns `DUPLICATE`. If the resent token counts differ from the
stored row, the first row stays and a warning that contains "conflict" is logged on the
`harness.costs` logger.

## Prices

`harness/costs/prices.py` holds one `PriceTable` for both providers: version `2026-11-20`,
effective from 2026-11-20. Each entry has four Decimal prices in USD per million tokens:
input, output, cache read and cache write.

- Anthropic source: https://platform.claude.com/docs/en/about-claude/pricing (checked
  2026-11-20). Cache write uses the 5-minute TTL rate (1.25x input).
- OpenAI source: https://developers.openai.com/api/docs/pricing, Standard tier (checked
  2026-11-20). Cache write equals the input price, because these models have "No additional
  cache-write charge" (https://developers.openai.com/api/docs/guides/prompt-caching).

`PRICE_BOOK` (a `PriceBook`) holds every table by `effective_from`. The ledger prices each
report with `PRICE_BOOK.table_at(recorded_at)`: the latest table whose `effective_from` is on
or before the UTC date of the report (decision C15). Two tables with one `effective_from` are
refused. A price change is a new table with a later date; never edit a table that rows
reference. Each row stores `price_table_version`.

`price_usage` and `check_priced` price or check against `PRICE_TABLE` by default; pass
`table=PRICE_BOOK.table_at(now)` to use the table in effect at another moment.

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
   Their prices differ from the first-party API.
4. Anything else: `UnknownModelError`.

Not modelled (decision C8): batch, priority, flex and fast modes; Anthropic
`inference_geo: "us"` (1.1x); OpenAI regional processing (+10%); Anthropic 1-hour cache
writes (2x input); web search requests (C12); Bedrock, Vertex AI and gateway prices (C13).

### Cost formula

```
cost_usd = (input_tokens × input + output_tokens × output
            + cache_read_tokens × cache_read + cache_write_tokens × cache_write) / 1,000,000
```

The math is exact Decimal. The result is rounded half up to 9 decimal places
(`COST_QUANTUM = 0.000000001`), which fits `numeric(20, 9)`. Totals are sums of rounded rows.
A cost of 10^11 USD or more does not fit and raises `CostOutOfRangeError`.

`price_usage(provider, model, *, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, table=PRICE_TABLE)` applies the formula to raw counts. `cost_usd(report,
table)` uses it for a report. Use `price_usage` for a running estimate inside a segment
(see "Spend caps").

### Unpriced usage

Usage is always recorded. When a report cannot be priced, its row gets
`pricing_status = 'unpriced'`, `cost_usd = null` and an `unpriced_reason`, and a warning with
the reason code is logged on `harness.costs`. The run must stop, and the caps fail closed
(decision C11). Reason codes (each is the `reason` of an `UnpricedUsageError` subclass;
`UNPRICED_REASONS` in `prices.py` lists them, a subclass with another code fails at import, the
base class cannot be raised, and a test checks that the SQL `unpriced_reason` list matches):

| `unpriced_reason` | Cause |
|---|---|
| `unknown_model` | The id is not in the table (`UnknownModelError`). |
| `tiered_pricing` | The model has prompt-length tiers (`TieredPricingError`, C2). |
| `non_first_party` | A Bedrock, Vertex AI, gateway or fine-tuned id (`NonFirstPartyModelError`, C13). |
| `web_search` | `web_search_requests > 0` (`WebSearchNotPricedError`, C12). |
| `cost_out_of_range` | The cost is 10^11 USD or more (`CostOutOfRangeError`). |
| `no_price_table` | No table was in effect at `recorded_at` (`NoPriceTableError`). `price_table_version` is null. |

Call `check_priced(provider, model)` before a run for every model the runtime may call (the
run model, fallback model, small fast model and subagent models), so a known-unpriced model
is refused before any token is spent.

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
  `hotel_daily_cap` and `stop_run`. `stop_run` is true when the run has an unpriced row or a
  checked cap denies.
- `run_spend(tenant, run_id)`, `run_rows(tenant, run_id)`
- `hotel_spend(tenant, day=None)`, `org_spend(tenant, day=None)`

A `Spend` is `priced_usd` (the sum of priced rows) and `unpriced_rows` (the count of rows
without a cost). Every read takes an explicit `TenantContext`. Rows of another tenant are
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
   model) do nothing returning *;` If no row returns, select the existing row (a duplicate).
3. Sum the run and the hotel's UTC day:
   `select coalesce(sum(cost_usd), 0), count(*) filter (where pricing_status = 'unpriced')
   from public.run_costs where ...`. Under READ COMMITTED each statement takes a new
   snapshot, so after the lock it sees every report of the hotel that committed before.
4. Commit, then return the cap decisions.

Set `recorded_at` from the same clock time that picked the price table, not from the column
default. Read-only checks (before a run or a segment) need no lock.

## Spend caps

`check_spend_cap(cap_usd, spent_usd)` allows only while `spent < cap`. At or over the cap it
denies. A cap of 0 denies everything. A negative, non-finite or float amount is an error.
`check_spend(cap_usd, spend, pending_usd=0)` checks `spend.priced_usd + pending_usd` and
denies whenever `spend.unpriced_rows > 0` (reason contains "unpriced", remaining 0).

- After each report: `record(report, caps=SpendCaps(run_usd=..., hotel_daily_usd=...))`. If
  `stop_run` is true, the run stops. A duplicate adds nothing, so a resume cannot push a run
  over its cap.
- Before each segment: `check_run_cap(ledger, tenant, run_id, cap)` and
  `check_hotel_daily_cap(ledger, tenant, cap, now=...)`. Before a new run:
  `check_hotel_daily_cap`. A deny of the daily cap blocks new runs for that hotel until the
  next UTC day.
- During a segment: the same checks with `pending_usd=price_usage(...)` for the tokens the
  open segment has used so far, when the runtime streams usage. A deny stops the run before
  the final report.

Unpriced usage fails closed (decision C11): the run that has it stops, and the hotel daily
cap denies for the rest of that UTC day. What the built default does, exactly:

- The run that has the unpriced row stops at that report, and its run cap denies from then on,
  also on later days.
- Only when the caller checks a hotel daily cap (`SpendCaps.hotel_daily_usd` or
  `check_hotel_daily_cap`): every other run of that hotel stops at its next report or segment
  check, including runs already in flight, and new runs are refused. With a run cap only,
  other runs continue.
- The hotel block lasts until 00:00 UTC (05:30 IST). A row at 00:01 UTC blocks for almost 24
  hours.
- Nothing clears it early. Rows are immutable, and a price added later does not reprice the
  row. Other hotels of the org are not affected.

The scope of this block is an open owner decision (C11 options).

Worst-case overshoot (decision C14):

- Per run: one segment, because usage is known only at the end of a segment. With in-segment
  estimates, the usage between two estimates.
- Per hotel per day: the segments that are in flight in that hotel's concurrent runs when the
  cap is crossed. The atomic record gives each report a distinct total, so the first report
  that reaches the cap stops its run, and every later report and segment check denies.
- An unpriced row cannot be bounded in USD; that is why it stops the run at once.

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
| `model` | `text` | no | The exact model id the runtime reported (not normalised) |
| `input_tokens` | `bigint` | no | ≥ 0 |
| `output_tokens` | `bigint` | no | ≥ 0 |
| `cache_read_tokens` | `bigint` | no | ≥ 0 |
| `cache_write_tokens` | `bigint` | no | ≥ 0 |
| `web_search_requests` | `bigint` | no | ≥ 0, default 0 |
| `pricing_status` | `text` | no | `'priced'` or `'unpriced'` |
| `unpriced_reason` | `text` | yes | A reason code; set only when unpriced |
| `cost_usd` | `numeric(20,9)` | yes | Billed cost from the price table, ≥ 0, not NaN; null only when unpriced |
| `sdk_reported_cost_usd` | `numeric(20,9)` | yes | Cross-check only, never billed; ≥ 0, not NaN; 9 places from Python |
| `price_table_version` | `text` | yes | For example `2026-11-20`; null only for `no_price_table` |
| `recorded_at` | `timestamptz` | no | Picks the price table. Day boundary for caps is UTC. |

Unique key: `(run_id, segment_id, provider, model)`, with the exact model id. The runtime is
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
  model text not null check (length(model) between 1 and 200),
  input_tokens bigint not null check (input_tokens >= 0),
  output_tokens bigint not null check (output_tokens >= 0),
  cache_read_tokens bigint not null check (cache_read_tokens >= 0),
  cache_write_tokens bigint not null check (cache_write_tokens >= 0),
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
  constraint run_costs_usage_key unique (run_id, segment_id, provider, model),
  constraint run_costs_pricing_consistent check (
    (pricing_status = 'priced' and cost_usd is not null and unpriced_reason is null
      and price_table_version is not null)
    or (pricing_status = 'unpriced' and cost_usd is null and unpriced_reason is not null
      and (price_table_version is null) = (unpriced_reason = 'no_price_table'))
  ),
  constraint run_costs_hotel_fk foreign key (org_id, hotel_id)
    references public.hotels (org_id, id) on delete cascade
  -- After DEX-14: a cost row must belong to the same tenant as its run. Needs
  -- `unique (org_id, hotel_id, id)` on public.runs.
  -- constraint run_costs_run_fk foreign key (org_id, hotel_id, run_id)
  --   references public.runs (org_id, hotel_id, id) on delete cascade
);

comment on table public.run_costs is
  'Token usage and billed cost per (run, segment, provider, exact model id). Unpriced rows '
  'have no cost and stop the run. Append-only. docs/costs.md.';

-- Daily hotel and org totals for caps and the usage dashboard.
create index run_costs_hotel_day_idx on public.run_costs (org_id, hotel_id, recorded_at);

-- Unpriced rows deny every cap (decision C11); find them fast for the cap check and alerts.
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
costs are rejected, an inconsistent priced or unpriced row is rejected, and a row whose run
belongs to another hotel is rejected.

## Owner decisions (each with a safe default)

| # | Decision | Safe default (built) | Options |
|---|---|---|---|
| C1 | Cache-write TTL to price | Anthropic 5-minute write rate (1.25x input); OpenAI write = input price | Add a TTL field in contract v3 to price 1-hour writes (2x input) |
| C2 | Models with prompt-length tiers (`claude-haiku-5-5`, `gpt-5.4`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`) | Refuse before the run (`check_priced` raises `TieredPricingError`); if one still runs, its usage is recorded unpriced and the run stops (C11) | B: price at the higher tier (over-bill); C: price the short tier and cap prompt length in the model policy (DEX-23); D: contract v3 splits reports per tier |
| C3 | Claude Agent SDK small fast model | Pin it to `claude-haiku-4-5` in the adapter: set `ANTHROPIC_SMALL_FAST_MODEL` (it has priority) and `ANTHROPIC_DEFAULT_HAIKU_MODEL`, because Haiku 5.5 is refused by C2. The orchestrator runs `check_priced` on it before the run | Accept Haiku 5.5 after C2 changes |
| C4 | Model ids | Key and store the exact id the runtime reported; resolve the price through explicit aliases only. An unknown snapshot id is recorded unpriced (the run stops) until it is added | Strip date suffixes automatically (rejected: a new snapshot can have a new price, and two ids in one segment would collapse to one key) |
| C5 | Day boundary for the hotel daily cap | UTC day | The hotel's time zone, once `hotels` stores it |
| C6 | SDK cost tolerance | Log above max(1%, 0.000001 USD) | Tighter tolerance, or alert instead of log |
| C7 | Who reads `run_costs` | Owners and admins of the hotel or org (billing data) | Every member who can read the hotel (`private.can_read_hotel`) |
| C8 | Pricing modes | Standard first-party only: no batch, flex, priority, fast or regional uplift; web search (C12) and Bedrock, Vertex AI or gateway prices (C13) are not modelled | Add a pricing-mode field in contract v3 |
| C9 | Explicit `pydantic` dependency | Not added now: it comes through FastAPI and pydantic-settings, and PR#5 edits `pyproject.toml`/`uv.lock` | Add `pydantic>=2.14,<3` after PR#5 merges |
| C10 | `docs/backlog.md` | Not edited (PR#4 edits it) | Link this PR from the DEX-24 row after PR#4 merges |
| C11 | Usage that cannot be priced | Record the tokens with `pricing_status = 'unpriced'` and no cost; fail closed: the run stops and, where a hotel daily cap is checked, the hotel is blocked until 00:00 UTC (see "Spend caps"). Open owner decision | 1: keep the hotel block until the UTC day ends (built); 2: stop only the run, alert, and refuse that model for the hotel; 3: block the hotel until an owner or admin clears it (needs an acknowledgement record, because cost rows are immutable); rejected: price unknown models at the most expensive known price (fail open, wrong bills) |
| C12 | Web search requests | Recorded unpriced, so a run that uses web search stops (C11) | Price them at the published rate (about 10 USD per 1,000 searches) in a later table |
| C13 | Non-first-party and fine-tuned ids (Bedrock, Vertex AI, gateway prefixes, `ft:`) | Refused (`NonFirstPartyModelError`), recorded unpriced if they still run | Add provider-specific tables when a hotel needs them |
| C14 | Concurrency of the hotel daily cap | Record, sum and check in one atomic step (a lock in memory; a per-hotel `pg_advisory_xact_lock` in Postgres) and check after each report. Worst case: the segments in flight when the cap is crossed | Reserve an estimated budget before each segment (stricter, more complex) |
| C15 | Price changes | One table per `effective_from` date (UTC); each report uses the table in effect at `recorded_at` and stores its version | Price by the run's start date |
