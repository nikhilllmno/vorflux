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
    async def consume(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck: ...  # one use per tool call
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
   - `consume` is idempotent per tool call. If the caller was cancelled after the store write
     (for example, a timeout), a retry of `consume` for the same approval and the same tool call
     returns the same `args` and `idempotency_key`, with `replayed=True`. The executor must
     dedupe the write on `idempotency_key`, so a replay never writes twice.
   - A replay passes every other check again: the run, the requester, the tool call id, the
     current policy, the proposal hash, and `use_by`. After `use_by`, a replay is refused.
   - `consume` for another tool call, and `check`, refuse a used approval.

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

`ToolCatalog` maps each `(toolset, tool)` to a `ToolSpec` with its `kind`, `risk` and `system`
(`catalog.py`). It is the only source of an action's kind and risk, and of the system name that
the approval card shows ("Beds24"). A hotel operation tool without a system cannot register. An unregistered tool is
denied, whatever its name suggests. Each harness MCP tool pack registers its tools when the
control plane starts. The default registry is empty, so everything is denied until a pack
registers.

## The preview the approver sees

The harness renders the approval card (Section 10.10 steps 1 and 2). Free text from the agent is
never the main content, and the card says which values the harness did not check.

- Each kind that can ask has a strict pydantic proposal type (`preview.py`): `PmsChange`,
  `MessageSend`, `ExternalWrite`, `Delete`, `GitPush` and `CodeChange`.
  - Unknown fields are refused, and values are not coerced.
  - So every argument that the approval binds to appears in the preview.
  - Args that do not fit the proposal type give `deny`.
- The card takes its names from trusted sources, never from the args:
  - The system ("Beds24") comes from the tool registry (`ToolSpec.system`).
  - The target is an id in the args (`target_id`, for example `room-type:dk`). The harness
    shows the label from the hotel config (`HotelDirectory`, scoped by org, hotel and toolset).
    An id that the hotel config does not know gives `deny` with a fixed reason ("target is not
    in the hotel config for ops"). The id is agent input and can hold guest data, so it never
    goes into the reason or the audit log; the `policy.denied` event holds only
    `target_sha256`, a hash of org, hotel, toolset and id. With no hotel directory, every
    targeted action is denied.
  - The rest of a title comes from closed lists or strict patterns, never from free text:
    - `setting` of a PMS change is one of `rate`, `availability`, `min_stay`, `max_stay`,
      `stop_sell`, `closed_to_arrival` and `closed_to_departure`. The card shows a fixed label
      ("minimum stay").
    - `channel` of a message is one of `email`, `sms` and `whatsapp` ("Send an email to 1
      recipient via Beds24").
    - `repository` (`owner/name`) and `branch` of a code action must match a strict pattern
      with no spaces.
    - Any other value gives `deny`. A new setting or channel is a code change.
  - Each card ends with the tool (`Tool: ops/set_rate`).
  - A date must be an ISO date (`2026-12-24`); the card shows it as "24 Dec 2026".
- Values that only the agent reports are marked `verified: false`, and the card labels them
  "(agent reported, not checked)": the old value and the guests affected of a PMS change, and the
  record count and guests affected of a delete. They are left out when the agent gives none.
  `TODO(DEX-19)`: read the old value from the PMS and mark it checked.
- Some values are `sensitive`: message recipients, subject and body, the changes of an external
  write, and every argument of a read. Only a principal who can decide the request sees them
  (see "Who sees what").
- A `read` that a rule makes ask lists every argument.
- `note` is optional agent text. The approver sees it as context only, and it is not bound.
- The approval binds to `proposal_hash` (document version 3), a SHA-256 over canonical JSON of
  these fields:
  - org and hotel;
  - toolset and tool;
  - kind and risk;
  - args;
  - the rendered preview, with the `verified` and `sensitive` flags.
- A change to the args, or to what the approver saw (also a new target label in the hotel
  config), needs a new approval.

Example: args `{"target_id": "room-type:dk", "date": "2026-12-24", "setting": "rate",
"old": 9000, "new": 99, "guests_affected": 0}` on the registered tool `ops/set_rate` (system
Beds24) render as "Change rate of Deluxe King, 24 Dec 2026 in Beds24 / From (agent reported, not
checked): 9000 / To: 99 / Guests affected (agent reported, not checked): 0 / Tool: ops/set_rate".
An agent note that says "9,900" cannot hide the 99. The agent cannot rename the system, the room or
the setting, and `"setting": "rate (TEST ONLY)"` gives `deny`.

### Who sees what

- A principal who can decide the request (the role check of "Roles", without the self-approval
  rule) sees the full card: every value, the args, and the note.
- Every other principal who can see the request (for example a hotel `viewer`, or a hotel member
  on a code request) gets a redacted card: sensitive values read
  "(hidden: only approvers see this)", the args are empty, and the note is left out. The title,
  the status and the fields that are not sensitive stay.
- The API marks a redacted card with `redacted: true`.
- A raised minimum role also hides the card from the roles below it.

## Evaluation order

1. Validate the call (see "Fail closed"). Any failure gives `deny`.
2. Look up the tool in the registry. An unregistered tool gives `deny`.
3. Collect the platform rules and the hotel rules that match the kind and toolset
   (`toolset=None` matches every toolset). The strictest effect wins: `deny` > `ask` > `allow`.
   With no matching rule, `read` is `allow` and every other kind is `ask`.
4. `deny` gives `deny`.
5. `allow` gives `allow` only for a read-only tool below `high` risk. A hotel operation (D19), a
   code action, or a `high` risk action asks. There is no yolo mode and no rule that skips this.
6. Parse the args into the kind's proposal type, look up each target id in the hotel config, and
   render the preview. Args that do not fit the kind, or an unknown target id, give `deny`.
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
  - one use: a used approval passes only for a repeat by the same tool call (a replay);
  - `use_by`;
  - the run, the requester and the tool call id;
  - the current policy and registry (a deny rule added after approval wins);
  - the proposal hash.
- If all pass, `consume` sets `consumed_at` with a guarded compare-and-set on `version` (see
  "Atomic transitions"). Only one caller writes; concurrent callers for the same tool call get
  the same grant as a replay.
- Only the requester can cancel a pending request.
- Audit events:
  - State changes: `approval.requested`, `approval.approved`, `approval.rejected`,
    `approval.expired`, `approval.cancelled`, `approval.consumed` and `approval_policy.updated`.
    The policy event includes the previous value.
  - Denials and refusals: `policy.denied` (from `evaluate` and `request`) and
    `approval.use_refused` (from `consume`, with the reason).
  - A replay: `approval.use_replayed` (from `consume`, with the approval id).
  - Each event has an actor type: `user`, `run` (with the run id) or `system`.
  - The details hold kind, risk, status, run id, tool call id, requester, proposal hash and
    reason. They never hold the args. A reason never quotes an argument value.
- The store writes each state change and its audit event together or not at all
  (`ApprovalStore.add/replace` and `PolicyStore.put` take the event). If the audit write fails,
  the state does not change, so no approval can be used without its audit event.
- A denial or refusal event goes to the `AuditSink` directly. If that write fails, the denial or
  refusal still stands.
- A decision calls `RunResumer.resume(ApprovalResolution)`. A resumer error is logged and does
  not undo the decision. The orchestrator recovers the outcome with
  `ApprovalService.resolution(tenant, approval_id)`, scoped to the requester's run.
  - If the decision call is cancelled while it resumes the run (for example, the HTTP client
    goes away), the decision is already stored and audited, but `resume` may not run. So the
    orchestrator must not rely on `resume` alone. `TODO(DEX-16)`: poll `resolution()` for each
    paused run (on start and on a timer) and resume the run from it.
- Staff operations take the caller's `Principal` (role checks) and an explicit `TenantContext`
  (storage scope). The API builds the `TenantContext` from the path once, at the edge.
- A policy update reads the stored policy once and writes it with a compare-and-set on
  `version`. Two concurrent updates never lose a setting; the loser gets 409.
- `tests/test_approvals_store.py` is the store contract. A future Postgres store must pass it,
  including the round trip of plain strings.
- A policy store parses the stored minimum roles with `parse_approver_min_roles`, which never
  raises. A bad value for a kind means `owner` (the strictest role) for that kind only, and a
  key that is not a hotel operation kind is ignored. So one bad value cannot break the reads of
  the whole hotel, and it never lowers the bar. The SQL check below also refuses such a row.
- A Postgres store writes `preview.redacted()` to `approval_requests.preview`, and the full
  preview, the args and the note to `approval_request_payloads`, in one transaction.

## Atomic transitions

The time-of-use checks and the current-role checks read the hotel policy, then write the approval.
A policy change or a deadline that passes between the read and the write must not let the write
through. So a decision and a use pass a `TransitionGuard` to `ApprovalStore.replace`:

- `policy_version`: the stored policy version that the service assessed, or `NO_STORED_POLICY`
  (-1) when the hotel uses the defaults.
- `deadline`: `expires_at` for a decision, `use_by` for a use.

The store checks both in the same atomic step as the compare-and-set on `version`. It uses its
own clock at the time of the write, not a time that the service read before it awaited I/O. The
result is a `WriteResult`:

| Result | Service action |
|---|---|
| `APPLIED` | Done. |
| `STALE` (another writer changed the row) | Read the row and the policy again and re-check. |
| `POLICY_CHANGED` | Read the row and the policy again and re-check. |
| `DEADLINE_PASSED` | Refuse: `consume` returns "approval expired"; a decision marks the request `expired` and returns 409. |

- The service re-checks at most `MAX_TRANSITION_ATTEMPTS` (3) times. If every attempt loses a
  race, `consume` refuses and a decision returns 409.
- `WriteResult` cannot be used as a bool, so a caller cannot treat a refusal as success by
  mistake.
- `request` and `cancel` do not take a guard. A new request authorises nothing: its decision and
  its use are guarded. A cancel only ends a pending request.
- In memory: the approval store shares the lock of the policy store, so a guarded write and a
  policy write of a hotel never overlap.
- In Postgres (proposed; the store comes after PR #5):
  - A policy write takes `pg_advisory_xact_lock(<hotel key>)` first. A guarded approval write takes
    `pg_advisory_xact_lock_shared(<hotel key>)` first. The lock key is derived from `hotel_id`.
    An advisory lock also covers a hotel with no policy row yet, where a row lock cannot.
  - Then the transaction locks the approval row, and checks the guard in a later statement.
    `clock_timestamp()` is then read after any wait for the row lock. In one statement, Postgres
    can evaluate the deadline before it waits for a row lock that another writer holds, and then
    write after that writer rolls back.

    ```sql
    select version from public.approval_requests
     where id = $1 and org_id = $2 and hotel_id = $3
       for update;

    update public.approval_requests set ... , version = version + 1
     where id = $1 and org_id = $2 and hotel_id = $3 and version = $4
       and coalesce((select p.version from public.approval_policies p
                      where p.org_id = $2 and p.hotel_id = $3), -1) = $5
       and ($6::timestamptz is null or clock_timestamp() < $6);
    ```

  - The transaction keeps both locks until the audit insert commits.
  - If no row changes, the store reads the row version and the policy version in the same
    transaction to return `STALE`, `POLICY_CHANGED` or `DEADLINE_PASSED`.
  - `clock_timestamp()` is the time of the write. `now()` is the start of the transaction and
    is not used.

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
  - the table `public.memberships`;
  - the function `private.can_read_hotel`.
- It follows the PR #5 style:
  - RLS is on and forced in the same migration that creates the table;
  - deny by default;
  - `anon` gets nothing, and `authenticated` gets SELECT through one policy per table;
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
  -- The redacted card that every hotel member may read: {title, fields: [{label, value,
  -- verified, sensitive}]}, sensitive values hidden. The args, the full card and the note are
  -- in approval_request_payloads.
  preview jsonb not null,
  proposal_hash text not null check (proposal_hash like 'sha256:%'),
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
  -- Target of the payload foreign key, so a payload cannot point at another tenant's request.
  constraint approval_requests_tenant_key unique (id, org_id, hotel_id),
  -- Redaction holds in the database: each field has a string value and a boolean
  -- `sensitive`, and a sensitive field holds only the hidden text.
  constraint approval_requests_preview_redacted check (
    coalesce(jsonb_typeof(preview -> 'fields'), 'missing') = 'array'
    and not jsonb_path_exists(
      preview,
      '$.fields[*] ? (!(@.sensitive.type() == "boolean") || !(@.value.type() == "string")
                      || (@.sensitive == true
                          && !(@.value == "(hidden: only approvers see this)")))'
    )
  ),
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

-- What only a principal who can decide sees: the bound args, the full card, and the agent note.
create table public.approval_request_payloads (
  approval_id uuid primary key,
  org_id uuid not null,
  hotel_id uuid not null,
  args jsonb not null check (jsonb_typeof(args) = 'object'),
  preview jsonb not null,           -- the full card, rendered by the harness
  note text check (char_length(note) <= 2000),
  constraint approval_request_payloads_request_fk
    foreign key (approval_id, org_id, hotel_id)
    references public.approval_requests (id, org_id, hotel_id) on delete cascade
);

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
  -- Only hotel operation kinds, and only the minimum roles that the service allows.
  constraint approval_policies_min_roles_check check (
    jsonb_typeof(approver_min_roles) = 'object'
    and not jsonb_path_exists(
      approver_min_roles,
      '$.keyvalue() ? (!(@.key == "message_send" || @.key == "pms_change"
                         || @.key == "external_write" || @.key == "delete")
                       || !(@.value.type() == "string")
                       || !(@.value == "owner" || @.value == "admin" || @.value == "operator"))'
    )
  ),
  constraint approval_policies_hotel_fk
    foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade
);

