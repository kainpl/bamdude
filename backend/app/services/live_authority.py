"""Bounded, lifespan-owned current authority for live transports.

Proofs are scalar and short lived. SQL is batched by credential family, never
performed by an event producer or body send. A waiter cannot release actual work.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import event, select, tuple_
from sqlalchemy.orm import Session, selectinload

from backend.app.core import database
from backend.app.core.auth_lifetime import FrozenAuthority, utc_naive, utcnow, validate_loaded
from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.api_key import APIKey
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.auth_session import AuthSession
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.printer import Printer
from backend.app.models.user import User

RECHECK_SECONDS = 1.0
PROOF_SECONDS = 3.0
CHECK_SECONDS = 1.0
MAX_HANDLES = 1024
MAX_PROJECTION_CLASSES = 256
CHUNK_SIZE = 256


def monotonic_expiry(value: datetime | None) -> float:
    return float("inf") if value is None else time.monotonic() + (utc_naive(value) - utcnow()).total_seconds()


@dataclass(frozen=True)
class LiveDescriptor:
    capture: FrozenAuthority
    permissions: tuple[str, ...]
    expires: float = float("inf")
    jti: str | None = None
    printer_id: int | None = None
    printer_identity: str | None = None
    key_hash: str | None = field(default=None, repr=False)
    namespace: tuple[str, str] | None = None
    any_permissions: tuple[str, ...] = ()
    resource_kind: str | None = None
    resource_id: int | None = None
    resource_stamp: tuple | None = field(default=None, repr=False)
    product_link: tuple | None = field(default=None, repr=False)
    long_lived_scope: str | None = None


@dataclass(frozen=True)
class Entitlements:
    permissions: frozenset[str]
    printers: frozenset[int] | None
    user_id: int
    account_identity: str
    printer_identities: tuple[tuple[int, str], ...] = ()

    def allows(self, permission: str) -> bool:
        return permission in self.permissions

    def printer_visible(self, printer_id: int) -> bool:
        return self.printers is None or printer_id in self.printers


@dataclass(eq=False)
class LiveHandle:
    descriptor: LiveDescriptor
    accepted_at: float = float("inf")
    proof_until: float = 0
    parent_until: float = float("inf")
    generation: int = 0
    admitted: bool = False
    retired: bool = False
    code: int = 1013
    entitlements: Entitlements | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    on_retire: Callable[[int], None] | None = None

    def deliverable(self) -> bool:
        return (
            live_authority.admitting
            and auth_runtime.admitting
            and self.admitted
            and not self.retired
            and time.monotonic() < min(self.proof_until, self.descriptor.expires)
        )

    def retire(self, code=1013):
        if self.retired:
            return
        self.retired, self.admitted, self.code = True, False, code
        self.stop.set()
        self.changed.set()
        if self.on_retire is not None:
            self.on_retire(code)

    def refusal_code(self):
        if self.retired:
            return self.code
        now = time.monotonic()
        if now >= self.descriptor.expires:
            return 1001
        return 4401 if now >= self.parent_until else 1013


class LiveAuthority:
    def __init__(self):
        self.admitting = True
        self.epoch = 0
        self.handles: set[LiveHandle] = set()
        self.tasks: set[asyncio.Task] = set()
        self.cleanups: set[asyncio.Task] = set()
        self._tick: asyncio.Task | None = None
        self._check: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._pending_resources: dict[tuple[str, int], float] = {}
        self._resource_cache: dict[tuple[str, int], tuple[float, dict]] = {}
        self._resource_changed = asyncio.Event()
        self._resource_waiters: dict[tuple, int] = {}

    def start(self):
        self.admitting = True
        if self._tick is None or self._tick.done():
            self._wake = asyncio.Event()
            self._resource_changed = asyncio.Event()
            self._tick = asyncio.create_task(self._run(), name="live-authority")

    def register(self, descriptor: LiveDescriptor) -> LiveHandle:
        auth_runtime.ensure_admitting()
        if not self.admitting or len(self.handles) >= MAX_HANDLES:
            raise HTTPException(503, "Authentication temporarily unavailable", headers={"Retry-After": "5"})
        self.start()
        handle = LiveHandle(descriptor)
        self.handles.add(handle)
        return handle

    def accepted(self, handle):
        handle.accepted_at = time.monotonic()
        self._wake.set()

    async def admit(self, handle):
        self.accepted(handle)
        try:
            async with asyncio.timeout(CHECK_SECONDS):
                while not handle.admitted and not handle.retired:
                    handle.changed.clear()
                    self._wake.set()
                    await handle.changed.wait()
            if not handle.deliverable():
                raise HTTPException(503, "Authentication temporarily unavailable")
        except TimeoutError as exc:
            handle.retire(1013)
            raise HTTPException(503, "Authentication temporarily unavailable", headers={"Retry-After": "5"}) from exc
        except BaseException:
            handle.retire()
            raise

    def release(self, handle):
        self.handles.discard(handle)

    def invalidate(self, selectors=None):
        """Fence pending admissions and request a fresh read, never guess revocation.

        An admitted proof retains only its original bounded lease. Extending
        it across this epoch is forbidden; O3 does not promise zero delivery
        at every mutation's commit before current authority is observed.
        """
        self.epoch += 1
        for handle in tuple(self.handles):
            if selectors is None or any(_selected(handle, selector) for selector in selectors):
                handle.generation += 1
        self._wake.set()

    def close_admission(self):
        self.admitting = False
        self.epoch += 1
        self._resource_cache.clear()
        for handle in tuple(self.handles):
            handle.retire(1001)
        self._wake.set()

    def own(self, task, *, cleanup=False):
        registry = self.cleanups if cleanup else self.tasks
        registry.add(task)

        def done(completed):
            registry.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    async def drain(self, timeout=30):
        self.close_admission()
        deadline = time.monotonic() + timeout
        while self.handles or self.tasks or self.cleanups:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def shutdown(self, timeout=30):
        result = await self.drain(timeout)
        if self._tick is not None:
            self._tick.cancel()
            await asyncio.gather(self._tick, return_exceptions=True)
            self._tick = None
        return result

    async def _run(self):
        while True:
            self._wake.clear()
            if self.admitting:
                self._schedule()
            try:
                await asyncio.wait_for(self._wake.wait(), RECHECK_SECONDS)
            except TimeoutError:
                pass
            for handle in tuple(self.handles):
                if time.monotonic() >= min(handle.descriptor.expires, handle.parent_until):
                    handle.retire(handle.refusal_code())
                elif handle.admitted and not handle.retired and time.monotonic() >= handle.proof_until:
                    # A failed/late check never extends an expired proof.
                    handle.retire(1013)

    def _schedule(self):
        if self._check is not None and not self._check.done():
            return
        batch = [h for h in self.handles if not h.retired and h.accepted_at <= time.monotonic()]
        if batch or self._pending_resources:
            self._check = self.own(asyncio.create_task(self._refresh(batch), name="live-authority-check"))

    async def _refresh(self, batch):
        generations = {handle: handle.generation for handle in batch}
        started = time.monotonic()
        try:
            # The actual task, including session close, stays owned after any waiter's timeout.
            async with asyncio.timeout(CHECK_SECONDS):
                snapshots, metadata = await self._read(batch)
            if not self.admitting or time.monotonic() >= started + CHECK_SECONDS:
                return
            for ref, row in metadata.items():
                if ref not in self._pending_resources:
                    continue  # handle proofs consume metadata directly, outside this demand cache
                self._resource_cache.pop(ref, None)
                self._resource_cache[ref] = (started, row)
                if self._pending_resources.get(ref, float("inf")) <= started:
                    self._pending_resources.pop(ref, None)
            while len(self._resource_cache) > MAX_HANDLES:
                self._resource_cache.pop(next(iter(self._resource_cache)))
            self._resource_changed.set()
            classes = {h.entitlements for h in self.handles if h.admitted and not h.retired}
            for handle, result in snapshots.items():
                if handle.retired or handle.accepted_at > started or handle.generation != generations[handle]:
                    continue
                if isinstance(result, int):
                    handle.retire(result)
                    continue
                entitlement, parent_until = result
                if handle.entitlements is not None and handle.entitlements != entitlement:
                    handle.retire(1013)  # resync; never silently purge a connected print/queue cache
                    continue
                if entitlement not in classes and len(classes) >= MAX_PROJECTION_CLASSES:
                    handle.retire(1013)
                    continue
                classes.add(entitlement)
                handle.entitlements = entitlement
                handle.parent_until = parent_until
                handle.proof_until = min(started + PROOF_SECONDS, parent_until)
                handle.admitted = True
                handle.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            for handle in batch:
                handle.retire(1013)
            self._resource_changed.set()
        finally:
            # New accepts must get a batch which began after their accept.
            if self._pending_resources or any(
                not h.retired and (not h.admitted or h.generation != generations.get(h)) for h in self.handles
            ):
                self._wake.set()

    async def resource_rows(self, references):
        """Coalesce metadata demand into the next family batch, never viewer SQL."""
        auth_runtime.ensure_admitting()
        if not self.admitting or len(self._pending_resources.keys() | references) > MAX_HANDLES:
            raise HTTPException(503, "Authentication temporarily unavailable")
        requested = time.monotonic()
        if all(
            ref in self._resource_cache and requested < self._resource_cache[ref][0] + PROOF_SECONDS
            for ref in references
        ):
            return {ref: self._resource_cache[ref][1] for ref in references}
        self._pending_resources.update(dict.fromkeys(references, requested))
        for ref in references:
            self._resource_waiters[ref] = self._resource_waiters.get(ref, 0) + 1
        self.start()
        try:
            async with asyncio.timeout(CHECK_SECONDS):
                while True:
                    self._resource_changed.clear()
                    if all(
                        ref in self._resource_cache and self._resource_cache[ref][0] >= requested for ref in references
                    ):
                        return {ref: self._resource_cache[ref][1] for ref in references}
                    self._wake.set()
                    await self._resource_changed.wait()
        except TimeoutError as exc:
            raise HTTPException(503, "Authentication temporarily unavailable") from exc
        finally:
            for ref in references:
                remaining = self._resource_waiters[ref] - 1
                if remaining:
                    self._resource_waiters[ref] = remaining
                else:
                    self._resource_waiters.pop(ref)
                    self._pending_resources.pop(ref, None)

    async def _metadata(self, db, references):
        from backend.app.models.archive import PrintArchive
        from backend.app.models.camera import Camera
        from backend.app.models.library import LibraryFile
        from backend.app.models.product import Product, ProductPart, ProductPlate
        from backend.app.models.project import Project

        models = {
            "archive": PrintArchive,
            "library": LibraryFile,
            "printer": Printer,
            "camera": Camera,
            "product": Product,
            "part": ProductPart,
            "project": Project,
        }
        metadata = {ref: {} for ref in references}
        fields = (
            "id",
            "recovery_identity",
            "connector_identity",
            "created_by_id",
            "printer_id",
            "project_id",
            "deleted_at",
            "filename",
            "print_name",
            "created_at",
            "archived",
            "enabled",
            "camera_type",
            "url",
            "snapshot_url",
            "file_hash",
            "product_id",
            "name_key",
            "image_source",
            "image_file_id",
            "image_plate_index",
            "image_identify_id",
            "image_photo",
        )
        for kind, model in models.items():
            ids = [ident for family, ident in references if family == kind]
            for offset in range(0, len(ids), CHUNK_SIZE):
                columns = [getattr(model, name) for name in fields if hasattr(model, name)]
                for row in (
                    await db.execute(select(*columns).where(model.id.in_(ids[offset : offset + CHUNK_SIZE])))
                ).mappings():
                    metadata[kind, row["id"]] = dict(row)
        links = [value for kind, value in references if kind == "product_link"]
        for offset in range(0, len(links), CHUNK_SIZE):
            statement = (
                select(
                    ProductPlate.product_id,
                    ProductPlate.library_file_id,
                    ProductPlate.plate_index,
                    Product.created_at,
                    LibraryFile.file_hash,
                )
                .join(Product, Product.id == ProductPlate.product_id)
                .join(LibraryFile, LibraryFile.id == ProductPlate.library_file_id)
                .where(
                    tuple_(ProductPlate.product_id, ProductPlate.library_file_id, ProductPlate.plate_index).in_(
                        links[offset : offset + CHUNK_SIZE]
                    ),
                    LibraryFile.deleted_at.is_(None),
                )
            )
            for row in (await db.execute(statement)).all():
                metadata["product_link", (row.product_id, row.library_file_id, row.plate_index)] = {
                    "stamp": (row.created_at, row.file_hash)
                }
        part_rows = [row for (kind, _), row in metadata.items() if kind == "part" and row]
        if part_rows:
            products = {row["product_id"] for row in part_rows}
            product_rows = dict(
                (await db.execute(select(Product.id, Product.created_at).where(Product.id.in_(products)))).all()
            )
            # A finite label may depend on auto-selected linked sources. Bound
            # their inventory; uncertainty refuses the label instead of proving
            # a truncated set as complete.
            plate_rows = (
                await db.execute(
                    select(
                        ProductPlate.product_id,
                        ProductPlate.library_file_id,
                        ProductPlate.plate_index,
                        LibraryFile.file_hash,
                    )
                    .join(LibraryFile)
                    .where(ProductPlate.product_id.in_(products), LibraryFile.deleted_at.is_(None))
                    .limit(MAX_HANDLES + 1)
                )
            ).all()
            if len(plate_rows) > MAX_HANDLES:
                raise HTTPException(503, "Authentication temporarily unavailable")
            for part in part_rows:
                part["_product_exists"] = part["product_id"] in product_rows
                part["_product_created"] = product_rows.get(part["product_id"])
                part["_linked_files"] = tuple(
                    sorted(
                        (r.library_file_id, r.plate_index, r.file_hash)
                        for r in plate_rows
                        if r.product_id == part["product_id"]
                    )
                )
        return metadata

    async def _read(self, batch):
        from backend.app.core.auth import _check_apikey_permissions
        from backend.app.core.permissions import Permission
        from backend.app.models.settings import Settings

        async with database.async_session() as db:
            auth_runtime.track_transaction(db)

            async def rows(model, column, values):
                values = list({v for v in values if v is not None})
                result = {}
                for offset in range(0, len(values), CHUNK_SIZE):
                    stmt = select(model).where(column.in_(values[offset : offset + CHUNK_SIZE]))
                    if model is User:
                        stmt = stmt.options(selectinload(User.groups))
                    for row in (await db.scalars(stmt)).all():
                        result[getattr(row, column.key)] = row
                return result

            captures = [h.descriptor.capture for h in batch]
            users = await rows(User, User.account_identity, [c.account_identity for c in captures])
            sessions = await rows(AuthSession, AuthSession.sid, [c.sid for c in captures])
            keys = await rows(APIKey, APIKey.connector_identity, [c.key_identity for c in captures])
            tokens = await rows(LongLivedToken, LongLivedToken.token_identity, [c.token_identity for c in captures])
            references = set(self._pending_resources)
            references.update(
                (h.descriptor.resource_kind, h.descriptor.resource_id)
                for h in batch
                if h.descriptor.resource_kind is not None and h.descriptor.resource_id is not None
            )
            references.update(
                ("product_link", h.descriptor.product_link[:3]) for h in batch if h.descriptor.product_link
            )
            metadata = await self._metadata(db, references)
            printer_ids = [h.descriptor.printer_id for h in batch]
            for key in keys.values():
                if key.printer_ids is not None:
                    printer_ids.extend(key.printer_ids)
            printers = await rows(Printer, Printer.id, printer_ids)
            jtis = [h.descriptor.jti for h in batch if h.descriptor.jti]
            revoked = set()
            for offset in range(0, len(jtis), CHUNK_SIZE):
                revoked.update(
                    (
                        await db.scalars(
                            select(AuthEphemeralToken.token).where(
                                AuthEphemeralToken.token_type == TokenType.REVOKED_JTI,
                                AuthEphemeralToken.token.in_(jtis[offset : offset + CHUNK_SIZE]),
                            )
                        )
                    ).all()
                )
            namespace = None
            if any(h.descriptor.namespace for h in batch):
                config = dict(
                    (
                        await db.execute(
                            select(Settings.key, Settings.value).where(
                                Settings.key.in_(
                                    ["connector_server_id", "connector_storage_epoch", "connector_enabled"]
                                )
                            )
                        )
                    ).all()
                )
                if (config.get("connector_enabled") or "true").strip().lower() != "false":
                    namespace = (config.get("connector_server_id"), config.get("connector_storage_epoch"))
            outcomes = {}
            permission_cache = {}
            for handle in batch:
                d, c = handle.descriptor, handle.descriptor.capture
                try:
                    principal = validate_loaded(
                        c,
                        users.get(c.account_identity),
                        session=sessions.get(c.sid),
                        key=keys.get(c.key_identity),
                        record=tokens.get(c.token_identity),
                        required_permissions=d.permissions,
                        printer_id=d.printer_id,
                    )
                    if d.jti in revoked:
                        raise HTTPException(401)
                    if d.printer_id is not None:
                        printer = printers.get(d.printer_id)
                        if printer is None or printer.connector_identity != d.printer_identity:
                            raise HTTPException(401)
                    if d.namespace is not None and d.namespace != namespace:
                        raise HTTPException(401)
                    if d.key_hash is not None and principal.api_key.key_hash != d.key_hash:
                        raise HTTPException(401)
                    if d.long_lived_scope is not None and (
                        principal.long_lived_token is None or principal.long_lived_token.scope != d.long_lived_scope
                    ):
                        raise HTTPException(401)
                    permission_key = (c.kind, c.account_identity, c.key_identity)
                    permissions = permission_cache.get(permission_key)
                    if permissions is None:
                        permissions = set()
                        for permission in Permission:
                            if principal.api_key is None:
                                allowed = principal.user.has_permission(permission.value)
                            else:
                                try:
                                    _check_apikey_permissions(
                                        principal.api_key, [permission.value], owner=principal.user
                                    )
                                    allowed = True
                                except HTTPException:
                                    allowed = False
                            if allowed:
                                permissions.add(permission.value)
                        permission_cache[permission_key] = permissions
                    scope = None if principal.api_key is None else principal.api_key.printer_ids
                    if d.any_permissions and not permissions.intersection(d.any_permissions):
                        raise HTTPException(403)
                    entitlement = Entitlements(
                        frozenset(permissions),
                        None if scope is None else frozenset(scope),
                        principal.user.id,
                        c.account_identity,
                        ()
                        if scope is None
                        else tuple(sorted((pid, printers[pid].connector_identity) for pid in scope if pid in printers)),
                    )
                    if d.resource_kind is not None:
                        resource = metadata.get((d.resource_kind, d.resource_id), {})
                        if not resource or resource.get("deleted_at") is not None:
                            raise HTTPException(401)
                        if (
                            d.resource_stamp is not None
                            and resource_stamp(d.resource_kind, resource) != d.resource_stamp
                        ):
                            raise HTTPException(401)
                        if d.resource_kind == "camera" and not resource.get("enabled"):
                            raise HTTPException(401)
                        if d.resource_kind == "part" and not resource.get("_product_exists"):
                            raise HTTPException(401)
                        if resource.get("printer_id") is not None and not entitlement.printer_visible(
                            resource["printer_id"]
                        ):
                            raise HTTPException(403)
                        domain = {"archive": "archives", "library": "library"}.get(d.resource_kind)
                        if domain and any(p.startswith(domain + ":read_") for p in d.permissions):
                            if (
                                not entitlement.allows(domain + ":read_all")
                                and resource.get("created_by_id") != principal.user.id
                            ):
                                raise HTTPException(403)
                    if d.product_link is not None:
                        link = metadata.get(("product_link", d.product_link[:3]), {})
                        if not link or link["stamp"] != d.product_link[3]:
                            raise HTTPException(401)
                    parent = (
                        sessions.get(c.sid)
                        if c.kind == "session"
                        else keys.get(c.key_identity)
                        if c.kind == "key"
                        else tokens.get(c.token_identity)
                    )
                    outcomes[handle] = entitlement, monotonic_expiry(parent.expires_at)
                except HTTPException as exc:
                    outcomes[handle] = 4403 if exc.status_code == 403 else 4401
            return outcomes, metadata


def resource_stamp(kind, row):
    if kind == "camera":
        return tuple(row.get(k) for k in ("created_at", "camera_type", "url", "snapshot_url", "enabled"))
    if kind == "part":
        return tuple(
            row.get(k)
            for k in (
                "product_id",
                "name_key",
                "image_source",
                "image_file_id",
                "image_plate_index",
                "image_identify_id",
                "image_photo",
                "_product_created",
                "_linked_files",
            )
        )
    identity = (row.get("recovery_identity") or row.get("connector_identity"), row.get("created_at"))
    return (*identity, row.get("file_hash")) if kind == "library" else identity


async def descriptor_for_request(request):
    """The route's successful gate winner, plus captured resource incarnation."""
    descriptor = getattr(request.state, "live_descriptor", None)
    if descriptor is None:
        raise HTTPException(401, "Invalid or revoked credential")
    binding = getattr(request.state, "live_media_binding", {})
    allowed = {name for name in LiveDescriptor.__dataclass_fields__ if name != "capture"}
    if binding:
        descriptor = replace(descriptor, **{k: v for k, v in binding.items() if k in allowed})
    params = request.path_params
    if descriptor.printer_id is None and "printer_id" in params:
        printer_id = int(params["printer_id"])
        row = (await live_authority.resource_rows({("printer", printer_id)}))["printer", printer_id]
        descriptor = replace(descriptor, printer_id=printer_id, printer_identity=row.get("connector_identity"))
    if descriptor.resource_kind is None:
        for param, kind in (
            ("archive_id", "archive"),
            ("file_id", "library"),
            ("library_file_id", "library"),
            ("camera_id", "camera"),
            ("part_id", "part"),
            ("product_id", "product"),
            ("project_id", "project"),
        ):
            if param in params:
                resource_id = int(params[param])
                row = (await live_authority.resource_rows({(kind, resource_id)}))[kind, resource_id]
                descriptor = replace(
                    descriptor, resource_kind=kind, resource_id=resource_id, resource_stamp=resource_stamp(kind, row)
                )
                break
    if all(name in params for name in ("product_id", "library_file_id", "plate_index")):
        captured = getattr(request.state, "_live_product_link", None)
        if captured is None:
            ids = tuple(int(params[name]) for name in ("product_id", "library_file_id", "plate_index"))
            row = (await live_authority.resource_rows({("product_link", ids)}))["product_link", ids]
            captured = (*ids, row.get("stamp"))
            request.state._live_product_link = captured
        descriptor = replace(descriptor, product_link=captured)
    return descriptor


