"""Behaviour contract of the harness.yaml manifest, schema v1 (DEX-30).

Spec: docs/architecture.md, Sections 9.3 and 9.9, and docs/harness-yaml.md.
"""

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

from harness.manifest import (
    DEFAULT_PROTECTED_PATHS,
    HARNESS_NEVER_EDITS,
    ChangeClass,
    Manifest,
    ManifestValidationError,
    ProductRuntime,
    load_manifest,
    manifest_json_schema,
    validate_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "manifests"
SCHEMA_FILE = Path(__file__).parents[1] / "schemas" / "harness-v1.schema.json"

MINIMAL: dict[str, Any] = {
    "version": 1,
    "layout": "standard/v1",
    "hotel": "minimal-hotel",
    "agents": [{"type": "text", "platform": "own", "prompt_dir": "prompts/text"}],
    "commands": {
        "setup": "uv sync --locked",
        "lint": "uv run ruff check .",
        "test": "uv run pytest -q",
        "render": "uv run python -m hotel.render --check",
        "eval": "uv run python -m evals.run --suite full",
    },
    "change_classes": {"content": ["kb/**"]},
    "deploy": {"owner": "target-repo-ci"},
}

VOICE_AGENT: dict[str, Any] = {
    "type": "voice",
    "platform": "vapi",
    "prompt_dir": "prompts/voice",
    "config_path": "vapi/assistant.yaml",
    "test_assistant": "minimal-hotel-test",
}


def manifest_with(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(MINIMAL)
    data.update(copy.deepcopy(overrides))
    return data


def issue_paths(source: Any) -> list[str]:
    result = validate_manifest(source)
    assert not result.ok
    assert result.manifest is None
    return [issue.path for issue in result.issues]


def parse(data: dict[str, Any]) -> Manifest:
    result = validate_manifest(data)
    assert result.ok, [str(issue) for issue in result.issues]
    assert result.manifest is not None
    return result.manifest


# --- Valid manifests -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["architecture-example", "sample-hotel", "minimal"])
def test_valid_fixtures_pass(name: str) -> None:
    result = validate_manifest((FIXTURES / "valid" / f"{name}.yaml").read_text())
    assert result.ok, [str(issue) for issue in result.issues]
    assert result.issues == ()


def test_sample_hotel_is_parsed_into_typed_agents() -> None:
    manifest = load_manifest(FIXTURES / "valid" / "sample-hotel.yaml")

    assert manifest.hotel == "sample-hotel"
    voice, text = manifest.agents
    assert (voice.type, voice.id, voice.platform) == ("voice", "front-desk-voice", "vapi")
    assert voice.test_assistant == "sample-hotel-test"
    assert (text.type, text.id, text.platform) == ("text", "front-desk-text", "own")
    assert text.runtime is ProductRuntime.CLAUDE_AGENT_SDK
    assert manifest.commands.smoke == "uv run python -m evals.run --suite smoke"
    assert manifest.deploy.staging_check == "https://staging.sample-hotel.example.invalid/healthz"


def test_minimal_manifest_takes_safe_defaults() -> None:
    manifest = load_manifest(FIXTURES / "valid" / "minimal.yaml")

    (agent,) = manifest.agents
    assert agent.id == "text"  # an agent without an id uses its type as the id
    assert agent.runtime is None
    assert manifest.commands.baml_test is None
    assert manifest.commands.smoke is None
    assert (manifest.limits.max_changed_files, manifest.limits.max_diff_lines) == (30, 800)
    assert manifest.evals.max_score_drop == 0.0
    assert manifest.eval_suite_for("text") == "evals/text.yaml"
    assert manifest.deploy.staging_check is None
    assert set(DEFAULT_PROTECTED_PATHS) <= set(manifest.effective_protected_paths)


def test_valid_mapping_and_bytes_sources_give_the_same_manifest() -> None:
    text = (FIXTURES / "valid" / "minimal.yaml").read_text()
    from_text = validate_manifest(text).manifest
    from_bytes = validate_manifest(text.encode()).manifest
    from_file = validate_manifest(FIXTURES / "valid" / "minimal.yaml").manifest
    assert from_text is not None
    assert from_text == from_bytes == from_file


# --- Invalid fixtures ------------------------------------------------------------------------


# One table for every invalid fixture: (fixture name, expected issue path, expected issue code).
INVALID_FIXTURES: list[tuple[str, str, str]] = [
    ("unknown-top-level-key", "content_paths", "extra_forbidden"),
    ("unknown-agent-key", "agents[0].api_key", "extra_forbidden"),
    ("duplicate-agent-ids", "agents[1].id", "duplicate_agent_id"),
    ("duplicate-default-agent-ids", "agents[1].id", "duplicate_agent_id"),
    ("protected-path-traversal", "protected_paths[1]", "unsafe_path"),
    ("protected-path-absolute", "protected_paths[0]", "unsafe_path"),
    ("forbidden-path-traversal", "forbidden_paths[0]", "unsafe_path"),
    ("unknown-change-class", "change_classes.secrets", "unknown_change_class"),
    ("email-agent-deferred", "agents[1].type", "agent_type_deferred"),
    ("duplicate-yaml-key", "(yaml)", "duplicate_key"),
]


@pytest.mark.parametrize(("name", "path", "code"), INVALID_FIXTURES)
def test_invalid_fixtures_fail_with_a_readable_path(name: str, path: str, code: str) -> None:
    result = validate_manifest((FIXTURES / "invalid" / f"{name}.yaml").read_text())

    assert not result.ok
    assert [(issue.path, issue.code) for issue in result.issues] == [(path, code)]
    (issue,) = result.issues
    assert str(issue).startswith(f"{path}: ")
    assert issue.message  # never empty


def test_every_invalid_fixture_has_an_expected_issue() -> None:
    on_disk = {p.stem for p in (FIXTURES / "invalid").glob("*.yaml")}
    assert on_disk == {name for name, _, _ in INVALID_FIXTURES}


def test_error_messages_name_the_problem() -> None:
    def message(name: str) -> str:
        (issue,) = validate_manifest((FIXTURES / "invalid" / f"{name}.yaml").read_text()).issues
        return issue.message

    assert "unknown key 'content_paths'" in message("unknown-top-level-key")
    assert "front-desk" in message("duplicate-agent-ids")
    assert "agents[0]" in message("duplicate-agent-ids")
    assert ".." in message("protected-path-traversal")
    assert "relative" in message("protected-path-absolute")
    assert "content, prompt, config, code" in message("unknown-change-class")
    assert "deferred" in message("email-agent-deferred")
    assert "line" in message("duplicate-yaml-key")


def test_all_problems_are_reported_at_once() -> None:
    data = manifest_with(
        hotel="Not A Slug",
        protected_paths=["/abs/**", "ok/**", "../up"],
        surprise=True,
    )
    assert sorted(issue_paths(data)) == sorted(
        ["hotel", "protected_paths[0]", "protected_paths[2]", "surprise"]
    )


# --- Field rules -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "/etc/passwd",
        "C:/Windows/**",
        "C:\\Windows",
        "~/secrets",
        "a/../b",
        "../b",
        "./a",
        "a//b",
        "a/",
        "",
        "a\\b",
        "src/**x/y",
        "src/[ab]/**",
        "src/{a,b}/**",
        "!src/**",
        "a\nb",
    ],
)
def test_unsafe_or_unsupported_path_patterns_are_rejected(pattern: str) -> None:
    for key in ("protected_paths", "forbidden_paths"):
        assert issue_paths(manifest_with(**{key: [pattern]})) == [f"{key}[0]"]
    assert issue_paths(manifest_with(change_classes={"content": [pattern]})) == [
        "change_classes.content[0]"
    ]


