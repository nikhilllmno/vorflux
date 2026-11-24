"""Parse and validate `harness.yaml`, and turn every problem into a readable `ManifestIssue`."""

import json
import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from harness.manifest.issues import ROOT, YAML, ManifestIssue, ManifestValidationError
from harness.manifest.schema import (
    COLLECT_SEMANTIC_ISSUES,
    DEFERRED_AGENT_TYPES,
    ENABLED_AGENT_TYPES,
    ChangeClass,
    ChangeClasses,
    Commands,
    Deploy,
    Evals,
    Limits,
    Manifest,
    TextAgent,
    VoiceAgent,
)

MAX_MANIFEST_BYTES: Final = 64 * 1024
# The deepest real manifest is about 4 levels (evals.suites.<id>, agents[n].key). The limit stops
# small inputs such as "[[[[...]]]]" from exhausting the parser's recursion.
MAX_YAML_DEPTH: Final = 16
SCHEMA_ID: Final = (
    "https://raw.githubusercontent.com/nikhilllmno/vorflux/main/backend/schemas/"
    "harness-v1.schema.json"
)

_AGENT_TAGS: Final = frozenset(agent_type.value for agent_type in ENABLED_AGENT_TYPES)
_ENABLED_LIST: Final = ", ".join(sorted(_AGENT_TAGS, reverse=True))
_SIMPLE_KEY = re.compile(r"[A-Za-z0-9_-]+")