live_authority = LiveAuthority()


# Post-transaction hints, never the durable proof. No-op lifetime locks and
# usage audits are not authority mutations. Signals select immutable identities.
def _selected(handle, selector):
    kind, value = selector
    d, c = handle.descriptor, handle.descriptor.capture
    return (
        kind == "all"
        or (kind == "account" and c.account_identity == value)
        or (kind == "session-account" and c.kind == "session" and c.account_identity == value)
        or (kind == "sid" and c.sid == value)
        or (kind == "key" and c.key_identity == value)
        or (kind == "token" and c.token_identity == value)
        or (kind == "jti" and d.jti == value)
        or (kind == "printer" and d.printer_id == value)
        or (kind == "camera" and d.resource_kind == "camera" and d.resource_id == value)
    )


def _mark(session, kind, value=None):
    session.info.setdefault("live_authority_changed", set()).add((kind, value))


@event.listens_for(Session, "before_flush")
def _before_flush(session, _context, _instances):
    from sqlalchemy import inspect

    from backend.app.models.camera import Camera
    from backend.app.models.group import Group

    for row in session.new | session.dirty | session.deleted:
        if isinstance(row, User):
            state = inspect(row)
            if (
                row in session.deleted
                or state.attrs.is_active.history.has_changes()
                or state.attrs.role.history.has_changes()
                or state.attrs.groups.history.has_changes()
            ):
                _mark(session, "account", row.account_identity)
            elif (
                state.attrs.session_generation.history.has_changes()
                or state.attrs.password_changed_at.history.has_changes()
            ):
                _mark(session, "session-account", row.account_identity)
        elif isinstance(row, AuthSession):
            _mark(session, "sid", row.sid)
        elif isinstance(row, APIKey):
            _mark(session, "key", row.connector_identity)
        elif isinstance(row, LongLivedToken):
            _mark(session, "token", row.token_identity)
        elif isinstance(row, AuthEphemeralToken) and row.token_type == TokenType.REVOKED_JTI:
            _mark(session, "jti", row.token)
        elif isinstance(row, Group):
            _mark(session, "all")
        elif isinstance(row, Printer):
            _mark(session, "printer", row.id)
        elif isinstance(row, Camera):
            _mark(session, "camera", row.id)


