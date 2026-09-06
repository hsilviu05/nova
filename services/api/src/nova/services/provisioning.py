"""Device provisioning and the claim flow.

    1. Device boots with no credential and calls ``provision``. It receives a
       short claim code, which it displays, and a provisioning token, which it
       keeps.
    2. A signed-in user types the code into the app and calls ``claim``. The
       device is bound to that user.
    3. The device polls ``collect`` with its provisioning token and receives
       its long-lived credential exactly once.

The two secrets do different jobs, and separating them is the whole design.
Anyone who can read the screen can claim the device to *their* account — that
is what physical presence is supposed to grant. But claiming does not reveal
the provisioning token, so it never yields the ability to impersonate the
device.

Physical possession is treated as authority to re-provision, the same way a
factory reset works on consumer hardware. Re-provisioning a claimed device
revokes its credentials and clears its owner, so a resold or recovered NOVA
does not keep streaming telemetry to whoever had it last.
"""

from __future__ import annotations

import uuid

from nova.core.clock import utc_now
from nova.core.config import DeviceSettings
from nova.core.device_security import (
    hash_claim_code,
    hash_provisioning_token,
    issue_claim_secrets,
    issue_device_token,
)
from nova.core.errors import AuthenticationError, NotFoundError
from nova.core.logging import get_logger
from nova.models.device import Device, DeviceClaim, DeviceCredential
from nova.models.user import User
from nova.repositories.device import (
    DeviceClaimRepository,
    DeviceCredentialRepository,
    DeviceRepository,
)
from nova.schemas.device import (
    DeviceRead,
    ProvisionCompleteResponse,
    ProvisionPendingResponse,
    ProvisionResponse,
)

logger = get_logger(__name__)


