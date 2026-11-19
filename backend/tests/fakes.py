"""Test doubles. They live outside the `harness` package, so production code cannot use them."""

from collections.abc import AsyncIterator

from harness.runtimes.base import (
    Capabilities,
    RunCompleted,
    RunEvent,
    RunRequest,
    TextDelta,
)

FAKE_KIND = "fake"
"""Registry id of the fake runtime. It is not a `RuntimeKind`."""


class FakeRuntime:
    """Deterministic in-process runtime. Calls no model."""

    @property
    def kind(self) -> str:
        return FAKE_KIND

    def capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=True,
            native_subagents=False,
            native_sandbox=False,
            resumable_sessions=False,
            mcp_client=False,
        )

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        reply = f"echo: {request.prompt}"
        for word in reply.split(" "):
            yield TextDelta(text=word + " ")
        yield RunCompleted(output=reply)

    async def cancel(self, session_id: str) -> None:
        return None
