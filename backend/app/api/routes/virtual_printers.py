import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.schemas.virtual_printer import VPDiagnosticResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/virtual-printers", tags=["virtual-printers"])


class VirtualPrinterCreate(BaseModel):
    name: str = "BamDude"
    enabled: bool = False
    mode: str = "file_manager"
    model: str | None = None
    access_code: str | None = None
    target_printer_id: int | None = None
    # Per-VP destination folder in the library for incoming files (Audit-2).
    # NULL = library root. Validated to exist if non-NULL.
    target_folder_id: int | None = None
    auto_dispatch: bool = True
    queue_force_color_match: bool = False
    gcode_injection: bool = False
    bind_ip: str | None = None
    remote_interface_ip: str | None = None
    tailscale_disabled: bool = True


class VirtualPrinterUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    mode: str | None = None
    model: str | None = None
    access_code: str | None = None
    target_printer_id: int | None = None
    # Pydantic can't distinguish "field not provided" from "field=null", so we
    # use this sentinel to explicitly clear target_printer_id (e.g. when the
    # operator switches a VP into auto-select mode).
    clear_target_printer: bool = False
    target_folder_id: int | None = None
    # Same sentinel pattern as ``clear_target_printer`` — lets the UI move
    # a VP back to "library root" without ambiguity.
    clear_target_folder: bool = False
    auto_dispatch: bool | None = None
    queue_force_color_match: bool | None = None
    gcode_injection: bool | None = None
    bind_ip: str | None = None
    remote_interface_ip: str | None = None
    tailscale_disabled: bool | None = None


def _resolve_printer_model(printer_model: str | None) -> str | None:
    """Map a printer's model (display name or SSDP code) to a valid VP SSDP model code.

    Printers store display names like 'X1C' while VPs need SSDP codes like 'BL-P001'.
    """
    if not printer_model:
        return None
    from backend.app.services.virtual_printer import VIRTUAL_PRINTER_MODELS
    from backend.app.services.virtual_printer.manager import DISPLAY_NAME_TO_MODEL_CODE

    # Already a valid SSDP model code
    if printer_model in VIRTUAL_PRINTER_MODELS:
        return printer_model
    # Map display name to SSDP code
    return DISPLAY_NAME_TO_MODEL_CODE.get(printer_model)


async def _vp_to_dict(vp, db: AsyncSession, status: dict | None = None) -> dict:
    """Convert VirtualPrinter model to response dict.

    In proxy mode the surfaced serial is the target printer's actual serial
    (what the bridge advertises over SSDP / what slicers see — see the manager's
    ``is_proxy`` cert-subject path), not the self-generated suffix. Archive /
    queue / file-manager modes keep the self-generated serial since those modes
    never speak the target's identity. An orphaned target falls back to the
    self-generated serial so the card still renders (#—, VP proxy identity).
    """
    from backend.app.models.printer import Printer
    from backend.app.services.virtual_printer import VIRTUAL_PRINTER_MODELS
    from backend.app.services.virtual_printer.manager import DEFAULT_VIRTUAL_PRINTER_MODEL, _get_serial_for_model

    model_code = vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL
    serial = _get_serial_for_model(model_code, vp.serial_suffix)
    if vp.mode == "proxy" and vp.target_printer_id:
        result = await db.execute(select(Printer.serial_number).where(Printer.id == vp.target_printer_id))
        target_serial = result.scalar_one_or_none()
        if target_serial:
            serial = target_serial

    return {
        "id": vp.id,
        "name": vp.name,
        "enabled": vp.enabled,
        "mode": vp.mode,
        "model": model_code,
        "model_name": VIRTUAL_PRINTER_MODELS.get(model_code, model_code),
        "access_code_set": bool(vp.access_code),
        "serial": serial,
        "target_printer_id": vp.target_printer_id,
        "target_folder_id": vp.target_folder_id,
        "auto_dispatch": vp.auto_dispatch,
        "queue_force_color_match": vp.queue_force_color_match,
        "gcode_injection": vp.gcode_injection,
        "bind_ip": vp.bind_ip,
        "remote_interface_ip": vp.remote_interface_ip,
        "tailscale_disabled": vp.tailscale_disabled,
        "position": vp.position,
        "status": status or {"running": False, "pending_files": 0},
    }


