# Request authentication and TenantContext

This page describes how the control-plane API authenticates a request and builds the
`TenantContext` for it. The design authority is
[`architecture.md` §7](architecture.md#7-multi-tenancy-and-isolation-b2b) (tenant model and isolation
layers). Linear: [DEX-12](https://linear.app/dextr-ai/issue/DEX-12/m1-supabase-jwt-auth-in-the-api-and-tenantcontext-at-the-edge).

| | |
|---|---|
| Code | `backend/src/harness/auth/` (`jwks.py`, `tokens.py`, `memberships.py`, `dependencies.py`, `api.py`), `backend/src/harness/tenancy.py` |
| Tests | `backend/tests/test_auth_*.py`, `backend/tests/test_tenancy.py`. Keys are generated in memory; no key is stored in the repository. |
| Config | `HARNESS_AUTH_*` in [`.env.example`](../.env.example). Auth is off until the issuer and the JWKS URL are set. |
| Depends on | Supabase Auth with **asymmetric JWT signing keys** (ES256 or RS256). Membership data from the tenancy schema ([PR #5](https://github.com/nikhilllmno/vorflux/pull/5)) through a `MembershipProvider`. |

## Request flow

1. The web app signs the user in with Supabase Auth (DEX-13) and sends the access token as
   `Authorization: Bearer <token>`.
2. `get_authenticated_user` verifies the token (next section) and gives an `AuthenticatedUser`
   (`user_id` = JWT `sub`, `session_id`).
3. The route path names the tenant: `/v1/orgs/{org_id}/hotels/{hotel_id}/...`.
4. `require_tenant(action)` asks the `MembershipProvider` for the user's role in that hotel of that
   org, compares it with the minimum role for the action, and returns an immutable
   `TenantContext(org_id, hotel_id, actor_id, role)`. Service functions take this context as an
   argument (§7.2); nothing reads tenant scope from globals or tool arguments.

```python
@router.post("/runs")
async def start_run(
    tenant: Annotated[TenantContext, Depends(require_tenant(Action.START_RUN))],
) -> RunOut: ...
```

`GET /v1/orgs/{org_id}/hotels/{hotel_id}/me` returns the caller's `user_id`, `org_id`, `hotel_id`,
and `role`. The org and hotel switcher (DEX-13) uses it.

## Token checks

The API accepts a token only when every check passes (`harness/auth/tokens.py`):

| Check | Rule |
|---|---|
| Shape | Compact JWS (three parts), at most 8192 characters. |
| Algorithm | Header `alg` is in `HARNESS_AUTH_ALGORITHMS`, which may hold only `ES256` and `RS256`. `none` (any case), `HS256` and all other values are refused before a key is looked up. Settings refuse a symmetric or `none` value. |
| Key | Header `kid` names a key in the JWKS, and that key's algorithm equals the header `alg`. An RS256 header never uses an EC key; no header uses a symmetric key. The token cannot choose its key in another way (`jwk`, `jku`, `x5u`, `x5c` headers are ignored). |
| Signature | Verified with the public key from the JWKS. |
| Time | `exp` and `iat` are required. `exp` must be in the future, and `nbf` (if present) and `iat` must not be in the future, each with `HARNESS_AUTH_LEEWAY_SECONDS` (default 10). |
| Issuer | `iss` equals `HARNESS_AUTH_ISSUER` exactly. |
| Audience | `aud` is, or contains, `HARNESS_AUTH_AUDIENCE` (default `authenticated`). |
| Subject | `sub` is a canonical lowercase UUID: the user id, the same value as `auth.uid()` in Postgres. |
| Session type | `role` is `authenticated` (this refuses legacy `anon` and `service_role` JWTs) and `is_anonymous` is not true (this refuses anonymous sign-ins). |

### Signing keys (JWKS)

Supabase publishes the public half of each asymmetric signing key at
`https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`. A new key is published as a
standby key before Supabase Auth signs with it
([Supabase: JWT signing keys](https://supabase.com/docs/guides/auth/signing-keys)).
`JwksCache` (`harness/auth/jwks.py`) follows these rules:

- It fetches the JWKS on first use and again when the copy is older than
  `HARNESS_AUTH_JWKS_CACHE_SECONDS` (default 600, the same as Supabase's edge cache).
- A token with an unknown `kid` causes at most one refetch per
  `HARNESS_AUTH_JWKS_MIN_REFETCH_SECONDS` (default 30). Forged tokens with random kids cannot turn
  the API into a JWKS request amplifier. Concurrent requests share one fetch.
- If a refresh fails, the last good keys stay in use for up to 1 hour after they were fetched.
  With no usable keys (none fetched, too old, or a JWKS that lists no usable key), the API
  returns 503, not 401: the fault is on the server side, not in the user's token.
- It keeps only signature keys of type `RSA` (RS256, at least 2048 bits) and `EC` on P-256
  (ES256). It never uses a symmetric (`oct`) key. A `kid` that is listed twice is ignored.
- The HTTP fetcher uses HTTPS, does not follow redirects, has a 5 second timeout, and refuses a
  document larger than 64 KiB. Settings allow plain HTTP only for `127.0.0.1`, `localhost` or
  `::1` when `HARNESS_ENVIRONMENT` is `local` or `test`.

### Why there is no HS256 fallback

Supabase documents the shared JWT secret as legacy and "not recommended for production", and
the JWKS endpoint does not publish it. An HS256 fallback would put a secret that can mint any
user's token into the API's environment. The harness Supabase project is not created yet
(runbook S1), so it can use asymmetric keys from the start. A project that still signs with the
legacy secret can move to signing keys from the dashboard with no downtime. See decision A1.

## HTTP behaviour

| Status | When | Body |
|---|---|---|
| 401, `WWW-Authenticate: Bearer` | No `Authorization` header, or not `Bearer <token>` | `not authenticated` |
| 401, `WWW-Authenticate: Bearer error="invalid_token"` | The token fails any check | `not authenticated` |
| 403 | No membership covers the hotel, the hotel is not in the org, the org or hotel does not exist, or the role is below the action's minimum | `forbidden` (identical in every case) |
| 422 | A path id is not a UUID (only after the token is valid) | validation error |
| 503 | Auth is not configured, no membership provider is set, the in-memory provider is set outside `local` or `test`, or no usable signing key can be loaded | short reason |

- The token is checked before the path ids. A caller without a valid token always gets 401 and
  learns nothing about orgs or hotels.
- Every denial for a valid token is the same 403, so the response does not tell whether an org or
  a hotel exists. The provider answers all these cases with one query shape.
- Inside an allowed hotel, a resource id that belongs to another tenant still returns 404, not 403
  (§7.4). That rule belongs to each resource route, not to this dependency.
- The reason for a 401 or 403 goes to the server log only.

## Roles

`HotelRole` in `harness/tenancy.py` has the labels of the PR #5 `public.hotel_role` enum, ranked
`viewer` < `operator` < `admin` < `owner`. The effective role in a hotel is the **highest** role of
the user's memberships that cover it: an org-wide row (`hotel_id` null) or a row for that hotel.
Admin rights in one hotel never carry into another hotel. This is the same scope as the PR #5 RLS
helper `private.has_hotel_role`.

Minimum role per action (`min_role_for`, `TenantContext.allows`):

| Action | Minimum role | Source |
|---|---|---|
| `view` | `viewer` | §7.1 |
| `start_run`, `request_change`, `confirm_change` | `operator` | §7.1, D19 |
| `configure_approvals`, `manage_members` | `admin` | §7.1 ("Admins choose which roles may approve"), PR #6 |
| `manage_org` | `owner` | §7.1 |

Approving a hotel operation is not in this table. Its approver roles come from the hotel's
approval policy (DEX-20, PR #6), which admins configure per operation kind.

Our team's roles (`code_reviewer`, `platform`) give no access through `require_tenant`. A
`TenantContext` that has no role allows no action.

## Membership provider

```python
class MembershipProvider(Protocol):
    async def role_for(self, user_id: UUID, org_id: UUID, hotel_id: UUID) -> HotelRole | None: ...
```

- `InMemoryMembershipProvider` is for tests and local development only. It enforces the PR #5
  rules: a membership's hotel belongs to its org, `owner` is org-wide only, and there is one row
  per `(org_id, user_id, hotel_id)`. The API returns 503 if it is set in `staging` or `production`.
- `create_app` sets no provider, so tenant routes return 503 until the Postgres provider exists.
- The provider is read on every request (no cache), so a removed membership takes effect on the
  next request, as in RLS.

## How this joins PR #5 (tenancy schema and RLS)

After PR #5 merges, a follow-up PR adds `PostgresMembershipProvider`. It is a small change because
the role names, the table names and the scope rule above already match PR #5:

```sql
select m.role
from public.memberships m
join public.hotels h on h.org_id = m.org_id and h.id = $3
where m.user_id = $1 and m.org_id = $2 and (m.hotel_id is null or m.hotel_id = $3)
```

- It returns the highest role, or None. The join on `hotels` makes a hotel of another org (and an
  unknown id) give no row, so all denials look the same. The query uses the
  `memberships (user_id)` and `memberships (org_id, hotel_id)` indexes.
- It runs in the control plane with the service role, which bypasses RLS (§4.1). The API checks
  the role in code; RLS is the second guard under it (§7.2).
- **`auth.uid()` alignment.** `TenantContext.actor_id` is the JWT `sub`, which is the value that
  `auth.uid()` returns in Postgres. The API and the RLS policies therefore name the same user. If a
  later route forwards the user's own token to the Data API, RLS applies to the same user and the
  same scope as `require_tenant`.
- `create_app` sets `app.state.membership_provider` to the Postgres provider. The Postgres driver
  becomes a runtime dependency then (PR #5 has `psycopg` as a dev dependency only); see decision A8.
- The PR #5 isolation tests already cover the scope rule in SQL. The follow-up adds a test that
  runs `role_for` against the local stack for the PR #5 two-org world.

## How this joins PR #6 (approvals)

PR #6's approval routes use `get_principal`, which returns 401 until DEX-12. After PR #6 merges, a
follow-up PR:

- Builds PR #6's `Principal` from `get_authenticated_user`: `kind=human` (a Supabase user session
  token is always a person; a run token from DEX-18 must use another issuer or audience, so it
  cannot pass these checks), `memberships` from a new `MembershipProvider.memberships_for(user_id)`,
  and `team_roles` from `public.team_roles` (unexpired rows only).
- Gates each approval route with `require_tenant(Action.VIEW)`, so a non-member gets the same
  403 as here. PR #6's service still makes its own approver check (role in the request snapshot and
  in the current policy, no self-approval of `high` risk). `code_reviewer` approvals
  (`git_push`, `code_change`) come from `team_roles`, not from a hotel membership.
- Makes `harness.approvals.model.HotelRole` an import of `harness.tenancy.HotelRole` (same labels
  and ranks), which closes PR #6's TODO.
- Uses `require_tenant(Action.CONFIGURE_APPROVALS)` (admin) for the route that sets approver roles,
  which matches PR #6's own admin check.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `HARNESS_AUTH_ISSUER` | unset | `https://<project-ref>.supabase.co/auth/v1`. Set it together with the JWKS URL. |
| `HARNESS_AUTH_JWKS_URL` | unset | `<issuer>/.well-known/jwks.json`. HTTPS; plain HTTP only for a local stack. |
| `HARNESS_AUTH_AUDIENCE` | `authenticated` | Supabase user tokens. Cannot be blank. |
| `HARNESS_AUTH_ALGORITHMS` | `["ES256","RS256"]` | JSON list. Only these two values are accepted. |
| `HARNESS_AUTH_LEEWAY_SECONDS` | `10` | 0 to 60. |
| `HARNESS_AUTH_JWKS_CACHE_SECONDS` | `600` | 30 to 86400. |
| `HARNESS_AUTH_JWKS_MIN_REFETCH_SECONDS` | `30` | 1 to 3600. |

None of these values is a secret. An empty variable means "use the default"
(`env_ignore_empty`). Setting them on Fly.io is part of a deploy and follows
[`runbook-deploy.md`](runbook-deploy.md) with owner approval. The `SUPABASE_JWT_ISSUER` placeholder
in `.env.example` is superseded by `HARNESS_AUTH_ISSUER` (decision A9).

Local stack: `supabase start` serves Auth at `http://127.0.0.1:54321/auth/v1`. Set
`HARNESS_ENVIRONMENT=local`, the issuer to that URL, and the JWKS URL to
`http://127.0.0.1:54321/auth/v1/.well-known/jwks.json`. If that JWKS has no keys, the local stack
signs with the legacy HS256 secret and the API refuses its tokens.

## Owner decisions

Each decision has a safe default, which is what this PR builds.

| # | Decision | Default (built) | Options |
|---|---|---|---|
| A1 | HS256 shared-secret fallback | None. ES256 and RS256 only; the hosted project uses asymmetric signing keys. | (b) Accept HS256 with the legacy JWT secret as a Fly secret (Supabase does not recommend it). |
| A2 | Where the request names the tenant | Path: `/v1/orgs/{org_id}/hotels/{hotel_id}/...` (PR #6 routes already use it). | (b) `X-Org-Id` and `X-Hotel-Id` headers. |
| A3 | Status for a denied tenant | One identical 403 for every case (no membership, wrong pair, unknown ids, role too low). Resource ids inside an allowed hotel stay 404 (§7.4). | (b) 404 for every denied tenant. |
| A4 | Effective role with several memberships | The highest covering role (org-wide or this hotel), as PR #5 RLS. | (b) The hotel-scoped row wins over the org-wide row. |
| A5 | Anonymous Supabase sign-ins | Refused (`is_anonymous`). | (b) Accept them; they still need a membership to reach a hotel. |
| A6 | Membership caching | None: one indexed query per request, so removal is immediate. | (b) Cache for up to 30 s. |
| A7 | JWKS timing | Cache 10 min, refetch for an unknown kid at most every 30 s, last good keys for up to 1 h in an outage. After an urgent key revocation, restart the API to drop the cache. | (b) Shorter cache; (c) fail closed at once on a refresh failure. |
| A8 | Postgres driver for the provider (follow-up) | `psycopg` (already in PR #5 as a dev dependency) becomes a runtime dependency. | (b) `asyncpg`. |
| A9 | `SUPABASE_JWT_ISSUER` in `.env.example` | Keep it until PR #5 merges, then remove it in the PR #5 follow-up (removing it now would conflict with PR #5). | (b) Read it as an alias of `HARNESS_AUTH_ISSUER`. |
| A10 | Minimum role per action | The table in "Roles". | Change a row. |
| A11 | Org-level routes (no hotel) | Not in this PR. A later `require_org(action)` uses org-wide memberships only. | — |

## Tests

| File | What it proves |
|---|---|
| `test_auth_tokens.py` | Valid ES256 and RS256 tokens; expired, no `exp`, `nbf` and `iat` in the future, no `iat`; wrong or missing `iss` and `aud`; another key with our `kid`, tampered payload, embedded `jwk` header; malformed and oversized tokens; `alg: none` in any case; HS256 signed with the public key; a symmetric key in the JWKS; header `alg` that does not match the key type or the JWK `alg`; an encryption key; an algorithm outside the allow-list; no `kid`; bad `sub`, non-user `role`, anonymous sessions. |
| `test_auth_jwks.py` | One fetch for many tokens; refetch after the TTL; rotation (a new `kid` causes one refetch and verifies; the old key stays valid while published, and is refused after it is removed); unknown-kid refetch rate limit; 10 concurrent requests share one fetch; outage: 503 with no keys, last good keys up to max stale, retries rate-limited; a JWKS with no usable key is 503 and an unknown kid next to usable keys is 401; malformed documents; only asymmetric signature keys are kept. |
| `test_auth_memberships.py` | In-memory provider: org-wide and hotel-scoped scope, hotel of another org, highest role wins, PR #5 constraints, removal. |
| `test_auth_api.py` | HTTP edge: 200 with the context; 401 for missing, malformed and bad tokens, before any membership check and before path validation; 403 for no membership, cross-org (including another org's hotel under your own org), cross-hotel; identical 403 bodies whether the org or hotel exists or not; role too low looks the same as no membership; hotel admin rights do not carry to other hotels; removal takes effect at once; 503 when auth, the provider or the JWKS is missing, and for the in-memory provider in staging and production. |
| `test_auth_config.py` | Settings defaults, env variable names, empty variables, refused algorithms, blank issuer or audience, issuer and JWKS URL set together, HTTPS rule, the HTTP fetcher (size cap, no redirects) and an end-to-end app that builds its verifier from settings and fetches the JWKS once. |
| `test_tenancy.py` | Role ranks match PR #5, the minimum role per action, a context without a role allows nothing. |
