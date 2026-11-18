"""The runtime-adapter contract every agent framework plugs into.

One adapter per framework (Claude Agent SDK, OpenAI Agents SDK, LangChain Deep Agents, opencode)
translates this contract to the framework's own API. Harness features (tenancy, approvals,
memory, skills, audit) live above this seam, so they work the same for every framework.
See docs/architecture.md, section "Agent-runtime adapter layer".
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from harness.tenancy import TenantContext


class RuntimeKind(StrEnum):
    CLAUDE_AGENT_SDK = "claude_agent_sdk"
    OPENAI_AGENTS_SDK = "openai_agents_sdk"
    DEEP_AGENTS = "deep_agents"
    OPENCODE = "opencode"
    FAKE = "fake"


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
    def kind(self) -> RuntimeKind: ...

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
        self._adapters: dict[RuntimeKind, AgentRuntime] = {}

    def register(self, runtime: AgentRuntime) -> None:
        if runtime.kind in self._adapters:
            raise ValueError(f"runtime already registered: {runtime.kind}")
        self._adapters[runtime.kind] = runtime

    def get(self, kind: RuntimeKind) -> AgentRuntime:
        try:
            return self._adapters[kind]
        except KeyError:
            raise UnknownRuntimeError(kind) from None

    def kinds(self) -> list[RuntimeKind]:
        return sorted(self._adapters)
