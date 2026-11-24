"""Repo-relative paths and glob patterns in `harness.yaml`, and how they match.

A pattern is a small, explicit subset of gitignore and CODEOWNERS globs:

- POSIX and relative to the repo root: no leading `/` or `~`, no drive letter, no `\\`, no `.`
  or `..` segment, no empty segment (so no `//` and no trailing `/`).
- Anchored at the repo root: `*.pem` matches `key.pem` only; write `**/*.pem` for any depth.
- `*` matches any characters inside one segment, `?` matches one character inside one segment,
  and `**` (a whole segment) matches zero or more segments.
- No character classes, brace expansion, or negation, so a pattern means what it shows.

Paths that the harness asks about (for example the files of a diff) pass the same safety checks.
A path that fails them raises `UnsafePathError`; callers must treat that as a refusal.
"""

import re
from collections.abc import Iterable
from functools import lru_cache

MAX_PATH_LENGTH = 255

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")
_GLOB_CHARS = frozenset("*?")
_UNSUPPORTED_CHARS = frozenset("[]{}")


class UnsafePathError(ValueError):
    """A path or pattern that could leave the repo or that the matcher cannot express."""


def _check_common(value: str) -> list[str]:
    if not value:
        raise UnsafePathError("must not be empty")
    if len(value) > MAX_PATH_LENGTH:
        raise UnsafePathError(f"must be at most {MAX_PATH_LENGTH} characters")
    if _CONTROL.search(value):
        raise UnsafePathError("must not contain control characters or line breaks")
    if "\\" in value:
        raise UnsafePathError("must use forward slashes (/), not backslashes")
    if value.startswith(("/", "~")) or _DRIVE.match(value):
        raise UnsafePathError("must be relative to the repo root, not an absolute path")
    segments = value.split("/")
    for segment in segments:
        if segment in (".", ".."):
            raise UnsafePathError("must not contain '.' or '..' segments (path traversal)")
        if not segment:
            raise UnsafePathError("must not contain empty segments ('//' or a trailing '/')")
    return segments


def check_pattern(value: str) -> str:
    """Return `value` if it is a safe, supported glob pattern; raise `UnsafePathError` if not."""
    segments = _check_common(value)
    if value.startswith("!"):
        raise UnsafePathError("negation ('!') is not supported; list only the paths to match")
    if _UNSUPPORTED_CHARS.intersection(value):
        raise UnsafePathError("character classes and brace expansion are not supported")
    for segment in segments:
        if "**" in segment and segment != "**":
            raise UnsafePathError("'**' must be a whole path segment, as in 'src/**/tests/**'")
    return value


def check_plain_path(value: str) -> str:
    """Return `value` if it is a safe repo-relative path with no glob characters."""
    _check_common(value)
    if _GLOB_CHARS.intersection(value) or _UNSUPPORTED_CHARS.intersection(value):
        raise UnsafePathError("must be a plain path, not a glob pattern")
    return value


def check_query_path(value: str) -> str:
    """Check a path that the harness asks about, such as a file in a diff."""
    _check_common(value)
    return value


@lru_cache(maxsize=2048)
def _compile(pattern: str, ignore_case: bool) -> re.Pattern[str]:
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".+" if last else "(?:[^/]+/)*")
            continue
        for char in segment:
            if char == "*":
                parts.append("[^/]*")
            elif char == "?":
                parts.append("[^/]")
            else:
                parts.append(re.escape(char))
        if not last:
            parts.append("/")
    return re.compile("".join(parts), re.IGNORECASE if ignore_case else 0)


def matches(path: str, pattern: str, *, ignore_case: bool) -> bool:
    """True when the checked repo path `path` matches the checked pattern `pattern`.

    Use `ignore_case=True` where a wider match is the safe side (protected and forbidden paths, so
    "Harness.YAML" fails closed) and `ignore_case=False` where a wider match would loosen a rule
    (change classes, where no match means the strictest class).
    """
    return _compile(pattern, ignore_case).fullmatch(path) is not None


def matches_any(path: str, patterns: Iterable[str], *, ignore_case: bool) -> bool:
    return any(matches(path, pattern, ignore_case=ignore_case) for pattern in patterns)
