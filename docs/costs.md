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
runtime adapter ──UsageReport──▶ CostLedger.record ──▶ one row per (run, segment, provider, model)
 (claude_agent_sdk,             │  price from PRICE_TABLE (never from the SDK)
  openai_agents_sdk)            │  SDK cost compared, mismatch logged
                                ▼
                    check_run_cap (before each segment, after each report)  ─▶ stop the run
                    check_hotel_daily_cap (before a new run)                ─▶ block until next UTC day
```

## Runtime contract v2: `UsageReport`

Each adapter sends one report per (segment, model). A segment that calls two models sends two
reports. Fields, exactly:

| Field | Type | Rule |
|---|---|---|
| `run_id` | UUID | The harness run. Must exist (the ledger finds its org and hotel). |
| `segment_id` | str | Non-blank, no surrounding whitespace, max 200 chars. Stable across resume. |
| `provider` | `"anthropic"` \| `"openai"` | No other value. |
| `model` | str | The exact model id that was called. Must be in the price table. |
| `input_tokens` | int ≥ 0 | Input billed at the base input rate (not a cache read or write). |
| `output_tokens` | int ≥ 0 | Output, including reasoning or thinking tokens. |
| `cache_read_tokens` | int ≥ 0 | Input served from the prompt cache. |
| `cache_write_tokens` | int ≥ 0 | Input written to the prompt cache. |
| `sdk_reported_cost_usd` | Decimal ≥ 0, optional | The SDK's own cost. A cross-check only. |

Validation is strict: token counts must be real ints (not `1.5`, `"10"`, `true` or null),
unknown fields are rejected, and the report is immutable.

### Adapter notes

- The four token buckets are disjoint. Do not count a token twice.
- Anthropic (Claude Agent SDK): the usage fields map one to one: `input_tokens`,
  `output_tokens`, `cache_read_input_tokens` → `cache_read_tokens`,
  `cache_creation_input_tokens` → `cache_write_tokens`. Use the per-model usage of the result
  message, so a segment that also used a small fast model sends a second report.
- OpenAI (Agents SDK): `input_tokens` INCLUDES cached tokens. Send
  `input_tokens = usage.input_tokens - usage.input_tokens_details.cached_tokens` and
  `cache_read_tokens = cached_tokens`. Send `cache_write_tokens = 0` for the models in the
  table: they have no separate cache-write charge.
- Send the model id that the API call used. A dated snapshot id must be in the table as an
  alias (for example `claude-sonnet-4-5-20250929`). An unknown id fails validation; add it to
  `prices.py` before you use the model.
- On resume, resend a segment with the same `segment_id`. The ledger ignores the repeat.

### Idempotency

The key is `(run_id, segment_id, provider, model)`, with `model` normalised to the table's
canonical id (a snapshot id and its alias are one key). A second report with the same key is
a no-op that returns `DUPLICATE`. If the resent token counts differ from the stored row, the
first row stays and a warning is logged on the `harness.costs` logger.

## Prices

`harness/costs/prices.py` holds one `PriceTable` for both providers: version `2026-11-20`,
effective from 2026-11-20. Each entry has four Decimal prices in USD per million tokens:
input, output, cache read and cache write.

- Anthropic source: https://platform.claude.com/docs/en/about-claude/pricing (checked
  2026-11-20). Cache write uses the 5-minute TTL rate (1.25x input).
- OpenAI source: https://developers.openai.com/api/docs/pricing, Standard tier (checked
  2026-11-20). Cache write equals the input price, because these models have "No additional
  cache-write charge" (https://developers.openai.com/api/docs/guides/prompt-caching).

Rules:

- An unknown model raises `UnknownModelError`. It is never priced as zero.
- A model whose price depends on each request's prompt length raises `TieredPricingError`
  (a subclass). The report carries per-segment totals, not per-request prompt lengths, so the
  tier cannot be chosen. These are `claude-haiku-5-5` (over 100,000 input tokens costs 5x)
  and the OpenAI models with a 272K short-context tier (`gpt-5.4`, `gpt-5.5`, `gpt-5.6-*`,
  `gpt-6*`). See decision C2.
- Not modelled: batch, priority, flex and fast modes; Anthropic `inference_geo: "us"` (1.1x);
  OpenAI regional processing (+10%); Anthropic 1-hour cache writes (2x input).
- A price change is a new table version. Each row stores `price_table_version`.

### Cost formula

```
cost_usd = (input_tokens × input + output_tokens × output
            + cache_read_tokens × cache_read + cache_write_tokens × cache_write) / 1,000,000
