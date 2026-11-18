"""Agent-runtime adapters.

`base` holds the contract and the registry; `fake` is the test double. Real adapters land in
order: `claude_agent_sdk` (default, M1), then `openai_agents_sdk` (second runtime, M3).
See docs/architecture.md, Section 6.6.
"""

from harness.runtimes.base import (
    AgentRuntime,
    Capabilities,
    RunEvent,
    RunRequest,
    RuntimeKind,
    RuntimeRegistry,
    UnknownRuntimeError,
)
from harness.runtimes.fake import FakeRuntime

__all__ = [
    "AgentRuntime",
    "Capabilities",
    "FakeRuntime",
    "RunEvent",
    "RunRequest",
    "RuntimeKind",
    "RuntimeRegistry",
    "UnknownRuntimeError",
]
