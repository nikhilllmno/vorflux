"""Agent-runtime adapters.

`base` holds the contract and the registry; `fake` is the test double. Real adapters
(`claude_agent_sdk`, `openai_agents_sdk`, `deep_agents`, `opencode`) land in milestone M1.
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
