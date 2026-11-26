"""OIDC group sync at session issuance (N07; vault ``60-specs/oidc-group-sync-n07-spec``).

A provider maps IdP groups to BamDude groups (``group_mapping``: IdP name → group id).
The values of the mapping are the MANAGED groups: at every issued OIDC session the
user's managed slice is replaced by what the IdP snapshot grants; manual assignments
outside it stay. Nothing is applied at the callback — the snapshot travels in the
login evidence of the one-time tokens and is applied only in the transaction that
issues the session (exchange without 2FA, or a successful ``/2fa/verify``).

The evidence (spec §C.2) is versioned and carries the provider revision the attempt
started under, the subject of the link, the IdP groups (``None`` when the mapping was
empty) and a fixed deadline ``min(callback + 10 min, floor(exp))``. Every refusal at
issuance is a raised 409 with a code, so the request transaction rolls back whole:
tokens, factors, groups, role — nothing changes and no session is issued.

Lock order (spec §C.5): account guard → OIDC policy lock → token consumption → the
administrators' advisory lock. Provider writers take ``lock_credential_config`` →
policy lock → provider row; a group deletion takes the policy lock before the group
row. Nothing takes an existing account's guard or ``lock_credential_config`` while it
holds the policy lock.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.id_allocator import in_domain

logger = logging.getLogger(__name__)

EVIDENCE_VERSION = 1
EVIDENCE_WINDOW_SECONDS = 600
MAX_CLAIM_GROUPS = 500
MAX_MAPPING_PAIRS = 100
MAX_GROUP_NAME_LENGTH = 255
# The administrators' guard holds (216, 1) — core/admin_membership.py.
POLICY_LOCK_KEY = (216, 2)

# Callback redirect codes (``/login?oidc_error=<code>``).
PROVIDER_CHANGED = "provider_changed"
LOGIN_RETRY = "oidc_login_retry"
CLAIMS_INCOMPLETE = "group_claims_incomplete"
CLAIMS_INVALID = "group_claims_invalid"
MAPPING_INVALID = "oidc_mapping_invalid"

# Issuance refusals: 409 with ``{"error": code, "message": sentence}``.
LOGIN_EXPIRED = "oidc_login_expired"
POLICY_CHANGED = "oidc_policy_changed"
LAST_ADMIN = "group_sync_last_admin"
LOGIN_EXPIRED_MESSAGE = "This sign-in has expired. Start the sign-in again."
POLICY_CHANGED_MESSAGE = "The sign-in provider changed during this sign-in. Start the sign-in again."
MAPPING_INVALID_MESSAGE = "The provider's group mapping is damaged. An administrator has to replace it."
LAST_ADMIN_MESSAGE = (
    "The identity provider no longer grants Administrators to the last active administrator. "
    "Sign-in refused; nothing was changed."
)


def now_utc() -> datetime:
    """The clock every N07 deadline comparison reads (tests replace it)."""
    return datetime.now(timezone.utc)


def _now_seconds() -> float:
    return now_utc().timestamp()


# ── Mapping (spec §F) ────────────────────────────────────────────────────────


def normalize_mapping(value) -> dict[str, int]:
    """The one normalizer for API, env (after names became ids) and the stored value.

    An object of at most 100 pairs; keys trimmed, non-empty, at most 255 characters
    and unique after trimming; values strict integers 1..2³¹ − 1 (``true`` is not
    group 1). Raises ``ValueError`` with the reason.
    """
    if not isinstance(value, dict):
        raise ValueError("The group mapping must be an object of IdP group to group id")
    if len(value) > MAX_MAPPING_PAIRS:
        raise ValueError("The group mapping has more than 100 entries")
    normalized: dict[str, int] = {}
    for raw_key, group_id in value.items():
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not key or len(key) > MAX_GROUP_NAME_LENGTH:
            raise ValueError("An IdP group in the mapping is empty or longer than 255 characters")
        if key in normalized:
            raise ValueError("Two IdP groups in the mapping are the same after trimming spaces")
        if type(group_id) is not int or not in_domain(group_id):
            raise ValueError("A group id in the mapping must be an integer from 1 to 2147483647")
        normalized[key] = group_id
    return normalized


def stored_mapping(value) -> dict[str, int] | None:
    """The stored mapping when it has the right shape, else ``None`` — a damaged mapping."""
    try:
        normalized = normalize_mapping(value)
    except ValueError:
        return None
    # A stored key must already be in its normalized form.
    return normalized if set(normalized) == set(value) else None


async def require_mapping_targets(db: AsyncSession, mapping: dict[str, int]) -> None:
    """Every group a submitted mapping names exists — checked under the policy lock in the
    writer's final transaction, so a group deletion cannot slip in before the commit."""
    from backend.app.models.group import Group

    ids = set(mapping.values())
    if not ids:
        return
    found = set((await db.execute(select(Group.id).where(Group.id.in_(ids)))).scalars().all())
    missing = sorted(ids - found)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"The group mapping names groups that do not exist: {', '.join(str(i) for i in missing)}",
        )


