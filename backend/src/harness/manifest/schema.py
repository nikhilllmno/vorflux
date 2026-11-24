"""Pydantic model of the `harness.yaml` manifest, schema version 1.

The manifest is the contract between the harness and one hotel repo (D10). Our team owns it
through CODEOWNERS, and the harness reads it from the repo's default branch only, so a change can
never widen its own rules. Design: docs/architecture.md, Sections 9.3 and 9.9. Reference for
hotel repos: docs/harness-yaml.md.

Every model rejects unknown keys. Strings, integers and numbers are strict: YAML `"30"` is not
an integer and `30` is not a string. Rules that span fields (unique agent ids, eval suites) live
in `Manifest.semantic_issues`, so `validate_manifest` can report each one with an exact path.
"""

import re
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    ValidationInfo,
    model_validator,
)
from pydantic_core import PydanticCustomError

from harness.manifest.issues import ManifestIssue
from harness.manifest.paths import (
    UnsafePathError,
    check_pattern,
    check_plain_path,
    check_query_path,
    matches_any,
)

MANIFEST_FILENAME: Final = "harness.yaml"
SCHEMA_VERSION: Final = 1

# Q28: these paths always need our team's review, whatever the manifest lists. The manifest can
# add paths; it cannot remove these. GitHub reads CODEOWNERS from the root, .github/ or docs/.
DEFAULT_PROTECTED_PATHS: Final[tuple[str, ...]] = (
    "harness.yaml",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    ".github/**",
    "evals/**",
    "src/**/migrations/**",
    "src/payments/**",
    "src/auth/**",
)

# D10: the harness can never change the manifest or CODEOWNERS, in any location. Secret-shaped
# files are also never edited (Section 9.9). The manifest's forbidden_paths add to this list.
HARNESS_NEVER_EDITS: Final[tuple[str, ...]] = (
    "harness.yaml",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
)

# Set by validate_manifest so that cross-field issues come back with exact paths instead of one
# combined error. Without it, Manifest.model_validate raises on them.
COLLECT_SEMANTIC_ISSUES: Final = "harness.manifest.collect_semantic_issues"

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_AGENT_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
_ASSISTANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ChangeClass(StrEnum):
    """Decides which evals run and how the staff diff looks (Section 9.6)."""

    CONTENT = "content"
    PROMPT = "prompt"
    CONFIG = "config"
    CODE = "code"


# Least strict first. A path in two classes takes the stricter one; a path in none is `code`.
CHANGE_CLASS_ORDER: Final[tuple[ChangeClass, ...]] = (
    ChangeClass.CONTENT,
    ChangeClass.PROMPT,
    ChangeClass.CONFIG,
    ChangeClass.CODE,
)


class AgentType(StrEnum):
    VOICE = "voice"
    TEXT = "text"
    EMAIL = "email"


ENABLED_AGENT_TYPES: Final = frozenset({AgentType.VOICE, AgentType.TEXT})
DEFERRED_AGENT_TYPES: Final = frozenset({AgentType.EMAIL})


class ProductRuntime(StrEnum):
    """The framework a hotel's own (non-Vapi) agent runs on.

    This is the product agent's runtime, not the harness runtime (`harness.runtimes.RuntimeKind`).
    The two lists can differ: the harness itself may support fewer frameworks than hotel agents use.
    """

    CLAUDE_AGENT_SDK = "claude_agent_sdk"
    OPENAI_AGENTS_SDK = "openai_agents_sdk"
    DEEP_AGENTS = "deep_agents"
    OPENCODE = "opencode"


def _wrap(check: Any, code: str) -> Any:
    def validate(value: str) -> str:
        try:
            result: str = check(value)
        except UnsafePathError as exc:
            raise PydanticCustomError(code, str(exc)) from None
        return result

    return validate


def _check_regex(regex: re.Pattern[str], description: str) -> Any:
    def check(value: str) -> str:
        if regex.fullmatch(value) is None:
            raise UnsafePathError(description)
        return value

    return check


def _check_command(value: str) -> str:
    if not value.strip():
        raise UnsafePathError("must not be empty")
    if _CONTROL.search(value):
        raise UnsafePathError("must be one line with no control characters")
    if len(value) > 500:
        raise UnsafePathError("must be at most 500 characters")
    return value


def _check_https_url(value: str) -> str:
    if len(value) > 2000 or _CONTROL.search(value) or " " in value:
        raise UnsafePathError("must be a single https URL of at most 2000 characters")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafePathError("must be an https:// URL with a host")
    if parts.username is not None or parts.password is not None:
        raise UnsafePathError("must not contain a user name or password")
    return value