-- Row level security on and forced, no client privileges (deny by default).
alter table public.approval_requests enable row level security;
alter table public.approval_request_payloads enable row level security;
alter table public.approval_policies enable row level security;
alter table public.approval_requests force row level security;
alter table public.approval_request_payloads force row level security;
alter table public.approval_policies force row level security;

revoke all on table
  public.approval_requests, public.approval_request_payloads, public.approval_policies
  from anon, authenticated;
grant select on table
  public.approval_requests, public.approval_request_payloads, public.approval_policies
  to authenticated;

-- True when the caller holds a hotel role that may decide this request: the role is in the
-- request's approver snapshot and at or above the hotel's current minimum role for the kind
-- (default operator). Enum order is owner < admin < operator < viewer, so "at or above" is <=.
-- Code requests snapshot only 'code_reviewer', which no hotel role matches.
-- The minimum role is mapped, not cast, so a bad stored value means owner and never raises.
create function private.can_see_approval_payload(target_approval_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.approval_requests r
    join public.memberships m
      on m.org_id = r.org_id
     and (m.hotel_id is null or m.hotel_id = r.hotel_id)
    left join public.approval_policies p
      on p.org_id = r.org_id
     and p.hotel_id = r.hotel_id
    where r.id = target_approval_id
      and m.user_id = (select auth.uid())
      and m.role::text = any (r.approver_roles)
      and m.role <= case coalesce(p.approver_min_roles ->> r.kind, 'operator')
        when 'owner' then 'owner'::public.hotel_role
        when 'admin' then 'admin'::public.hotel_role
        when 'operator' then 'operator'::public.hotel_role
        else 'owner'::public.hotel_role
      end
  );
