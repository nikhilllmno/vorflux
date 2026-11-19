"""Hotel config lookups for the approval card.

A tool call names its target by id (for example a room type id). The approver sees the label
that the hotel config gives that id, never a label that the agent wrote. An id that the hotel
config does not know gives `Deny`.

The production directory reads the hotel config (DEX-10 tenancy tables and the per-hotel tool
pack config). Labels are scoped by org, hotel, and toolset.
"""

from typing import Protocol
from uuid import UUID


class HotelDirectory(Protocol):
    async def target_label(
        self, org_id: UUID, hotel_id: UUID, toolset: str, target_id: str
    ) -> str | None:
        """The label of `target_id` in this hotel's config for `toolset`, or None if unknown."""
        ...


class InMemoryHotelDirectory:
    """For tests and local development."""

    def __init__(self) -> None:
        self._labels: dict[tuple[UUID, UUID, str, str], str] = {}

    def set_label(
        self, org_id: UUID, hotel_id: UUID, toolset: str, target_id: str, label: str
    ) -> None:
        if not label.strip():
            raise ValueError("a target label cannot be empty")
        self._labels[(org_id, hotel_id, toolset, target_id)] = label.strip()

    async def target_label(
        self, org_id: UUID, hotel_id: UUID, toolset: str, target_id: str
    ) -> str | None:
        return self._labels.get((org_id, hotel_id, toolset, target_id))
