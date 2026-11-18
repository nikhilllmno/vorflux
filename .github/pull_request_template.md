## What and why

<!-- One short paragraph: the problem, and the outcome of this PR. Link the issue. -->

## Hotel and agent impact

- Hotels affected: <!-- all / list of hotel slugs / none -->
- Agents affected: <!-- voice / text / email / harness only -->
- Runtime adapters touched: <!-- claude_agent_sdk / openai_agents_sdk / deep_agents / opencode / none -->

## Safety checklist

- [ ] No secret values in code, tests, fixtures, logs or this description (repo is public).
- [ ] New environment variables are added to `.env.example` by name only.
- [ ] Tenant scope: every new query, job, and tool call is filtered by `org_id` / `hotel_id`.
- [ ] No migration ran against a live database. Migrations ship as files for review.
- [ ] No deploy happened from this PR.
- [ ] Agent-authored change: a human reviewed the diff and the eval results before approval.

## Testing

<!-- Commands you ran and their results. For agent changes, attach the eval run summary. -->

## Rollback

<!-- How to undo this change if it causes a problem after merge. -->
