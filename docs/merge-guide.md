# Merge guide: PR #4 to PR #7

Status on 2026-11-21. All four PRs are drafts with green CI (4 jobs each; #5 adds a fifth).
Each PR description has the full detail.

## Merge order and check

Merge in this order: #4, #5, #6, #7. Use a merge commit, as for #1 and #2.

- I merged the four branches locally onto `main` at `17b91f0`, in this order. Each merge was clean.
- Backend checks passed after each step. All five CI jobs passed on the combined result:
  secret scan, backend (584 passed), database (917 RLS tests), web, deploy config.
- No branch needed a rebase. If `main` moves before you merge, ask me to check again.
- Each PR is a draft. Mark it ready, then merge it. Merge the next PR after the last one is on `main`.
- Merge the four PRs as they are. Each "Unblocked after merge" item is a separate follow-up PR,
  opened right after the merge that unblocks it. It never changes a PR branch in this stack.
  No later PR in the stack changes the files that a follow-up edits, so the order stays conflict-free.
- To undo a merged PR: `git revert -m 1 <merge commit>`, then open a PR with the revert.
- Merging changes no hosted database and deploys nothing. CI only validates.

## PR #4: two runtimes ([description](https://github.com/nikhilllmno/vorflux/pull/4))

- Changes: the harness runtimes are the Claude Agent SDK and the OpenAI Agents SDK (design v0.6).
  `RuntimeKind` drops `deep_agents` and `opencode`. The fake runtime moves to the tests.
- Risk: low. Docs, an enum, and test code. No deploy, no data.
- Rollback: revert the PR. Nothing else breaks.
- Open decisions: none. The owner decided the runtimes on 2026-11-18.
- Unblocked after merge (follow-up PR right after this merge):
  - C10 (PR #7): `docs/backlog.md` gets its cost-tracking edits.

## PR #5: tenancy schema and RLS, DEX-10/11 ([description](https://github.com/nikhilllmno/vorflux/pull/5))

- Changes: two migrations add `plans`, `orgs`, `hotels`, `memberships`, `team_roles`, with forced RLS.
  Clients can only read their own org and hotel. Only the control plane writes.
- Adds a fifth CI job, "Database (Supabase migrations, RLS isolation)": it applies the migrations
  to a local stack and runs 917 isolation tests.
- Risk: low in code (3/10). The risk is in the decisions below, which are hard to change after the first push to a hosted project.
- Merging PR #5 changes no hosted database. The first `supabase db push` (runbook 3.1) and
  runbook step S5 (Data API exposure) are separate steps, and each needs your approval.
  Do S5 before the first push. Without S5, the decision 13 default (detection only) is not safe.
- Rollback before the first `supabase db push`: revert the PR. No hosted database has these migrations.
- Rollback after a push (needs your approval): write a new forward migration that drops the 5 tables,
  the `hotel_role` and `team_role` types and the `private` helper functions, and restores the `postgres` default grants.
  Apply it with runbook section 3.1. Last resort: PITR restore (runbook section 5).
- Open decisions (default = what the PR builds):
  1. Clients are read-only; every write goes through the control plane.
  2. Team roles get no data access through RLS.
  3. RLS reads memberships, not custom JWT claims.
  4. Hotel slug is unique across all orgs.
  5. Owner is org-wide only.
  6. A user can have one org-wide and several hotel-scoped memberships in one org.
  7. Only org-wide owners and admins see plan rows.
  8. Hotel-scoped members see the org row.
  9. Admins see memberships in their scope; others see only their own.
  10. Plans allow only the two runtimes; default `claude_agent_sdk`; empty model list allows no model.
  11. `org_id` and `hotel_id` cannot change.
  12. Hard delete; deleting an org cascades; a plan in use cannot be deleted.
  13. `supabase_admin` exposure gap: default (a) detect only. Options: (b) revoke `usage on schema public`
      from `PUBLIC` and grant it back to `authenticated` and `service_role` only, (c) own schema, (d) ask Supabase.
  14. Hotel slug cannot change.
  15. Model-to-runtime check: default (c) in the control plane. Options: (a) `plan_models` table, (b) prefixed model ids.
  16. Add the "Database" job to the required checks (DEX-9). Default: yes.
- Unblocked after merge (follow-up PR right after this merge; PR #4 is already on `main`):
  - Set `PLAN_RUNTIMES = frozenset(kind.value for kind in RuntimeKind)` in
    `backend/tests/rls/test_tenancy_constraints.py`.
  - C9 (PR #7): add `pydantic` as an explicit dependency in `backend/pyproject.toml`.

## PR #6: approval policy engine, DEX-20 ([description](https://github.com/nikhilllmno/vorflux/pull/6))

- Changes: the policy engine says allow, ask or deny for each tool call. A risky call runs only after
  a person with an allowed role approves the exact preview and arguments, once, in a time window.
  Adds the approval inbox API. In-memory stores only; the routes return 401 or 503 until auth (DEX-12).
- Risk: medium-low (4/10). No database, no wiring, no data. Nothing calls it yet.
- Rollback: revert the PR.
- Open decisions (default = what the PR builds):
  1. Self-approval of `high` risk: forbidden; a per-hotel flag can allow it.
  2. The decider needs an allowed role in the request snapshot and in the current policy.
  3. `git_push` and `code_change` always ask; only `code_reviewer` approves.
  4. The harness renders the preview from typed args; the agent note is context only.
  5. The approval SQL stays in `docs/approvals.md` until PR #5 merges.
  6. Use window: 15 min (1 min to 24 h).
  7. No `sandbox_local` kind; native `Bash` or `Write` is denied until registered (DEX-15).
  8. At most 3 retries on a concurrent change, then a conflict.
  9. Only a principal who passes the role check (without the self-approval rule) sees the full card;
     others see a redacted card.
  10. Fixed lists for PMS settings and message channels; a new value is a code change.
  11. A bad stored minimum role reads as `owner` for that kind only.
  12. An unknown target is audited only as a tenant-scoped SHA-256.
  - Open question: can a platform `allow` rule let `git_push` to `harness/*` branches skip code review? Default: no.
- Unblocked after merge (follow-up PR right after this merge; PR #5 is already on `main`):
  - The approval SQL in `docs/approvals.md` becomes a migration. Its 3 tables join
    `TABLES` in `backend/tests/rls/world.py` and the table-coverage test.

## PR #7: cost price table and caps, DEX-24 part 1 ([description](https://github.com/nikhilllmno/vorflux/pull/7))

- Changes: a versioned price table for Anthropic and OpenAI models, a cost ledger that records each
  run segment once, and hard spend caps per run and per hotel per UTC day. Library code only; nothing imports it yet.
- Risk: low (3/10). No migration, no wiring, no dependency change.
- Rollback: revert the PR.
- Open decisions (default = what the PR builds; full table in `docs/costs.md`):
  - C1 Cache writes: use the 5-minute/1-hour split when known; else price all at the 1-hour rate.
  - C2 Tiered-price models: refused before the run.
  - C3 Claude small fast model: pin to `claude-haiku-4-5`.
  - C4 Key cost rows on the exact model id; aliases only for the price.
  - C5 The daily cap uses the UTC day.
  - C6 SDK cost mismatch over max(1%, $0.000001): log only, never billed.
  - C7 Hotel or org owners and admins read `run_costs`.
  - C8 Standard first-party prices only.
  - C9 Add `pydantic` as an explicit dependency after PR #5 (listed under PR #5).
  - C10 Edit `docs/backlog.md` after PR #4 (listed under PR #4).
  - C11 Usage with no price: option (a), stop that run and alert (`unpriced_usage_alert` log event,
    `on_unpriced` hook). The model id is refused for every hotel until priced, because the price table is global.
    The refusal clears by itself when a price table prices the id.
    Options: (b) block the hotel until cleared, (c) estimate (rejected).
  - C12 Web search requests: recorded with no price; that run stops.
  - C13 Non-first-party and fine-tuned ids: refused. A report with `serving` other than
    `first_party` is recorded unpriced and its run stops, but the id is not refused.
  - C14 Daily cap concurrency: atomic record, check after each report.
  - C15 Price changes: one table per `effective_from` date (UTC).
- Already decided (round 4, listed for completeness): C16. A hotel with 3 runs stopped for
  `unknown_model` or `tiered_pricing` in one UTC day is blocked (`UNPRICED_LIMIT`) until 00:00 UTC
  (05:30 IST). Other stop reasons, such as web search, do not count.
- Unblocked after merge: nothing yet. The `run_costs` SQL in `docs/costs.md` also waits for the
  `runs` table (DEX-14). In the PR that adds `runs`, or right after it, `run_costs` becomes a migration.
  It then joins `TABLES` in `backend/tests/rls/world.py` and the table-coverage test.

Reply 'use defaults' to accept every default above and merge in this order. That reply approves the defaults and the four merges only. It does not approve the first `supabase db push`, runbook step S5, or the DEX-9 required-check change (decision 16); each needs its own approval.