# ── Claims (spec §D) ─────────────────────────────────────────────────────────


class CallbackRefused(Exception):
    """A callback refusal that is a redirect code, not an HTTP error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def idp_groups(claims: dict, claim_name: str) -> list[str]:
    """The IdP groups an ID token grants, read strictly.

    Incompleteness markers win over any value: ``_claim_names`` naming the claim
    (distributed claims, Entra overage) or ``hasgroups: true``. A JSON array of
    strings is taken element by element, one string is one group, a missing or
    ``null`` claim is no groups. Anything else is invalid.
    """
    names = claims.get("_claim_names")
    if names is not None and not isinstance(names, dict):
        raise CallbackRefused(CLAIMS_INVALID)
    if (isinstance(names, dict) and claim_name in names) or claims.get("hasgroups") is True:
        raise CallbackRefused(CLAIMS_INCOMPLETE)
    value = claims.get(claim_name)
    if value is None:
        logger.warning("OIDC ID token carries no %r claim: the managed groups are removed", claim_name)
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        if len(value) > MAX_CLAIM_GROUPS or any(not isinstance(item, str) for item in value):
            raise CallbackRefused(CLAIMS_INVALID)
        items = value
    else:
        raise CallbackRefused(CLAIMS_INVALID)
    return sorted({item.strip() for item in items if item.strip()})


def exp_seconds(claims: dict) -> int:
    """``floor(exp)`` of a verified ID token (spec §C.2, S35, S39).

    PyJWT's own ``exp`` check is off; this is the only one. ``exp`` must be a JSON
    number — not a boolean, not a numeric string — finite and representable as a
    date. Raises ``ValueError``.
    """
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        raise ValueError("exp is missing or not a number")
    if isinstance(exp, float) and not math.isfinite(exp):
        raise ValueError("exp is not finite")
    seconds = math.floor(exp)
    try:
        datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("exp is not a representable date") from None
    return seconds


# ── Evidence (spec §C.2) ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evidence:
    provider_id: int
    revision: str
    sub: str
    groups: tuple[str, ...] | None
    deadline: int


def state_evidence(revision: str) -> dict:
    return {"v": EVIDENCE_VERSION, "revision": revision}


def state_revision(raw) -> str | None:
    if isinstance(raw, dict) and type(raw.get("v")) is int and raw.get("v") == EVIDENCE_VERSION:
        revision = raw.get("revision")
        if isinstance(revision, str) and 0 < len(revision) <= 32:
            return revision
    return None


def login_deadline(exp: int) -> int:
    """``min(callback + 10 min, floor(exp))`` — fixed at the callback, never moved."""
    return min(math.floor(_now_seconds()) + EVIDENCE_WINDOW_SECONDS, exp)


def build_evidence(*, provider_id: int, revision: str, sub: str, groups: list[str] | None, deadline: int) -> dict:
    return {
        "v": EVIDENCE_VERSION,
        "provider_id": provider_id,
        "revision": revision,
        "sub": sub,
        "groups": None if groups is None else list(groups),
        "deadline": deadline,
    }


def read_evidence(raw) -> Evidence | None:
    """The evidence of an OIDC exchange or pre-auth row, or ``None`` when absent or malformed."""
    if not isinstance(raw, dict) or type(raw.get("v")) is not int or raw.get("v") != EVIDENCE_VERSION:
        return None
    provider_id, revision, sub = raw.get("provider_id"), raw.get("revision"), raw.get("sub")
    groups, deadline = raw.get("groups"), raw.get("deadline")
    if type(provider_id) is not int or not in_domain(provider_id):
        return None
    if not isinstance(revision, str) or not 0 < len(revision) <= 32:
        return None
    if not isinstance(sub, str) or not 0 < len(sub) <= 500:
        return None
    if groups is not None and (
        not isinstance(groups, list) or len(groups) > MAX_CLAIM_GROUPS or any(not isinstance(g, str) for g in groups)
    ):
        return None
    if type(deadline) is not int:
        return None
    return Evidence(provider_id, revision, sub, None if groups is None else tuple(groups), deadline)


def evidence_is_live(evidence: Evidence | None) -> bool:
    return evidence is not None and _now_seconds() < evidence.deadline


def require_live_evidence(raw) -> Evidence:
    """The evidence of an OIDC token row that is still valid, or a raised 409 ``oidc_login_expired``."""
    evidence = read_evidence(raw)
    if not evidence_is_live(evidence):
        raise HTTPException(status_code=409, detail={"error": LOGIN_EXPIRED, "message": LOGIN_EXPIRED_MESSAGE})
    return evidence


def require_deadline(deadline: int) -> None:
    """The permission boundary's own check: rows flushed, then this, then one commit (spec §C.2)."""
    if not _now_seconds() < deadline:
        raise HTTPException(status_code=409, detail={"error": LOGIN_EXPIRED, "message": LOGIN_EXPIRED_MESSAGE})


