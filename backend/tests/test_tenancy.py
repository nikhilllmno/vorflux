from uuid import uuid4

from harness.tenancy import TenantContext


def _tenant(*repos: str) -> TenantContext:
    return TenantContext(
        org_id=uuid4(), hotel_id=uuid4(), actor_id=uuid4(), allowed_repos=frozenset(repos)
    )


def test_repo_access_requires_exact_allowlist_match() -> None:
    tenant = _tenant("acme-hotels/voice-agent")

    assert tenant.can_access_repo("acme-hotels/voice-agent")
    assert tenant.can_access_repo("Acme-Hotels/Voice-Agent")
    assert not tenant.can_access_repo("acme-hotels/voice-agent-fork")
    assert not tenant.can_access_repo("other-hotel/voice-agent")


def test_empty_allowlist_denies_everything() -> None:
    assert not _tenant().can_access_repo("acme-hotels/voice-agent")