@pytest.mark.parametrize(
    "pattern", ["**/*.pem", ".env*", "src/**/migrations/**", "kb/*.md", "a?c", "facts.yaml"]
)
def test_supported_path_patterns_are_accepted(pattern: str) -> None:
    parse(manifest_with(protected_paths=[pattern], forbidden_paths=[pattern]))


@pytest.mark.parametrize("value", ["prompts/*", "../prompts", "/prompts/text", "prompts/text/"])
def test_agent_directories_must_be_plain_repo_paths(value: str) -> None:
    data = manifest_with(agents=[{**MINIMAL["agents"][0], "prompt_dir": value}])
    assert issue_paths(data) == ["agents[0].prompt_dir"]


@pytest.mark.parametrize(
    ("overrides", "path"),
    [
        ({"version": 2}, "version"),
        ({"version": "1"}, "version"),
        ({"version": True}, "version"),
        ({"version": 1.0}, "version"),
        ({"layout": "custom"}, "layout"),
        ({"hotel": "Example Hotel"}, "hotel"),
        ({"hotel": 123}, "hotel"),
        ({"agents": []}, "agents"),
        ({"agents": [{"type": "fax"}]}, "agents[0].type"),
        ({"agents": [{"platform": "own", "prompt_dir": "p"}]}, "agents[0].type"),
        ({"agents": [{**VOICE_AGENT, "platform": "retell"}]}, "agents[0].platform"),
        ({"agents": [{**VOICE_AGENT, "id": "Front Desk"}]}, "agents[0].id"),
        ({"agents": [{**MINIMAL["agents"][0], "runtime": "langgraph"}]}, "agents[0].runtime"),
        ({"commands": {**MINIMAL["commands"], "lint": ""}}, "commands.lint"),
        ({"commands": {**MINIMAL["commands"], "lint": "ruff\nrm -rf /"}}, "commands.lint"),
        ({"commands": {**MINIMAL["commands"], "deploy": "fly deploy"}}, "commands.deploy"),
        ({"change_classes": {}}, "change_classes"),
        ({"limits": {"max_diff_lines": 100_000}}, "limits.max_diff_lines"),
        ({"limits": {"max_changed_files": 0}}, "limits.max_changed_files"),
        ({"limits": {"max_changed_files": "30"}}, "limits.max_changed_files"),
        ({"limits": {"max_changed_files": True}}, "limits.max_changed_files"),
        ({"evals": {"max_score_drop": 0.5}}, "evals.max_score_drop"),
        ({"evals": {"max_score_drop": -0.1}}, "evals.max_score_drop"),
        ({"deploy": {"owner": "harness"}}, "deploy.owner"),
        ({"deploy": {"owner": "target-repo-ci", "staging_check": "http://x.invalid/"}}, None),
        ({"deploy": {"owner": "target-repo-ci", "staging_check": "https://u:p@x.invalid/"}}, None),
    ],
)
def test_field_rules(overrides: dict[str, Any], path: str | None) -> None:
    expected = path or "deploy.staging_check"
    assert issue_paths(manifest_with(**overrides)) == [expected]


