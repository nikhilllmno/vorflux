"""Deterministic in-process runtime for tests and local development. Calls no model."""

from collections.abc import AsyncIterator

from harness.runtimes.base import (
    Capabilities,
    RunCompleted,
    RunEvent,
    RunRequest,
    RuntimeKind,
    TextDelta,
)


class FakeRuntime:
    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.FAKE

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
