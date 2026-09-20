"""Parse per-slot filament requirements out of a 3MF file.

The auto-queue intake used to pull only `required_filament_types` (a
de-duplicated list) out of a 3MF and drop the per-slot ``color`` info on the
floor. That meant the eligibility check had no way to express "slot 1 needs
red PLA, slot 2 needs green PLA" — the scheduler matched on canonical type
only and dispatched onto whichever printer happened to be free, even when
its loaded slots had the right material in the wrong colours.

This helper is the shared per-slot extractor: it returns one dict per slot
that actually consumed filament on the chosen plate. Callers wire the list
into ``filament_overrides`` (with ``force_color_match=True`` when they want
the scheduler to refuse colour mismatches) so the existing
:func:`backend.app.services.auto_queue_eligibility._get_missing_force_color_slots`
path can do exact type+colour matching against printer AMS state.

Returned shape mirrors the override JSON the eligibility helper validates
against:

    [{"slot_id": int, "type": str, "color": str, "tray_info_idx": str,
      "used_grams": float, "nozzle_id": int | None}, ...]

— minus the ``force_color_match`` flag, which the caller adds based on
its own setting (the per-VP ``queue_force_color_match`` toggle, in our
case).

The legacy ``extract_filament_requirements`` list wrapper remains forgiving
for preview/upload consumers. It cannot prove a job safe to dispatch.
``read_print_requirements`` is the strict print-time contract: an exact
printable plate, complete used-channel evidence, or a typed unavailable
result. Queue/dispatch callers must explicitly adopt that contract.
"""

from __future__ import annotations

import json
import logging
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, TypedDict
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from backend.app.services.archive import ThreeMFParser
from backend.app.services.source_io import SOURCE_FAILURES, SourceUnavailable, source_probe
from backend.app.utils.printer_models import is_dual_nozzle_model, normalize_model_name
from backend.app.utils.threemf_tools import extract_nozzle_mapping_from_3mf

logger = logging.getLogger(__name__)


#: The two shapes a *stored* revision can have, and the reason there are two.
#: ``STAT`` describes a file somebody else may edit — an original on a share, a
#: library file, an archive's 3MF — and the only cheap evidence about it is its
#: stat. ``HASH`` describes a captured snapshot (m173), whose bytes are frozen
#: and whose mtime is merely the moment BamDude wrote them.
#:
#: They are deliberately disjoint key sets: a reader can tell which question a
#: stored revision answers without being told, and can therefore refuse to
#: compare one against the other (see :func:`revision_refutes`).
STAT_REVISION_KEYS = frozenset({"size", "mtime_ns"})
HASH_REVISION_KEYS = frozenset({"sha256", "size_bytes"})


@dataclass(frozen=True, eq=False)
class SourceIdentity:
    """What "the same source" means for one file — and it is not always the mtime.

    ``sha256`` is set only for a **captured snapshot** (spec §4/§7): the object
    under ``queue-sources`` is immutable and content-addressed, so its hash is its
    identity and its ``mtime_ns`` is the time BamDude copied the bytes. That
    timestamp changes on a portable restore, on a file-level restore and on any
    tooling that rewrites the spool, while every byte stays the same — so an
    identity that read it would report "the evidence changed" about a file nobody
    touched, and defer every assigned-but-undispatched job after a restore.

    The hash is not re-computed here and is not meant to be: it is the identity
    the capture established and the spool's own sweep re-verifies (§9). Asking
    for it on the dispatch path would mean hashing a 3MF on every queue tick.

    Equality and hashing therefore run on :attr:`anchor`, which drops the mtime
    exactly when a hash is present — never otherwise. :meth:`same_stat_as` is the
    full-stat comparison for the one question where the mtime IS the evidence: a
    file mutating *during* a single read.
    """

    path: str
    size: int
    mtime_ns: int
    sha256: str | None = None

    @classmethod
    def of(cls, path: Path, *, sha256: str | None = None) -> SourceIdentity:
        stat = path.stat()
        return cls(str(path.resolve()), stat.st_size, stat.st_mtime_ns, sha256)

    @property
    def anchor(self) -> tuple:
        if self.sha256 is not None:
            return (self.path, self.size, self.sha256)
        return (self.path, self.size, self.mtime_ns)

    def __eq__(self, other) -> bool:
        if not isinstance(other, SourceIdentity):
            return NotImplemented
        return self.anchor == other.anchor

    def __hash__(self) -> int:
        return hash(self.anchor)

    def same_stat_as(self, other: SourceIdentity) -> bool:
        """Every stat field — the check *inside* one read, where an mtime is evidence.

        A snapshot cannot legitimately change under a parse either, so this stays
        strict for both kinds of source: nothing about the single-read mutation
        guard was relaxed when the cross-time identity moved onto the hash.
        """
        return (self.path, self.size, self.mtime_ns) == (other.path, other.size, other.mtime_ns)

    def revision(self) -> dict:
        """The revision as it is PERSISTED into a routing intent (spec §7).

        Path-free on purpose: ``settings.base_dir`` differs between installs and
        the spool tree is re-created by a restore, so a stored path would rot in
        exactly the cases the hash exists to survive.
        """
        if self.sha256 is not None:
            return {"sha256": self.sha256, "size_bytes": self.size}
        return {"size": self.size, "mtime_ns": self.mtime_ns}


