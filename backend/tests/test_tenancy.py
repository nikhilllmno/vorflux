from uuid import uuid4

import pytest

from harness.tenancy import Action, HotelRole, TenantContext, min_role_for


def _tenant(*repos: str, role: HotelRole | None = None) -> TenantContext:
    return TenantContext(
        org_id=uuid4(),
        hotel_id=uuid4(),
        actor_id=uuid4(),
        allowed_repos=frozenset(repos),
        role=role,
    )


def test_repo_access_requires_exact_allowlist_match() -> None:
    tenant = _tenant("acme-hotels/voice-agent")

    assert tenant.can_access_repo("acme-hotels/voice-agent")
    assert tenant.can_access_repo("Acme-Hotels/Voice-Agent")
    assert not tenant.can_access_repo("acme-hotels/voice-agent-fork")
    assert not tenant.can_access_repo("other-hotel/voice-agent")


def test_empty_allowlist_denies_everything() -> None:
    assert not _tenant().can_access_repo("acme-hotels/voice-agent")


# --- Roles and the minimum role per action (docs/auth.md) --------------------------------------


def test_hotel_roles_match_the_pr5_enum_in_rank_order() -> None:
    assert [r.value for r in sorted(HotelRole, key=lambda r: r.rank)] == [
        "viewer",
        "operator",
        "admin",
        "owner",
    ]
    assert HotelRole.ADMIN.at_least(HotelRole.OPERATOR)
    assert not HotelRole.OPERATOR.at_least(HotelRole.ADMIN)


@pytest.mark.parametrize(
    ("action", "lowest_allowed"),
    [
        (Action.VIEW, HotelRole.VIEWER),
        (Action.START_RUN, HotelRole.OPERATOR),
        (Action.REQUEST_CHANGE, HotelRole.OPERATOR),
        (Action.CONFIRM_CHANGE, HotelRole.OPERATOR),
        (Action.CONFIGURE_APPROVALS, HotelRole.ADMIN),
        (Action.MANAGE_MEMBERS, HotelRole.ADMIN),
        (Action.MANAGE_ORG, HotelRole.OWNER),
    ],
)
def test_each_action_needs_its_minimum_role(action: Action, lowest_allowed: HotelRole) -> None:
    assert min_role_for(action) == lowest_allowed
    for role in HotelRole:
        assert _tenant(role=role).allows(action) == role.at_least(lowest_allowed)


def test_every_action_has_a_minimum_role() -> None:
    for action in Action:
        assert isinstance(min_role_for(action), HotelRole)


def test_context_without_a_role_allows_nothing() -> None:
    tenant = _tenant()

    assert tenant.role is None
    assert not any(tenant.allows(action) for action in Action)