```

The math is exact Decimal. The result is rounded half up to 9 decimal places
(`COST_QUANTUM = 0.000000001`), which fits `numeric(20, 9)`. Totals are sums of rounded rows.

### SDK cost cross-check

`check_sdk_cost` compares `sdk_reported_cost_usd` with our cost. If the difference is larger
than `max(0.000001 USD, 1% of our cost)`, it logs a warning on `harness.costs` with run_id,
segment_id, provider, model, both costs and the tolerance. It never changes the billed value.

### Claude run budget

The per-run cap is computed by the harness from the ledger and this price table, for both
runtimes. The Claude Agent SDK's own budget option uses the SDK's estimate, so it is at most a
backstop set to the same cap. It is never the authority.

## Ledger

`CostLedger` (protocol) has:

- `record(report) -> RecordStatus` (`recorded` | `duplicate`). It finds the run's org and hotel
  through `RunTenantLookup.tenant_of(run_id)`. An unknown run raises `UnknownRunError` and
  nothing is stored.
- `run_total(tenant, run_id)`, `run_rows(tenant, run_id)`
- `hotel_total(tenant, day=None)`, `org_total(tenant, day=None)`

Every read takes an explicit `TenantContext`. Rows of another tenant are invisible (they add
zero and are not returned), the same as RLS. Days are UTC calendar days of `recorded_at`.
`org_total` sums every hotel in the caller's org; it is a control-plane aggregate for an
org-level cap.

`InMemoryCostLedger` implements the protocol for tests and local runs. A Postgres store will
implement it with `INSERT ... ON CONFLICT (run_id, segment_id, provider, model) DO NOTHING` and
must build rows with `build_cost_row`, so both stores compute the same values.

## Spend caps

`check_spend_cap(cap_usd, spent_usd)` allows only while `spent < cap`. At or over the cap it
denies. A cap of 0 denies everything. A negative, non-finite or float amount is an error.

- Per-run cap: `check_run_cap(ledger, tenant, run_id, cap)`. The runner calls it before each
  segment and after each recorded report. A deny stops the run. A duplicate report adds
  nothing, so a resume cannot push a run over its cap.
- Per-hotel daily cap: `check_hotel_daily_cap(ledger, tenant, cap, now=...)`. The orchestrator
  calls it before it starts a run. A deny blocks new runs for that hotel until the next UTC day.

A run can overshoot its cap by at most one segment, because usage is known only after the
segment. Wiring the caps into the orchestrator, and where the cap values live, is later work
(DEX-14 and DEX-23).

## PROPOSED `run_costs` table

Becomes a migration after PR#5 merges. Do not apply it before then. It follows PR#5's
conventions: forced RLS, deny by default, select-only for `authenticated`, writes only through
the service role.

### Columns (for the usage dashboard)

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `uuid` | no | Primary key, `gen_random_uuid()` |
| `org_id` | `uuid` | no | Tenant |
| `hotel_id` | `uuid` | no | Tenant; FK `(org_id, hotel_id)` → `hotels (org_id, id)` |
| `run_id` | `uuid` | no | FK → `runs (id)` once DEX-14 adds `runs` |
| `segment_id` | `text` | no | From the report |
| `provider` | `text` | no | `'anthropic'` or `'openai'` |
| `model` | `text` | no | Canonical model id from the price table |
| `input_tokens` | `bigint` | no | ≥ 0 |
| `output_tokens` | `bigint` | no | ≥ 0 |
| `cache_read_tokens` | `bigint` | no | ≥ 0 |
| `cache_write_tokens` | `bigint` | no | ≥ 0 |
| `cost_usd` | `numeric(20,9)` | no | Billed cost from the price table, ≥ 0 |
| `sdk_reported_cost_usd` | `numeric(20,9)` | yes | Cross-check only, never billed |
| `price_table_version` | `text` | no | For example `2026-11-20` |
| `recorded_at` | `timestamptz` | no | Default `now()`. Day boundary for caps is UTC. |

Unique key: `(run_id, segment_id, provider, model)`. The runtime is not stored; join `runs`.

### SQL

```sql
-- PROPOSED. Becomes a migration after PR#5 (tenancy schema + RLS) merges, and after DEX-14
-- adds public.runs. Design: docs/costs.md.

