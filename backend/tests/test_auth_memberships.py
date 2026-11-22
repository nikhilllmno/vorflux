"""The in-memory MembershipProvider follows the PR #5 `memberships` rules (docs/auth.md)."""

import asyncio
from uuid import UUID, uuid4

import pytest

from harness.auth import InMemoryMembershipProvider
from harness.tenancy import HotelRole


def role(provider: InMemoryMembershipProvider, user: UUID, org: UUID, hotel: UUID) -> object:
    return asyncio.run(provider.role_for(user, org, hotel))


@pytest.fixture
def ids() -> dict[str, UUID]:
    return {name: uuid4() for name in ("user", "org_a", "org_b", "a1", "a2", "b1")}


@pytest.fixture
def provider(ids: dict[str, UUID]) -> InMemoryMembershipProvider:
    provider = InMemoryMembershipProvider()
    provider.add_hotel(ids["org_a"], ids["a1"])
    provider.add_hotel(ids["org_a"], ids["a2"])
    provider.add_hotel(ids["org_b"], ids["b1"])
    return provider


def test_no_membership_gives_no_role(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    assert role(provider, ids["user"], ids["org_a"], ids["a1"]) is None


def test_org_wide_membership_covers_every_hotel_of_that_org_only(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    provider.add_membership(ids["user"], ids["org_a"], HotelRole.OPERATOR)

    assert role(provider, ids["user"], ids["org_a"], ids["a1"]) == HotelRole.OPERATOR
    assert role(provider, ids["user"], ids["org_a"], ids["a2"]) == HotelRole.OPERATOR
    # A hotel of another org, named under our org, is not covered.
    assert role(provider, ids["user"], ids["org_a"], ids["b1"]) is None
    assert role(provider, ids["user"], ids["org_b"], ids["b1"]) is None
    assert role(provider, ids["user"], ids["org_a"], uuid4()) is None


def test_hotel_scoped_membership_covers_that_hotel_only(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    provider.add_membership(ids["user"], ids["org_a"], HotelRole.ADMIN, hotel_id=ids["a1"])

    assert role(provider, ids["user"], ids["org_a"], ids["a1"]) == HotelRole.ADMIN
    assert role(provider, ids["user"], ids["org_a"], ids["a2"]) is None


def test_highest_covering_role_wins(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    provider.add_membership(ids["user"], ids["org_a"], HotelRole.VIEWER)
    provider.add_membership(ids["user"], ids["org_a"], HotelRole.ADMIN, hotel_id=ids["a1"])

    assert role(provider, ids["user"], ids["org_a"], ids["a1"]) == HotelRole.ADMIN
    assert role(provider, ids["user"], ids["org_a"], ids["a2"]) == HotelRole.VIEWER


def test_membership_hotel_must_belong_to_the_org(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    with pytest.raises(ValueError):
        provider.add_membership(ids["user"], ids["org_a"], HotelRole.ADMIN, hotel_id=ids["b1"])


def test_owner_is_org_wide_only(provider: InMemoryMembershipProvider, ids: dict[str, UUID]) -> None:
    with pytest.raises(ValueError):
        provider.add_membership(ids["user"], ids["org_a"], HotelRole.OWNER, hotel_id=ids["a1"])


def test_one_membership_per_user_and_scope(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    provider.add_membership(ids["user"], ids["org_a"], HotelRole.VIEWER)

    with pytest.raises(ValueError):
        provider.add_membership(ids["user"], ids["org_a"], HotelRole.ADMIN)


def test_removed_membership_takes_effect_at_once(
    provider: InMemoryMembershipProvider, ids: dict[str, UUID]
) -> None:
    provider.add_membership(ids["user"], ids["org_a"], HotelRole.ADMIN)

    provider.remove_membership(ids["user"], ids["org_a"])

    assert role(provider, ids["user"], ids["org_a"], ids["a1"]) is None