# Models by location (list indexes removed, agent type in place of the index) for "allowed keys".
_MODELS_AT: Final[dict[tuple[str, ...], type[BaseModel]]] = {
    (): Manifest,
    ("agents", "voice"): VoiceAgent,
    ("agents", "text"): TextAgent,
    ("commands",): Commands,
    ("change_classes",): ChangeClasses,
    ("limits",): Limits,
    ("evals",): Evals,
    ("deploy",): Deploy,
}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of `validate_manifest`: a manifest, or every issue found (never both)."""

    manifest: Manifest | None
    issues: tuple[ManifestIssue, ...]

    @property
    def ok(self) -> bool:
        return self.manifest is not None


class _YamlProblem(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _where(node: yaml.Node) -> str:
    mark = node.start_mark
    return f"line {mark.line + 1}, column {mark.column + 1}"


class _ManifestLoader(yaml.SafeLoader):
    """SafeLoader that also rejects duplicate keys and aliases.

    A duplicate key would let a later `protected_paths:` silently replace an earlier one, and
    aliases allow exponential expansion ("billion laughs"). Neither has a use in a manifest.
    """

    _depth = 0

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node | None:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        line = event.start_mark.line + 1
        if isinstance(event, yaml.AliasEvent):
            raise _YamlProblem(
                f"line {line}: YAML aliases (*name) are not allowed in harness.yaml; "
                "write the value out",
                "yaml_alias",
            )
        if self._depth >= MAX_YAML_DEPTH:
            raise _YamlProblem(
                f"line {line}: nested deeper than {MAX_YAML_DEPTH} levels", "too_deep"
            )
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, Hashable):
                if key in seen:
                    raise _YamlProblem(
                        f"{_where(key_node)}: duplicate key {key!r}; each key may appear once",
                        "duplicate_key",
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_yaml(source: str | bytes) -> Any:
    try:
        raw = source.encode() if isinstance(source, str) else source
    except UnicodeEncodeError as exc:
        raise _YamlProblem(f"not valid UTF-8 (character {exc.start})", "not_utf8") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _YamlProblem(f"the file is larger than {MAX_MANIFEST_BYTES} bytes", "too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _YamlProblem(f"not valid UTF-8 (byte {exc.start})", "not_utf8") from None
    try:
        return yaml.load(text, Loader=_ManifestLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise _YamlProblem(f"{where}{exc.problem or exc}", "yaml_syntax") from None
    except yaml.YAMLError as exc:
        raise _YamlProblem(f"invalid YAML: {exc}", "yaml_syntax") from None


def _format_path(parts: Sequence[str | int]) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif _SIMPLE_KEY.fullmatch(part):
            out += f".{part}" if out else part
        else:
            out += f"[{part!r}]"
    return out or ROOT


def _clean_loc(loc: Sequence[str | int]) -> list[str | int]:
    """Drop pydantic's internal location parts, and only those.

    Pydantic inserts the union tag after an agent index (`agents, 0, "voice", ...`) and appends
    `"[key]"` after a dict key whose key failed validation (`evals, suites, "Bad", "[key]"`). A user
    key may have the same text, so each marker is removed only at its exact position.
    """
    parts = list(loc)
    if len(parts) >= 3 and parts[0] == "agents" and isinstance(parts[1], int):
        if parts[2] in _AGENT_TAGS:
            del parts[2]
    if len(parts) == 4 and parts[:2] == ["evals", "suites"] and parts[3] == "[key]":
        del parts[3]
    return parts


def _model_key(loc: Sequence[str | int], agent_types: Sequence[str | None]) -> tuple[str, ...]:
    key: list[str] = []
    for part in loc:
        if isinstance(part, int):
            if key == ["agents"] and part < len(agent_types) and agent_types[part]:
                key.append(str(agent_types[part]))
            continue
        key.append(part)
    return tuple(key)


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _issue_from_error(error: ErrorDetails, agent_types: Sequence[str | None]) -> ManifestIssue:
    loc = _clean_loc(error["loc"])
    kind = error["type"]
    ctx = error.get("ctx", {})
    value = error.get("input")

    if kind == "extra_forbidden":
        name = str(loc[-1])
        parent = _model_key(loc[:-1], agent_types)
        if parent == ("change_classes",):
            allowed = ", ".join(change_class.value for change_class in ChangeClass)
            return ManifestIssue(
                _format_path(loc),
                f"unknown change class {name!r}; the change classes are {allowed}",
                "unknown_change_class",
            )
        model = _MODELS_AT.get(parent)
        allowed_here = f"; allowed here: {', '.join(model.model_fields)}" if model else ""
        return ManifestIssue(_format_path(loc), f"unknown key {name!r}{allowed_here}", kind)
    if kind == "missing":
        return ManifestIssue(_format_path(loc), "required key is missing", kind)
    if kind == "union_tag_not_found":
        return ManifestIssue(
            _format_path([*loc, "type"]),
            f"required key is missing; set the agent type ({_ENABLED_LIST})",
            "missing",
        )
    if kind == "union_tag_invalid":
        tag = str(ctx.get("tag", value))
        if tag in DEFERRED_AGENT_TYPES:
            return ManifestIssue(
                _format_path([*loc, "type"]),
                f"{tag} agents are deferred and not accepted in schema v1; accepted agent "
                f"types now: {_ENABLED_LIST}",
                "agent_type_deferred",
            )
        return ManifestIssue(
            _format_path([*loc, "type"]),
            f"unknown agent type {tag!r}; accepted agent types: {_ENABLED_LIST}",
            "unknown_agent_type",
        )

    message = error["msg"]
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    if not isinstance(value, dict | list):
        message = f"{message} (got {_short(value)})"
    return ManifestIssue(_format_path(loc), message, kind)


def _agent_types(data: Mapping[str, Any]) -> list[str | None]:
    agents = data.get("agents")
    if not isinstance(agents, list):
        return []
    types: list[str | None] = []
    for agent in agents:
        agent_type = agent.get("type") if isinstance(agent, dict) else None
        types.append(agent_type if isinstance(agent_type, str) else None)
    return types


def validate_manifest(source: str | bytes | Path | Mapping[str, Any]) -> ValidationResult:
    """Validate a manifest and collect every issue.

    `source` is YAML text (`str` or `bytes`), a `Path` to a YAML file, or already-parsed data.
    Never raises for bad input; read `ValidationResult.issues`. A missing or unreadable `Path`
    raises `OSError`.
    """
    data: Any
    if isinstance(source, Path):
        source = source.read_bytes()
    if isinstance(source, str | bytes):
        try:
            data = _load_yaml(source)
        except _YamlProblem as problem:
            return ValidationResult(None, (ManifestIssue(YAML, problem.message, problem.code),))
    else:
        data = source

    if not isinstance(data, Mapping):
        found = "an empty file" if data is None else f"a {type(data).__name__}"
        return ValidationResult(
            None,
            (
                ManifestIssue(
                    ROOT,
                    f"harness.yaml must be a mapping of keys at the top level, not {found}",
                    "not_a_mapping",
                ),
            ),
        )

    try:
        manifest = Manifest.model_validate(data, context={COLLECT_SEMANTIC_ISSUES: True})
    except ValidationError as exc:
        agent_types = _agent_types(data)
        issues = tuple(_issue_from_error(error, agent_types) for error in exc.errors())
        return ValidationResult(None, issues)

    semantic = manifest.semantic_issues()
    if semantic:
        return ValidationResult(None, semantic)
    return ValidationResult(manifest, ())


def load_manifest(path: Path) -> Manifest:
    """Read and validate a manifest file. Raises `ManifestValidationError` with every issue."""
    result = validate_manifest(path)
    if result.manifest is None:
        raise ManifestValidationError(result.issues)
    return result.manifest


def manifest_json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) of the manifest, for editors and other tools.

    The schema checks shape and field rules. Cross-field rules (unique agent ids, eval suites)
    and the path safety rules are only in `validate_manifest`, which is the authority.
    """
    schema = Manifest.model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
    }


def manifest_json_schema_text() -> str:
    """The schema as committed in `backend/schemas/harness-v1.schema.json`."""
    return json.dumps(manifest_json_schema(), indent=2, sort_keys=False) + "\n"
