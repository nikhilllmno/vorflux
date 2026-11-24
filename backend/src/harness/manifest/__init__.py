"""The `harness.yaml` manifest: schema v1, validator and path rules (DEX-30).

See docs/harness-yaml.md for the reference and docs/architecture.md, Section 9.3, for the design.
"""

from harness.manifest.issues import ManifestIssue, ManifestValidationError
from harness.manifest.paths import UnsafePathError
from harness.manifest.schema import (
    DEFAULT_PROTECTED_PATHS,
    HARNESS_NEVER_EDITS,
    MANIFEST_FILENAME,
    SCHEMA_VERSION,
    AgentType,
    ChangeClass,
    Manifest,
    ProductRuntime,
    TextAgent,
    VoiceAgent,
)
from harness.manifest.validate import (
    ValidationResult,
    load_manifest,
    manifest_json_schema,
    validate_manifest,
)

__all__ = [
    "DEFAULT_PROTECTED_PATHS",
    "HARNESS_NEVER_EDITS",
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "AgentType",
    "ChangeClass",
    "Manifest",
    "ManifestIssue",
    "ManifestValidationError",
    "ProductRuntime",
    "TextAgent",
    "UnsafePathError",
    "ValidationResult",
    "VoiceAgent",
    "load_manifest",
    "manifest_json_schema",
    "validate_manifest",
]
