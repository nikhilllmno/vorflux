"""The tool registry: the only source of an action's kind and risk.

A tool call names a toolset and a tool. The registry maps that pair to its kind and risk. A
caller cannot label a call: an unregistered tool is denied, whatever the adapter thinks it is.
Each harness MCP tool pack (Section 10.10) registers its tools here when the control plane starts.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from harness.approvals.model import ActionKind, RiskLevel


@dataclass(frozen=True, slots=True)
class ToolSpec:
    toolset: str
    tool: str
    kind: ActionKind
    risk: RiskLevel

    def __post_init__(self) -> None:
        if not self.toolset or not self.tool:
            raise ValueError("a tool spec needs a toolset and a tool")
        object.__setattr__(self, "kind", ActionKind(self.kind))
        object.__setattr__(self, "risk", RiskLevel(self.risk))


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
