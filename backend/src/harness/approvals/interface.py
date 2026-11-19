"""The small interface that runtime adapters (and the harness MCP server) call.

Flow for one gated tool call:

1. `evaluate(action)`:
   - `Allow`: run the tool.
   - `Deny`: do not run the tool; return the reason to the model as a tool error.
   - `RequireApproval`: call `request_approval`, emit `ApprovalRequired`, and pause the run.
2. When the orchestrator resumes the run with an `ApprovalResolution`:
   - outcome `approved`: call `consume(approval_id, action)` with the same action. Run the tool
     only if `ok` is True, and only with that exact payload.
   - any other outcome: do not run the tool.

Fail closed: `evaluate`, `check`, and `consume` never raise for policy reasons; every failure is
`Deny` or `ok=False`. If any call raises anyway, treat it as a denial and do not run the tool.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from harness.approvals.model import Action, ApprovalCheck, ApprovalRequest, Decision


@runtime_checkable
class ApprovalPolicy(Protocol):
    def evaluate(self, action: Action) -> Decision:
        """Allow, require approval (with the approver roles), or deny. Never raises."""
        ...

    def request_approval(self, action: Action, *, call_id: str, summary: str) -> ApprovalRequest:
        """Create a pending request bound to the exact action. `summary` is the staff preview.

        Raises `ActionDeniedError` when the policy denies the action, and
        `ApprovalNotRequiredError` when it allows the action without approval.
        """
        ...

    def check(self, approval_id: UUID, action: Action) -> ApprovalCheck:
        """Would `consume` succeed now? Read-only. Never raises."""
        ...

    def consume(self, approval_id: UUID, action: Action) -> ApprovalCheck:
        """Use an approval once. `ok` is True at most once per approval. Never raises."""
        ...