$$;

revoke all on function private.can_see_approval_payload(uuid) from public;
grant execute on function private.can_see_approval_payload(uuid) to authenticated, service_role;

-- Members of the hotel (org-wide or this hotel) read its approval requests, code requests
-- included. Team roles grant no tenant data access here (PR #5); code reviewers read code
-- requests through the control plane.
create policy approval_requests_select_hotel_members on public.approval_requests
  for select to authenticated
  using (private.can_read_hotel(org_id, hotel_id));

-- Only a member who can decide the request reads its args, full card and note.
create policy approval_request_payloads_select_deciders on public.approval_request_payloads
  for select to authenticated
  using (private.can_see_approval_payload(approval_id));

-- Members of the hotel read its approval policy.
create policy approval_policies_select_hotel_members on public.approval_policies
  for select to authenticated
  using (private.can_read_hotel(org_id, hotel_id));

-- No INSERT, UPDATE or DELETE policy on any of these tables.
```

RLS notes:

- RLS is a second guard for reads only. Every write (request, approve, reject, cancel, expire,
  consume, and policy changes) goes through the control plane with the service role. The service
  role has `bypassrls`, so these rules are enforced in application code only:
  - the role checks;
  - the person-only check;
  - the binding checks;
  - the guarded compare-and-set (see "Atomic transitions").
- The store writes each audit event in the same transaction as the state change.
- A hotel member reads the redacted card of every request in their hotel
  (`approval_requests.preview`). Only a member who can decide the request reads its args, full
  card and note (`approval_request_payloads`), the same rule as "Who sees what".
- The control plane writes the request row and its payload row in one transaction.
- `private.can_see_approval_payload` repeats only the role check. It does not apply the
  self-approval rule, because a requester may always reject and so may see the card.