def test_missing_required_keys_are_named() -> None:
    data = manifest_with()
    del data["commands"]
    del data["deploy"]
    result = validate_manifest(data)
    assert {(issue.path, issue.code) for issue in result.issues} == {
        ("commands", "missing"),
        ("deploy", "missing"),
    }


def test_eval_suites_must_name_declared_agents() -> None:
    data = manifest_with(evals={"suites": {"ghost": "evals/ghost.yaml"}})
    result = validate_manifest(data)
    assert [(issue.path, issue.code) for issue in result.issues] == [
        ("evals.suites.ghost", "unknown_agent_id")
    ]


def test_eval_suites_must_be_protected_so_a_change_cannot_weaken_its_own_evals() -> None:
    data = manifest_with(evals={"suites": {"text": "kb/evals.yaml"}})
    result = validate_manifest(data)
    assert [(issue.path, issue.code) for issue in result.issues] == [
        ("evals.suites.text", "eval_suite_not_protected")
    ]
    # Adding the file to protected_paths makes it a valid suite location.
    parse({**data, "protected_paths": ["kb/evals.yaml"]})


def test_explicit_agent_ids_and_eval_suites() -> None:
    manifest = parse(
        manifest_with(
            agents=[{**VOICE_AGENT, "id": "night-voice"}, MINIMAL["agents"][0]],
            evals={"suites": {"night-voice": "evals/night.yaml"}},
        )
    )
    assert [agent.id for agent in manifest.agents] == ["night-voice", "text"]
    assert manifest.eval_suite_for("night-voice") == "evals/night.yaml"
    assert manifest.eval_suite_for("text") == "evals/text.yaml"
    with pytest.raises(KeyError):
        manifest.eval_suite_for("ghost")


@pytest.mark.parametrize("runtime", [r.value for r in ProductRuntime])
def test_text_agents_accept_each_product_runtime(runtime: str) -> None:
    manifest = parse(manifest_with(agents=[{**MINIMAL["agents"][0], "runtime": runtime}]))
    assert manifest.agents[0].runtime == runtime


# --- YAML input ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("version: [1\n", "yaml_syntax"),
        ("", "not_a_mapping"),
        ("- a\n- b\n", "not_a_mapping"),
        ("base: &b [kb/**]\nprotected_paths: *b\n", "yaml_alias"),
        ("x: !!python/object:os.system {}\n", "yaml_syntax"),
        (b"version: 1\nhotel: \xff\n", "not_utf8"),
        ("a: 1\n" + "#" * 70_000, "too_large"),
        ("x: " + "[" * 1000 + "0" + "]" * 1000, "too_deep"),
        ("x: " + "{a: " * 1000 + "0" + "}" * 1000, "too_deep"),
        ("hotel: \ud800\n", "not_utf8"),
    ],
)
def test_unsafe_or_broken_yaml_is_rejected(text: str | bytes, code: str) -> None:
    result = validate_manifest(text)
    assert not result.ok
    assert [issue.code for issue in result.issues] == [code]
    assert result.issues[0].path in {"(yaml)", "(root)"}


