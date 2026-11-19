# Approvals (DEX-20)

The approval policy engine and the approval service live in `backend/src/harness/approvals/`.
They implement `docs/architecture.md` D19, Sections 4.2, 5.2, 7.1, 7.4 and 10.10.
This is plain Python with typed store interfaces and in-memory stores. Nothing here touches a
database yet. The proposed tables are at the end of this document and are NOT applied.

## ApprovalPolicy: the interface for runtime adapters

Runtime adapters and the harness MCP server call only `harness.approvals.ApprovalPolicy`
(`interface.py`). `ApprovalService` implements it.

```python
class ApprovalPolicy(Protocol):
    def evaluate(self, action: Action) -> Decision: ...            # Allow | RequireApproval | Deny
    def request_approval(self, action: Action, *, call_id: str, summary: str) -> ApprovalRequest: ...
    def check(self, approval_id: UUID, action: Action) -> ApprovalCheck: ...    # read-only
    def consume(self, approval_id: UUID, action: Action) -> ApprovalCheck: ...  # single use
```

`Action` holds the `TenantContext`, `run_id`, `toolset`, `tool`, `kind` (`ActionKind`), `risk`
(`RiskLevel`) and the JSON `payload` that the tool will run with.

Flow for one gated tool call:

1. Call `evaluate(action)`.
   - `Allow`: run the tool.
   - `Deny`: do not run the tool. Return the reason to the model as a tool error.
   - `RequireApproval`: call `request_approval(action, call_id=..., summary=...)`. The summary is
     the plain-language preview for hotel staff. Emit `ApprovalRequired` and pause the run.
2. The orchestrator resumes the run with an `ApprovalResolution` (`approved`, `rejected`,
   `expired` or `cancelled`). The name avoids a clash with the `ApprovalDecision` input that
   DEX-15 plans for the runtime contract.
3. On `approved`, call `consume(approval_id, action)` with the same action. Run the tool only
   when `ok` is True, and only with that exact payload. On any other outcome, do not run the tool.

Fail closed (deny by default):

- `evaluate`, `check` and `consume` never raise. Every failure is `Deny` or `ok=False`.
- An unknown action kind, an unknown risk, a missing tenant, an empty toolset, tool or run id,
  a payload that is not plain JSON, or any exception (for example a broken policy store)
  gives `Deny`. Nothing unknown gives `Allow`.
- If a call raises anyway, the adapter treats the call as a denial.

## Evaluation order

1. Validate the action (see "Fail closed"). Any failure gives `Deny`.
2. Collect the platform rules and the hotel rules that match the kind and toolset
   (`toolset=None` matches every toolset). The strictest effect wins: `deny` > `ask` > `allow`.
   With no matching rule, `read` is `allow` and every other kind is `ask`.
3. `deny` gives `Deny`.
4. `allow` gives `Allow`, except that a hotel operation (D19) or a `high` risk action becomes
   `ask`. There is no yolo mode and no rule that skips this step.
5. `ask` gives `RequireApproval` with the approver roles:
   - code kinds (`git_push`, `code_change`): `code_reviewer` only;
   - hotel operations: the hotel roles at or above the hotel's minimum role for that kind
     (default `operator`).

Hotel rules cannot target code kinds. Only platform rules can.

## Roles

- Hotel roles: `owner` > `admin` > `operator` > `viewer` (Section 7.1).
  `TODO(DEX-10, PR #5)`: align `HotelRole` with the `public.hotel_role` enum.
- Team roles: `code_reviewer` and `platform`. `platform` is break-glass and never a super-user.
  `platform` alone cannot list or approve anything.
- Hotel roles can never approve code. A `code_reviewer` sees and decides code requests only.
- An org-level membership (no hotel) covers every hotel in that org.
- Hotel admins and owners set the minimum approver role per action kind
  (`PUT .../approval-policy/approvers/{kind}`). The minimum cannot be `viewer`, and cannot be set
  for `read` or code kinds.
- A request stores a snapshot of its approver roles. A later policy change does not change who
  can decide a pending request.
- No two-approver rule (Q8, D16). By default the requester may approve their own request.
  A hotel can set `forbid_self_approval_of_high_risk` to block self-approval of `high` risk.

## Lifecycle

`pending` -> `approved` | `rejected` | `expired` | `cancelled`. Every decided state is final.

