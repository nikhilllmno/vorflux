"""Typed proposals per action kind, and the preview that the harness renders from them.

Section 10.10: the agent prepares a structured proposal, and the harness shows the
plain-language preview. The approver sees only what the harness renders here, never free text
from the agent as the main content.

Rules:

- Each kind that can need an approval has one strict pydantic model. Unknown fields are refused
  (`extra="forbid"`) and values are not coerced (`strict=True`), so every argument that the
  approval binds to appears in the preview.
- Arguments that do not fit the model of their kind give `Deny`.
- Facts on the card come from the harness, not the agent (`PreviewContext`):
  - the system name and the tool come from the tool registry;
  - a target is an id in the arguments, and its label comes from the hotel config. An unknown
    id gives `Deny`.
- A card title holds only registry names, hotel-config labels, and closed or validated values:
  the PMS `setting` and the message `channel` come from fixed lists with fixed labels, and a
  repository or branch name must match a strict pattern (no spaces). Anything else gives `Deny`.
- A value that only the agent knows (an old value, a count of guests or records) is shown as
  "agent reported, not checked" (`PreviewField.verified` is False), or left out if absent.
- Guest data and written content are `sensitive`: only a principal who can decide sees them.
- `read` has no model: if a rule makes a read ask, the preview lists every argument (sensitive).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

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
TargetId = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^\S+$")]
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Scalar = str | int | float | bool | None
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]
RepositoryName = Annotated[
    str, StringConstraints(max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
]
BranchName = Annotated[str, StringConstraints(max_length=200, pattern=r"^[A-Za-z0-9_./-]+$")]

# The PMS settings that a `pms_change` can write, with the words that the card shows. A new
# setting is a code change, so the agent cannot put its own words in the title.
PmsSetting = Literal[
    "rate",
    "availability",
    "min_stay",
    "max_stay",
    "stop_sell",
    "closed_to_arrival",
    "closed_to_departure",
]
PMS_SETTING_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "rate": "rate",
        "availability": "availability",
        "min_stay": "minimum stay",
        "max_stay": "maximum stay",
        "stop_sell": "stop sell",
        "closed_to_arrival": "closed to arrival",
        "closed_to_departure": "closed to departure",
    }
)

# The message channels, with the words that the card title shows.
MessageChannel = Literal["email", "sms", "whatsapp"]
MESSAGE_CHANNEL_LABELS: Mapping[str, str] = MappingProxyType(
    {"email": "an email", "sms": "an SMS", "whatsapp": "a WhatsApp message"}
)


class UnknownTargetError(ValueError):
    """The arguments name a target id that the hotel config does not know."""


@dataclass(frozen=True, slots=True)
class PreviewContext:
    """Facts from the harness, not the agent: the registry's system and tool names, and the
    hotel config's labels for the target ids in the arguments."""

    toolset: str
    tool: str
    system: str | None = None
    targets: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def tool_field(self) -> PreviewField:
        return PreviewField("Tool", f"{self.toolset}/{self.tool}")

    def system_name(self) -> str:
        if not self.system:
            raise ValueError(f"tool {self.toolset}/{self.tool} has no system in the registry")
        return self.system

    def target_label(self, target_id: str) -> str:
        label = self.targets.get(target_id)
        if label is None:
            # A fixed text: the id is agent input and can hold guest data.
            raise UnknownTargetError(f"target is not in the hotel config for {self.toolset}")
        return label


def show(value: Any) -> str:
    """One value as the approver reads it. Strings as they are; everything else as JSON."""
    return value if isinstance(value, str) else canonical_json(value)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _reported(label: str, value: Any) -> PreviewField:
    """A value that only the agent reports. The harness does not check it."""
    return PreviewField(label, show(value), verified=False)


class Proposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    def target_ids(self) -> tuple[str, ...]:
        """Target ids whose labels come from the hotel config."""
        return ()

    def preview(self, context: PreviewContext) -> Preview:
        raise NotImplementedError


