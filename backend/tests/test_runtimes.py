import asyncio
import re
from pathlib import Path
from uuid import uuid4

import pytest
from fakes import FAKE_KIND, FakeRuntime

from harness.runtimes import (
    AgentRuntime,
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
    assert registry.kinds() == [FAKE_KIND]


REPO = Path(__file__).resolve().parents[2]
_PLAN_RUNTIMES = re.compile(r"allowed_runtimes\s*<@\s*array\[([^\]]*)\]", re.IGNORECASE)


def test_runtime_kinds_follow_the_owner_decision() -> None:
    # Owner decision 2026-11-18 (docs/architecture.md D14): Anthropic and OpenAI models only.
    assert {kind.value for kind in RuntimeKind} == {"claude_agent_sdk", "openai_agents_sdk"}


def test_production_config_cannot_select_the_fake_runtime() -> None:
    with pytest.raises(ValueError):
        RuntimeKind(FAKE_KIND)


def test_database_plan_runtimes_match_selectable_runtimes() -> None:
    """The latest `plans.allowed_runtimes` check allows exactly the selectable runtime ids."""
    migrations = sorted((REPO / "supabase" / "migrations").glob("*.sql"))
    found = [m.group(1) for path in migrations for m in _PLAN_RUNTIMES.finditer(path.read_text())]
    if not found:
        pytest.skip("no migration constrains plans.allowed_runtimes yet")
    allowed = set(re.findall(r"'([^']+)'", found[-1]))
    assert allowed == {kind.value for kind in RuntimeKind}