@event.listens_for(Session, "do_orm_execute")
def _bulk_mutation(state):
    if not (state.is_update or state.is_delete or state.is_insert):
        return
    table = getattr(state.statement, "table", None)
    if table is None or table.name not in {
        "users",
        "user_groups",
        "groups",
        "auth_sessions",
        "api_keys",
        "long_lived_tokens",
        "printers",
        "cameras",
    }:
        return
    values = getattr(state.statement, "_values", None)
    names = {getattr(key, "key", key) for key in values} if values else set()
    if names and names <= {"last_used", "last_used_at"}:
        return
    if values and all(getattr(key, "key", key) == getattr(value, "key", None) for key, value in values.items()):
        return  # guard_accounts takes a write reservation without changing a grant
    from sqlalchemy.sql import visitors

    candidates = {}
    for clause in getattr(state.statement, "_where_criteria", ()):
        for node in visitors.iterate(clause):
            left, right = getattr(node, "left", None), getattr(node, "right", None)
            name, value = getattr(left, "key", None), getattr(right, "value", None)
            if name and isinstance(value, (str, int)):
                candidates[name] = value
    if table.name == "auth_sessions":
        if "sid" in candidates:
            _mark(state.session, "sid", candidates["sid"])
            return
        if "account_identity" in candidates:
            _mark(state.session, "session-account", candidates["account_identity"])
            return
    if table.name == "users" and "account_identity" in candidates:
        kind = "session-account" if names <= {"session_generation", "password_changed_at", "updated_at"} else "account"
        _mark(state.session, kind, candidates["account_identity"])
        return
    for table_name, column, kind in (
        ("api_keys", "connector_identity", "key"),
        ("long_lived_tokens", "token_identity", "token"),
    ):
        if table.name == table_name and column in candidates:
            _mark(state.session, kind, candidates[column])
            return
    _mark(state.session, "all")


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _transaction_finished(session):
    if session.get_nested_transaction() is not None:
        return
    selectors = session.info.pop("live_authority_changed", None)
    if selectors:
        live_authority.invalidate(selectors)


@event.listens_for(Session, "after_transaction_end")
def _transaction_closed(session, transaction):
    # Failed/unknown commit and implicit rollback via close need the same
    # fresh-proof requirement. Successful commit/explicit rollback already
    # consumed the marker; a nested savepoint must not consume its parent.
    if transaction.parent is None:
        _transaction_finished(session)
