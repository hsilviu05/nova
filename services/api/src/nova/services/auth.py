"""Authentication and session lifecycle.

Implements registration, login, refresh-token rotation with reuse detection,
and revocation. All decisions live here; the route handlers only translate
HTTP to these calls.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.exc import IntegrityError

from nova.core.clock import utc_now
from nova.core.errors import AuthenticationError, ConflictError
from nova.core.logging import get_logger
from nova.core.security import PasswordHasher, TokenService
from nova.models.refresh_token import RefreshToken
from nova.models.user import User
from nova.repositories.refresh_token import RefreshTokenRepository
from nova.repositories.user import UserRepository
from nova.schemas.auth import AuthenticatedUser, TokenPair
from nova.schemas.user import UserRead

logger = get_logger(__name__)

# Argon2 digest of a throwaway password, hashed once per process. Login
# verifies against this when the email is unknown so that a missing account
# and a wrong password cost the same wall-clock time -- otherwise response
# latency alone reveals which emails are registered.
# S105 is a false positive: this is deliberately not a secret. It is hashed to
# burn CPU and is never compared against a user-supplied credential.
_TIMING_EQUALISATION_PASSWORD = "nova-timing-equalisation-placeholder"  # noqa: S105


class AuthService:
    """Owns every authentication decision."""

    def __init__(
        self,
        *,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        hasher: PasswordHasher,
        tokens: TokenService,
    ) -> None:
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._hasher = hasher
        self._tokens = tokens
        self._dummy_hash: str | None = None

    # -- public API -------------------------------------------------------

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthenticatedUser:
        """Create an account and sign the new user in.

        Raises:
            ConflictError: if the email is already registered.
        """
        normalised = self._normalise_email(email)

        if await self._users.exists_by_email(normalised):
            raise ConflictError(
                "An account with that email already exists.",
                code="email_already_registered",
            )

        # Argon2 is deliberately slow and would hold the event loop for the
        # whole hash, stalling every other request on this worker. argon2-cffi
        # releases the GIL, so a thread runs it truly in parallel.
        password_hash = await asyncio.to_thread(self._hasher.hash, password)
        user = User(
            email=normalised,
            password_hash=password_hash,
            display_name=display_name.strip(),
            last_login_at=utc_now(),
        )
        self._users.add(user)

        try:
            # Surface a concurrent insert now rather than at commit time,
            # where it would escape as a 500.
            await self._users.session.flush()
        except IntegrityError as exc:
            await self._users.session.rollback()
            raise ConflictError(
                "An account with that email already exists.",
                code="email_already_registered",
            ) from exc

        tokens = await self._start_session(user, user_agent=user_agent, ip_address=ip_address)
        logger.info("user_registered", user_id=str(user.id))
        return AuthenticatedUser(user=UserRead.model_validate(user), tokens=tokens)

    async def login(
        self,
        *,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthenticatedUser:
        """Verify credentials and open a new token family.

        Raises:
            AuthenticationError: on unknown email, wrong password, or a
                deactivated account. The message is identical in all three
                cases so the response cannot be used to enumerate accounts.
        """
        normalised = self._normalise_email(email)
        user = await self._users.get_by_email(normalised)

        if user is None:
            await self._equalise_timing(password)
            logger.info("login_failed", reason="unknown_email")
            raise self._invalid_credentials()

        if not await asyncio.to_thread(self._hasher.verify, password, user.password_hash):
            logger.info("login_failed", reason="bad_password", user_id=str(user.id))
            raise self._invalid_credentials()

        if not user.is_active:
            logger.info("login_failed", reason="inactive", user_id=str(user.id))
            raise self._invalid_credentials()

        # Opportunistically upgrade hashes written under older work factors.
        if self._hasher.needs_rehash(user.password_hash):
            user.password_hash = await asyncio.to_thread(self._hasher.hash, password)
            logger.info("password_hash_upgraded", user_id=str(user.id))

        user.last_login_at = utc_now()
        tokens = await self._start_session(user, user_agent=user_agent, ip_address=ip_address)
        logger.info("login_succeeded", user_id=str(user.id))
        return AuthenticatedUser(user=UserRead.model_validate(user), tokens=tokens)

    async def refresh(
        self,
        *,
        refresh_token: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> TokenPair:
        """Rotate ``refresh_token`` for a fresh pair.

        Detects replay: a token that was already rotated is only presentable
        by someone who should no longer have it, so the entire family is
        revoked and every session descended from that login dies.

        Raises:
            AuthenticationError: if the token is unknown, replayed, expired,
                or belongs to a deactivated account.
        """
        digest = self._tokens.hash_refresh_token(refresh_token)
        stored = await self._refresh_tokens.get_by_hash(digest)

        if stored is None:
            logger.info("refresh_failed", reason="unknown_token")
            raise self._invalid_refresh()

        if stored.is_revoked:
            revoked = await self._refresh_tokens.revoke_family(stored.family_id, at=utc_now())
            # Commit before raising. The request's unit of work rolls back on
            # exception, so without this the revocation would be undone by the
            # very 401 it triggers -- leaving the compromised family alive and
            # making reuse detection a log line with no effect.
            await self._refresh_tokens.session.commit()

            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(stored.user_id),
                family_id=str(stored.family_id),
                revoked_tokens=revoked,
            )
            raise self._invalid_refresh()

        if stored.is_expired:
            logger.info("refresh_failed", reason="expired", user_id=str(stored.user_id))
            raise self._invalid_refresh()

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            logger.info("refresh_failed", reason="inactive", user_id=str(stored.user_id))
            raise self._invalid_refresh()

        # Rotate: retire the presented token, mint its successor in the same
        # family so a later replay of the old one is still detectable.
        stored.revoked_at = utc_now()
        return self._issue_pair(
            user,
            family_id=stored.family_id,
            user_agent=user_agent,
            ip_address=ip_address,
        )

    async def logout(self, *, refresh_token: str) -> None:
        """Revoke the family behind ``refresh_token``.

        Silent on an unknown token: logout is idempotent, and reporting that
        a token was unrecognised would leak which digests exist.
        """
        digest = self._tokens.hash_refresh_token(refresh_token)
        stored = await self._refresh_tokens.get_by_hash(digest)
        if stored is None:
            return

        revoked = await self._refresh_tokens.revoke_family(stored.family_id, at=utc_now())
        logger.info("logout", user_id=str(stored.user_id), revoked_tokens=revoked)

    async def revoke_all_sessions(self, user_id: uuid.UUID) -> int:
        """Sign ``user_id`` out everywhere. Returns the token count revoked."""
        revoked = await self._refresh_tokens.revoke_all_for_user(user_id, at=utc_now())
        logger.info("all_sessions_revoked", user_id=str(user_id), revoked_tokens=revoked)
        return revoked

    # -- internals --------------------------------------------------------

    async def _start_session(
        self,
        user: User,
        *,
        user_agent: str | None,
        ip_address: str | None,
    ) -> TokenPair:
        """Open a brand-new token family for ``user``."""
        return self._issue_pair(
            user,
            family_id=uuid.uuid4(),
            user_agent=user_agent,
            ip_address=ip_address,
        )

    def _issue_pair(
        self,
        user: User,
        *,
        family_id: uuid.UUID,
        user_agent: str | None,
        ip_address: str | None,
    ) -> TokenPair:
        """Mint an access token and persist the digest of a refresh token."""
        access = self._tokens.issue_access_token(user.id)
        refresh = self._tokens.issue_refresh_token()

        self._refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                token_hash=refresh.digest,
                family_id=family_id,
                expires_at=refresh.expires_at,
                user_agent=user_agent[:255] if user_agent else None,
                ip_address=ip_address,
            )
        )

        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.plaintext,
            expires_in=int((access.expires_at - utc_now()).total_seconds()),
        )

    async def _equalise_timing(self, password: str) -> None:
        """Burn the same CPU an unsuccessful verify would."""
        if self._dummy_hash is None:
            self._dummy_hash = await asyncio.to_thread(
                self._hasher.hash, _TIMING_EQUALISATION_PASSWORD
            )
        await asyncio.to_thread(self._hasher.verify, password, self._dummy_hash)

    @staticmethod
    def _normalise_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _invalid_credentials() -> AuthenticationError:
        return AuthenticationError("Incorrect email or password.", code="invalid_credentials")

    @staticmethod
    def _invalid_refresh() -> AuthenticationError:
        return AuthenticationError(
            "Refresh token is invalid or has expired.", code="invalid_refresh_token"
        )