async def probe_identity(identity: SourceIdentity) -> SourceIdentity:
    """Re-read the file an identity names, asking the SAME question it answered.

    The one spelling of the re-probe, shared by ``filament_preflight.final_guard``
    and the auto scheduler's two claim-time checks. Carrying ``sha256`` is what
    makes it the same question: a captured source's identity is hash-anchored, and
    a bare stat of the same path produces a stat-anchored identity that can never
    equal it — so a caller that dropped the label would refuse every
    snapshot-backed dispatch with "the evidence changed".
    """
    return await source_probe(
        ("identity", identity.path, identity.sha256),
        SourceIdentity.of,
        Path(identity.path),
        sha256=identity.sha256,
    )


def revision_refutes(stored, identity: SourceIdentity | None) -> bool:
    """Does a stored revision contradict the source we can read right now?

    Four answers, and the two shape-mismatch ones are deliberately NOT symmetric:

    * **shapes agree** → compare them. A legacy row whose original was edited is
      still refused (``source_changed``), exactly as before m173; a captured row
      whose blob holds other bytes is refused for the first time.
    * **a stat-shaped stamp against a hash-anchored read** → say nothing. That is
      an intent written before v2, which stamped the COPY's ``(size, mtime_ns)``;
      reading it against a captured source is the restore trap the Task 6
      reader-ignore closed, and it must stay closed.
    * **a hash-shaped stamp against a stat-anchored read** → **refuse.** This one
      can never be legitimate: the intent was written about a captured object and is
      being checked against a file that is not that object. It is reachable exactly
      where ``filament_intake.item_descriptor`` documents it — a ``queue_source_id``
      whose row has gone reads as legacy, i.e. as its own original — and without
      this such a row would dispatch a possibly re-sliced original with no
      changed-file check at all.
    * **the stored shape is neither** → refuse. Evidence this version cannot read
      is not evidence that nothing changed.
    """
    if identity is None or not stored:
        return False
    if not isinstance(stored, dict) or set(stored) not in (STAT_REVISION_KEYS, HASH_REVISION_KEYS):
        return True
    current = identity.revision()
    if set(stored) == set(current):
        return stored != current
    return set(stored) == HASH_REVISION_KEYS


class UsedFilament(TypedDict):
    slot_id: int
    type: str
    color: str | None
    tray_info_idx: str | None
    used_grams: float
    nozzle_id: int | None
    # The family-owned material class resolved from ``tray_info_idx`` after
    # parsing.  It is absent when the 3MF does not carry a resolvable family.
    filament_type: NotRequired[str]


@dataclass(frozen=True)
class PrintRequirements:
    """Strict print evidence. Unavailable never exposes a partial slot list.

    ``nozzle_constraints`` contains the file's declared configuration, not a
    printer's live capability verdict or the H2C wire nozzle/rack mapping.
    Reason codes are internal; API adapters must translate them for operators.
    """

    status: Literal["ok", "unavailable"]
    reason: str | None = None
    source_identity: SourceIdentity | None = None
    resolved_plate_id: int | None = None
    gcode_member: str | None = None
    model: str | None = None
    print_time_seconds: int | None = None
    nozzle_constraints: dict = field(default_factory=dict)
    used_filaments: tuple[UsedFilament, ...] = ()


class _Unavailable(ValueError):
    """Expected input failure, not an empty successful requirements list."""


