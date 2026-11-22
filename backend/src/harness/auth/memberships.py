"""Where the API reads a user's hotel role (docs/auth.md, "Membership provider").

The rules match the `public.memberships` and `public.hotels` tables of the tenancy schema
(DEX-10, https://github.com/nikhilllmno/vorflux/pull/5):

- A membership row has `org_id`, `user_id`, `role` and an optional `hotel_id`. `hotel_id` null
  means the role covers every hotel of the org; otherwise it covers that one hotel.
- The hotel of a membership belongs to the membership's org. `owner` is org-wide only. There is
  one row per `(org_id, user_id, hotel_id)`.
- A user's role in a hotel is the highest role among the rows that cover it, and only if the
  hotel belongs to the org named in the request.

The Postgres provider (after PR #5 merges) runs one query with the service role:

    select m.role
    from public.memberships m
    join public.hotels h on h.org_id = m.org_id and h.id = $3
    where m.user_id = $1 and m.org_id = $2 and (m.hotel_id is null or m.hotel_id = $3)

and returns the highest role, or None when no row matches.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from harness.tenancy import HotelRole


@runtime_checkable
class MembershipProvider(Protocol):
    async def role_for(self, user_id: UUID, org_id: UUID, hotel_id: UUID) -> HotelRole | None:
        """The user's effective role in this hotel of this org, or None.

        None when the user has no covering membership, when the hotel is not in the org, and
        when either does not exist. Callers must not tell these cases apart.
        """
        ...


class InMemoryMembershipProvider:
    """For tests and local development only. The API refuses it in staging and production."""

    def __init__(self) -> None:
        self._hotel_org: dict[UUID, UUID] = {}
        self._roles: dict[tuple[UUID, UUID, UUID | None], HotelRole] = {}

    def add_hotel(self, org_id: UUID, hotel_id: UUID) -> None:
        if self._hotel_org.setdefault(hotel_id, org_id) != org_id:
            raise ValueError("a hotel belongs to one org")

    def add_membership(
        self, user_id: UUID, org_id: UUID, role: HotelRole, *, hotel_id: UUID | None = None
    ) -> None:
        role = HotelRole(role)
        if hotel_id is not None and self._hotel_org.get(hotel_id) != org_id:
            raise ValueError("the membership's hotel must belong to its org")
        if role is HotelRole.OWNER and hotel_id is not None:
            raise ValueError("owner is an org-wide role")
        scope = (org_id, user_id, hotel_id)
        if scope in self._roles:
            raise ValueError("one membership per user and scope")
        self._roles[scope] = role

    def remove_membership(
        self, user_id: UUID, org_id: UUID, *, hotel_id: UUID | None = None
    ) -> None:
        self._roles.pop((org_id, user_id, hotel_id), None)

    async def role_for(self, user_id: UUID, org_id: UUID, hotel_id: UUID) -> HotelRole | None:
        if self._hotel_org.get(hotel_id) != org_id:
            return None
        covering = [
            role
            for scope in ((org_id, user_id, None), (org_id, user_id, hotel_id))
            if (role := self._roles.get(scope)) is not None
        ]
        return max(covering, key=lambda role: role.rank, default=None)
