"""Typed proposals per action kind, and the preview that the harness renders from them.

Section 10.10: the agent prepares a structured proposal (system, object, old value, new value,
guests affected), and the harness shows the plain-language preview. The approver sees only what
the harness renders here, never free text from the agent as the main content.

Rules:

- Each kind that can need an approval has one strict pydantic model. Unknown fields are refused
  (`extra="forbid"`) and values are not coerced (`strict=True`), so every argument that the
  approval binds to appears in the preview.
- Arguments that do not fit the model of their kind give `Deny`.
- `read` has no model: if a rule makes a read ask, the preview lists every argument.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from harness.approvals.model import (
    ActionKind,
    JsonObject,
    Preview,
    PreviewField,
    canonical_json,
    thaw_json,
)

Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=5000)]
Scalar = str | int | float | bool | None
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]


def show(value: Any) -> str:
    """One value as the approver reads it. Strings as they are; everything else as JSON."""
    return value if isinstance(value, str) else canonical_json(value)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


class Proposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    def preview(self) -> Preview:
        raise NotImplementedError


class PmsChange(Proposal):
    """A rate, inventory, restriction, or booking change in a PMS."""

    system: Text  # for example "Beds24"
    target: Text  # for example "Deluxe King, 24 Dec 2026"
    setting: Text  # for example "rate"
    old: Scalar = None
    new: Scalar
    guests_affected: int = Field(default=0, ge=0)

    def preview(self) -> Preview:
        return Preview(
            f"Change {self.setting} of {self.target} in {self.system}",
            (
                PreviewField("From", "(not set)" if self.old is None else show(self.old)),
                PreviewField("To", show(self.new)),
                PreviewField("Guests affected", str(self.guests_affected)),
            ),
        )


class MessageSend(Proposal):
    """A guest message or campaign. It cannot be unsent."""

    channel: Text  # for example "email" or "whatsapp"
    recipients: list[Text] = Field(min_length=1, max_length=100)
    subject: Text | None = None
    body: LongText

    def preview(self) -> Preview:
        fields = [PreviewField("Recipients", ", ".join(self.recipients))]
        if self.subject is not None:
            fields.append(PreviewField("Subject", self.subject))
        fields.append(PreviewField("Message", self.body))
        title = f"Send a {self.channel} message to {_plural(len(self.recipients), 'recipient')}"
        return Preview(title, tuple(fields))


class ExternalWrite(Proposal):
    """Any other write to a system outside the harness."""

    system: Text
    target: Text
    changes: dict[Text, Scalar] = Field(min_length=1, max_length=50)

    def preview(self) -> Preview:
        fields = tuple(PreviewField(key, show(self.changes[key])) for key in sorted(self.changes))
        return Preview(f"Write to {self.target} in {self.system}", fields)


class Delete(Proposal):
    """Delete data in a hotel or external system."""

    system: Text
    target: Text
    count: int = Field(ge=1)
    guests_affected: int = Field(default=0, ge=0)

    def preview(self) -> Preview:
        return Preview(
            f"Delete {_plural(self.count, 'record')}: {self.target} in {self.system}",
            (PreviewField("Guests affected", str(self.guests_affected)),),
        )


class GitPush(Proposal):
    repository: Text
    branch: Text
    commit: CommitSha

    def preview(self) -> Preview:
        return Preview(
            f"Push {self.branch} to {self.repository}", (PreviewField("Commit", self.commit),)
        )


class CodeChange(Proposal):
    repository: Text
    branch: Text
    paths: list[Text] = Field(min_length=1, max_length=200)
    description: LongText

    def preview(self) -> Preview:
        return Preview(
            f"Change {_plural(len(self.paths), 'file')} in {self.repository}",
            (
                PreviewField("Branch", self.branch),
                PreviewField("Files", ", ".join(self.paths)),
                PreviewField("Description", self.description),
            ),
        )


PROPOSAL_TYPES: Mapping[ActionKind, type[Proposal]] = {
    ActionKind.PMS_CHANGE: PmsChange,
    ActionKind.MESSAGE_SEND: MessageSend,
    ActionKind.EXTERNAL_WRITE: ExternalWrite,
    ActionKind.DELETE: Delete,
    ActionKind.GIT_PUSH: GitPush,
    ActionKind.CODE_CHANGE: CodeChange,
}


def _read_preview(args: JsonObject) -> Preview:
    return Preview("Read data", tuple(PreviewField(key, show(args[key])) for key in sorted(args)))


def render_preview(kind: ActionKind, args: JsonObject) -> Preview:
    """The approver's preview for these arguments. Raises ValueError if they do not fit `kind`."""
    if kind == ActionKind.READ:
        return _read_preview(args)
    proposal_type = PROPOSAL_TYPES[ActionKind(kind)]
    return proposal_type.model_validate(thaw_json(args)).preview()