def test_load_manifest_raises_with_all_issues(tmp_path: Path) -> None:
    bad = tmp_path / "harness.yaml"
    bad.write_text((FIXTURES / "invalid" / "duplicate-agent-ids.yaml").read_text())
    with pytest.raises(ManifestValidationError) as excinfo:
        load_manifest(bad)
    assert [issue.path for issue in excinfo.value.issues] == ["agents[1].id"]
    assert "agents[1].id" in str(excinfo.value)


def test_direct_model_validation_enforces_cross_field_rules() -> None:
    data = manifest_with(agents=[MINIMAL["agents"][0], MINIMAL["agents"][0]])
    with pytest.raises(ValueError, match="duplicate agent id"):
        Manifest.model_validate(data)


# --- Path rules: protected, forbidden, change classes -----------------------------------------


def test_default_protected_paths_cannot_be_dropped_by_the_manifest() -> None:
    manifest = parse(manifest_with(protected_paths=["src/billing/**"]))
    for path in [
        "harness.yaml",
        "CODEOWNERS",
        ".github/CODEOWNERS",
        "docs/CODEOWNERS",
        ".github/workflows/ci.yml",
        "evals/text.yaml",
        "src/migrations/0001_init.sql",
        "src/db/migrations/0002.sql",
        "src/payments/stripe.py",
        "src/auth/session.py",
        "src/billing/invoice.py",
    ]:
        assert manifest.is_protected(path), path
    for path in ["kb/breakfast.md", "facts.yaml", "src/tools/lookup.py", "evalsx/a.yaml"]:
        assert not manifest.is_protected(path), path


def test_harness_can_never_edit_the_manifest_or_codeowners() -> None:
    manifest = parse(manifest_with(forbidden_paths=[]))
    for path in [
        "harness.yaml",
        "CODEOWNERS",
        ".github/CODEOWNERS",
        "docs/CODEOWNERS",
        "Harness.YAML",  # matching is case-insensitive, so case tricks fail closed
        "codeowners",
        ".env",
        "config/.env.production",
        "certs/server.pem",
    ]:
        assert not manifest.harness_may_edit(path), path
    assert manifest.harness_may_edit("kb/breakfast.md")
    assert set(HARNESS_NEVER_EDITS) <= set(manifest.effective_forbidden_paths)


def test_manifest_forbidden_paths_add_to_the_fixed_list() -> None:
    manifest = load_manifest(FIXTURES / "valid" / "sample-hotel.yaml")
    assert not manifest.harness_may_edit("infra/prod/main.tf")
    assert not manifest.harness_may_edit(".env.local")
    assert manifest.harness_may_edit("infra/staging/main.tf")


@pytest.mark.parametrize(
    "path", ["kb/../harness.yaml", "/harness.yaml", "../other/harness.yaml", "a//b", ""]
)
def test_path_queries_reject_traversal_instead_of_guessing(path: str) -> None:
    manifest = parse(manifest_with())
    with pytest.raises(ValueError):
        manifest.harness_may_edit(path)
    with pytest.raises(ValueError):
        manifest.is_protected(path)
    with pytest.raises(ValueError):
        manifest.change_class_of(path)


def test_glob_semantics_are_anchored_at_the_repo_root() -> None:
    manifest = parse(
        manifest_with(
            change_classes={
                "content": ["kb/*.md", "facts.yaml"],
                "prompt": ["prompts/**"],
                "config": ["*.yaml"],
            }
        )
    )
    assert manifest.change_class_of("kb/breakfast.md") is ChangeClass.CONTENT
    assert manifest.change_class_of("prompts/voice/greeting.md") is ChangeClass.PROMPT
    assert manifest.change_class_of("prompts/a/b/c.md") is ChangeClass.PROMPT
    # "*" stays inside one directory, and patterns are anchored at the root.
    assert manifest.change_class_of("kb/sub/deep.md") is ChangeClass.CODE
    assert manifest.change_class_of("vapi/assistant.yaml") is ChangeClass.CODE
    assert manifest.change_class_of("kbx/a.md") is ChangeClass.CODE