@router.get("/tailscale-status")
async def get_tailscale_status(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Return the host's Tailscale identity for the VP card display.

    Surface-only — never blocks the UI on absence. Drives the VP card's
    "Tailscale IP / MagicDNS hostname / copy" pane when the per-VP toggle
    is on. Cert provisioning was removed (#1070 post-rip-out) because
    BambuStudio / OrcaSlicer's printer-MQTT trust path validates only
    against its bundled BBL CA, so an LE-signed cert is rejected
    regardless of hostname. The toggle is now informational; this route
    exposes the network identity users need to paste into the slicer.
    """
    from backend.app.services.virtual_printer.tailscale import tailscale_service

    status = await tailscale_service.get_status()
    return {
        "available": status.available,
        "hostname": status.hostname,
        "tailnet_name": status.tailnet_name,
        "fqdn": status.fqdn,
        "tailscale_ips": status.tailscale_ips,
        "error": status.error,
    }


@router.get("")
async def list_virtual_printers(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """List all virtual printers with status."""
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import VIRTUAL_PRINTER_MODELS, virtual_printer_manager

    result = await db.execute(select(VirtualPrinter).order_by(VirtualPrinter.position, VirtualPrinter.id))
    vps = result.scalars().all()

    printers = []
    for vp in vps:
        instance = virtual_printer_manager.get_instance(vp.id)
        status = instance.get_status() if instance else {"running": False, "pending_files": 0}
        printers.append(await _vp_to_dict(vp, db, status))

    return {
        "printers": printers,
        "models": VIRTUAL_PRINTER_MODELS,
    }


@router.post("")
async def create_virtual_printer(
    body: VirtualPrinterCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Create a new virtual printer."""
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import VIRTUAL_PRINTER_MODELS, virtual_printer_manager
    from backend.app.services.virtual_printer.manager import DEFAULT_VIRTUAL_PRINTER_MODEL

    # Validate mode
    if body.mode not in ("print_queue", "auto_queue", "file_manager", "proxy"):
        return JSONResponse(status_code=400, content={"detail": "Invalid mode"})

    # Validate model
    if body.model and body.model not in VIRTUAL_PRINTER_MODELS:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid model. Must be one of: {', '.join(VIRTUAL_PRINTER_MODELS.keys())}"},
        )

    # Validate access code length
    if body.access_code and len(body.access_code) != 8:
        return JSONResponse(status_code=400, content={"detail": "Access code must be exactly 8 characters"})

    # Validation when enabling. Non-proxy VPs with a target printer derive their
    # access code from the target (the bridge forwards the slicer's auth bytes
    # through to the real printer, so the codes MUST match), so a separately
    # supplied access_code isn't required in that case.
    if body.enabled:
        if not body.bind_ip:
            return JSONResponse(status_code=400, content={"detail": "Bind IP is required when enabling"})
        if body.mode == "proxy":
            if not body.target_printer_id:
                return JSONResponse(status_code=400, content={"detail": "Target printer is required for proxy mode"})
        else:
            if not body.access_code and not body.target_printer_id:
                return JSONResponse(status_code=400, content={"detail": "Access code is required when enabling"})

    # In print_queue mode without auto-select (auto_queue toggle off) AND
    # without a target printer, auto_dispatch=True is unsafe — uploads have
    # nowhere to land automatically and would silently fall through to the
    # library. Force the operator to either pick a target or switch to
    # auto_queue / disable auto_dispatch.
    if body.mode == "print_queue" and body.auto_dispatch and not body.target_printer_id:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Auto-dispatch in Queue mode requires a Target Printer. "
                    "Pick a target, enable Auto-select printer, or turn Auto-dispatch off."
                )
            },
        )

    # Validate proxy target printer exists
    target_printer = None
    if body.target_printer_id:
        from backend.app.models.printer import Printer

        result = await db.execute(select(Printer).where(Printer.id == body.target_printer_id))
        target_printer = result.scalar_one_or_none()
        if not target_printer:
            return JSONResponse(
                status_code=400, content={"detail": f"Printer with ID {body.target_printer_id} not found"}
            )

    # Force-inherit the access code from the target printer for non-proxy VPs.
    # The non-proxy bridge (Archive / Review / Queue with a target set) forwards
    # the slicer's MQTT / RTSPS auth bytes through to the real printer, so any
    # value the user supplied here would silently break the bridge if it didn't
    # match the printer's code. The UI renders the field read-only when a target
    # is set; this is the belt-and-braces backstop for any non-UI client.
    effective_access_code = body.access_code
    if body.mode != "proxy" and target_printer is not None:
        effective_access_code = target_printer.access_code

    # Validate target folder exists (NULL is fine — means library root).
    if body.target_folder_id:
        from backend.app.models.library import LibraryFolder

        result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == body.target_folder_id))
        if not result.scalar_one_or_none():
            return JSONResponse(
                status_code=400, content={"detail": f"Library folder with ID {body.target_folder_id} not found"}
            )

    # Validate bind_ip uniqueness (against all enabled VPs)
    if body.bind_ip:
        result = await db.execute(
            select(VirtualPrinter).where(
                VirtualPrinter.bind_ip == body.bind_ip,
                VirtualPrinter.enabled == True,  # noqa: E712
            )
        )
        if result.scalar_one_or_none():
            return JSONResponse(status_code=400, content={"detail": f"Bind IP {body.bind_ip} is already in use"})

    # Generate next serial suffix
    result = await db.execute(select(VirtualPrinter.serial_suffix).order_by(VirtualPrinter.id.desc()))
    last_suffix = result.scalar()
    if last_suffix:
        try:
            next_num = int(last_suffix) + 1
            new_suffix = str(next_num).zfill(9)
        except ValueError:
            new_suffix = "391800002"
    else:
        new_suffix = "391800001"

    # Get next position
    result = await db.execute(select(VirtualPrinter.position).order_by(VirtualPrinter.position.desc()))
    last_pos = result.scalar()
    next_pos = (last_pos or 0) + 1

    vp = VirtualPrinter(
        name=body.name,
        enabled=body.enabled,
        mode=body.mode,
        model=body.model
        or _resolve_printer_model(target_printer.model if target_printer and body.mode == "proxy" else None)
        or DEFAULT_VIRTUAL_PRINTER_MODEL,
        access_code=effective_access_code,
        target_printer_id=body.target_printer_id,
        target_folder_id=body.target_folder_id,
        auto_dispatch=body.auto_dispatch,
        queue_force_color_match=body.queue_force_color_match,
        gcode_injection=body.gcode_injection,
        bind_ip=body.bind_ip,
        remote_interface_ip=body.remote_interface_ip,
        tailscale_disabled=body.tailscale_disabled,
        serial_suffix=new_suffix,
        position=next_pos,
    )
    db.add(vp)
    await db.commit()
    await db.refresh(vp)

    logger.info("Created virtual printer: %s (id=%d)", vp.name, vp.id)

    # Sync services if enabled
    if body.enabled:
        try:
            await virtual_printer_manager.sync_from_db()
        except Exception as e:
            logger.error("Failed to start virtual printer after create: %s", e)

    return await _vp_to_dict(vp, db)