- Binding (Section 10.10): the request stores `payload_hash`, a SHA-256 over canonical JSON of the
  org, hotel, kind, risk, toolset, tool and payload. `consume` checks the hash, the tenant, the
  run id and the requester. Any change needs a new approval.
- Single use: `consume` sets `consumed_at` with a compare-and-set on `version`. Only one caller
  wins.
- Expiry: `expires_at` = request time + the hotel's `approval_ttl` (default 24 h, bounds 1 min to
  7 days). It is the deadline to decide and to use the approval. An expired request is marked
  `expired` when it is next read or decided, or by `expire_due()`.
- Only the requester can cancel a pending request.
- Every state change writes an audit event: `approval.requested`, `approval.approved`,
  `approval.rejected`, `approval.expired`, `approval.cancelled`, `approval.consumed` and
  `approval_policy.updated`. The details hold kind, risk, status, run id, call id, payload hash and
  reason. They never hold the raw payload.
- A decision calls `RunResumer.resume(ApprovalResolution)`. A resumer error is logged and does
  not undo the decision.

## HTTP API

Prefix: `/v1/orgs/{org_id}/hotels/{hotel_id}`.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/approvals?status=` | The requests the caller can see, oldest first |
| GET | `/approvals/{approval_id}` | One request (no raw payload) |
| POST | `/approvals/{approval_id}/approve` | Body `{"reason": "..."}` (optional) |
| POST | `/approvals/{approval_id}/reject` | Body `{"reason": "..."}` (optional) |
| PUT | `/approval-policy/approvers/{kind}` | Body `{"min_role": "admin"}` |

Errors: 404 for an unknown id or another tenant's id (never 403, Section 7.4), 403 for a member
without the role, 409 for a request that is already decided or expired, 422 for invalid input.

The routes fail closed until they are wired:

- `get_principal` returns 401 until DEX-12 provides authentication.
- The routes return 503 until the app sets `app.state.approval_service`.
- `RunResumer` is a stub until the DEX-16 orchestrator implements it.
- `AuditSink` is in memory until the DEX-24 audit table exists.

## Proposed tables (NOT applied)

This SQL is a proposal for review. It is not in `supabase/migrations/`. It depends on the
PR #5 tenancy schema (`public.orgs`, `public.hotels`, `public.hotel_role`,
`private.has_hotel_role`). The service writes the audit event in the same transaction as the
state change.

```sql
create type public.approval_status as enum
  ('pending', 'approved', 'rejected', 'expired', 'cancelled');

create table public.approval_requests (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  hotel_id uuid not null,
  run_id text not null,
  call_id text not null,
  requester_id uuid not null references auth.users (id),
  toolset text not null,
  tool text not null,
  kind text not null,              -- ActionKind
  risk text not null check (risk in ('low', 'medium', 'high')),
  payload_hash text not null check (payload_hash like 'sha256:%'),
  summary text not null check (char_length(summary) <= 2000),
  approver_roles text[] not null,  -- snapshot: hotel_role values and/or 'code_reviewer'
  status public.approval_status not null default 'pending',
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  decided_by uuid references auth.users (id),
  decided_at timestamptz,
  reason text check (char_length(reason) <= 1000),
  consumed_at timestamptz,
  version integer not null default 0,
  constraint approval_requests_hotel_fk
    foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade,
  constraint approval_requests_decided check
    ((status = 'pending') = (decided_at is null)),
  constraint approval_requests_decider check
    ((status in ('approved', 'rejected')) = (decided_by is not null)),
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
  forbid_self_approval_of_high_risk boolean not null default false,
  version integer not null default 0,
  primary key (org_id, hotel_id),
  constraint approval_policies_hotel_fk
    foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade
);
```

RLS notes:

- Enable and force RLS on both tables, as PR #5 does for the tenancy tables.
- `select` on `approval_requests` for hotel members (`private.can_read_hotel`). Code requests
  (`kind in ('git_push', 'code_change')`) are visible to `code_reviewer` only.
- No direct `insert`, `update` or `delete` for `authenticated`. The service writes through the
  control plane, so the role, binding and compare-and-set checks stay in one place.
- `approval_policies`: `select` for hotel members; writes through the service only, which checks
  `private.has_hotel_role(org_id, hotel_id, array['owner', 'admin'])`.
