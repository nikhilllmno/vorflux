# Approvals (DEX-20)

The approval policy engine and the approval service live in `backend/src/harness/approvals/`.
They implement `docs/architecture.md` D19, Sections 4.2, 5.2, 7.1, 7.4 and 10.10.
This is plain Python with typed store interfaces and in-memory stores. Nothing here touches a
database yet. The proposed tables are at the end of this document and are NOT applied.

## ApprovalPolicy: the interface for runtime adapters

Runtime adapters and the harness MCP server call only `harness.approvals.ApprovalPolicy`
(`interface.py`). `ApprovalService` implements it. Every method is async, and every call is keyed
by the tool call id.

```python
class ApprovalPolicy(Protocol):
    async def evaluate(self, call: ProposedCall) -> Decision: ...  # verdict: allow | ask | deny
    async def request(self, call: ProposedCall, *, note: str | None = None) -> ApprovalRequest: ...
    async def check(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck: ...    # read-only
    async def consume(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck: ...  # single use
```

`ProposedCall` holds the `TenantContext`, `run_id`, `tool_call_id` (the `call_id` of the
runtime's `ToolCall` event), `toolset`, `tool` and the JSON `args`. The caller gives no kind and
no risk: the tool registry sets both. Building a `ProposedCall` with args that are not plain JSON
(a key that is not a string, NaN, a set) raises; treat that as a denial.

Flow for one gated tool call:

1. Call `evaluate(call)`.
   - `allow`: run the tool.
   - `deny`: do not run the tool. Return the reason to the model as a tool error.
   - `ask`: call `request(call, note=...)`. Emit `ApprovalRequired` and pause the run. A second
     `request` for the same tool call returns the same approval.
2. The orchestrator resumes the run with an `ApprovalResolution` (`approved`, `rejected`,
   `expired` or `cancelled`). The name avoids a clash with the `ApprovalDecision` input that
   DEX-15 plans for the runtime contract.
3. On `approved`, the control plane calls `consume(approval_id, call)` at the point where it
   performs the write. Run the tool only when `ok` is True, with `args` and `idempotency_key`
   from the result. On any other outcome, do not run the tool.

**Where `consume` runs.** `consume` runs in the control plane (the harness MCP server or the
operation executor), at the point of the write, never in the sandboxed adapter. The adapter runs
in the sandbox (D4), so a check there enforces nothing. The control plane executes hotel
operations (Section 10.10 step 4).

Fail closed (deny by default):

- `evaluate`, `check` and `consume` never raise. Every failure is `deny` or `ok=False`.
- These calls give `deny`:
  - a missing tenant, run id, tool call id, toolset or tool;
  - a tool that is not in the registry;
  - args that do not fit the proposal type of the tool's kind;
  - any exception, for example a broken policy store.
- Nothing unknown gives `allow`. If a call raises anyway, the adapter treats the call as a denial.

## The tool registry

`ToolCatalog` maps each `(toolset, tool)` to a `ToolSpec` with its `kind` and `risk`
(`catalog.py`). It is the only source of an action's kind and risk. An unregistered tool is
denied, whatever its name suggests. Each harness MCP tool pack registers its tools when the
control plane starts. The default registry is empty, so everything is denied until a pack
registers.

## The preview the approver sees

The harness renders the approval card from the args (Section 10.10 steps 1 and 2). Free text
from the agent is never the main content.

- Each kind that can ask has a strict pydantic proposal type (`preview.py`): `PmsChange`,
  `MessageSend`, `ExternalWrite`, `Delete`, `GitPush` and `CodeChange`.
  - Unknown fields are refused, and values are not coerced.
  - So every argument that the approval binds to appears in the preview.
  - Args that do not fit the proposal type give `deny`.
- A `read` that a rule makes ask lists every argument.
- `note` is optional agent text. The approver sees it as context only, and it is not bound.
- The approval binds to `proposal_hash`, a SHA-256 over canonical JSON of these fields:
  - org and hotel;
  - toolset and tool;
  - kind and risk;
  - args;
  - the rendered preview.
- A change to the args, or to what the approver saw, needs a new approval.

Example: args `{"system": "Beds24", "target": "Deluxe King, 24 Dec 2026", "setting": "rate",
"old": 9000, "new": 99}` render as "Change rate of Deluxe King, 24 Dec 2026 in Beds24 / From:
9000 / To: 99 / Guests affected: 0". An agent note that says "9,900" cannot hide the 99.

## Evaluation order

1. Validate the call (see "Fail closed"). Any failure gives `deny`.
2. Look up the tool in the registry. An unregistered tool gives `deny`.
3. Collect the platform rules and the hotel rules that match the kind and toolset
   (`toolset=None` matches every toolset). The strictest effect wins: `deny` > `ask` > `allow`.
   With no matching rule, `read` is `allow` and every other kind is `ask`.
4. `deny` gives `deny`.
5. `allow` gives `allow` only for a read-only tool below `high` risk. A hotel operation (D19), a
   code action, or a `high` risk action asks. There is no yolo mode and no rule that skips this.
6. Render the preview. Args that do not fit the kind give `deny`.
7. `ask` carries the approver roles:
   - code kinds (`git_push`, `code_change`): `code_reviewer` only;
   - hotel operations: the hotel roles at or above the hotel's minimum role for that kind
     (default `operator`).

Hotel rules cannot target code kinds. Only platform rules can, and they cannot make code allow.

Rules, roles and stored requests are parsed on construction, so a value that a database driver
returns as a plain string keeps its meaning. A stored `"deny"` stays deny. An unknown stored
kind, effect, role or status cannot load, so evaluation denies. An unknown stored risk is read
as `high`.

## Roles

- Hotel roles: `owner` > `admin` > `operator` > `viewer` (Section 7.1).
  `TODO(DEX-10, PR #5)`: align `HotelRole` with the `public.hotel_role` enum.
- Team roles: `code_reviewer` and `platform`. `platform` is break-glass and never a super-user.
  `platform` alone cannot list or approve anything.
- Hotel roles can never approve code. A `code_reviewer` sees and decides code requests only.
  Hotel members also see the code requests of their hotel, but cannot decide them.
- An org-level membership (no hotel) covers every hotel in that org.
- Hotel admins and owners set the minimum approver role per action kind
  (`PUT .../approval-policy/approvers/{kind}`). The minimum cannot be `viewer`, and cannot be set
  for `read` or code kinds.
- A decision needs a role that both the request's snapshot and the current policy allow. So a
  raised minimum role also protects pending requests, and a lowered one does not widen them.
- Only a person decides. `Principal.kind` is `human` (a user session) or `run` (an agent run, an
  MCP tool, or any credential that acts for a requester). A `run` principal can never approve,
  reject or change approver roles, even with the requester's id and roles. DEX-12 must build a
  `human` principal only from a Supabase user JWT. The MCP surface may request and cancel, but
  never decide.
- No two-approver rule (Q8, D16). The requester may approve their own `low` or `medium` risk
  action. By default the requester cannot approve their own `high` risk action
  (`forbid_self_approval_of_high_risk=True`); a hotel can turn this off. The requester may always
  reject.

## Lifecycle

`pending` -> `approved` | `rejected` | `expired` | `cancelled`. Every decided state is final.

- One approval per tool call: `(org_id, hotel_id, run_id, tool_call_id)` is unique.
  - A second `request` with the same proposal returns the existing pending or approved request.
  - A request with different args, or after the approval was used, rejected or expired, is a
    conflict. The agent needs a new tool call.
- Decide deadline: `expires_at` = request time + the hotel's `approval_ttl` (default 24 h,
  bounds 1 min to 7 days). An expired request is marked `expired` when it is next read or
  decided, or by `expire_due()`.