class PmsChange(Proposal):
    """A rate, availability, or restriction change in a PMS (one of `PmsSetting`)."""

    target_id: TargetId  # for example a room type id; the label comes from the hotel config
    setting: PmsSetting
    new: Scalar
    date: IsoDate | None = None  # the day the change applies to, if any
    old: Scalar = None  # agent reported
    guests_affected: int | None = Field(default=None, ge=0)  # agent reported

    @field_validator("date")
    @classmethod
    def _real_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    def target_ids(self) -> tuple[str, ...]:
        return (self.target_id,)

    def preview(self, context: PreviewContext) -> Preview:
        target = context.target_label(self.target_id)
        if self.date is not None:
            day = date.fromisoformat(self.date)
            target = f"{target}, {day.day} {day:%b %Y}"
        fields = []
        if "old" in self.model_fields_set:
            fields.append(_reported("From", "(not set)" if self.old is None else self.old))
        fields.append(PreviewField("To", show(self.new)))
        if self.guests_affected is not None:
            fields.append(_reported("Guests affected", self.guests_affected))
        fields.append(context.tool_field())
        setting = PMS_SETTING_LABELS[self.setting]
        title = f"Change {setting} of {target} in {context.system_name()}"
        return Preview(title, tuple(fields))


class MessageSend(Proposal):
    """A guest message or campaign. It cannot be unsent."""

    channel: MessageChannel
    recipients: list[Text] = Field(min_length=1, max_length=100)
    subject: Text | None = None
    body: LongText

    def preview(self, context: PreviewContext) -> Preview:
        fields = [PreviewField("Recipients", ", ".join(self.recipients), sensitive=True)]
        if self.subject is not None:
            fields.append(PreviewField("Subject", self.subject, sensitive=True))
        fields.append(PreviewField("Message", self.body, sensitive=True))
        fields.append(context.tool_field())
        recipients = _plural(len(self.recipients), "recipient")
        channel = MESSAGE_CHANNEL_LABELS[self.channel]
        title = f"Send {channel} to {recipients} via {context.system_name()}"
        return Preview(title, tuple(fields))


class ExternalWrite(Proposal):
    """Any other write to a system outside the harness."""

    target_id: TargetId
    changes: dict[Text, Scalar] = Field(min_length=1, max_length=50)

    def target_ids(self) -> tuple[str, ...]:
        return (self.target_id,)

    def preview(self, context: PreviewContext) -> Preview:
        fields = [
            PreviewField(key, show(self.changes[key]), sensitive=True)
            for key in sorted(self.changes)
        ]
        fields.append(context.tool_field())
        title = f"Write to {context.target_label(self.target_id)} in {context.system_name()}"
        return Preview(title, tuple(fields))


class Delete(Proposal):
    """Delete data in a hotel or external system."""

    target_id: TargetId
    count: int = Field(ge=1)  # agent reported
    guests_affected: int | None = Field(default=None, ge=0)  # agent reported

    def target_ids(self) -> tuple[str, ...]:
        return (self.target_id,)

    def preview(self, context: PreviewContext) -> Preview:
        fields = [_reported("Records", self.count)]
        if self.guests_affected is not None:
            fields.append(_reported("Guests affected", self.guests_affected))
        fields.append(context.tool_field())
        target = context.target_label(self.target_id)
        return Preview(f"Delete records: {target} in {context.system_name()}", tuple(fields))


class GitPush(Proposal):
    repository: RepositoryName  # owner/name
    branch: BranchName
    commit: CommitSha

    def preview(self, context: PreviewContext) -> Preview:
        return Preview(
            f"Push {self.branch} to {self.repository}",
            (PreviewField("Commit", self.commit), context.tool_field()),
        )


class CodeChange(Proposal):
    repository: RepositoryName
    branch: BranchName
    paths: list[Text] = Field(min_length=1, max_length=200)
    description: LongText

    def preview(self, context: PreviewContext) -> Preview:
        return Preview(
            f"Change {_plural(len(self.paths), 'file')} in {self.repository}",
            (
                PreviewField("Branch", self.branch),
                PreviewField("Files", ", ".join(self.paths)),
                PreviewField("Description", self.description),
                context.tool_field(),
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


def _read_preview(args: JsonObject, context: PreviewContext) -> Preview:
    fields = [PreviewField(key, show(args[key]), sensitive=True) for key in sorted(args)]
    return Preview("Read data", (*fields, context.tool_field()))


def parse_proposal(kind: ActionKind, args: JsonObject) -> Proposal | None:
    """The typed proposal (None for `read`). Raises ValueError if the arguments do not fit."""
    if kind == ActionKind.READ:
        return None
    proposal_type = PROPOSAL_TYPES[ActionKind(kind)]
    return proposal_type.model_validate(thaw_json(args))


def render_preview(kind: ActionKind, args: JsonObject, context: PreviewContext) -> Preview:
    """The approver's preview for these arguments.

    Raises ValueError if they do not fit `kind`, and UnknownTargetError (a ValueError) if a
    target id is not in `context.targets`.
    """
    proposal = parse_proposal(kind, args)
    if proposal is None:
        return _read_preview(args, context)
    return proposal.preview(context)