@router.get("/ca-certificate")
async def get_ca_certificate(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Return the shared virtual-printer CA certificate (PEM) for slicer trust import.

    One CA is shared by every virtual printer — the user imports it into their
    slicer's trust store once. Only the public certificate is returned; the CA
    private key never leaves the backend.
    """
    from backend.app.services.virtual_printer import virtual_printer_manager

    try:
        return virtual_printer_manager.get_ca_certificate_info()
    except Exception as e:
        logger.error("Failed to obtain virtual printer CA certificate: %s", e)
        return JSONResponse(status_code=500, content={"detail": "Could not generate the CA certificate"})


@router.get("/{vp_id}/diagnostic", response_model=VPDiagnosticResult)
async def diagnose_virtual_printer(
    vp_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Run setup diagnostics for a virtual printer.

    Probes the VP's own bind IP and services so the user can self-diagnose the
    common "my virtual printer doesn't show up in the slicer" failures.
    """
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import virtual_printer_manager
    from backend.app.services.virtual_printer.diagnostic import run_vp_diagnostic

    result = await db.execute(select(VirtualPrinter).where(VirtualPrinter.id == vp_id))
    vp = result.scalar_one_or_none()
    if not vp:
        return JSONResponse(status_code=404, content={"detail": "Virtual printer not found"})

    instance = virtual_printer_manager.get_instance(vp.id)
    return await run_vp_diagnostic(vp, instance)


@router.get("/{vp_id}")
async def get_virtual_printer(
    vp_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get a single virtual printer with status."""
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import virtual_printer_manager

    result = await db.execute(select(VirtualPrinter).where(VirtualPrinter.id == vp_id))
    vp = result.scalar_one_or_none()
    if not vp:
        return JSONResponse(status_code=404, content={"detail": "Virtual printer not found"})

    instance = virtual_printer_manager.get_instance(vp.id)
    status = instance.get_status() if instance else {"running": False, "pending_files": 0}

    return await _vp_to_dict(vp, db, status)


@router.put("/{vp_id}")
async def update_virtual_printer(
    vp_id: int,
    body: VirtualPrinterUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Update a virtual printer."""
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import VIRTUAL_PRINTER_MODELS, virtual_printer_manager

    result = await db.execute(select(VirtualPrinter).where(VirtualPrinter.id == vp_id))
    vp = result.scalar_one_or_none()
    if not vp:
        return JSONResponse(status_code=404, content={"detail": "Virtual printer not found"})

    # Redact the plaintext access code before it reaches the DEBUG log — dumping
    # ``model_dump`` verbatim leaked it whenever a user saved a new code
    # (upstream Bambuddy v0.2.4.5). Not exploitable in the field (DEBUG is off by
    # default) but exactly the leak the no-secrets-in-logs rule exists to prevent.
    _logged_body = body.model_dump(exclude_unset=True)
    if "access_code" in _logged_body and _logged_body["access_code"]:
        _logged_body["access_code"] = "***"
    logger.debug(
        "Update VP %d: body=%s, current state: mode=%s, enabled=%s, access_code_set=%s, bind_ip=%s, target=%s",
        vp_id,
        _logged_body,
        vp.mode,
        vp.enabled,
        bool(vp.access_code),
        vp.bind_ip,
        vp.target_printer_id,
    )

    # Apply updates
    if body.name is not None:
        vp.name = body.name
    if body.mode is not None:
        if body.mode not in ("print_queue", "auto_queue", "file_manager", "proxy"):
            return JSONResponse(status_code=400, content={"detail": "Invalid mode"})
        vp.mode = body.mode
    if body.model is not None:
        if body.model not in VIRTUAL_PRINTER_MODELS:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Invalid model. Must be one of: {', '.join(VIRTUAL_PRINTER_MODELS.keys())}"},
            )
        vp.model = body.model
    if body.access_code is not None:
        if body.access_code and len(body.access_code) != 8:
            return JSONResponse(status_code=400, content={"detail": "Access code must be exactly 8 characters"})
        vp.access_code = body.access_code
    if body.clear_target_printer:
        vp.target_printer_id = None
    elif body.target_printer_id is not None:
        from backend.app.models.printer import Printer

        result = await db.execute(select(Printer).where(Printer.id == body.target_printer_id))
        target_printer = result.scalar_one_or_none()
        if not target_printer:
            return JSONResponse(
                status_code=400, content={"detail": f"Printer with ID {body.target_printer_id} not found"}
            )
        vp.target_printer_id = body.target_printer_id
        # Auto-inherit model from target printer in proxy mode (unless user explicitly set model)
        if body.model is None and vp.mode == "proxy" and target_printer.model:
            vp.model = _resolve_printer_model(target_printer.model) or target_printer.model
    if body.clear_target_folder:
        vp.target_folder_id = None
    elif body.target_folder_id is not None:
        from backend.app.models.library import LibraryFolder

        result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == body.target_folder_id))
        if not result.scalar_one_or_none():
            return JSONResponse(
                status_code=400,
                content={"detail": f"Library folder with ID {body.target_folder_id} not found"},
            )
        vp.target_folder_id = body.target_folder_id
    if body.auto_dispatch is not None:
        vp.auto_dispatch = body.auto_dispatch
    if body.queue_force_color_match is not None:
        vp.queue_force_color_match = body.queue_force_color_match
    if body.gcode_injection is not None:
        vp.gcode_injection = body.gcode_injection
    if body.bind_ip is not None:
        vp.bind_ip = body.bind_ip
    if body.remote_interface_ip is not None:
        vp.remote_interface_ip = body.remote_interface_ip
    if body.tailscale_disabled is not None:
        vp.tailscale_disabled = body.tailscale_disabled

    # After all partial updates: validate the effective state. In print_queue
    # mode (auto_queue toggle off) without a Target Printer, auto_dispatch=True
    # is unsafe — uploads have nowhere to land automatically.
    if vp.mode == "print_queue" and vp.auto_dispatch and not vp.target_printer_id:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Auto-dispatch in Queue mode requires a Target Printer. "
                    "Pick a target, enable Auto-select printer, or turn Auto-dispatch off."
                )
            },
        )

    # Auto-inherit model when switching to proxy mode with existing target printer
    if body.mode == "proxy" and body.model is None and body.target_printer_id is None and vp.target_printer_id:
        from backend.app.models.printer import Printer as PrinterModel

        result = await db.execute(select(PrinterModel).where(PrinterModel.id == vp.target_printer_id))
        existing_target = result.scalar_one_or_none()
        if existing_target and existing_target.model:
            vp.model = _resolve_printer_model(existing_target.model) or existing_target.model

    # Force-inherit the access code from the target printer for non-proxy VPs.
    # See create_virtual_printer for the rationale: the bridge forwards the
    # slicer's auth bytes through, so the VP's code MUST equal the target's.
    # This runs after every patch (whether or not access_code or target were in
    # the body), so changing the target also resyncs the code, and an explicit
    # access_code submitted alongside a target is silently overridden.
    if vp.mode != "proxy" and vp.target_printer_id is not None:
        from backend.app.models.printer import Printer as PrinterModelAC

        result = await db.execute(select(PrinterModelAC).where(PrinterModelAC.id == vp.target_printer_id))
        target_for_ac = result.scalar_one_or_none()
        if target_for_ac is not None and vp.access_code != target_for_ac.access_code:
            vp.access_code = target_for_ac.access_code

    # Determine final enabled state
    explicitly_enabling = body.enabled is True
    new_enabled = body.enabled if body.enabled is not None else vp.enabled
    effective_mode = vp.mode

    if explicitly_enabling:
        # User is explicitly toggling on - enforce all requirements
        if not vp.bind_ip:
            logger.warning("Update VP %d rejected: no bind_ip", vp_id)
            return JSONResponse(status_code=400, content={"detail": "Bind IP is required when enabling"})
        # Validate bind_ip uniqueness (against all enabled VPs)
        existing = await db.execute(
            select(VirtualPrinter).where(
                VirtualPrinter.bind_ip == vp.bind_ip,
                VirtualPrinter.id != vp_id,
                VirtualPrinter.enabled == True,  # noqa: E712
            )
        )
        conflict = existing.scalar_one_or_none()
        if conflict:
            logger.warning(
                "Update VP %d rejected: bind_ip %s already in use by VP %d (enabled=%s, mode=%s)",
                vp_id,
                vp.bind_ip,
                conflict.id,
                conflict.enabled,
                conflict.mode,
            )
            return JSONResponse(
                status_code=400,
                content={"detail": f"Bind IP {vp.bind_ip} is already in use by '{conflict.name}'"},
            )
        if effective_mode == "proxy":
            if not vp.target_printer_id:
                logger.warning("Update VP %d rejected: no target_printer_id for proxy mode", vp_id)
                return JSONResponse(status_code=400, content={"detail": "Target printer is required for proxy mode"})
        else:
            if not vp.access_code:
                logger.warning(
                    "Update VP %d rejected: no access_code for non-proxy enable (mode=%s)", vp_id, effective_mode
                )
                return JSONResponse(status_code=400, content={"detail": "Access code is required when enabling"})
    elif new_enabled and body.enabled is None:
        # VP is already enabled and user is changing other fields -
        # auto-disable if new state doesn't meet requirements
        if not vp.bind_ip:
            new_enabled = False
        elif effective_mode == "proxy":
            if not vp.target_printer_id:
                new_enabled = False
        else:
            if not vp.access_code:
                new_enabled = False

    vp.enabled = new_enabled

    await db.commit()
    await db.refresh(vp)

    logger.info("Updated virtual printer: %s (id=%d)", vp.name, vp.id)

    # Sync services
    try:
        await virtual_printer_manager.sync_from_db()
    except Exception as e:
        logger.error("Failed to sync virtual printers after update: %s", e)

    instance = virtual_printer_manager.get_instance(vp.id)
    status = instance.get_status() if instance else {"running": False, "pending_files": 0}

    return await _vp_to_dict(vp, db, status)


