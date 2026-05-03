"""Virtual Printer Manager - coordinates SSDP, MQTT, and FTP services.

Each virtual printer runs its own independent services (FTP, MQTT, SSDP, Bind)
bound to its dedicated IP address, regardless of mode.
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from backend.app.core.config import settings as app_settings
from backend.app.services.virtual_printer.bind_server import BindServer
from backend.app.services.virtual_printer.certificate import CertificateService
from backend.app.services.virtual_printer.ftp_server import VirtualPrinterFTPServer
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer
from backend.app.services.virtual_printer.ssdp_server import SSDPProxy, VirtualPrinterSSDPServer
from backend.app.services.virtual_printer.tailscale import tailscale_service
from backend.app.services.virtual_printer.tcp_proxy import SlicerProxyManager

logger = logging.getLogger(__name__)


# Mapping of SSDP model codes to display names
# These are the codes that slicers expect during discovery
# Sources:
#   - https://gist.github.com/Alex-Schaefer/72a9e2491a42da2ef99fb87601955cc3
#   - https://github.com/psychoticbeef/BambuLabOrcaSlicerDiscovery
VIRTUAL_PRINTER_MODELS = {
    # X1 Series
    "BL-P001": "X1C",  # X1 Carbon
    "BL-P002": "X1",  # X1
    "C13": "X1E",  # X1E
    # X2 Series
    "N6": "X2D",  # X2D
    # P Series
    "C11": "P1P",  # P1P
    "C12": "P1S",  # P1S
    "N7": "P2S",  # P2S
    # A1 Series
    "N2S": "A1",  # A1
    "N1": "A1 Mini",  # A1 Mini
    # H2 Series
    "O1D": "H2D",  # H2D
    "O1C": "H2C",  # H2C
    "O1C2": "H2C",  # H2C (dual nozzle variant)
    "O1S": "H2S",  # H2S
}

# Serial number prefixes for each model (based on Bambu Lab serial number format)
# Format: MMM??RYMDDUUUUU (15 chars total)
#   MMM = Model prefix (3 chars)
#   ?? = Unknown/revision code (2 chars)
#   R = Revision letter (1 char)
#   Y = Year digit (1 char)
#   M = Month (1 char, hex: 1-9, A=Oct, B=Nov, C=Dec)
#   DD = Day (2 chars)
#   UUUUU = Unit number (5 chars)
MODEL_SERIAL_PREFIXES = {
    # X1 Series
    "BL-P001": "00M00A",  # X1C
    "BL-P002": "00M00A",  # X1
    "C13": "03W00A",  # X1E
    # X2 Series
    "N6": "20P90A",  # X2D (first 4 chars "20P9" match real serials)
    # P Series
    "C11": "01S00A",  # P1P
    "C12": "01P00A",  # P1S
    "N7": "22E00A",  # P2S
    # A1 Series
    "N2S": "03900A",  # A1
    "N1": "03000A",  # A1 Mini
    # H2 Series
    "O1D": "09400A",  # H2D
    "O1C": "09400A",  # H2C
    "O1C2": "09400A",  # H2C (dual nozzle variant)
    "O1S": "09400A",  # H2S
}

# Reverse mapping: display name → SSDP model code (for auto-inheriting from printer model)
DISPLAY_NAME_TO_MODEL_CODE = {v: k for k, v in VIRTUAL_PRINTER_MODELS.items()}

# Default model
DEFAULT_VIRTUAL_PRINTER_MODEL = "BL-P001"  # X1C


def _get_serial_for_model(model: str, serial_suffix: str) -> str:
    """Get serial number for the given model and suffix."""
    prefix = MODEL_SERIAL_PREFIXES.get(model, "00M09A")
    return f"{prefix}{serial_suffix}"


class VirtualPrinterInstance:
    """Per-printer state and file handling logic.

    Each instance represents one virtual printer with its own config,
    upload directory, certificates, and file handling mode.
    """

    def __init__(
        self,
        *,
        vp_id: int,
        name: str,
        mode: str,
        model: str,
        access_code: str,
        serial_suffix: str,
        target_printer_ip: str = "",
        target_printer_serial: str = "",
        target_printer_id: int | None = None,
        target_folder_id: int | None = None,
        auto_dispatch: bool = True,
        bind_ip: str = "",
        remote_interface_ip: str = "",
        tailscale_disabled: bool = True,
        base_dir: Path,
        session_factory: Callable | None = None,
    ):
        self.id = vp_id
        self.name = name
        self.mode = mode
        self.model = model
        self.access_code = access_code
        self.serial_suffix = serial_suffix
        self.target_printer_ip = target_printer_ip
        self.target_printer_serial = target_printer_serial
        self.target_printer_id = target_printer_id
        # Library folder where incoming files land. None = library root.
        # Forwarded to ``_save_to_library`` for every file the VP receives.
        self.target_folder_id = target_folder_id
        self.auto_dispatch = auto_dispatch
        self.bind_ip = bind_ip
        self.remote_interface_ip = remote_interface_ip
        self.tailscale_disabled = tailscale_disabled
        self._session_factory = session_factory

        # Tailscale FQDN used for this instance (set at start_server/start_proxy time
        # if Tailscale is available and not disabled). When set, SSDP advertises it
        # so slicers see the hostname that matches the trusted LE cert.
        self.tailscale_fqdn: str | None = None

        # Directories
        self.upload_dir = base_dir / "uploads" / str(vp_id)
        self.cert_dir = base_dir / "certs" / str(vp_id)
        shared_ca_dir = base_dir / "certs"

        # Ensure directories exist
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / "cache").mkdir(exist_ok=True)
        self.cert_dir.mkdir(parents=True, exist_ok=True)

        # Certificate service (shared CA, per-instance printer cert)
        self._cert_service = CertificateService(
            cert_dir=self.cert_dir,
            serial=self.serial,
            shared_ca_dir=shared_ca_dir,
        )

        # Pending files for MQTT correlation
        self._pending_files: dict[str, Path] = {}

        # Per-instance services
        self._proxy: SlicerProxyManager | None = None
        self._ftp: VirtualPrinterFTPServer | None = None
        self._mqtt: SimpleMQTTServer | None = None
        self._bind: BindServer | None = None
        self._ssdp: VirtualPrinterSSDPServer | None = None
        self._ssdp_proxy: SSDPProxy | None = None
        self._tasks: list[asyncio.Task] = []
        # Cert renewal/restart tasks tracked separately from _tasks because they
        # have a different lifecycle (run for the lifetime of the VP, not just
        # during service start). See A.16: _cancel_restart_task must skip when
        # the caller IS the restart task itself, otherwise the cert-rotation
        # cron self-deadlocks the renewal restart.
        self._cert_renewal_task: asyncio.Task | None = None
        self._cert_restart_task: asyncio.Task | None = None

    @property
    def serial(self) -> str:
        """Full serial number for this virtual printer."""
        return _get_serial_for_model(self.model or DEFAULT_VIRTUAL_PRINTER_MODEL, self.serial_suffix)

    @property
    def cert_path(self) -> Path:
        return self._cert_service.cert_path

    @property
    def key_path(self) -> Path:
        return self._cert_service.key_path

    @property
    def is_proxy(self) -> bool:
        return self.mode == "proxy"

    @property
    def is_running(self) -> bool:
        return len(self._tasks) > 0 and all(not t.done() for t in self._tasks)

    def generate_certificates(self) -> tuple[Path, Path]:
        """Generate certificates for this instance."""
        self._cert_service.serial = self.serial if not self.is_proxy else (self.target_printer_serial or self.serial)
        additional_ips = [self.remote_interface_ip] if self.remote_interface_ip else None
        if self.bind_ip:
            additional_ips = additional_ips or []
            additional_ips.append(self.bind_ip)
        self._cert_service.delete_printer_certificate()
        return self._cert_service.generate_certificates(additional_ips=additional_ips)

    # -- File handling callbacks --

    async def on_file_received(self, file_path: Path, source_ip: str) -> None:
        """Handle file upload completion from FTP."""
        logger.info("[VP %s] Received file: %s from %s", self.name, file_path.name, source_ip)

        self._pending_files[file_path.name] = file_path

        # Four supported modes (m002 already migrated legacy ``immediate``
        # / ``review`` rows to ``file_manager``, and ``queue`` to
        # ``print_queue``):
        #   * print_queue  — archive + push directly to a per-printer queue
        #                    (specific target_printer_id, or "least busy of
        #                    matching model" via _find_best_queue).
        #   * auto_queue   — archive + drop into the global auto-queue
        #                    layer; AutoQueueScheduler picks an eligible
        #                    printer with full filament/color check.
        #   * file_manager — save to library for review.
        #   * proxy        — bypasses this handler (the TCP proxy passes
        #                    bytes straight through to the real printer).
        # Anything unexpected falls back to file_manager rather than
        # silently dropping the file.
        if self.mode == "print_queue":
            await self._add_to_print_queue(file_path, source_ip)
        elif self.mode == "auto_queue":
            await self._add_to_auto_queue(file_path, source_ip)
        else:
            await self._save_to_library(file_path, source_ip)

        # Reset MQTT status back to IDLE
        if self._mqtt and file_path.suffix.lower() == ".3mf":
            self._mqtt.set_gcode_state("IDLE")

    async def on_print_command(self, filename: str, data: dict) -> None:
        """Handle print command from MQTT."""
        logger.info("[VP %s] Print command for: %s", self.name, filename)

    async def _save_to_library(self, file_path: Path, source_ip: str):  # noqa: ARG002
        """Save file to the File Manager library.

        Returns the persisted ``LibraryFile`` row (or ``None`` on failure /
        non-3MF input). The Audit-2 redesign in 0.4.2 made this the single
        ingestion entry-point for **all** VP modes (not just file_manager) —
        ``_add_to_print_queue`` and ``_add_to_auto_queue`` now call it first
        and queue against the resulting ``library_file_id`` instead of
        pre-creating a placeholder ``status='archived'`` archive row.

        ``source_ip`` is currently informational only (preserved in the
        argument list so callers don't have to know it became a no-op).
        """
        if not self._session_factory:
            logger.error("Cannot save to library: no database session factory configured")
            return None

        if file_path.suffix.lower() != ".3mf":
            logger.debug("Skipping non-3MF file: %s", file_path.name)
            self._pending_files.pop(file_path.name, None)
            try:
                file_path.unlink()
            except OSError:
                pass
            return None

        try:
            import hashlib
            import uuid

            from backend.app.api.routes.library import (
                get_library_files_dir,
                get_library_thumbnails_dir,
                to_relative_path,
            )
            from backend.app.models.library import LibraryFile
            from backend.app.services.archive import ThreeMFParser
            from backend.app.services.library_helpers import compute_file_tags, detect_file_type

            async with self._session_factory() as db_session:
                filename = file_path.name
                ext = file_path.suffix.lower()
                # Sliced 3MFs uploaded via the slicer's "Send to printer"
                # carry a ``.gcode.3mf`` suffix; the helper collapses both
                # that and plain ``.3mf`` to canonical primary types so VP
                # entries match the file_manager's badging.
                file_type = detect_file_type(filename)

                library_files_dir = get_library_files_dir()
                unique_filename = f"{uuid.uuid4().hex}{ext}"
                dest_path = library_files_dir / unique_filename

                import shutil

                shutil.copy2(str(file_path), str(dest_path))

                sha256_hash = hashlib.sha256()
                with open(dest_path, "rb") as f:
                    for block in iter(lambda: f.read(4096), b""):
                        sha256_hash.update(block)
                file_hash = sha256_hash.hexdigest()

                metadata = None
                thumbnail_path = None

                try:
                    parser = ThreeMFParser(str(dest_path))
                    raw_metadata = parser.parse()

                    thumbnail_data = raw_metadata.get("_thumbnail_data")
                    thumbnail_ext = raw_metadata.get("_thumbnail_ext", ".png")

                    if thumbnail_data:
                        thumbnails_dir = get_library_thumbnails_dir()
                        thumb_filename = f"{uuid.uuid4().hex}{thumbnail_ext}"
                        thumb_path = thumbnails_dir / thumb_filename
                        with open(thumb_path, "wb") as f:
                            f.write(thumbnail_data)
                        thumbnail_path = str(thumb_path)

                    def clean_metadata(obj):
                        if isinstance(obj, dict):
                            return {
                                k: clean_metadata(v)
                                for k, v in obj.items()
                                if not isinstance(v, bytes) and k not in ("_thumbnail_data", "_thumbnail_ext")
                            }
                        elif isinstance(obj, list):
                            return [clean_metadata(i) for i in obj if not isinstance(i, bytes)]
                        elif isinstance(obj, bytes):
                            return None
                        return obj

                    metadata = clean_metadata(raw_metadata)

                    # Per-plate cache (matches the regular library upload path
                    # in routes/library.py::upload_file). Without this, VP-saved
                    # library entries miss the gallery on the file-manager UI
                    # because ``is_multi_plate`` is unset.
                    try:
                        import zipfile as _zf

                        from backend.app.services.archive import parse_plates_from_3mf

                        with _zf.ZipFile(str(dest_path), "r") as _zfh:
                            plates_payload = parse_plates_from_3mf(_zfh)
                        if plates_payload and metadata is not None:
                            metadata["plates"] = plates_payload
                            metadata["is_multi_plate"] = len(plates_payload) > 1
                    except Exception as _pe:
                        logger.debug("[VP %s] per-plate parse failed (non-critical): %s", self.name, _pe)
                except Exception as e:
                    logger.warning("[VP %s] Failed to parse 3MF metadata: %s", self.name, e)

                library_file = LibraryFile(
                    folder_id=self.target_folder_id,
                    filename=filename,
                    file_path=to_relative_path(dest_path),
                    file_type=file_type,
                    file_tags=compute_file_tags(
                        filename=filename,
                        file_type=file_type,
                        file_metadata=metadata,
                        source_type=None,
                        swap_compatible=False,
                    ),
                    file_size=file_path.stat().st_size,
                    file_hash=file_hash,
                    thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
                    file_metadata=metadata,
                )
                db_session.add(library_file)
                await db_session.commit()
                await db_session.refresh(library_file)
                logger.info(
                    "[VP %s] Saved to library: %s (id=%s, folder=%s)",
                    self.name,
                    filename,
                    library_file.id,
                    self.target_folder_id,
                )

                # Notify frontend to refresh File Manager
                try:
                    from backend.app.core.websocket import ws_manager

                    await ws_manager.send_library_file_added({"id": library_file.id, "filename": filename})
                except Exception:
                    pass

                try:
                    file_path.unlink()
                except OSError:
                    pass
                self._pending_files.pop(file_path.name, None)
                return library_file
        except Exception as e:
            logger.error("Error saving to library: %s", e)
            return None

    async def _add_to_print_queue(self, file_path: Path, source_ip: str) -> None:
        """Save file to library and add to a per-printer queue.

        Audit-2 redesign (0.4.2): the file lands in the library FIRST
        (single source-of-truth for "files we have"), then the queue item
        is created with ``library_file_id``. The dispatcher's
        ``_run_print_library_file`` path creates the archive at print-start
        with ``status='printing'`` — we no longer pre-create a synthetic
        ``status='archived'`` placeholder.
        """
        if not self._session_factory:
            logger.error("Cannot add to print queue: no database session factory configured")
            return

        if file_path.suffix.lower() != ".3mf":
            self._pending_files.pop(file_path.name, None)
            try:
                file_path.unlink()
            except OSError:
                pass
            return

        try:
            # Step 1: save to library (handles file copy + metadata extraction
            # + cleanup of source temp + WS broadcast). Returns the persisted
            # ``LibraryFile`` row or None on failure.
            library_file = await self._save_to_library(file_path, source_ip)
            if not library_file:
                logger.error("[VP %s] Failed to save to library: %s", self.name, file_path.name)
                return

            # Step 2: pick a queue based on the library row's metadata and
            # link a queue item to it.
            from backend.app.models.print_queue import PrintQueueItem

            sliced_model = None
            if isinstance(library_file.file_metadata, dict):
                sliced_model = library_file.file_metadata.get("sliced_for_model")

            async with self._session_factory() as db:
                queue = await self._find_best_queue(db, sliced_model)
                if not queue:
                    # File already in library; just no auto-queue placement.
                    # Operator can pick it up from the File Manager and queue
                    # manually. Same UX as ``mode='file_manager'`` from here on.
                    logger.info(
                        "[VP %s] No matching printer queue for %s — file stays in library only",
                        self.name,
                        library_file.filename,
                    )
                    return

                plate_id = self._extract_plate_id_from_metadata(library_file.file_metadata)
                queue_item = PrintQueueItem(
                    queue_id=queue.id,
                    library_file_id=library_file.id,
                    archive_id=None,  # archive created at print-start by _run_print_library_file
                    plate_id=plate_id,
                    position=1,
                    status="pending",
                    manual_start=not self.auto_dispatch,
                )
                db.add(queue_item)
                await db.commit()
                logger.info(
                    "[VP %s] Added to queue %s (printer %s): item %s, library_file=%s",
                    self.name,
                    queue.id,
                    queue.printer_id,
                    queue_item.id,
                    library_file.id,
                )
        except Exception as e:
            logger.exception("[VP %s] Error adding to print queue: %s", self.name, e)

    async def _add_to_auto_queue(self, file_path: Path, source_ip: str) -> None:
        """Save file to library and drop it into the global auto-queue layer.

        Audit-2 redesign (0.4.2): mirrors ``_add_to_print_queue`` — file
        lands in the library first, then the auto-queue row carries
        ``library_file_id`` instead of pre-creating an
        ``status='archived'`` placeholder archive. Unlike
        ``_add_to_print_queue`` this never picks a printer up-front;
        the AutoQueueScheduler later assigns the item to whichever idle
        printer matches model + filaments + colors. ``target_printer_id``
        on the VP config is intentionally ignored in this mode (use
        ``print_queue`` mode for specific-printer routing).
        """
        if not self._session_factory:
            logger.error("Cannot add to auto-queue: no database session factory configured")
            return

        if file_path.suffix.lower() != ".3mf":
            self._pending_files.pop(file_path.name, None)
            try:
                file_path.unlink()
            except OSError:
                pass
            return

        try:
            # Step 1: save to library. Returns the persisted LibraryFile or
            # None on failure (handles file copy + metadata + cleanup).
            library_file = await self._save_to_library(file_path, source_ip)
            if not library_file:
                logger.error("[VP %s] Failed to save to library: %s", self.name, file_path.name)
                return

            from sqlalchemy import func as sa_func, select as sa_select

            from backend.app.models.auto_queue import AutoQueueItem
            from backend.app.services.auto_queue_threemf import extract_auto_queue_requirements

            plate_id = self._extract_plate_id_from_metadata(library_file.file_metadata)

            # Re-extract requirements from the saved library file so the
            # auto-queue's per-slot filament info matches the bytes we'll
            # actually upload to the printer (the original upload temp is
            # gone after _save_to_library cleans it up).
            on_disk = Path(app_settings.base_dir) / library_file.file_path
            requirements = extract_auto_queue_requirements(on_disk, plate_id=plate_id)

            sliced_model: str | None = None
            if isinstance(library_file.file_metadata, dict):
                sliced_model = library_file.file_metadata.get("sliced_for_model")

            async with self._session_factory() as db:
                # Position at the end of pending items so VP-uploads don't
                # jump ahead of UI submissions.
                max_pos = await db.scalar(
                    sa_select(sa_func.coalesce(sa_func.max(AutoQueueItem.position), 0)).where(
                        AutoQueueItem.status == "pending"
                    )
                )
                next_pos = (max_pos or 0) + 1

                item = AutoQueueItem(
                    library_file_id=library_file.id,
                    archive_id=None,  # archive created at print-start by _run_print_library_file
                    target_model=requirements.target_model or sliced_model,
                    required_filament_types=(
                        list(requirements.required_filament_types) if requirements.required_filament_types else None
                    ),
                    plate_id=plate_id,
                    position=next_pos,
                    status="pending",
                    manual_start=not self.auto_dispatch,
                )
                db.add(item)
                await db.commit()
                logger.info(
                    "[VP %s] Added to auto-queue (item %s, library_file=%s, target_model=%s, filaments=%s)",
                    self.name,
                    item.id,
                    library_file.id,
                    item.target_model,
                    item.required_filament_types,
                )
        except Exception as e:
            logger.exception("[VP %s] Error adding to auto-queue: %s", self.name, e)

    @staticmethod
    def _extract_plate_id(file_path: Path) -> int | None:
        """Extract plate index from 3MF slice_info.config (on-disk fallback).

        Kept as a fallback for code paths that don't yet have a parsed
        ``library_file.file_metadata`` to read from. The Audit-2 redesign
        prefers ``_extract_plate_id_from_metadata`` so VP file ingestion
        doesn't reopen the ZIP a second time.
        """
        try:
            import xml.etree.ElementTree as ET
            import zipfile

            with zipfile.ZipFile(file_path, "r") as zf:
                if "Metadata/slice_info.config" in zf.namelist():
                    content = zf.read("Metadata/slice_info.config").decode()
                    root = ET.fromstring(content)  # noqa: S314  # nosec B314
                    plate = root.find(".//plate")
                    if plate is not None:
                        for meta in plate.findall("metadata"):
                            if meta.get("key") == "index" and meta.get("value"):
                                return int(meta.get("value"))
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_plate_id_from_metadata(file_metadata: dict | None) -> int | None:
        """Pick a plate index out of an already-parsed ``file_metadata`` dict.

        Audit-2 redesign helper: ``_save_to_library`` already opens the
        3MF and caches per-plate info under ``file_metadata['plates']``
        (each entry has an ``index`` field). The slicer's "Send to
        printer" workflow exports ONE plate per upload, so a sliced
        ``.gcode.3mf`` arriving via VP should have a single-entry
        plates list whose index is the user-picked plate.

        Returns ``None`` when the metadata is missing / multi-plate /
        malformed; the dispatcher then defaults plate_id to 1, which is
        the existing behaviour for queue items uploaded without plate
        context.
        """
        if not isinstance(file_metadata, dict):
            return None
        plates = file_metadata.get("plates")
        if isinstance(plates, list) and len(plates) == 1:
            idx = plates[0].get("index") if isinstance(plates[0], dict) else None
            if isinstance(idx, int) and idx >= 1:
                return idx
        return None

    async def _find_best_queue(self, db, sliced_model: str | None):
        """Find the best printer queue for this job.

        If target_printer_id is set and online → use it.
        Otherwise, find the least busy online printer matching ``sliced_model``.
        Returns None if no matching printer is available (file should go
        to library only).

        "Least busy" = lowest total queue time (current print remaining +
        sum of pending print times). Pending-time accounting walks both
        archive- and library-file-backed queue items so the redesigned
        ingestion path (Audit-2 0.4.2) doesn't make the queue look
        artificially empty.
        """
        from sqlalchemy import select as sa_select
        from sqlalchemy.sql import func as sa_func

        from backend.app.models.archive import PrintArchive
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer import Printer
        from backend.app.models.printer_queue import PrinterQueue
        from backend.app.services.printer_manager import printer_manager

        # If explicit target is set and printer is online, use it directly
        if self.target_printer_id:
            state = printer_manager.get_status(self.target_printer_id)
            if state and state.connected:
                result = await db.execute(
                    sa_select(PrinterQueue).where(PrinterQueue.printer_id == self.target_printer_id)
                )
                queue = result.scalar_one_or_none()
                if queue:
                    return queue
            logger.info(
                "[VP %s] Target printer %s not available, searching alternatives", self.name, self.target_printer_id
            )

        if not sliced_model:
            logger.info("[VP %s] No sliced_for_model on library file, cannot auto-assign to queue", self.name)
            return None

        # Get online printers matching the model
        result = await db.execute(
            sa_select(PrinterQueue, Printer.model)
            .join(Printer, Printer.id == PrinterQueue.printer_id)
            .where(Printer.is_active.is_(True), Printer.model == sliced_model)
        )
        matching_queues = result.all()

        # Filter to online and calculate total queue time
        candidates: list[tuple[PrinterQueue, int]] = []
        for queue, _model in matching_queues:
            state = printer_manager.get_status(queue.printer_id)
            if not state or not state.connected:
                continue

            # Current print remaining (minutes → seconds)
            current_remaining = state.remaining_time * 60 if state.remaining_time > 0 else 0

            # Pending items: COALESCE(archive.print_time_seconds,
            # library_file.file_metadata->>'print_time_seconds') so a
            # queue full of library-file dispatches still contributes
            # to the busy-score. JSON path syntax differs between
            # SQLite and Postgres — fall back to summing in Python so
            # the comparator stays portable.
            pending_rows = await db.execute(
                sa_select(
                    sa_func.coalesce(PrintArchive.print_time_seconds, 0),
                    LibraryFile.file_metadata,
                )
                .select_from(PrintQueueItem)
                .join(PrintArchive, PrintArchive.id == PrintQueueItem.archive_id, isouter=True)
                .join(LibraryFile, LibraryFile.id == PrintQueueItem.library_file_id, isouter=True)
                .where(PrintQueueItem.queue_id == queue.id, PrintQueueItem.status == "pending")
            )
            pending_time = 0
            for archive_secs, lib_meta in pending_rows.all():
                if archive_secs:
                    pending_time += int(archive_secs)
                    continue
                if isinstance(lib_meta, dict):
                    secs = lib_meta.get("print_time_seconds")
                    if isinstance(secs, int) and secs > 0:
                        pending_time += secs
            candidates.append((queue, current_remaining + pending_time))

        if not candidates:
            return None

        # Least busy first, then by printer_id for determinism
        candidates.sort(key=lambda c: (c[1], c[0].printer_id))
        return candidates[0][0]

    # -- Tailscale cert renewal lifecycle (#1070, A.16) --

    async def _cancel_renewal_task(self) -> None:
        """Cancel the cert renewal task and await its completion."""
        if self._cert_renewal_task:
            self._cert_renewal_task.cancel()
            try:
                await self._cert_renewal_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("[VP %s] Unexpected error in cert renewal task: %s", self.name, e)
            self._cert_renewal_task = None

    async def _cancel_restart_task(self) -> None:
        """Cancel the cert restart task and await its completion.

        A.16 fix: skip when the caller IS the restart task itself —
        ``stop_server()`` / ``stop_proxy()`` are called from inside
        ``_restart_for_cert_renewal``, which runs AS ``_cert_restart_task``.
        Cancelling + awaiting self flags a CancelledError on the next
        ``await`` in stop_server, which tears down the old listeners but
        never lets start_server run — the VP would sit on an expired cert
        until process restart.
        """
        task = self._cert_restart_task
        if task is asyncio.current_task():
            # Renewal path cleaning up its own restart task: clear the
            # reference so future callers don't see a stale task handle,
            # but do NOT cancel-and-await ourselves.
            self._cert_restart_task = None
            return
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("[VP %s] Unexpected error in cert restart task: %s", self.name, e)
            self._cert_restart_task = None

    async def _restart_for_cert_renewal(self) -> None:
        """Restart VP services to load the newly renewed Tailscale cert into TLS listeners."""
        logger.info("[VP %s] Restarting services to apply renewed Tailscale cert", self.name)
        try:
            if self.is_proxy:
                await self.stop_proxy()
                await self.start_proxy()
            else:
                await self.stop_server()
                await self.start_server()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[VP %s] Failed to restart after cert renewal: %s", self.name, e)

    async def _cert_renewal_loop(self) -> None:
        """Daily background check for Tailscale cert renewal while VP is running.

        Checks first, then sleeps, so a cert that was just barely renewed at startup
        is not re-checked for another 24 h. When a renewal actually happens the loop
        schedules a VP restart so the new cert is loaded into the running TLS listeners.
        """
        while True:
            try:
                if self.tailscale_fqdn:
                    needs_renewal = tailscale_service.cert_needs_renewal(
                        self._cert_service.ts_cert_path, fqdn=self.tailscale_fqdn
                    )
                    if needs_renewal:
                        renewed = await self._cert_service.use_tailscale_cert(self.tailscale_fqdn, tailscale_service)
                        if renewed:
                            logger.info(
                                "[VP %s] Tailscale cert renewed for %s, scheduling restart",
                                self.name,
                                self.tailscale_fqdn,
                            )
                            # Schedule restart in a separate task; this loop ends here
                            # so the restart can cleanly cancel _cert_renewal_task and
                            # create a fresh one via start_server/start_proxy.
                            self._cert_restart_task = asyncio.create_task(
                                self._restart_for_cert_renewal(),
                                name=f"vp_{self.id}_cert_restart",
                            )
                            break
                await asyncio.sleep(86400)  # check once per day
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[VP %s] Cert renewal loop error: %s", self.name, e)
                await asyncio.sleep(3600)  # back off 1 h on unexpected error

    async def _resolve_cert_and_advertise(self) -> tuple[Path, Path, str]:
        """Return (cert_path, key_path, advertise_address) for TLS services.

        When Tailscale is available, provisions a LE cert and returns the
        Tailscale FQDN as the advertise address so SSDP broadcasts the hostname
        that matches the trusted cert.

        Falls back to the self-signed cert and IP-based advertising when
        Tailscale is absent or provisioning fails.
        """
        if self.tailscale_disabled:
            logger.info("[VP %s] Tailscale integration disabled by user, using self-signed cert", self.name)
        else:
            try:
                ts_status = await tailscale_service.get_status()
                if ts_status.available:
                    ts_result = await self._cert_service.use_tailscale_cert(ts_status.fqdn, tailscale_service)
                    if ts_result:
                        self.tailscale_fqdn = ts_status.fqdn
                        logger.info("[VP %s] Using Tailscale cert for %s", self.name, ts_status.fqdn)
                        return ts_result[0], ts_result[1], ts_status.fqdn
                    logger.warning(
                        "[VP %s] Tailscale available (%s) but cert provisioning failed, falling back to self-signed",
                        self.name,
                        ts_status.fqdn,
                    )
                else:
                    logger.info(
                        "[VP %s] Tailscale not available (%s), using self-signed cert",
                        self.name,
                        ts_status.error or "not connected",
                    )
            except Exception as e:
                logger.warning("[VP %s] Tailscale cert check failed, falling back to self-signed: %s", self.name, e)

        self.tailscale_fqdn = None
        cert_path, key_path = self.generate_certificates()
        advertise = self.remote_interface_ip or self.bind_ip or ""
        return cert_path, key_path, advertise

    # -- Service lifecycle --

    async def start_server(self) -> None:
        """Start server-mode services (FTP, MQTT, SSDP, Bind) on this VP's bind_ip."""
        logger.info("[VP %s] Starting server-mode services on %s", self.name, self.bind_ip)

        cert_path, key_path, advertise_addr = await self._resolve_cert_and_advertise()
        bind_addr = self.bind_ip or "0.0.0.0"  # nosec B104

        async def run_with_logging(coro, svc_name):
            try:
                await coro
            except Exception as e:
                logger.error("[VP %s] %s failed: %s", self.name, svc_name, e)

        self._tasks = []

        # FTP server
        self._ftp = VirtualPrinterFTPServer(
            upload_dir=self.upload_dir,
            access_code=self.access_code,
            cert_path=cert_path,
            key_path=key_path,
            on_file_received=self.on_file_received,
            bind_address=bind_addr,
            vp_name=self.name,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._ftp.start(), "FTP"),
                name=f"vp_{self.id}_ftp",
            )
        )

        # MQTT server
        self._mqtt = SimpleMQTTServer(
            serial=self.serial,
            access_code=self.access_code,
            cert_path=cert_path,
            key_path=key_path,
            on_print_command=self.on_print_command,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            bind_address=bind_addr,
            vp_name=self.name,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._mqtt.start(), "MQTT"),
                name=f"vp_{self.id}_mqtt",
            )
        )

        # Bind server
        self._bind = BindServer(
            serial=self.serial,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            name=self.name,
            bind_address=bind_addr,
            cert_path=cert_path,
            key_path=key_path,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._bind.start(), "Bind"),
                name=f"vp_{self.id}_bind",
            )
        )

        # SSDP server. When Tailscale is in play, ``advertise_addr`` is the
        # tailnet FQDN — broadcasting it lets slicers connect via a hostname
        # that matches the trusted LE cert (no manual CA install required).
        self._ssdp = VirtualPrinterSSDPServer(
            name=self.name,
            serial=self.serial,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            advertise_ip=advertise_addr,
            bind_ip=bind_addr,
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._ssdp.start(), "SSDP"),
                name=f"vp_{self.id}_ssdp",
            )
        )

        # Schedule daily cert-renewal check; replaces any stale renewal task
        # left over from a previous start_server call.
        await self._cancel_renewal_task()
        self._cert_renewal_task = asyncio.create_task(
            self._cert_renewal_loop(),
            name=f"vp_{self.id}_cert_renewal",
        )

        logger.info("[VP %s] Server-mode services started on %s", self.name, bind_addr)

    async def stop_server(self) -> None:
        """Stop server-mode services."""
        await self._cancel_renewal_task()
        await self._cancel_restart_task()
        if self._ftp:
            await self._ftp.stop()
            self._ftp = None
        if self._mqtt:
            await self._mqtt.stop()
            self._mqtt = None
        if self._bind:
            await self._bind.stop()
            self._bind = None
        if self._ssdp:
            await self._ssdp.stop()
            self._ssdp = None
        await self._cancel_tasks()

    async def start_proxy(self) -> None:
        """Start proxy mode services for this instance."""
        logger.info("[VP %s] Starting proxy mode to %s", self.name, self.target_printer_ip)

        # _resolve_cert_and_advertise sets self.tailscale_fqdn when LE provisioning
        # succeeds; the advertise_addr is unused by proxy SSDP (it has its own
        # advertise_ip wired below) but we still want LE provisioning + the
        # daily renewal loop to run for proxy VPs.
        cert_path, key_path, _ = await self._resolve_cert_and_advertise()

        self._proxy = SlicerProxyManager(
            target_host=self.target_printer_ip,
            cert_path=cert_path,
            key_path=key_path,
            on_activity=lambda n, m: logger.info("[VP %s] Proxy %s: %s", self.name, n, m),
            bind_address=self.bind_ip or "0.0.0.0",  # nosec B104
            bind_identity={
                "serial": self.target_printer_serial or self.serial,
                "model": self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                "name": self.name,
                "version": "01.00.00.00",
            },
        )

        async def run_with_logging(coro, svc_name):
            try:
                await coro
            except Exception as e:
                logger.error("[VP %s] %s failed: %s", self.name, svc_name, e)

        self._tasks = []

        # SSDP for proxy
        proxy_serial = self.target_printer_serial or self.serial
        if self.remote_interface_ip:
            from backend.app.services.network_utils import find_interface_for_ip

            local_iface = find_interface_for_ip(self.target_printer_ip)
            if local_iface:
                self._ssdp_proxy = SSDPProxy(
                    local_interface_ip=local_iface["ip"],
                    remote_interface_ip=self.remote_interface_ip,
                    target_printer_ip=self.target_printer_ip,
                    name=self.name,
                )
                self._tasks.append(
                    asyncio.create_task(
                        run_with_logging(self._ssdp_proxy.start(), "SSDP Proxy"),
                        name=f"vp_{self.id}_ssdp_proxy",
                    )
                )
            else:
                self._start_fallback_ssdp(proxy_serial, run_with_logging)
        else:
            self._start_fallback_ssdp(proxy_serial, run_with_logging)

        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._proxy.start(), "Proxy"),
                name=f"vp_{self.id}_proxy",
            )
        )

        # Schedule daily cert-renewal check for the proxy as well.
        await self._cancel_renewal_task()
        self._cert_renewal_task = asyncio.create_task(
            self._cert_renewal_loop(),
            name=f"vp_{self.id}_cert_renewal",
        )

    def _start_fallback_ssdp(self, proxy_serial: str, run_with_logging) -> None:
        """Start single-interface SSDP server as fallback for proxy mode."""
        self._ssdp = VirtualPrinterSSDPServer(
            name=f"{self.name} (Proxy)",
            serial=proxy_serial,
            model=self.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
            advertise_ip=self.bind_ip or "",
            bind_ip=self.bind_ip or "",
        )
        self._tasks.append(
            asyncio.create_task(
                run_with_logging(self._ssdp.start(), "SSDP"),
                name=f"vp_{self.id}_ssdp",
            )
        )

    async def stop_proxy(self) -> None:
        """Stop proxy mode services for this instance."""
        await self._cancel_renewal_task()
        await self._cancel_restart_task()
        if self._proxy:
            await self._proxy.stop()
            self._proxy = None
        if self._ssdp:
            await self._ssdp.stop()
            self._ssdp = None
        if self._ssdp_proxy:
            await self._ssdp_proxy.stop()
            self._ssdp_proxy = None
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        """Cancel all running tasks and wait for cleanup."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=1.0)
            except TimeoutError:
                pass
        self._tasks = []

    def get_status(self) -> dict:
        """Get status for this instance."""
        status: dict = {
            "running": self.is_running,
            "pending_files": len(self._pending_files),
            "tailscale_disabled": self.tailscale_disabled,
        }
        if self.tailscale_fqdn:
            status["tailscale_fqdn"] = self.tailscale_fqdn
        if self.is_proxy and self._proxy:
            status["proxy"] = self._proxy.get_status()
        return status


class VirtualPrinterManager:
    """Multi-instance virtual printer registry and orchestrator.

    Every VP runs its own independent services on a dedicated bind IP.
    """

    def __init__(self):
        self._session_factory: Callable | None = None
        self._instances: dict[int, VirtualPrinterInstance] = {}

        # Directories
        self._base_dir = app_settings.base_dir / "virtual_printer"

        # Ensure base directories exist
        self._ensure_base_directories()

    def _ensure_base_directories(self) -> None:
        """Create base directories at startup."""
        for dir_path in [self._base_dir, self._base_dir / "uploads", self._base_dir / "certs"]:
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                logger.error(
                    f"Cannot create directory {dir_path}: Permission denied. "
                    f"For Docker: ensure the data volume is writable by the container user. "
                    f"For bare metal: run 'sudo chown -R $(whoami) {self._base_dir}'"
                )

    def set_session_factory(self, session_factory: Callable) -> None:
        """Set the database session factory."""
        self._session_factory = session_factory

    @property
    def is_enabled(self) -> bool:
        """Check if any virtual printer is running."""
        return len(self._instances) > 0

    async def sync_from_db(self) -> None:
        """Load all VPs from DB, reconcile running state."""
        if not self._session_factory:
            logger.warning("Cannot sync virtual printers: no session factory")
            return

        from sqlalchemy import select

        from backend.app.models.printer import Printer
        from backend.app.models.virtual_printer import VirtualPrinter

        async with self._session_factory() as db:
            result = await db.execute(
                select(VirtualPrinter).where(VirtualPrinter.enabled == True).order_by(VirtualPrinter.position)  # noqa: E712
            )
            enabled_vps = result.scalars().all()

        # Stop instances that are no longer enabled or changed mode
        enabled_ids = {vp.id for vp in enabled_vps}
        for vp_id in list(self._instances.keys()):
            if vp_id not in enabled_ids:
                await self.remove_instance(vp_id)

        # Look up printer IPs for proxy VPs
        proxy_vps = [vp for vp in enabled_vps if vp.mode == "proxy"]
        proxy_ips: dict[int, tuple[str, str]] = {}
        if proxy_vps:
            async with self._session_factory() as db:
                for pvp in proxy_vps:
                    if pvp.target_printer_id:
                        result = await db.execute(select(Printer).where(Printer.id == pvp.target_printer_id))
                        printer = result.scalar_one_or_none()
                        if printer:
                            proxy_ips[pvp.id] = (printer.ip_address, printer.serial_number)

        # Detect config changes on running instances and restart if needed
        for vp in enabled_vps:
            instance = self._instances.get(vp.id)
            if not instance:
                continue

            changed = (
                instance.mode != vp.mode
                or instance.model != (vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL)
                or instance.access_code != (vp.access_code or "")
                or instance.bind_ip != (vp.bind_ip or "")
                or instance.remote_interface_ip != (vp.remote_interface_ip or "")
                or instance.target_printer_id != vp.target_printer_id
                or instance.target_folder_id != vp.target_folder_id
                or instance.auto_dispatch != vp.auto_dispatch
                or instance.tailscale_disabled != vp.tailscale_disabled
            )

            if changed:
                logger.info(
                    "VP %s config changed (mode: %s→%s), restarting",
                    instance.name,
                    instance.mode,
                    vp.mode,
                )
                await self.remove_instance(vp.id)

        # Start instances for all enabled VPs (skip already running)
        for vp in enabled_vps:
            if vp.id in self._instances:
                continue

            if vp.mode == "proxy":
                ip_info = proxy_ips.get(vp.id)
                if not ip_info:
                    logger.warning("Proxy VP %s: target printer not found, skipping", vp.name)
                    continue
                target_ip, target_serial = ip_info
                instance = VirtualPrinterInstance(
                    vp_id=vp.id,
                    name=vp.name,
                    mode=vp.mode,
                    model=vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                    access_code=vp.access_code or "",
                    serial_suffix=vp.serial_suffix,
                    target_printer_ip=target_ip,
                    target_printer_serial=target_serial,
                    auto_dispatch=vp.auto_dispatch,
                    bind_ip=vp.bind_ip or "",
                    remote_interface_ip=vp.remote_interface_ip or "",
                    tailscale_disabled=vp.tailscale_disabled,
                    base_dir=self._base_dir,
                    session_factory=self._session_factory,
                )
                self._instances[vp.id] = instance
                await instance.start_proxy()
                logger.info("Started proxy VP: %s → %s (bind=%s)", instance.name, target_ip, instance.bind_ip)
            else:
                instance = VirtualPrinterInstance(
                    vp_id=vp.id,
                    name=vp.name,
                    mode=vp.mode,
                    model=vp.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                    access_code=vp.access_code or "",
                    serial_suffix=vp.serial_suffix,
                    target_printer_id=vp.target_printer_id,
                    target_folder_id=vp.target_folder_id,
                    auto_dispatch=vp.auto_dispatch,
                    bind_ip=vp.bind_ip or "",
                    remote_interface_ip=vp.remote_interface_ip or "",
                    tailscale_disabled=vp.tailscale_disabled,
                    base_dir=self._base_dir,
                    session_factory=self._session_factory,
                )
                self._instances[vp.id] = instance
                await instance.start_server()
                logger.info("Started server-mode VP: %s on %s", instance.name, vp.bind_ip)

    async def remove_instance(self, vp_id: int) -> None:
        """Stop and remove a single VP instance."""
        instance = self._instances.pop(vp_id, None)
        if instance:
            if instance.is_proxy:
                await instance.stop_proxy()
            else:
                await instance.stop_server()
            logger.info("Removed VP instance: %s", instance.name)

    async def stop_all(self) -> None:
        """Shutdown all virtual printer services."""
        logger.info("Stopping all virtual printer services...")

        for vp_id in list(self._instances.keys()):
            await self.remove_instance(vp_id)

        logger.info("All virtual printer services stopped")

    def get_instance(self, vp_id: int) -> VirtualPrinterInstance | None:
        """Get a running instance by ID."""
        return self._instances.get(vp_id)

    def get_all_status(self) -> list[dict]:
        """Get status for all running instances."""
        return [
            {
                "id": inst.id,
                "name": inst.name,
                "mode": inst.mode,
                **inst.get_status(),
            }
            for inst in self._instances.values()
        ]

    # -- Legacy single-printer compat --

    def get_status(self) -> dict:
        """Get status for first virtual printer (backward compat)."""
        if self._instances:
            first = next(iter(self._instances.values()))
            return {
                "enabled": True,
                "running": first.is_running,
                "mode": first.mode,
                "name": first.name,
                "serial": first.serial,
                "model": first.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                "model_name": VIRTUAL_PRINTER_MODELS.get(
                    first.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                    first.model or DEFAULT_VIRTUAL_PRINTER_MODEL,
                ),
                "pending_files": first.get_status().get("pending_files", 0),
                **({"target_printer_ip": first.target_printer_ip} if first.is_proxy else {}),
                **({"proxy": first.get_status().get("proxy", {})} if first.is_proxy else {}),
            }
        return {
            "enabled": False,
            "running": False,
            "mode": "file_manager",
            "name": "Bambuddy",
            "serial": "",
            "model": DEFAULT_VIRTUAL_PRINTER_MODEL,
            "model_name": VIRTUAL_PRINTER_MODELS[DEFAULT_VIRTUAL_PRINTER_MODEL],
            "pending_files": 0,
        }

    async def configure(
        self,
        enabled: bool,
        access_code: str = "",
        mode: str = "file_manager",
        model: str = "",
        target_printer_ip: str = "",
        target_printer_serial: str = "",
        remote_interface_ip: str = "",
    ) -> None:
        """Legacy single-printer configure. Delegates to sync_from_db()."""
        # This method is kept for backward compat with the settings endpoint.
        # The actual work is done by sync_from_db() which reads from the DB.
        await self.sync_from_db()


# Global instance
virtual_printer_manager = VirtualPrinterManager()
