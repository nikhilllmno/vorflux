# `harness.yaml` reference (schema v1)

Every hotel repo that the harness may change has a `harness.yaml` at its root. It is the contract
between the harness and the repo (D10): which agents live there, which commands check them, how
changes are classified, and which paths need our team or are off limits. Design:
[architecture.md §9.3](architecture.md#93-the-harnessyaml-manifest) and
[§9.9](architecture.md#99-guardrails-summary). Linear: [DEX-30](https://linear.app/dextr-ai/issue/DEX-30).

| Item | Where |
|---|---|
| Pydantic model (the authority) | `backend/src/harness/manifest/schema.py` |
| Validator and readable errors | `backend/src/harness/manifest/validate.py` (`validate_manifest`, `load_manifest`) |
| JSON Schema for editors | `backend/schemas/harness-v1.schema.json` |
| CLI | `cd backend && uv run harness-validate <file> [<file> ...]` |
| Fixtures | `backend/tests/fixtures/manifests/{valid,invalid}/` |

## Rules that always apply

1. **Our team owns the manifest.** `harness.yaml` and every CODEOWNERS file are protected and on
   the list of files the harness never edits. A manifest cannot remove them from either list.
2. **Default branch only.** The harness reads `harness.yaml` from the repo's default branch, never
   from a PR branch, so a change cannot widen its own rules. (The loader that enforces this comes
   with the change-request service, DEX-31.)
3. **Unknown keys are errors**, at every level. A typo never turns into a silent default.
4. **Strict types.** `"30"` is not an integer, `30` is not a string, and `version` must be the
   integer `1`.
5. **Safe YAML only.** Duplicate keys, aliases (`*name`), custom tags, files over 64 KiB, and
   non-UTF-8 text are rejected.

## Example

```yaml
version: 1
layout: standard/v1
hotel: sample-hotel
agents:
  - type: voice
    id: front-desk-voice              # optional; defaults to the type
    platform: vapi
    prompt_dir: prompts/voice
    config_path: vapi/assistant.yaml
    test_assistant: sample-hotel-test
  - type: text
    id: front-desk-text
    platform: own
    runtime: claude_agent_sdk         # optional
    prompt_dir: prompts/text
commands:
  setup: uv sync --locked
  lint: uv run ruff check .
  test: uv run pytest -q
  render: uv run python -m hotel.render --check
  baml_test: uv run baml-cli test     # optional
  eval: uv run python -m evals.run --suite full
  smoke: uv run python -m evals.run --suite smoke   # optional
change_classes:
  content: [kb/**, facts.yaml, policies/**]
  prompt: [prompts/**]
  config: [vapi/**, tools.yaml]
  code: [src/**, baml_src/**, tests/**]
protected_paths: [src/billing/**]     # added to the defaults below
forbidden_paths: [infra/prod/**]      # added to the fixed list below
limits:
  max_changed_files: 30
  max_diff_lines: 800
evals:
  suites:
    front-desk-voice: evals/voice.yaml
    front-desk-text: evals/text.yaml
  max_score_drop: 0.0
deploy:
  owner: target-repo-ci
  staging_check: https://staging.sample-hotel.example.invalid/healthz
```

The example from architecture.md §9.3 is a test fixture
(`valid/architecture-example.yaml`) and must stay valid.

## Keys

| Key | Required | Rule |
|---|---|---|
| `version` | yes | The integer `1`. |
| `layout` | yes | `standard/v1` (architecture.md §9.2). |
| `hotel` | yes | Slug: lowercase letters and digits in words joined by single hyphens, at most 63 characters. |
| `agents` | yes | 1 to 20 agents. See [Agents](#agents). |
| `commands` | yes | `setup`, `lint`, `test`, `render`, `eval` are required; `baml_test` and `smoke` are optional. Each is one line, at most 500 characters, run in the sandbox from the repo root. |
| `change_classes` | yes | Path patterns per class `content`, `prompt`, `config`, `code`. Any other class is an error. At least one class must list a path. |
| `protected_paths` | no | Patterns added to the [default protected paths](#protected-and-forbidden-paths). |
| `forbidden_paths` | no | Patterns added to the [fixed forbidden list](#protected-and-forbidden-paths). |
| `limits` | no | `max_changed_files` 1-200 (default 30), `max_diff_lines` 1-5000 (default 800). |
| `evals` | no | `suites` (agent id to suite file) and `max_score_drop` 0-0.2 (default 0.0). See [Evals](#evals). |
| `deploy` | yes | `owner: target-repo-ci` (the only value: the harness never deploys). Optional `staging_check`: an `https://` URL with no user name or password. |

### Agents

Agent types are pluggable: each type is one Pydantic model in a discriminated union on `type`.
Schema v1 accepts `voice` and `text`. `email` is reserved and gives a clear "deferred" error
(architecture.md §9). Every agent has an optional `id` (a lowercase slug of at most 40
characters that starts with a letter). It defaults to the agent type, and ids must be unique, so
two agents of one type need explicit ids.

| `type` | Keys |
|---|---|
| `voice` | `platform: vapi`, `prompt_dir`, `config_path` (Vapi settings that the repo's CI pushes on merge), `test_assistant` (the assistant in the hotel's Vapi test org). |
| `text` | `platform: own`, `prompt_dir`, optional `runtime`: `claude_agent_sdk`, `openai_agents_sdk`, `deep_agents` or `opencode`. |

`runtime` is the framework of the hotel's own agent. It is not the harness runtime
(`harness.runtimes.RuntimeKind`), and the two lists may differ.

### Paths and patterns

`prompt_dir`, `config_path` and eval suite files are plain paths. `change_classes`,
`protected_paths` and `forbidden_paths` hold glob patterns. Both are relative to the repo root.

- Rejected: absolute paths (`/x`, `C:/x`, `~/x`), backslashes, `.` or `..` segments, empty
  segments (`a//b`, `a/`), control characters, and paths over 255 characters.
- `*` and `?` match inside one directory. `**` must be a whole segment and matches zero or more
  directories: `src/**/migrations/**` matches `src/migrations/1.sql` and `src/db/migrations/1.sql`.
- Patterns are **anchored at the repo root**, unlike gitignore: `*.pem` matches only `key.pem` at
  the root. Write `**/*.pem` for any depth.
- Not supported, so rejected: character classes (`[ab]`), brace expansion (`{a,b}`), and
  negation (`!x`).

### Change classes

The class of a changed file decides which evals run and how the staff diff looks (§9.6). A file
that matches patterns of several classes takes the strictest one (`content` < `prompt` <
`config` < `code`). A file that matches no class is `code`. Change-class patterns match
case-sensitively, so a case trick can only make a file stricter.

### Protected and forbidden paths

A change to a **protected** path always needs our team's review (Q28). These defaults always
apply, and `protected_paths` adds to them:

```text
harness.yaml  CODEOWNERS  docs/CODEOWNERS  .github/**  evals/**
src/**/migrations/**  src/payments/**  src/auth/**
```

The harness **never edits** a forbidden path. This fixed list always applies, and
`forbidden_paths` adds to it:

```text
harness.yaml  CODEOWNERS  .github/CODEOWNERS  docs/CODEOWNERS
**/.env  **/.env.*  **/*.pem  **/*.key
```

Protected and forbidden patterns match case-insensitively, so `Harness.YAML` is also blocked. A
path query that contains `..` or is absolute raises an error instead of returning an answer.

### Evals

Each agent has one eval suite file: `evals.suites.<agent id>` or, by default,
`evals/<type>.yaml`. A suite key must be a declared agent id. A suite file must be a protected
path, so a change cannot weaken the evals that check it (§9.8). `max_score_drop` is the largest
allowed drop of a 0-1 eval score against `main`. The default 0.0 means "no drop" (D18).

## Errors

`validate_manifest` reports every key and value problem at once, each with a path to the key:

```text
$ uv run harness-validate harness.yaml
harness.yaml: hotel: must be a slug: lowercase letters and digits in words joined by single hyphens (got 'Broken Hotel')
harness.yaml: change_classes.secrets: unknown change class 'secrets'; the change classes are content, prompt, config, code
harness.yaml: protected_paths[1]: must not contain '.' or '..' segments (path traversal) (got '../shared/**')
```

Cross-field rules run when every key and value is valid, and are then reported the same way:

```text
harness.yaml: agents[1].id: duplicate agent id 'front-desk' (first used by agents[0]); give each agent a unique id
```

`(yaml)` marks a problem in the YAML text, with its line, and `(root)` marks the document as a
whole. Each issue also has a stable `code`, for example `duplicate_agent_id`, `unsafe_path`,
`unknown_change_class`, `agent_type_deferred`, `extra_forbidden` or `duplicate_key`.

The CLI exits with 0 when every file is valid, 1 when any file has issues, and 2 for a usage error
or a file it cannot read.

## JSON Schema

`backend/schemas/harness-v1.schema.json` lets editors check a manifest as you type, for example
with this first line in `harness.yaml`:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/nikhilllmno/vorflux/main/backend/schemas/harness-v1.schema.json
```

The JSON Schema checks the shape. The cross-field rules (unique ids, eval suites) and the path
safety rules are only in the validator, which is the authority. A test fails when the committed
file differs from the model. To update it:

```bash
cd backend && uv run harness-validate --schema > schemas/harness-v1.schema.json
```

## Not in this version

- Onboarding checks against GitHub (standard layout, branch protection, no App bypass,
  CODEOWNERS covers every protected path) and the default-branch loader. These are the rest of
  DEX-30 and need the GitHub Apps (DEX-29) and the branch-protection spike (DEX-42).
- Per-class eval selection (which suites run for `content` versus `code`), with DEX-34.
- The `email` agent type.