@router.delete("/{vp_id}")
async def delete_virtual_printer(
    vp_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Delete a virtual printer."""
    from sqlalchemy import delete as sql_delete

    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import virtual_printer_manager

    result = await db.execute(select(VirtualPrinter).where(VirtualPrinter.id == vp_id))
    vp = result.scalar_one_or_none()
    if not vp:
        return JSONResponse(status_code=404, content={"detail": "Virtual printer not found"})

    vp_name = vp.name

    # Stop instance if running
    await virtual_printer_manager.remove_instance(vp_id)

    # Delete from DB
    await db.execute(sql_delete(VirtualPrinter).where(VirtualPrinter.id == vp_id))
    await db.commit()

    logger.info("Deleted virtual printer: %s (id=%d)", vp_name, vp_id)

    # Remove the VP's on-disk upload dir (uploads/<vp_id>) — the DB row is gone
    # so any leftover cache/temp files there are orphaned and would accumulate
    # across create/delete churn. Done after the commit so a crash between only
    # leaves orphan files (harmless, swept by prune scripts), never an orphan DB
    # row (upstream Bambuddy v0.2.4.5).
    try:
        upload_dir = virtual_printer_manager._base_dir / "uploads" / str(vp_id)
        if upload_dir.exists():
            import shutil

            shutil.rmtree(upload_dir, ignore_errors=True)
    except Exception as e:
        logger.error("Failed to remove upload dir for VP %d: %s", vp_id, e)

    # Resync remaining services
    try:
        await virtual_printer_manager.sync_from_db()
    except Exception as e:
        logger.error("Failed to sync virtual printers after delete: %s", e)

    return {"detail": "Deleted", "id": vp_id}