def _resolve_print_plate(
    zf: zipfile.ZipFile, root: Element, plate_id: int | None, archive_plate_id: int | None
) -> tuple[int, Element, str]:
    if plate_id is not None and (type(plate_id) is not int or plate_id < 0):
        raise _Unavailable("invalid_plate_id")
    requested = plate_id or (archive_plate_id if plate_id is None else None)
    if requested is not None and (type(requested) is not int or requested <= 0):
        raise _Unavailable("invalid_plate_id")
    plates: dict[int, Element] = {}
    for plate in root.findall(".//plate"):
        indices = [m.get("value") for m in plate.findall("metadata") if m.get("key") == "index"]
        if len(indices) != 1:
            raise _Unavailable("invalid_plate_metadata")
        try:
            index = int(indices[0])
        except (TypeError, ValueError) as exc:
            raise _Unavailable("invalid_plate_metadata") from exc
        if index <= 0 or index in plates:
            raise _Unavailable("invalid_plate_metadata")
        plates[index] = plate
    # Match the full basename, not an arbitrary suffix or the first ZIP entry.
    members: dict[int, str] = {}
    for name in zf.namelist():
        match = re.fullmatch(r"(?:.*/)?plate_([1-9][0-9]*)\.gcode", name, re.IGNORECASE)
        if match:
            index = int(match.group(1))
            if index in members:
                raise _Unavailable("ambiguous_plate_gcode")
            members[index] = name
    if requested is None:
        printable = plates.keys() & members.keys()
        if len(printable) > 1:
            raise _Unavailable("plate_selection_required")
        if not printable:
            raise _Unavailable("plate_gcode_missing" if plates else "plate_not_found")
        requested = next(iter(printable))
    if requested not in plates:
        raise _Unavailable("plate_not_found")
    if requested not in members:
        raise _Unavailable("plate_gcode_missing")
    return requested, plates[requested], members[requested]


def _used_filaments(plate: Element, nozzle_mapping: dict[int, int]) -> tuple[UsedFilament, ...]:
    slots: list[UsedFilament] = []
    seen: set[int] = set()
    for filament in plate.findall("filament"):
        try:
            slot_id = int(filament.get("id"))
            usage = float(filament.get("used_g"))
        except (TypeError, ValueError) as exc:
            raise _Unavailable("filament_usage_unavailable") from exc
        if slot_id <= 0 or slot_id in seen or not math.isfinite(usage) or usage < 0:
            raise _Unavailable("filament_usage_unavailable")
        seen.add(slot_id)
        if usage == 0:
            continue
        material = (filament.get("type") or "").strip()
        if not material:
            raise _Unavailable("filament_type_unavailable")
        slots.append(
            UsedFilament(
                slot_id=slot_id,
                type=material,
                color=filament.get("color") or None,
                tray_info_idx=filament.get("tray_info_idx") or None,
                used_grams=usage,
                nozzle_id=nozzle_mapping.get(slot_id),
            )
        )
    if not slots:
        raise _Unavailable("filament_usage_unavailable")
    return tuple(sorted(slots, key=lambda s: s["slot_id"]))


