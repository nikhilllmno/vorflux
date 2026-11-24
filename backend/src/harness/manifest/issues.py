"""Validation issues with readable paths, such as `agents[1].id` or `change_classes.secrets`."""

from collections.abc import Sequence
from dataclasses import dataclass

ROOT = "(root)"
YAML = "(yaml)"


@dataclass(frozen=True, slots=True)
class ManifestIssue:
    """One problem in a manifest.

    `path` points at the key or list item (`agents[1].id`), or is `(yaml)` for a problem in the
    YAML text and `(root)` for the document as a whole. `code` is a stable machine-readable name.
    """

    path: str
    message: str
    code: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ManifestValidationError(ValueError):
    """Raised by `load_manifest` when a manifest is invalid. Holds every issue found."""

    def __init__(self, issues: Sequence[ManifestIssue]) -> None:
        self.issues: tuple[ManifestIssue, ...] = tuple(issues)
        lines = "\n".join(f"  {issue}" for issue in self.issues)
        super().__init__(f"invalid harness.yaml ({len(self.issues)} issue(s)):\n{lines}")
