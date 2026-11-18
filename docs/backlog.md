# Harness backlog

This page connects the milestone plan in [`architecture.md` §12](architecture.md#12-phased-milestone-plan) to the
Linear backlog, and tracks parity with [Hermes Agent](https://github.com/NousResearch/hermes-agent).

| | |
|---|---|
| Linear project | [Vorflux harness](https://linear.app/dextr-ai/project/vorflux-harness-59deac561cc3) (team DEX, "Dextr AI") |
| Created | 2026-11-18 |
| Source of truth | Linear for task status. This page for the parity checklist. Update both when an issue closes. |

## Milestones

Each milestone is a Linear project milestone with the scope and exit criteria from §12. M0 to M2 have
task-level issues (1-3 days each). M3 to M6 have one epic each, which we split into tasks before the
milestone starts.

| Milestone | Scope (short) | Issues | Linear |
|---|---|---|---|
| **M0 — Foundations** | Design doc, skeletons, CI, gitleaks, infra map, backlog | 5 tasks (DEX-5 to DEX-9) | [DEX-5](https://linear.app/dextr-ai/issue/DEX-5/m0-owner-review-of-the-architecture-doc-and-foundations-pr), [DEX-6](https://linear.app/dextr-ai/issue/DEX-6/m0-decide-open-questions-q5-data-residency-and-q7-openai-compatible), [DEX-7](https://linear.app/dextr-ai/issue/DEX-7/m0-one-page-infra-map-and-deploy-runbook-for-the-harness), [DEX-8](https://linear.app/dextr-ai/issue/DEX-8/m0-first-linear-backlog-and-docsbacklogmd-with-a-hermes-parity), [DEX-9](https://linear.app/dextr-ai/issue/DEX-9/m0-branch-protection-on-main-with-required-ci-checks) |
| **M1 — Tenancy and first runtime** | Supabase schema + RLS, auth, sessions and runs, Docker sandbox, Claude Agent SDK adapter, MCP server, approvals, conformance suite | 15 tasks (DEX-10 to DEX-24) | [DEX-10](https://linear.app/dextr-ai/issue/DEX-10/m1-supabase-schema-migrations-for-orgs-hotels-memberships-and-roles), [DEX-11](https://linear.app/dextr-ai/issue/DEX-11/m1-rls-policies-and-two-org-isolation-tests-in-ci), [DEX-12](https://linear.app/dextr-ai/issue/DEX-12/m1-supabase-jwt-auth-in-the-api-and-tenantcontext-at-the-edge), [DEX-13](https://linear.app/dextr-ai/issue/DEX-13/m1-web-sign-in-with-supabase-auth-and-an-orghotel-switcher), [DEX-14](https://linear.app/dextr-ai/issue/DEX-14/m1-sessions-runs-and-run-events-tables-and-api), [DEX-15](https://linear.app/dextr-ai/issue/DEX-15/m1-runtime-contract-v2-resume-approval-decisions-usage-runconfig), [DEX-16](https://linear.app/dextr-ai/issue/DEX-16/m1-orchestrator-and-sandboxprovider-with-a-local-docker-provider), [DEX-17](https://linear.app/dextr-ai/issue/DEX-17/m1-claude-agent-sdk-adapter), [DEX-18](https://linear.app/dextr-ai/issue/DEX-18/m1-harness-mcp-server-with-run-scoped-tokens-and-toolsets-todo-clarify), [DEX-19](https://linear.app/dextr-ai/issue/DEX-19/m1-web-search-and-extract-mcp-tools), [DEX-20](https://linear.app/dextr-ai/issue/DEX-20/m1-approval-policy-engine-and-approval-service), [DEX-21](https://linear.app/dextr-ai/issue/DEX-21/m1-web-session-view-with-streamed-events-and-an-approval-inbox), [DEX-22](https://linear.app/dextr-ai/issue/DEX-22/m1-adapter-conformance-suite-v1-with-a-scripted-model), [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files), [DEX-24](https://linear.app/dextr-ai/issue/DEX-24/m1-audit-log-per-run-cost-and-langfuse-tracing) |
| **M2 — Safe code changes** | Fly Machines, egress proxy, GitHub App, `harness.yaml`, change requests, `harness/*` draft PRs, text-agent evals, skills v1, snapshots | 13 tasks (DEX-25 to DEX-37) | [DEX-25](https://linear.app/dextr-ai/issue/DEX-25/m2-fly-machines-sandbox-provider), [DEX-26](https://linear.app/dextr-ai/issue/DEX-26/m2-sandbox-base-image-built-and-scanned-in-ci), [DEX-27](https://linear.app/dextr-ai/issue/DEX-27/m2-egress-proxy-with-default-deny-and-credential-injection), [DEX-28](https://linear.app/dextr-ai/issue/DEX-28/m2-workspace-snapshots-checkpoints-and-undo), [DEX-29](https://linear.app/dextr-ai/issue/DEX-29/m2-github-app-with-per-hotel-repo-allowlists-and-per-run-tokens), [DEX-30](https://linear.app/dextr-ai/issue/DEX-30/m2-harnessyaml-manifest-schema-and-repo-onboarding-checks), [DEX-31](https://linear.app/dextr-ai/issue/DEX-31/m2-change-request-service-and-risk-classification), [DEX-32](https://linear.app/dextr-ai/issue/DEX-32/m2-coding-runs-on-harness-branches-with-pre-push-checks), [DEX-33](https://linear.app/dextr-ai/issue/DEX-33/m2-draft-prs-with-evidence-and-no-merge-guarantees), [DEX-34](https://linear.app/dextr-ai/issue/DEX-34/m2-text-agent-eval-starter-and-a-sample-text-agent-repo), [DEX-35](https://linear.app/dextr-ai/issue/DEX-35/m2-skills-library-v1-with-progressive-disclosure), [DEX-36](https://linear.app/dextr-ai/issue/DEX-36/m2-context-compaction-fallback-for-long-sessions), [DEX-37](https://linear.app/dextr-ai/issue/DEX-37/m2-web-change-request-view-with-pr-status-and-eval-report) |
| **M3 — Connectors, memory, scheduling, self-improvement** | Slack and Telegram, memory, cron, session search, self-improving skills, browser, opencode adapter, delegation | epic (DEX-38) | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) |
| **M4 — All runtimes and workflows** | OpenAI Agents SDK and Deep Agents adapters, voice and email evals, kanban workflows, curator, goals, MCP catalog | epic (DEX-39) | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) |
| **M5 — Pilot and extension** | Chrome extension, credential vault, audit export, retention, staging deploy, pilot hotel | epic (DEX-40) | [DEX-40](https://linear.app/dextr-ai/issue/DEX-40/m5-epic-pilot-hotel-and-chrome-extension) |
| **M6 — Hardening for more hotels** | SSO, region options, load and chaos tests, security review, onboarding automation | epic (DEX-41) | [DEX-41](https://linear.app/dextr-ai/issue/DEX-41/m6-epic-hardening-for-more-hotels) |

Total: 37 issues. All issues are unassigned and have no due date.

### Labels

The planned labels are `backend`, `web`, `infra`, `runtime`, `security`, `hermes-parity`, and `epic`. The
Linear API key cannot create labels in team DEX (`You are not allowed to create labels in this team.`), so
each issue lists its planned labels in a `Labels (to apply)` line at the top of the description. When a
workspace admin creates the labels, apply them from that line.

## Hermes feature parity

Each row is one Hermes feature from the feature map in
[`architecture.md` §5](architecture.md#5-feature-map-hermes-to-the-harness). The Linear issue that delivers
the feature has the parity id (for example `HP-06`) in its description. A feature with two target
milestones is done only when both parts are done.

Status values: `Not started`, `In progress`, `Done`.

**Parity: 0 / 39 done.**

| Id | Hermes feature | Doc | Target | Linear issue | Status |
|---|---|---|---|---|---|
| HP-01 | Agent loop (replaced by runtimes behind AgentRuntime) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-17](https://linear.app/dextr-ai/issue/DEX-17/m1-claude-agent-sdk-adapter) | Not started |
| HP-02 | Any model, provider switch, fallback providers, credential pools | [§5.1](architecture.md#51-agent-core-and-context) | M1, M4 | [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files), [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-03 | Prompt-cache discipline (frozen per-session prefix) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-15](https://linear.app/dextr-ai/issue/DEX-15/m1-runtime-contract-v2-resume-approval-decisions-usage-runconfig) | Not started |
| HP-04 | Context files (SOUL, AGENTS.md, CLAUDE.md) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files) | Not started |
| HP-05 | Context compression | [§5.1](architecture.md#51-agent-core-and-context) | M2 | [DEX-36](https://linear.app/dextr-ai/issue/DEX-36/m2-context-compaction-fallback-for-long-sessions) | Not started |
| HP-06 | Sessions and persistence (resume, retry, undo lineage) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-14](https://linear.app/dextr-ai/issue/DEX-14/m1-sessions-runs-and-run-events-tables-and-api) | Not started |
| HP-07 | Session search | [§5.1](architecture.md#51-agent-core-and-context) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-08 | Checkpoints and rollback | [§5.1](architecture.md#51-agent-core-and-context) | M2 | [DEX-28](https://linear.app/dextr-ai/issue/DEX-28/m2-workspace-snapshots-checkpoints-and-undo) | Not started |
| HP-09 | Personality (hotel persona in SOUL) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files) | Not started |
| HP-10 | Insights and usage | [§5.1](architecture.md#51-agent-core-and-context) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-11 | Tool registry and toolsets | [§5.2](architecture.md#52-tools) | M1 | [DEX-18](https://linear.app/dextr-ai/issue/DEX-18/m1-harness-mcp-server-with-run-scoped-tokens-and-toolsets-todo-clarify) | Not started |
| HP-12 | Deferred tools / tool search | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-13 | Terminal, process management, file read/write/patch/search | [§5.2](architecture.md#52-tools) | M1 | [DEX-17](https://linear.app/dextr-ai/issue/DEX-17/m1-claude-agent-sdk-adapter) | Not started |
| HP-14 | Terminal backends (sandbox providers) | [§5.2](architecture.md#52-tools) | M2 | [DEX-25](https://linear.app/dextr-ai/issue/DEX-25/m2-fly-machines-sandbox-provider) | Not started |
| HP-15 | Code execution with tool RPC | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-16 | Web search and extract | [§5.2](architecture.md#52-tools) | M1 | [DEX-19](https://linear.app/dextr-ai/issue/DEX-19/m1-web-search-and-extract-mcp-tools) | Not started |
| HP-17 | Browser automation (with credential vault) | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-18 | Vision and image generation | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-19 | TTS and STT (as voice-agent eval tools) | [§5.2](architecture.md#52-tools) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-20 | MCP client, catalog, OAuth | [§5.2](architecture.md#52-tools) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-21 | Dangerous-command approval (policy engine) | [§5.2](architecture.md#52-tools) | M1 | [DEX-20](https://linear.app/dextr-ai/issue/DEX-20/m1-approval-policy-engine-and-approval-service) | Not started |
| HP-22 | Todo list, clarify | [§5.2](architecture.md#52-tools) | M1 | [DEX-18](https://linear.app/dextr-ai/issue/DEX-18/m1-harness-mcp-server-with-run-scoped-tokens-and-toolsets-todo-clarify) | Not started |
| HP-23 | Bounded curated memory | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-24 | Memory providers | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-25 | Skills (SKILL.md, progressive disclosure) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M2 | [DEX-35](https://linear.app/dextr-ai/issue/DEX-35/m2-skills-library-v1-with-progressive-disclosure) | Not started |
| HP-26 | Agent-managed skills (skill_manage) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-27 | Self-improvement nudge (background review) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-28 | Curator (skill lifecycle) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-29 | Skills security scan | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-30 | Cron jobs with delivery | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-31 | Goals, loops, heartbeats | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-32 | Sub-agent delegation | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-33 | Multi-agent board (Kanban) and workflow dispatcher | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-34 | Messaging gateway (Slack and Telegram first) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-35 | DM pairing and channel binding | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-connectors-memory-scheduling-and-self-improvement) | Not started |
| HP-36 | Hooks (outbound signed webhooks) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) | Not started |
| HP-37 | Plugins (internal extension points only) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M1 | [DEX-15](https://linear.app/dextr-ai/issue/DEX-15/m1-runtime-contract-v2-resume-approval-decisions-usage-runconfig) | Not started |
| HP-38 | API server (public REST API; OpenAI-compatible endpoint is open question Q7) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M5 | [DEX-40](https://linear.app/dextr-ai/issue/DEX-40/m5-epic-pilot-hotel-and-chrome-extension) | Not started |
| HP-39 | Web dashboard (Next.js UI only) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M1 | [DEX-21](https://linear.app/dextr-ai/issue/DEX-21/m1-web-session-view-with-streamed-events-and-an-approval-inbox) | Not started |

### Not counted in the parity total

The design defers or skips these Hermes features. They are not in the total above.

| Hermes feature | Decision | Doc |
|---|---|---|
| Computer use | Defer | [§5.2](architecture.md#52-tools) |
| Voice mode for the harness agent | Defer | [§5.2](architecture.md#52-tools) |
| i18n (UI locales) | Defer | [§5.1](architecture.md#51-agent-core-and-context) |
| Mixture of agents | Defer | [§5.4](architecture.md#54-automation-delegation-and-connectors) |
| Skills Hub (customer marketplace) | Skip | [§5.3](architecture.md#53-learning-loop-memory-and-skills) |
| Desktop app and terminal UI | Skip | [§5.4](architecture.md#54-automation-delegation-and-connectors) |
| Model training pipeline (batch trajectories, trajectory compression) | Non-goal | [§2.2](architecture.md#22-non-goals-v1) |

### Additions that are not in Hermes

| Capability | Target | Linear issue |
|---|---|---|
| Chrome extension (Manifest V3, per-action consent) | M5 | [DEX-40](https://linear.app/dextr-ai/issue/DEX-40/m5-epic-pilot-hotel-and-chrome-extension) |
| Multi-tenancy (orgs, hotels, roles, RLS) | M1 | [DEX-10](https://linear.app/dextr-ai/issue/DEX-10/m1-supabase-schema-migrations-for-orgs-hotels-memberships-and-roles), [DEX-11](https://linear.app/dextr-ai/issue/DEX-11/m1-rls-policies-and-two-org-isolation-tests-in-ci), [DEX-12](https://linear.app/dextr-ai/issue/DEX-12/m1-supabase-jwt-auth-in-the-api-and-tenantcontext-at-the-edge) |
| Change-request pipeline (GitHub App, `harness/*` draft PRs, evals) | M2 | [DEX-29](https://linear.app/dextr-ai/issue/DEX-29/m2-github-app-with-per-hotel-repo-allowlists-and-per-run-tokens), [DEX-31](https://linear.app/dextr-ai/issue/DEX-31/m2-change-request-service-and-risk-classification), [DEX-32](https://linear.app/dextr-ai/issue/DEX-32/m2-coding-runs-on-harness-branches-with-pre-push-checks), [DEX-33](https://linear.app/dextr-ai/issue/DEX-33/m2-draft-prs-with-evidence-and-no-merge-guarantees) |
| `harness.yaml` repo manifest | M2 | [DEX-30](https://linear.app/dextr-ai/issue/DEX-30/m2-harnessyaml-manifest-schema-and-repo-onboarding-checks) |
| Egress proxy with credential injection | M2 | [DEX-27](https://linear.app/dextr-ai/issue/DEX-27/m2-egress-proxy-with-default-deny-and-credential-injection) |
| Agent-type evals (voice, text, email) | M2-M4 | [DEX-34](https://linear.app/dextr-ai/issue/DEX-34/m2-text-agent-eval-starter-and-a-sample-text-agent-repo), [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-openai-agents-sdk-and-deep-agents-adapters-and-workflows) |
| Per-hotel budgets and audit log | M1-M3 | [DEX-24](https://linear.app/dextr-ai/issue/DEX-24/m1-audit-log-per-run-cost-and-langfuse-tracing) |

## How to update this page

1. When a Linear issue with a parity id closes, set the row status to `Done` (or `In progress` while open work remains).
2. Update the `Parity: N / total done` line.
3. When an epic is split, replace the epic link in the table with the new task issues.
