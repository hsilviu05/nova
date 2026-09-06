"""AuthService paths that are hard to reach through HTTP alone.

Deactivated accounts, expired refresh tokens, hash upgrades, and the
concurrent-registration race all need direct control over database state.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nova.core.clock import utc_now
from nova.core.config import SecuritySettings, Settings
from nova.core.errors import AuthenticationError, ConflictError
from nova.core.security import Argon2PasswordHasher, TokenService
from nova.models.refresh_token import RefreshToken
from nova.models.user import User
from nova.repositories.refresh_token import RefreshTokenRepository
from nova.repositories.user import UserRepository
from nova.services.auth import AuthService

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def auth_service(session: AsyncSession, settings: Settings) -> AuthService:
    return AuthService(
        users=UserRepository(session),
        refresh_tokens=RefreshTokenRepository(session),
        hasher=Argon2PasswordHasher(settings.security),
        tokens=TokenService(settings.jwt),
    )


async def _register(service: AuthService, session: AsyncSession) -> User:
    email = f"svc-{uuid.uuid4().hex[:12]}@example.com"
    await service.register(email=email, password=PASSWORD, display_name="Svc")
    await session.flush()
    user = await UserRepository(session).get_by_email(email)
    assert user is not None
    return user


class TestDeactivatedAccounts:
    async def test_login_is_refused(self, auth_service: AuthService, session: AsyncSession) -> None:
        user = await _register(auth_service, session)
        user.is_active = False
        await session.flush()

        with pytest.raises(AuthenticationError) as exc:
            await auth_service.login(email=user.email, password=PASSWORD)
        # Same code as a wrong password: deactivation must not be observable.
        assert exc.value.code == "invalid_credentials"

    async def test_refresh_is_refused(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        """Deactivation must take effect before the refresh token expires."""
        email = f"svc-{uuid.uuid4().hex[:12]}@example.com"
        result = await auth_service.register(email=email, password=PASSWORD, display_name="Svc")
        await session.flush()

        user = await UserRepository(session).get_by_email(email)
        assert user is not None
        user.is_active = False
        await session.flush()

        with pytest.raises(AuthenticationError) as exc:
            await auth_service.refresh(refresh_token=result.tokens.refresh_token)
        assert exc.value.code == "invalid_refresh_token"


class TestExpiredRefreshTokens:
    async def test_expired_token_is_refused(
        self, auth_service: AuthService, session: AsyncSession, settings: Settings
    ) -> None:
        email = f"svc-{uuid.uuid4().hex[:12]}@example.com"
        result = await auth_service.register(email=email, password=PASSWORD, display_name="Svc")
        await session.flush()

        digest = TokenService(settings.jwt).hash_refresh_token(result.tokens.refresh_token)
        stored = await RefreshTokenRepository(session).get_by_hash(digest)
        assert stored is not None
        # Backdate rather than sleep.
        stored.expires_at = utc_now() - timedelta(seconds=1)
        await session.flush()

        with pytest.raises(AuthenticationError):
            await auth_service.refresh(refresh_token=result.tokens.refresh_token)

    async def test_expired_token_does_not_revoke_the_family(
        self, auth_service: AuthService, session: AsyncSession, settings: Settings
    ) -> None:
        """Expiry is not compromise.

        A token that merely aged out must not trigger the family-wide
        revocation reserved for detected replay.
        """
        email = f"svc-{uuid.uuid4().hex[:12]}@example.com"
        result = await auth_service.register(email=email, password=PASSWORD, display_name="Svc")
        await session.flush()

        tokens = TokenService(settings.jwt)
        repo = RefreshTokenRepository(session)
        stored = await repo.get_by_hash(tokens.hash_refresh_token(result.tokens.refresh_token))
        assert stored is not None
        stored.expires_at = utc_now() - timedelta(seconds=1)
        await session.flush()

        with pytest.raises(AuthenticationError):
            await auth_service.refresh(refresh_token=result.tokens.refresh_token)

        await session.refresh(stored)
        assert stored.revoked_at is None


class TestPasswordHashUpgrade:
    async def test_login_rehashes_a_weak_stored_hash(
        self, session: AsyncSession, settings: Settings
    ) -> None:
        weak = Argon2PasswordHasher(SecuritySettings(argon2_time_cost=1, argon2_memory_cost_kib=8))
        strong = Argon2PasswordHasher(
            SecuritySettings(argon2_time_cost=3, argon2_memory_cost_kib=16)
        )

        email = f"svc-{uuid.uuid4().hex[:12]}@example.com"
        user = User(
            email=email,
            password_hash=weak.hash(PASSWORD),
            display_name="Legacy",
        )
        session.add(user)
        await session.flush()
        original_hash = user.password_hash

        service = AuthService(
            users=UserRepository(session),
            refresh_tokens=RefreshTokenRepository(session),
            hasher=strong,
            tokens=TokenService(settings.jwt),
        )
        await service.login(email=email, password=PASSWORD)
        await session.flush()

        assert user.password_hash != original_hash
        assert strong.verify(PASSWORD, user.password_hash)
        assert not strong.needs_rehash(user.password_hash)


class TestRegistrationRace:
    async def test_duplicate_insert_becomes_a_conflict(
        self, auth_service: AuthService, session: AsyncSession, settings: Settings
    ) -> None:
        """The unique index, not the pre-check, is the real guard.

        Two simultaneous registrations both pass ``exists_by_email``; the
        loser must surface as 409, not as an unhandled IntegrityError.
        """
        email = f"svc-{uuid.uuid4().hex[:12]}@example.com"
        session.add(
            User(
                email=email,
                password_hash=Argon2PasswordHasher(settings.security).hash(PASSWORD),
                display_name="First",
            )
        )
        await session.flush()

        # Bypass the pre-check the way a concurrent request would, by pointing
        # the service at a repository that reports the email as free.
        class RaceRepository(UserRepository):
            async def exists_by_email(self, email: str) -> bool:
                return False

        racing = AuthService(
            users=RaceRepository(session),
            refresh_tokens=RefreshTokenRepository(session),
            hasher=Argon2PasswordHasher(settings.security),
            tokens=TokenService(settings.jwt),
        )

        with pytest.raises(ConflictError) as exc:
            await racing.register(email=email, password=PASSWORD, display_name="Second")
        assert exc.value.code == "email_already_registered"


class TestSessionRevocation:
    async def test_revoke_all_reports_the_count(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _register(auth_service, session)
        await auth_service.login(email=user.email, password=PASSWORD)
        await auth_service.login(email=user.email, password=PASSWORD)
        await session.flush()

        revoked = await auth_service.revoke_all_sessions(user.id)
        assert revoked == 3

    async def test_revoking_twice_reports_zero(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        user = await _register(auth_service, session)
        await session.flush()

        await auth_service.revoke_all_sessions(user.id)
        assert await auth_service.revoke_all_sessions(user.id) == 0

    async def test_revocation_does_not_touch_other_users(
        self, auth_service: AuthService, session: AsyncSession
    ) -> None:
        victim = await _register(auth_service, session)
        bystander = await _register(auth_service, session)
        await session.flush()

        await auth_service.revoke_all_sessions(victim.id)

        stored = await RefreshTokenRepository(session).session.get(RefreshToken, uuid.uuid4())
        assert stored is None  # sanity: unknown id yields nothing
        assert await auth_service.revoke_all_sessions(bystander.id) == 1