- Use deadline: `use_by` = decision time + the hotel's `use_window` (default 15 min, bounds 1 min
  to 24 h). An approval given just before `expires_at` still has the full use window.
- `consume` checks again at the time of use:
  - the status;
  - single use;
  - `use_by`;
  - the run, the requester and the tool call id;
  - the current policy and registry (a deny rule added after approval wins);
  - the proposal hash.
- If all pass, `consume` sets `consumed_at` with a compare-and-set on `version`. Only one caller
  wins.
- Only the requester can cancel a pending request.
- Audit events:
  - State changes: `approval.requested`, `approval.approved`, `approval.rejected`,
    `approval.expired`, `approval.cancelled`, `approval.consumed` and `approval_policy.updated`.
    The policy event includes the previous value.
  - Denials and refusals: `policy.denied` (from `evaluate` and `request`) and
    `approval.use_refused` (from `consume`, with the reason).
  - Each event has an actor type: `user`, `run` (with the run id) or `system`.
  - The details hold kind, risk, status, run id, tool call id, requester, proposal hash and
    reason. They never hold the args.
- The store writes each state change and its audit event together or not at all
  (`ApprovalStore.add/replace` and `PolicyStore.put` take the event). If the audit write fails,
  the state does not change, so no approval can be used without its audit event.