SLUG_DESCRIPTION = "lowercase letters and digits in words joined by single hyphens"
Slug = Annotated[
    str,
    Strict(),
    AfterValidator(_wrap(_check_regex(_SLUG, f"must be a slug: {SLUG_DESCRIPTION}"), "bad_slug")),
    Field(max_length=63, json_schema_extra={"pattern": f"^{_SLUG.pattern}$"}),
]
AgentId = Annotated[
    str,
    Strict(),
    AfterValidator(
        _wrap(
            _check_regex(
                _AGENT_ID,
                "must start with a lowercase letter, then up to 39 lowercase letters, digits "
                "or hyphens",
            ),
            "bad_agent_id",
        )
    ),
    Field(json_schema_extra={"pattern": f"^{_AGENT_ID.pattern}$"}),
]
AssistantName = Annotated[
    str,
    Strict(),
    AfterValidator(
        _wrap(
            _check_regex(_ASSISTANT, "must be a Vapi assistant name or id (letters, digits, ._-)"),
            "bad_assistant",
        )
    ),
    Field(json_schema_extra={"pattern": f"^{_ASSISTANT.pattern}$"}),
]
RepoPath = Annotated[
    str,
    Strict(),
    AfterValidator(_wrap(check_plain_path, "unsafe_path")),
    Field(description="A plain path relative to the repo root (no glob, no '..')."),
]
PathPattern = Annotated[
    str,
    Strict(),
    AfterValidator(_wrap(check_pattern, "unsafe_path")),
    Field(
        description=(
            "A glob relative to the repo root and anchored there. '*' and '?' stay in one "
            "directory; '**' (a whole segment) spans directories. No '..', classes or negation."
        )
    ),
]
PatternList = Annotated[tuple[PathPattern, ...], Field(max_length=200)]
Command = Annotated[
    str,
    Strict(),
    AfterValidator(_wrap(_check_command, "bad_command")),
    Field(description="One shell command line, run in the sandbox from the repo root."),
]
HttpsUrl = Annotated[
    str,
    Strict(),
    AfterValidator(_wrap(_check_https_url, "bad_url")),
    Field(json_schema_extra={"format": "uri", "pattern": "^https://"}),
]
StrictInt = Annotated[int, Strict()]


def _exact_int(value: Any) -> Any:
    # Literal[1] alone accepts True and 1.0, because they compare equal to 1.
    if type(value) is not int:
        raise PydanticCustomError("int_type", "must be the integer 1")
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _id_from_type(data: dict[str, Any]) -> str:
    return str(data.get("type", "agent"))


_ID_FIELD_DESCRIPTION = "Unique among the agents. Defaults to the agent type."


class VoiceAgent(_Model):
    """A voice agent on Vapi. Its prompts and Vapi config live in the hotel repo."""

    type: Literal["voice"]
    id: AgentId = Field(default_factory=_id_from_type, description=_ID_FIELD_DESCRIPTION)
    platform: Literal["vapi"]
    prompt_dir: RepoPath
    config_path: RepoPath = Field(
        description="Vapi assistant settings; the repo's CI pushes them to production on merge."
    )
    test_assistant: AssistantName = Field(
        description="Assistant in the hotel's Vapi test org that voice evals run against."
    )

    @property
    def runtime(self) -> None:
        """Voice agents run on Vapi, so they have no product runtime."""
        return None


class TextAgent(_Model):
    """A text agent (SMS, WhatsApp, web chat) that is our own code in the hotel repo."""

    type: Literal["text"]
    id: AgentId = Field(default_factory=_id_from_type, description=_ID_FIELD_DESCRIPTION)
    platform: Literal["own"]
    prompt_dir: RepoPath
    runtime: Annotated[ProductRuntime, Field(strict=False)] | None = Field(
        default=None, description="The framework the agent runs on. Unset means not declared."
    )


# Pluggable agent types: a new type is one model added to this union and to ENABLED_AGENT_TYPES.
Agent = Annotated[VoiceAgent | TextAgent, Field(discriminator="type")]


class Commands(_Model):
    """Commands the harness runs in the sandbox, from the repo root."""

    setup: Command
    lint: Command
    test: Command
    render: Command
    baml_test: Command | None = None
    eval: Command
    smoke: Command | None = None


class ChangeClasses(_Model):
    """Path patterns per change class. A path in no class is treated as `code`."""

    content: PatternList = ()
    prompt: PatternList = ()
    config: PatternList = ()
    code: PatternList = ()

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not any(getattr(self, change_class.value) for change_class in ChangeClass):
            raise PydanticCustomError(
                "no_change_classes", "declare at least one change class with at least one path"
            )
        return self

    def patterns(self, change_class: ChangeClass) -> tuple[str, ...]:
        value: tuple[str, ...] = getattr(self, change_class.value)
        return value


class Limits(_Model):
    """Diff limits per change request. A larger change splits into several CRs."""

    max_changed_files: Annotated[StrictInt, Field(ge=1, le=200)] = 30
    max_diff_lines: Annotated[StrictInt, Field(ge=1, le=5000)] = 800


class Evals(_Model):
    """Where each agent's eval suite lives, and how far scores may fall against `main`."""

    suites: dict[AgentId, RepoPath] = Field(
        default_factory=dict,
        description="Agent id to suite file. An agent not listed uses evals/<type>.yaml.",
    )
    max_score_drop: Annotated[float, Strict(), Field(ge=0.0, le=0.2)] = Field(
        default=0.0,
        description="Largest allowed drop of a 0-1 eval score against main. 0 means no drop.",
    )