# ── Policy lock (spec §C.5) ──────────────────────────────────────────────────


async def lock_oidc_policy(db: AsyncSession) -> None:
    """The one serialization point of OIDC policy, provider rows and group deletion.

    PostgreSQL: a transaction advisory lock. SQLite: the writer is reserved by a real
    no-op write of the existing credential anchor before any authoritative read, as
    ``lock_credential_config`` does; a transaction that already holds the writer
    loses nothing.
    """
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": POLICY_LOCK_KEY[0], "b": POLICY_LOCK_KEY[1]}
        )
        return
    from backend.app.core.credential_config import CONFIG_GUARD_KEY
    from backend.app.models.settings import Settings

    result = await db.execute(
        update(Settings)
        .where(Settings.key == CONFIG_GUARD_KEY)
        .values(value=Settings.value, updated_at=Settings.updated_at)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=503, detail="Credential configuration is not ready")


async def fresh_provider(db: AsyncSession, provider_id: int):
    """The provider row read past the identity map (spec §C.3, S22)."""
    from backend.app.models.oidc_provider import OIDCProvider

    return (
        await db.execute(
            select(OIDCProvider).where(OIDCProvider.id == provider_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def provider_is_current(provider, revision: str | None) -> bool:
    return (
        provider is not None and provider.is_enabled and revision is not None and provider.policy_revision == revision
    )


# ── Issuance (spec §C.7, §C.8, §E) ───────────────────────────────────────────


async def _default_group(db: AsyncSession, provider):
    from backend.app.models.group import Group

    if provider.default_group_id is not None:
        group = (await db.execute(select(Group).where(Group.id == provider.default_group_id))).scalar_one_or_none()
        if group is not None:
            return group
    return (await db.execute(select(Group).where(Group.name == "Viewers"))).scalar_one_or_none()


async def apply_at_issuance(db: AsyncSession, user_id: int, evidence: Evidence) -> None:
    """Steps 4–7 of §C.8, under the account guard and the policy lock the caller holds.

    Checks the evidence, the provider and the link, computes the final groups and role
    from the COMMITTED state, guards the last administrator, writes and flushes —
    before the issuer re-reads the account. Every refusal is a raised 409.
    """
    from backend.app.core.admin_membership import LAST_ADMIN_DEMOTION, require_other_active_admin
    from backend.app.models.group import Group
    from backend.app.models.oidc_provider import UserOIDCLink
    from backend.app.models.user import ADMIN_GROUP_NAME, User

    if not evidence_is_live(evidence):
        raise HTTPException(status_code=409, detail={"error": LOGIN_EXPIRED, "message": LOGIN_EXPIRED_MESSAGE})
    provider = await fresh_provider(db, evidence.provider_id)
    if not provider_is_current(provider, evidence.revision):
        raise HTTPException(status_code=409, detail={"error": POLICY_CHANGED, "message": POLICY_CHANGED_MESSAGE})
    link = (
        await db.execute(
            select(UserOIDCLink)
            .where(UserOIDCLink.provider_id == evidence.provider_id, UserOIDCLink.provider_user_id == evidence.sub)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if link is None or link.user_id != user_id:
        raise HTTPException(status_code=409, detail={"error": POLICY_CHANGED, "message": POLICY_CHANGED_MESSAGE})
    mapping = stored_mapping(provider.group_mapping)
    if mapping is None:
        logger.warning("OIDC provider %d has a damaged group mapping: sign-in refused", provider.id)
        raise HTTPException(status_code=409, detail={"error": MAPPING_INVALID, "message": MAPPING_INVALID_MESSAGE})
    if mapping and evidence.groups is None:
        # The mapping was empty when the attempt started; a change would have moved the revision.
        raise HTTPException(status_code=409, detail={"error": POLICY_CHANGED, "message": POLICY_CHANGED_MESSAGE})

    # The authoritative read, under both locks, past the identity map (S18, r4 note).
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.groups))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    committed = list(user.groups)
    target = list(committed)
    if link.first_login_pending:
        default = await _default_group(db, provider)
        if default is not None and all(g.id != default.id for g in target):
            target.append(default)

    admin_group = (await db.execute(select(Group).where(Group.name == ADMIN_GROUP_NAME))).scalar_one_or_none()
    admin_managed = False
    if mapping:
        managed_ids = set(mapping.values())
        granted = {mapping[name] for name in evidence.groups or () if name in mapping}
        present = {g.id: g for g in (await db.execute(select(Group).where(Group.id.in_(managed_ids)))).scalars().all()}
        target = [g for g in target if g.id not in managed_ids] + [present[i] for i in sorted(granted) if i in present]
        admin_managed = admin_group is not None and admin_group.id in managed_ids

    in_admin_group = any(g.name == ADMIN_GROUP_NAME for g in target)
    role = "user" if admin_managed and not in_admin_group and user.role == "admin" else user.role
    was_admin = user.role == "admin" or any(g.name == ADMIN_GROUP_NAME for g in committed)
    stays_admin = role == "admin" or in_admin_group
    if was_admin and not stays_admin and user.is_active:
        try:
            await require_other_active_admin(db, user.id, LAST_ADMIN_DEMOTION)
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            raise HTTPException(status_code=409, detail={"error": LAST_ADMIN, "message": LAST_ADMIN_MESSAGE}) from None

    if {g.id for g in target} != {g.id for g in committed}:
        user.groups = target
    if role != user.role:
        user.role = role
    if link.first_login_pending:
        link.first_login_pending = False
    await db.flush()