create table public.run_costs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  hotel_id uuid not null,
  run_id uuid not null,  -- references public.runs (id) on delete cascade, after DEX-14
  segment_id text not null check (length(segment_id) between 1 and 200),
  provider text not null check (provider in ('anthropic', 'openai')),
  model text not null check (length(model) between 1 and 200),
  input_tokens bigint not null check (input_tokens >= 0),
  output_tokens bigint not null check (output_tokens >= 0),
  cache_read_tokens bigint not null check (cache_read_tokens >= 0),
  cache_write_tokens bigint not null check (cache_write_tokens >= 0),
  cost_usd numeric(20, 9) not null check (cost_usd >= 0),
  sdk_reported_cost_usd numeric(20, 9) check (sdk_reported_cost_usd >= 0),
  price_table_version text not null check (length(price_table_version) > 0),
  recorded_at timestamptz not null default now(),
  constraint run_costs_usage_key unique (run_id, segment_id, provider, model),
  constraint run_costs_hotel_fk foreign key (org_id, hotel_id)
    references public.hotels (org_id, id) on delete cascade
);

comment on table public.run_costs is
  'Billed token cost per (run, segment, provider, model). Append-only. docs/costs.md.';

-- Daily hotel and org totals for caps and the usage dashboard.
create index run_costs_hotel_day_idx on public.run_costs (org_id, hotel_id, recorded_at);

-- Rows are immutable. Deletes happen only through the hotel (or run) cascade.
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

revoke all on table public.run_costs from public, anon, authenticated;
grant all on table public.run_costs to service_role;
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
forced, no write policy, and a two-org read test.

## Owner decisions (each with a safe default)

| # | Decision | Safe default (built) | Options |
|---|---|---|---|
| C1 | Cache-write TTL to price | Anthropic 5-minute write rate (1.25x input); OpenAI write = input price | Add a TTL field in contract v3 to price 1-hour writes (2x input) |
| C2 | Models with prompt-length tiers (`claude-haiku-5-5`, `gpt-5.4`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`) | Refuse: `TieredPricingError`, so the model cannot run unpriced | B: price at the higher tier (over-bill); C: price the short tier and cap prompt length in the model policy (DEX-23); D: contract v3 splits reports per tier |
| C3 | Claude Agent SDK small fast model | Pin it to `claude-haiku-4-5` in the adapter (for example `ANTHROPIC_DEFAULT_HAIKU_MODEL`), because Haiku 5.5 is refused by C2 | Accept Haiku 5.5 after C2 changes |
| C4 | Model ids | Adapters send the called id; unknown snapshot ids fail until added as aliases | Strip date suffixes automatically (risk: a new snapshot with a new price) |
| C5 | Day boundary for the hotel daily cap | UTC day | The hotel's time zone, once `hotels` stores it |
| C6 | SDK cost tolerance | Log above max(1%, 0.000001 USD) | Tighter tolerance, or alert instead of log |
| C7 | Who reads `run_costs` | Owners and admins of the hotel or org (billing data) | Every member who can read the hotel (`private.can_read_hotel`) |
| C8 | Pricing modes | Standard only: no batch, flex, priority, fast or regional uplift | Add a pricing-mode field in contract v3 |
| C9 | Explicit `pydantic` dependency | Not added now: it comes through FastAPI and pydantic-settings, and PR#5 edits `pyproject.toml`/`uv.lock` | Add `pydantic>=2.14,<3` after PR#5 merges |
| C10 | `docs/backlog.md` | Not edited (PR#4 edits it) | Link this PR from the DEX-24 row after PR#4 merges |
