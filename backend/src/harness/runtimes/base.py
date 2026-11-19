"""The runtime-adapter contract every agent framework plugs into.

One adapter per runtime (Claude Agent SDK, OpenAI Agents SDK) translates this contract to the
runtime's own API. A later runtime needs only a new adapter. Harness features (tenancy, approvals,
memory, skills, audit) live above this seam, so they work the same for every framework.
See docs/architecture.md, section "Agent-runtime adapter layer".
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from harness.tenancy import TenantContext


class RuntimeKind(StrEnum):
    """The production runtimes that a plan, a hotel policy, or a run config may name.

    This enum is the one source of truth for the runtime ids. The `plans.allowed_runtimes`
    database constraint must hold the same ids. Test doubles are not members: they live under
    `tests/` and register under their own id, so production config cannot select them.
    """

    CLAUDE_AGENT_SDK = "claude_agent_sdk"
    OPENAI_AGENTS_SDK = "openai_agents_sdk"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a framework does natively. The harness fills each gap itself."""

    streaming: bool
    native_subagents: bool
    native_sandbox: bool
    resumable_sessions: bool
    mcp_client: bool


@dataclass(frozen=True, slots=True)
class RunRequest:
    tenant: TenantContext
    session_id: str
    prompt: str
    workspace_id: str
    model: str | None = None
    allowed_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalRequired:
    """The run pauses until a human decides. Adapters map framework permission hooks here."""

    call_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RunCompleted:
    output: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RunFailed:
    error: str


RunEvent = TextDelta | ToolCall | ApprovalRequired | RunCompleted | RunFailed


@runtime_checkable
class AgentRuntime(Protocol):
    @property
    def kind(self) -> str:
        """A `RuntimeKind` for production adapters. Test doubles use their own id."""
        ...

    def capabilities(self) -> Capabilities: ...

    def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        """Stream events for one turn. The last event is RunCompleted or RunFailed."""
        ...

    async def cancel(self, session_id: str) -> None: ...


class UnknownRuntimeError(LookupError):
    pass


class RuntimeRegistry:
    """Name -> adapter table. Adapters register at startup; lookups never import lazily."""

    def __init__(self) -> None:
        self._adapters: dict[str, AgentRuntime] = {}

    def register(self, runtime: AgentRuntime) -> None:
        if runtime.kind in self._adapters:
            raise ValueError(f"runtime already registered: {runtime.kind}")
        self._adapters[runtime.kind] = runtime

    def get(self, kind: str) -> AgentRuntime:
        try:
            return self._adapters[kind]
        except KeyError:
            raise UnknownRuntimeError(kind) from None

    def kinds(self) -> list[str]:
        return sorted(self._adapters)