def read_print_requirements(
    file_path: Path | str,
    plate_id: int | None = None,
    *,
    archive_plate_id: int | None = None,
    sha256: str | None = None,
) -> PrintRequirements:
    """Resolve an exact printable plate and its complete used-channel evidence.

    Run with ``asyncio.to_thread`` on async paths. This does blocking ZIP I/O;
    it neither writes files nor caches across requests. Unlike the legacy
    preview wrapper below, a missing G-code, unknown usage or ambiguous plate
    is a typed refusal. A positive explicit selection always outranks a hint.

    ``sha256`` labels the identity this read reports as a **captured snapshot's**
    (m173): the caller knows the file is that content-addressed object, and the
    identity it hands back is then portable across a restore. It is a label, not
    a verification — see :class:`SourceIdentity`.
    """
    path = Path(file_path)
    identity = None
    try:
        identity = SourceIdentity.of(path, sha256=sha256)
        with zipfile.ZipFile(path) as zf:
            for member in ("Metadata/slice_info.config", "Metadata/project_settings.config"):
                if zf.namelist().count(member) > 1:
                    raise _Unavailable("invalid_plate_metadata")
            if "Metadata/slice_info.config" not in zf.namelist():
                raise _Unavailable("slice_info_missing")
            root = ET.fromstring(zf.read("Metadata/slice_info.config"))
            resolved, plate, gcode = _resolve_print_plate(zf, root, plate_id, archive_plate_id)
            project = {}
            if "Metadata/project_settings.config" in zf.namelist():
                project = json.loads(zf.read("Metadata/project_settings.config"))
                if not isinstance(project, dict):
                    raise _Unavailable("invalid_project_settings")
            metadata = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
            raw_model = metadata.get("printer_model_id") or project.get("printer_model")
            if not raw_model:
                with zf.open(gcode) as member:
                    header = member.read(4096).decode("utf-8", errors="replace")
                match = re.search(r";\s*printer_model\s*=\s*(.+)", header, re.IGNORECASE)
                raw_model = match.group(1).strip() if match else None
            if raw_model is not None and not isinstance(raw_model, str):
                raise _Unavailable("invalid_printer_model")
            model = normalize_model_name(raw_model)
            nozzle_mapping = extract_nozzle_mapping_from_3mf(zf, resolved) or {}
            slots = _used_filaments(plate, nozzle_mapping)
            physical_map = project.get("physical_extruder_map") or []
            if not isinstance(physical_map, list):
                raise _Unavailable("nozzle_mapping_unavailable")
            if is_dual_nozzle_model(model) or len(physical_map) > 1:
                if any(s["nozzle_id"] not in (0, 1) for s in slots):
                    raise _Unavailable("nozzle_mapping_unavailable")
            constraints = {
                key: project[key]
                for key in ("nozzle_diameter", "nozzle_type", "physical_extruder_map", "extruder_nozzle_stats")
                if key in project
            }
        # ``same_stat_as``, not ``!=``: this is the one comparison where the mtime
        # is real evidence, because the question is whether the file moved under
        # the parse that just finished — a window no restore can fall inside.
        if not SourceIdentity.of(path, sha256=sha256).same_stat_as(identity):
            raise _Unavailable("source_changed")
        return PrintRequirements(
            status="ok",
            source_identity=identity,
            resolved_plate_id=resolved,
            gcode_member=gcode,
            model=model,
            print_time_seconds=(int(metadata["prediction"]) if str(metadata.get("prediction", "")).isdigit() else None),
            nozzle_constraints=constraints,
            used_filaments=slots,
        )
    except _Unavailable as exc:
        return PrintRequirements(status="unavailable", reason=str(exc), source_identity=identity)
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, ET.ParseError, DefusedXmlException):
        logger.warning("Cannot read print requirements from %s", path, exc_info=True)
        return PrintRequirements(status="unavailable", reason="source_unreadable", source_identity=identity)


class PrintRequirementsCache:
    """One request/tick's revision-aware reads, with ZIP work off the event loop."""

    def __init__(self):
        self._results: dict[tuple[SourceIdentity, int | None, int | None], PrintRequirements] = {}
        self._unavailable: dict[str, PrintRequirements] = {}

    async def read(
        self,
        file_path: Path | str | None,
        plate_id: int | None = None,
        *,
        archive_plate_id: int | None = None,
        sha256: str | None = None,
    ) -> PrintRequirements:
        if plate_id is not None and (type(plate_id) is not int or plate_id < 0):
            return PrintRequirements(status="unavailable", reason="invalid_plate_id")
        if (
            plate_id is None
            and archive_plate_id is not None
            and (type(archive_plate_id) is not int or archive_plate_id <= 0)
        ):
            return PrintRequirements(status="unavailable", reason="invalid_plate_id")
        if file_path is None:
            return PrintRequirements(status="unavailable", reason="source_unreadable")
        path = Path(file_path)
        if str(path) in self._unavailable:
            return self._unavailable[str(path)]
        try:
            # ``sha256`` is part of the coalescing key: the same path read once as
            # an original and once as a captured object answers two different
            # questions, and a shared probe would hand one the other's label.
            identity = await source_probe(("identity", str(path), sha256), SourceIdentity.of, path, sha256=sha256)
        except SourceUnavailable as exc:
            result = PrintRequirements(status="unavailable", reason=exc.reason)
            if exc.reason in SOURCE_FAILURES:
                self._unavailable[str(path)] = result
            return result
        key = (identity, plate_id or None, archive_plate_id if plate_id is None else None)
        if key not in self._results:
            try:
                result = await source_probe(
                    ("requirements", str(path), plate_id, archive_plate_id, sha256),
                    read_print_requirements,
                    path,
                    plate_id,
                    archive_plate_id=archive_plate_id,
                    sha256=sha256,
                )
            except SourceUnavailable as exc:
                result = PrintRequirements(status="unavailable", reason=exc.reason)
            if result.reason in SOURCE_FAILURES:
                self._unavailable[str(path)] = result
                return result
            if result.reason == "source_check_busy":
                return result
            if result.source_identity != identity:
                return PrintRequirements(status="unavailable", reason="source_changed", source_identity=identity)
            self._results[key] = result
            if result.status == "ok":
                self._results[(identity, result.resolved_plate_id, None)] = result
        return self._results[key]