- A denial or refusal event goes to the `AuditSink` directly. If that write fails, the denial or
  refusal still stands.
- A decision calls `RunResumer.resume(ApprovalResolution)`. A resumer error is logged and does
  not undo the decision. The orchestrator recovers the outcome with
  `ApprovalService.resolution(tenant, approval_id)`, scoped to the requester's run.
- Staff operations take the caller's `Principal` (role checks) and an explicit `TenantContext`
  (storage scope). The API builds the `TenantContext` from the path once, at the edge.
- A policy update reads the stored policy once and writes it with a compare-and-set on
  `version`. Two concurrent updates never lose a setting; the loser gets 409.
- `tests/test_approvals_store.py` is the store contract. A future Postgres store must pass it,
  including the round trip of plain strings.

## HTTP API

Prefix: `/v1/orgs/{org_id}/hotels/{hotel_id}`.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/approvals?status=` | The requests the caller can see, oldest first |
| GET | `/approvals/{approval_id}` | One request, with the rendered `preview` and the agent `note` |
| POST | `/approvals/{approval_id}/approve` | Body `{"reason": "..."}` (optional) |
| POST | `/approvals/{approval_id}/reject` | Body `{"reason": "..."}` (optional) |
| PUT | `/approval-policy/approvers/{kind}` | Body `{"min_role": "admin"}` |

Errors:

- 404 for an unknown id or another tenant's id (never 403, Section 7.4);
- 403 for a member without the role, or a `run` principal;
- 409 for a request that is already decided or expired;
- 422 for invalid input.

The routes fail closed until they are wired:

- `get_principal` returns 401 until DEX-12 provides authentication.
- The routes return 503 until the app sets `app.state.approval_service`.
- `RunResumer` is a stub until the DEX-16 orchestrator implements it.
- `AuditSink` is in memory until the DEX-24 audit table exists.

## Proposed tables (NOT applied)

This SQL is a proposal for review. It is not in `supabase/migrations/`.

- It depends on the PR #5 tenancy schema (https://github.com/nikhilllmno/vorflux/pull/5):
  - the tables `public.orgs` and `public.hotels`, with the `hotels (org_id, id)` unique key;
  - the enum `public.hotel_role`;
  - the functions `private.can_read_hotel` and `private.has_hotel_role`.
- It follows the PR #5 style:
  - RLS is on and forced in the same migration that creates the table;
  - deny by default;
  - `anon` gets nothing, and `authenticated` gets SELECT through one policy;
  - there are no write policies.

```sql
create type public.approval_status as enum
  ('pending', 'approved', 'rejected', 'expired', 'cancelled');