class ProvisioningService:
    """Owns the claim flow."""

    def __init__(
        self,
        *,
        devices: DeviceRepository,
        credentials: DeviceCredentialRepository,
        claims: DeviceClaimRepository,
        settings: DeviceSettings,
    ) -> None:
        self._devices = devices
        self._credentials = credentials
        self._claims = claims
        self._settings = settings

    # -- step 1: the device announces itself -----------------------------

    async def provision(
        self, *, hardware_id: str, model: str, firmware_version: str | None
    ) -> ProvisionResponse:
        """Start provisioning and return the code to display.

        Idempotent per device in the sense that calling it again simply
        issues a new code and retires the old one. A code still visible on a
        previous screen must not stay valid once a new one is shown.
        """
        now = utc_now()
        device = await self._devices.get_by_hardware_id(hardware_id)

        if device is None:
            device = Device(
                hardware_id=hardware_id,
                model=model,
                firmware_version=firmware_version,
            )
            self._devices.add(device)
            await self._devices.session.flush()
            logger.info("device_registered", device_id=str(device.id))
        else:
            # Re-provisioning: physical access implies authority to reset.
            # Detach the previous owner and kill the old credentials, so a
            # transferred device stops reporting to whoever had it before.
            device.model = model
            device.firmware_version = firmware_version

            if device.is_claimed:
                revoked = await self._credentials.revoke_all_for_device(device.id, at=now)
                logger.warning(
                    "device_reprovisioned",
                    device_id=str(device.id),
                    previous_owner=str(device.user_id),
                    revoked_credentials=revoked,
                )
                device.user_id = None
                device.claimed_at = None

            await self._claims.expire_open_claims_for_device(device.id, at=now)

        secrets_ = issue_claim_secrets(ttl_seconds=self._settings.claim_code_ttl_seconds)
        self._claims.add(
            DeviceClaim(
                device_id=device.id,
                code_hash=secrets_.code_digest,
                provisioning_token_hash=secrets_.provisioning_token_digest,
                expires_at=secrets_.expires_at,
            )
        )

        logger.info("device_provisioning_started", device_id=str(device.id))
        return ProvisionResponse(
            device_id=device.id,
            claim_code=secrets_.code,
            provisioning_token=secrets_.provisioning_token,
            expires_at=secrets_.expires_at,
            poll_interval_seconds=3,
        )

    # -- step 2: a user claims the code ----------------------------------

    async def claim(self, *, user: User, code: str, name: str | None) -> DeviceRead:
        """Bind the device showing ``code`` to ``user``.

        Raises:
            NotFoundError: if the code is unknown, expired, or already used.
                All three report identically, so the endpoint cannot be used
                to probe which codes exist.
            ConflictError: if the code has been guessed at too many times.
        """
        now = utc_now()
        claim = await self._claims.get_claimable_by_code_hash(hash_claim_code(code))

        if claim is None:
            logger.info("device_claim_failed", reason="unknown_code")
            raise self._unknown_code()

        if claim.is_consumed:
            # Already claimed by someone. Logged as a warning because a
            # replayed code is worth noticing, but nothing is written: the
            # 404 rolls the transaction back, and rate limiting is what
            # actually bounds repeated attempts.
            logger.warning("device_claim_replay", device_id=str(claim.device_id))
            raise self._unknown_code()

        if claim.is_expired:
            logger.info("device_claim_failed", reason="expired")
            raise self._unknown_code()

        device = await self._devices.get_by_id(claim.device_id)
        if device is None:  # pragma: no cover - foreign key makes this unreachable
            raise self._unknown_code()

        claim.consumed_at = now
        device.user_id = user.id
        device.claimed_at = now
        device.name = name or device.name or device.model

        logger.info("device_claimed", device_id=str(device.id), user_id=str(user.id))
        return DeviceRead.model_validate(device)

    # -- step 3: the device collects its credential ----------------------

    async def collect(
        self, *, provisioning_token: str
    ) -> ProvisionPendingResponse | ProvisionCompleteResponse:
        """Return the device's credential once a user has claimed it.

        Raises:
            AuthenticationError: if the token is unknown, expired, or already
                exchanged for a credential.
        """
        claim = await self._claims.get_by_provisioning_token_hash(
            hash_provisioning_token(provisioning_token)
        )

        if claim is None:
            logger.info("device_collect_failed", reason="unknown_token")
            raise self._invalid_provisioning_token()

        if claim.collected_at is not None:
            # The credential was already issued. A second collection attempt
            # means either a device bug or a stolen provisioning token; in
            # both cases refusing is correct, because the credential is
            # returned exactly once.
            logger.warning("device_collect_replay", device_id=str(claim.device_id))
            raise self._invalid_provisioning_token()

        if not claim.is_consumed:
            if claim.is_expired:
                logger.info("device_collect_failed", reason="expired")
                raise self._invalid_provisioning_token()
            return ProvisionPendingResponse(expires_at=claim.expires_at, poll_interval_seconds=3)

        device = await self._devices.get_by_id(claim.device_id)
        if device is None or not device.is_claimed:  # pragma: no cover - defensive
            raise self._invalid_provisioning_token()

        token = issue_device_token()
        self._credentials.add(DeviceCredential(device_id=device.id, token_hash=token.digest))
        claim.collected_at = utc_now()

        logger.info("device_credential_issued", device_id=str(device.id))
        return ProvisionCompleteResponse(
            device_id=device.id,
            device_token=token.plaintext,
            device_name=device.name,
            heartbeat_interval_seconds=self._settings.heartbeat_interval_seconds,
        )

    # -- ownership management --------------------------------------------

    async def release(self, *, user: User, device_id: uuid.UUID) -> None:
        """Remove a device from ``user``'s account.

        Revokes credentials before deleting so that an in-flight connection
        cannot outlive the row.

        Raises:
            NotFoundError: if the device does not exist or is not theirs.
        """
        device = await self._devices.get_for_owner(device_id, user.id)
        if device is None:
            raise NotFoundError("Device not found.", code="device_not_found")

        await self._credentials.revoke_all_for_device(device.id, at=utc_now())
        await self._devices.delete(device)
        logger.info("device_released", device_id=str(device_id), user_id=str(user.id))

    # -- internals --------------------------------------------------------

    @staticmethod
    def _unknown_code() -> NotFoundError:
        # One message for unknown, expired, and already-consumed codes, so
        # the endpoint cannot be used to enumerate valid ones.
        return NotFoundError(
            "That code is not valid. Check the screen and try again.",
            code="invalid_claim_code",
        )

    @staticmethod
    def _invalid_provisioning_token() -> AuthenticationError:
        return AuthenticationError(
            "Provisioning token is invalid or has expired.",
            code="invalid_provisioning_token",
        )