def extract_filament_requirements(file_path: Path | str, plate_id: int | None = None) -> list[dict]:
    """Return ``[{slot_id, type, color, tray_info_idx, used_grams, [nozzle_id]}]`` from a 3MF.

    Args:
        file_path: Path to the 3MF on disk.
        plate_id: 1-indexed plate to extract for. ``None`` falls through to
            ThreeMFParser's default plate (plate 1 for single-plate exports).

    Returns:
        List of per-slot filament dicts, sorted by ``slot_id``. Empty list
        when the 3MF is unreadable, has no slice_info, or no filaments
        consumed any material on the chosen plate.
    """
    path = Path(file_path)
    if not path.exists():
        return []

    try:
        parser = ThreeMFParser(path, plate_number=plate_id)
        md = parser.parse()
    except Exception as e:  # noqa: BLE001 — defensive: never raise from intake path
        logger.warning("Failed to parse filament requirements from %s: %s", path, e)
        return []

    raw_slots = md.get("filament_slots") or []
    out: list[dict] = []
    for slot in raw_slots:
        slot_id = slot.get("slot_id")
        ftype = slot.get("type")
        used_g = slot.get("used_g_raw", slot.get("used_g")) or 0
        if slot_id is None or not ftype:
            continue
        try:
            used_grams = float(used_g)
        except (TypeError, ValueError):
            continue
        if used_grams <= 0:
            # Slot present in the slicer config but not consumed by this plate
            # — irrelevant to routing, skip so the override list doesn't carry
            # phantom requirements.
            continue
        entry: dict = {
            "slot_id": int(slot_id),
            "type": ftype,
            "color": slot.get("color") or "",
            # Empty for third-party spools and for 3MFs sliced before the field
            # existed; callers must treat "" as "no variant constraint" (#2650).
            "tray_info_idx": slot.get("tray_info_idx") or "",
            "used_grams": used_grams,
        }
        # Preserve the plate-scoped physical binding from threemf_tools;
        # this is not the H2C's protocol-specific rack nozzle mapping.
        nozzle_id = slot.get("nozzle_id")
        if nozzle_id is not None:
            try:
                entry["nozzle_id"] = int(nozzle_id)
            except (TypeError, ValueError):
                pass
        out.append(entry)

    out.sort(key=lambda x: x["slot_id"])
    return out


def overrides_for_plate(
    overrides: list[dict],
    file_path: Path | str | None,
    plate_id: int | None,
) -> list[dict]:
    """Drop the filament overrides whose slots this plate never prints (#2551).

    Queueing several plates of one 3MF builds a single override list out of every
    selected plate's filaments and hands that same list to each plate's item. A
    ``force_color_match`` entry blocks dispatch until the printer has that exact
    colour loaded, so a single-colour plate ended up waiting on every colour in
    the batch. Each item may only demand what its own plate consumes.

    Overrides are kept as-is when the plate's slots cannot be established (whole
    file selected, source gone, unreadable 3MF, malformed entry): an item that
    waits on a colour it does not need is visible and fixable, whereas one that
    silently loses a forced colour can dispatch the print in the wrong filament.
    """
    if not overrides or plate_id is None or file_path is None:
        return overrides
    path = Path(file_path)
    if not path.exists():
        return overrides

    plate_slots = {f["slot_id"] for f in extract_filament_requirements(path, plate_id)}
    if not plate_slots:
        logger.warning(
            "Cannot read the filaments of plate %s in %s; keeping all %d filament override(s)",
            plate_id,
            path.name,
            len(overrides),
        )
        return overrides

    narrowed = []
    for override in overrides:
        try:
            slot_id = int(override["slot_id"])
        except (KeyError, TypeError, ValueError):
            narrowed.append(override)
            continue
        if slot_id in plate_slots:
            narrowed.append(override)

    if len(narrowed) != len(overrides):
        logger.info(
            "Plate %s: kept %d of %d filament override(s) — the rest belong to other plates",
            plate_id,
            len(narrowed),
            len(overrides),
        )
    return narrowed