class Deploy(_Model):
    """The harness never deploys. The hotel repo's own CI does."""

    owner: Literal["target-repo-ci"]
    staging_check: HttpsUrl | None = Field(
        default=None, description="Read-only status URL the harness may poll after a merge."
    )


def _at_least_one_agent(agents: tuple[Agent, ...]) -> tuple[Agent, ...]:
    # Not Field(min_length=1): pydantic then also reports "too short" when an item is invalid.
    if not agents:
        raise PydanticCustomError("no_agents", "list at least one agent")
    return agents


class Manifest(_Model):
    """`harness.yaml`, schema version 1."""

    model_config = ConfigDict(title="harness.yaml manifest v1")

    version: Annotated[Literal[1], BeforeValidator(_exact_int)]
    layout: Literal["standard/v1"]
    hotel: Slug
    agents: Annotated[
        tuple[Agent, ...],
        Field(max_length=20, json_schema_extra={"minItems": 1}),
        AfterValidator(_at_least_one_agent),
    ]
    commands: Commands
    change_classes: ChangeClasses
    protected_paths: PatternList = Field(
        default=(), description="Added to the default protected paths, which always apply."
    )
    forbidden_paths: PatternList = Field(
        default=(), description="Added to the paths the harness never edits, which always apply."
    )
    limits: Limits = Limits()
    evals: Evals = Evals()
    deploy: Deploy

    @model_validator(mode="after")
    def _cross_field_rules(self, info: ValidationInfo) -> Self:
        if info.context and info.context.get(COLLECT_SEMANTIC_ISSUES):
            return self
        issues = self.semantic_issues()
        if issues:
            raise ValueError("; ".join(str(issue) for issue in issues))
        return self

    def semantic_issues(self) -> tuple[ManifestIssue, ...]:
        """Rules that span fields. Empty when the manifest is valid."""
        issues: list[ManifestIssue] = []
        first_index: dict[str, int] = {}
        for index, agent in enumerate(self.agents):
            if agent.id in first_index:
                issues.append(
                    ManifestIssue(
                        f"agents[{index}].id",
                        f"duplicate agent id {agent.id!r} (first used by "
                        f"agents[{first_index[agent.id]}]); give each agent a unique id",
                        "duplicate_agent_id",
                    )
                )
            else:
                first_index[agent.id] = index
        for agent_id, suite in self.evals.suites.items():
            path = f"evals.suites.{agent_id}"
            if agent_id not in first_index:
                declared = ", ".join(repr(known) for known in first_index)
                issues.append(
                    ManifestIssue(
                        path,
                        f"no agent has the id {agent_id!r} (declared: {declared})",
                        "unknown_agent_id",
                    )
                )
            # Case-sensitive here: a wider match would wrongly pass a suite that is not protected.
            elif not matches_any(suite, self.effective_protected_paths, ignore_case=False):
                issues.append(
                    ManifestIssue(
                        path,
                        f"eval suite {suite!r} is not a protected path, so a change could weaken "
                        "the evals that check it; keep suites under evals/ or add the file to "
                        "protected_paths",
                        "eval_suite_not_protected",
                    )
                )
        return tuple(issues)

    @property
    def effective_protected_paths(self) -> tuple[str, ...]:
        """The default protected paths plus the manifest's own, in that order."""
        return tuple(dict.fromkeys((*DEFAULT_PROTECTED_PATHS, *self.protected_paths)))

    @property
    def effective_forbidden_paths(self) -> tuple[str, ...]:
        """The paths the harness never edits plus the manifest's own, in that order."""
        return tuple(dict.fromkeys((*HARNESS_NEVER_EDITS, *self.forbidden_paths)))

    def is_protected(self, path: str) -> bool:
        """True when a change to `path` needs our team's review. Raises on an unsafe path."""
        return matches_any(check_query_path(path), self.effective_protected_paths, ignore_case=True)

    def harness_may_edit(self, path: str) -> bool:
        """False when the harness must never change `path`. Raises on an unsafe path."""
        return not matches_any(
            check_query_path(path), self.effective_forbidden_paths, ignore_case=True
        )

    def change_class_of(self, path: str) -> ChangeClass:
        """The strictest class whose patterns match `path`; `code` when none match."""
        checked = check_query_path(path)
        for change_class in reversed(CHANGE_CLASS_ORDER):
            if matches_any(checked, self.change_classes.patterns(change_class), ignore_case=False):
                return change_class
        return ChangeClass.CODE

    def eval_suite_for(self, agent_id: str) -> str:
        """The eval suite file of an agent. Raises `KeyError` for an unknown agent id."""
        for agent in self.agents:
            if agent.id == agent_id:
                return self.evals.suites.get(agent_id, f"evals/{agent.type}.yaml")
        raise KeyError(agent_id)
