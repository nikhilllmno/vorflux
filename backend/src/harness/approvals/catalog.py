"""The tool registry: the only source of an action's kind, risk, and target system.

A tool call names a toolset and a tool. The registry maps that pair to its kind and risk. A
caller cannot label a call: an unregistered tool is denied, whatever the adapter thinks it is.
Each harness MCP tool pack (Section 10.10) registers its tools here when the control plane starts.

A hotel operation also names the `system` that the tool writes to (for example "Beds24"). The
approval card shows this name, never a system name from the arguments.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from harness.approvals.model import ACTION_CLASSES, ActionClass, ActionKind, RiskLevel


@dataclass(frozen=True, slots=True)
class ToolSpec:
    toolset: str
    tool: str
    kind: ActionKind
    risk: RiskLevel
    system: str | None = None  # display name of the system the tool writes to

    def __post_init__(self) -> None:
        if not self.toolset or not self.tool:
            raise ValueError("a tool spec needs a toolset and a tool")
        object.__setattr__(self, "kind", ActionKind(self.kind))
        object.__setattr__(self, "risk", RiskLevel(self.risk))
        if self.system is not None and not self.system.strip():
            raise ValueError("a tool spec system cannot be empty")
        if ACTION_CLASSES[self.kind] == ActionClass.HOTEL_OPERATION and self.system is None:
            raise ValueError("a hotel operation tool needs the system it writes to")


class ToolCatalog:
    """Immutable after construction. Registering one (toolset, tool) twice is an error."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        index: dict[tuple[str, str], ToolSpec] = {}
        for spec in specs:
            key = (spec.toolset, spec.tool)
            if key in index:
                raise ValueError(f"tool registered twice: {spec.toolset}/{spec.tool}")
            index[key] = spec
        self._specs = index

    def get(self, toolset: str, tool: str) -> ToolSpec | None:
        return self._specs.get((toolset, tool))

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)