create table public.approval_requests (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  hotel_id uuid not null,
  run_id text not null check (run_id <> ''),
  tool_call_id text not null check (tool_call_id <> ''),
  requester_id uuid not null references auth.users (id),
  toolset text not null,
  tool text not null,
  kind text not null check (kind in ('read', 'git_push', 'code_change', 'message_send',
                                     'pms_change', 'external_write', 'delete')),
  risk text not null check (risk in ('low', 'medium', 'high')),
  args jsonb not null check (jsonb_typeof(args) = 'object'),
  preview jsonb not null,           -- {title, fields: [{label, value}]}, rendered by the harness
  proposal_hash text not null check (proposal_hash like 'sha256:%'),
  note text check (char_length(note) <= 2000),
  approver_roles text[] not null check (cardinality(approver_roles) > 0),
  status public.approval_status not null default 'pending',
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  decided_by uuid references auth.users (id),
  decided_at timestamptz,
  use_by timestamptz,
  reason text check (char_length(reason) <= 1000),
  consumed_at timestamptz,
  version integer not null default 0,
  constraint approval_requests_hotel_fk
    foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade,
  -- One approval per tool call.
  constraint approval_requests_tool_call_key unique (org_id, hotel_id, run_id, tool_call_id),
  constraint approval_requests_decided check
    ((status = 'pending') = (decided_at is null)),
  constraint approval_requests_decider check
    ((status in ('approved', 'rejected')) = (decided_by is not null)),
  constraint approval_requests_use_by check
    ((status = 'approved') = (use_by is not null)),
  constraint approval_requests_consumed check
    (consumed_at is null or status = 'approved')
);

create index approval_requests_inbox
  on public.approval_requests (org_id, hotel_id, status, created_at);

create table public.approval_policies (
  org_id uuid not null references public.orgs (id) on delete cascade,
  hotel_id uuid not null,
  rules jsonb not null default '[]',              -- [{kind, effect, toolset}]
  approver_min_roles jsonb not null default '{}', -- {kind: hotel_role}
  approval_ttl interval not null default interval '24 hours'
    check (approval_ttl between interval '1 minute' and interval '7 days'),
  use_window interval not null default interval '15 minutes'
    check (use_window between interval '1 minute' and interval '24 hours'),
  forbid_self_approval_of_high_risk boolean not null default true,
  version integer not null default 0,
  primary key (org_id, hotel_id),
  constraint approval_policies_hotel_fk
    foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade
);

-- Row level security on and forced, no client privileges (deny by default).
alter table public.approval_requests enable row level security;
alter table public.approval_policies enable row level security;
alter table public.approval_requests force row level security;
alter table public.approval_policies force row level security;

revoke all on table public.approval_requests, public.approval_policies from anon, authenticated;
grant select on table public.approval_requests, public.approval_policies to authenticated;

-- Members of the hotel (org-wide or this hotel) read its approval requests, code requests
-- included. Team roles grant no tenant data access here (PR #5); code reviewers read code
-- requests through the control plane.
create policy approval_requests_select_hotel_members on public.approval_requests
  for select to authenticated
  using (private.can_read_hotel(org_id, hotel_id));

-- Members of the hotel read its approval policy.
create policy approval_policies_select_hotel_members on public.approval_policies
  for select to authenticated
  using (private.can_read_hotel(org_id, hotel_id));

-- No INSERT, UPDATE or DELETE policy on either table.
```

RLS notes:

- RLS is a second guard for reads only. Every write (request, approve, reject, cancel, expire,
  consume, and policy changes) goes through the control plane with the service role. The service
  role has `bypassrls`, so these rules are enforced in application code only:
  - the role checks;
  - the person-only check;
  - the binding checks;
  - the compare-and-set.
- The store writes each audit event in the same transaction as the state change.
- A hotel member can read the args of a request in their hotel. That is the same data that the
  preview shows.
