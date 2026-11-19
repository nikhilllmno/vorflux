"""The small async interface that runtime adapters (and the harness MCP server) call.

Every call is keyed by the tool call id (`ProposedCall.tool_call_id`, the `call_id` of the
runtime's ToolCall event), and `evaluate` returns ALLOW, ASK, or DENY for that call.

Flow for one gated tool call:

1. `evaluate(call)`:
   - `allow`: run the tool.
   - `deny`: do not run the tool; return the reason to the model as a tool error.
   - `ask`: call `request(call)`, emit `ApprovalRequired`, and pause the run. Calling `request`
     again for the same tool call returns the same approval.
2. When the orchestrator resumes the run with an `ApprovalResolution`:
   - outcome `approved`: the control plane calls `consume(approval_id, call)` at the point where
     it performs the write, and runs the tool only if `ok` is True, with `args` from the result
     and its `idempotency_key`.
   - any other outcome: do not run the tool.
   - If the caller is cancelled after `consume` (for example, a timeout), it may call `consume`
     again for the same approval and the same tool call. The retry returns the same `args` and
     `idempotency_key` with `replayed=True`, until `use_by`. The executor dedupes on the key.

`consume` must run in the control plane (the MCP server or the operation executor), never in
the sandboxed adapter: a check in the sandbox enforces nothing (D4, Section 10.10 step 4).

Fail closed: `evaluate`, `check`, and `consume` never raise; every failure is `deny` or
`ok=False`. If any call raises anyway, or a `ProposedCall` cannot be built, treat it as a denial.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from harness.approvals.model import ApprovalCheck, ApprovalRequest, Decision, ProposedCall


@runtime_checkable
class ApprovalPolicy(Protocol):
    async def evaluate(self, call: ProposedCall) -> Decision:
        """ALLOW, ASK (with approver roles), or DENY for this tool call. Never raises."""
        ...

    async def request(self, call: ProposedCall, *, note: str | None = None) -> ApprovalRequest:
        """Create (or return the existing) approval request for an `ask` tool call.

        The harness renders the preview from the arguments. `note` is optional agent text that
        the approver sees as context only. Raises ActionDeniedError or ApprovalNotRequiredError
        when the policy does not ask, and ConflictError when the tool call already has an
        approval for different arguments.
        """
        ...

    async def check(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck:
        """Read-only: True only if `consume` would succeed now. Never raises."""
        ...

    async def consume(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck:
        """Use the approval, in the control plane, right before the write. Never raises.

        Idempotent per tool call: a repeat by the same tool call returns the same grant
        (`replayed=True`) and writes nothing. Another tool call is refused.
        """
        ...