def test_change_class_picks_the_strictest_match_and_defaults_to_code() -> None:
    manifest = load_manifest(FIXTURES / "valid" / "sample-hotel.yaml")
    assert manifest.change_class_of("kb/pets.md") is ChangeClass.CONTENT
    assert manifest.change_class_of("facts.yaml") is ChangeClass.CONTENT
    assert manifest.change_class_of("prompts/text/tone.md") is ChangeClass.PROMPT
    assert manifest.change_class_of("tools.yaml") is ChangeClass.CONFIG
    assert manifest.change_class_of("src/agent/loop.py") is ChangeClass.CODE
    assert manifest.change_class_of("README.md") is ChangeClass.CODE  # no class: strictest

    overlapping = parse(
        manifest_with(change_classes={"content": ["kb/**"], "config": ["kb/tools/**"]})
    )
    assert overlapping.change_class_of("kb/tools/search.yaml") is ChangeClass.CONFIG
    assert overlapping.change_class_of("kb/faq.md") is ChangeClass.CONTENT


# --- JSON Schema ------------------------------------------------------------------------------


def test_committed_json_schema_matches_the_model() -> None:
    committed = json.loads(SCHEMA_FILE.read_text())
    regenerate = "uv run harness-validate --schema > schemas/harness-v1.schema.json"
    assert committed == manifest_json_schema(), f"Regenerate in backend/: {regenerate}"


def test_json_schema_is_closed_and_lists_only_enabled_agent_types() -> None:
    schema = manifest_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"version", "layout", "hotel", "agents", "commands"}
    agent_types = {
        definition["properties"]["type"]["const"]
        for name, definition in schema["$defs"].items()
        if name.endswith("Agent")
    }
    assert agent_types == {"voice", "text"}
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, definition["title"]


def test_case_tricks_never_loosen_a_rule() -> None:
    manifest = load_manifest(FIXTURES / "valid" / "sample-hotel.yaml")
    # Protected and forbidden paths match case-insensitively (the safe side) ...
    assert manifest.is_protected("EVALS/text.yaml")
    assert not manifest.harness_may_edit("Infra/Prod/main.tf")
    # ... but change classes match case-sensitively, so "KB/" is not content: it falls to code.
    assert manifest.change_class_of("KB/evil.py") is ChangeClass.CODE
    # An eval suite must be protected with the exact case.
    data = manifest_with(evals={"suites": {"text": "EVALS/text.yaml"}})
    assert issue_paths(data) == ["evals.suites.text"]


@pytest.mark.parametrize(
    ("place", "path"),
    [
        ((), "['[key]']"),
        (("agents", 0), "agents[0]['[key]']"),
        (("commands",), "commands['[key]']"),
    ],
)
def test_a_user_key_that_looks_like_an_internal_marker_is_reported_as_given(
    place: tuple[str | int, ...], path: str
) -> None:
    data = manifest_with()
    target: Any = data
    for part in place:
        target = target[part]
    target["[key]"] = 1
    result = validate_manifest(data)
    assert [(issue.path, issue.code) for issue in result.issues] == [(path, "extra_forbidden")]
    assert "unknown key '[key]'" in result.issues[0].message


def test_an_agent_key_named_like_an_agent_type_is_reported_at_its_path() -> None:
    data = manifest_with(agents=[{**MINIMAL["agents"][0], "voice": 1}])
    assert issue_paths(data) == ["agents[0].voice"]


@pytest.mark.parametrize(
    ("key", "path"),
    [("Bad Key", "evals.suites['Bad Key']"), ("[key]", "evals.suites['[key]']")],
)
def test_bad_eval_suite_keys_keep_their_name_in_the_path(key: str, path: str) -> None:
    result = validate_manifest(manifest_with(evals={"suites": {key: "evals/x.yaml"}}))
    assert [(issue.path, issue.code) for issue in result.issues] == [(path, "bad_agent_id")]


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        ("a" * 64, "*a" * 32 + "b"),
        ("a" * 200, "*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b"),
        ("/".join(["a"] * 100), "/".join(["**"] * 50) + "/b"),
        ("/".join(["a"] * 60), "/".join(["**/*a*a*a"] * 20) + "/b"),
    ],
)
def test_adversarial_patterns_do_not_stall_path_queries(path: str, pattern: str) -> None:
    manifest = parse(manifest_with(protected_paths=[pattern], forbidden_paths=[pattern]))
    started = time.perf_counter()
    assert not manifest.is_protected(path)
    assert manifest.harness_may_edit(path)
    assert manifest.change_class_of(path) is ChangeClass.CODE
    assert time.perf_counter() - started < 1.0
