"""Agent-runtime adapters.

`base` holds the contract and the registry. The fake runtime is a test double in `tests/fakes.py`,
outside this package, so production config cannot select it. Real adapters land in
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

__all__ = [
    "AgentRuntime",
    "Capabilities",
    "RunEvent",
    "RunRequest",
    "RuntimeKind",
    "RuntimeRegistry",
    "UnknownRuntimeError",
]
