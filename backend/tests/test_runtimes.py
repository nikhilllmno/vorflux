import asyncio
from uuid import uuid4

import pytest

from harness.runtimes import (
    AgentRuntime,
    FakeRuntime,
    RunRequest,
    RuntimeKind,
    RuntimeRegistry,
    UnknownRuntimeError,
)
from harness.runtimes.base import RunCompleted, TextDelta
from harness.tenancy import TenantContext


def _request(prompt: str) -> RunRequest:
    tenant = TenantContext(org_id=uuid4(), hotel_id=uuid4(), actor_id=uuid4())
    return RunRequest(tenant=tenant, session_id="s1", prompt=prompt, workspace_id="w1")


async def _collect(runtime: AgentRuntime, request: RunRequest) -> list[object]:
    return [event async for event in runtime.run(request)]


def test_fake_runtime_satisfies_protocol() -> None:
    assert isinstance(FakeRuntime(), AgentRuntime)


def test_fake_runtime_streams_then_completes() -> None:
    events = asyncio.run(_collect(FakeRuntime(), _request("hello world")))

    assert isinstance(events[-1], RunCompleted)
    assert events[-1].output == "echo: hello world"
    streamed = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert streamed.strip() == "echo: hello world"


def test_registry_rejects_duplicate_and_unknown_kinds() -> None:
    registry = RuntimeRegistry()
    registry.register(FakeRuntime())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeRuntime())
    with pytest.raises(UnknownRuntimeError):
        registry.get(RuntimeKind.OPENAI_AGENTS_SDK)
    assert registry.kinds() == [RuntimeKind.FAKE]
