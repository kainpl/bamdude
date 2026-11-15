"""Copy the tags BamDude keeps in ``extra.tag`` into Spoolman's native tags.

Spoolman 0.27 links tags to spools itself; BamDude writes both since then,
but the spools linked before carry ``extra.tag`` alone. This is the one-off
copy for them (upstream #3168, a8965b0e9 / 842dd9109), with our rules:

* ``extra.tag`` is never touched — an older reader and a downgrade lose nothing;
* a slot's fallback id is not a physical tag and stays behind;
* the AMS's padded chip uid is stored as the chip's own 4 bytes, as the AMS
  sync stores it, so it is what a reader sees;
* archived spools are not copied — and a tag an archived spool already holds
  natively is NOT taken from it: an archived reel is a known reel;
* every conflict is REPORTED and nothing is decided for the operator: two
  active spools sharing one ``extra.tag`` both stay as they are, and a uid
  Spoolman holds elsewhere stays there.

The plan is computed from one listing, so the preview and the run report the
same thing; the run then links what the plan says, and a link Spoolman
refuses after all (something changed in between) joins the conflicts.
"""

from __future__ import annotations

import logging

from backend.app.services.spoolman import (
    AMS_CHIP_UID_PADDING,
    BAMBU_RFID_TAG_LENGTH,
    NativeTagConflict,
    SpoolmanClient,
    SpoolmanClientError,
    SpoolmanNotFoundError,
    SpoolmanUnavailableError,
    extra_tag,
    is_linkable_tag_uid,
    native_tag_uids,
)
from backend.app.services.spoolman_tracking import get_fallback_spool_tag_for_slot

logger = logging.getLogger(__name__)

# Why a tag was not copied. The frontend has a sentence for each.
SHARED = "shared_extra_tag"  # another active spool has the same extra.tag
HELD_BY_SPOOL = "held_by_spool"  # another spool holds the uid natively
HELD_BY_FILAMENT = "held_by_filament"  # a filament holds it
HELD_ELSEWHERE = "held_elsewhere"  # Spoolman refused without naming a holder

# Why a link the plan called for did not happen. A failure is one spool's: the
# run records it and goes on, and always answers with its report.
SPOOL_GONE = "spool_gone"  # the spool was deleted since the listing
REFUSED = "refused"  # Spoolman would not take this uid
UNREACHABLE = "unreachable"  # Spoolman stopped answering; nothing after it was tried


def is_slot_fallback_tag(tag: str, printer_serials) -> bool:
    """Whether a 16-character tag is an AMS slot's generated id rather than a physical tag.

    A fallback id starts with the hash of the printer's serial (see
    ``get_fallback_spool_tag_for_slot``); it names a slot, not a spool.
    """
    tag = (tag or "").strip('"').upper()
    if len(tag) != 16:
        return False
    return any(tag[:8] == get_fallback_spool_tag_for_slot(serial, 0, 0)[:8] for serial in printer_serials if serial)


def native_uid_for(tag: str) -> tuple[str, str | None]:
    """The native uid and format an ``extra.tag`` value becomes."""
    if len(tag) == 16 and tag.endswith(AMS_CHIP_UID_PADDING):
        return tag[:8], "bambu"
    return tag, "bambu" if len(tag) == BAMBU_RFID_TAG_LENGTH else None


def _conflict(spool_id: int, tag: str, reason: str, *, holder=None, archived=False, filament=None) -> dict:
    return {
        "spool_id": spool_id,
        "tag": tag,
        "reason": reason,
        "holder": holder,
        "holder_archived": archived,
        "holder_filament": filament,
    }


async def copy_extra_tags_to_native(client: SpoolmanClient, printer_serials, *, dry_run: bool) -> dict:
    """Preview (``dry_run``) or perform the copy; see the module docstring for the rules."""
    spools = [s for s in await client.get_all_spools(allow_archived=True) if isinstance(s.get("id"), int)]
    filaments = await client.get_filaments()

    archived = {s["id"] for s in spools if s.get("archived")}
    spool_holder = {uid: s["id"] for s in spools for uid in native_tag_uids(s)}
    filament_holder = {
        uid: f["id"] for f in filaments if isinstance(f, dict) and "id" in f for uid in native_tag_uids(f)
    }

    report: dict = {
        "dry_run": dry_run,
        "moved": [],
        "already": 0,
        "slot_ids": 0,
        "archived": 0,
        "conflicts": [],
        "failed": [],
    }

    # What each active spool would link, and who else wants the same uid.
    wanted: list[tuple[int, str, str | None]] = []
    claimants: dict[str, list[int]] = {}
    for spool in sorted(spools, key=lambda s: s["id"]):
        tag = extra_tag(spool)
        if not is_linkable_tag_uid(tag) or len(tag) < 8:
            continue
        if is_slot_fallback_tag(tag, printer_serials):
            report["slot_ids"] += 1
            continue
        if spool["id"] in archived:
            report["archived"] += 1
            continue
        uid, tag_format = native_uid_for(tag)
        wanted.append((spool["id"], uid, tag_format))
        claimants.setdefault(uid, []).append(spool["id"])

    unreachable = False
    for spool_id, uid, tag_format in wanted:
        holder = spool_holder.get(uid)
        if holder == spool_id:
            report["already"] += 1
            continue
        if holder is not None:
            report["conflicts"].append(
                _conflict(spool_id, uid, HELD_BY_SPOOL, holder=holder, archived=holder in archived)
            )
            continue
        if uid in filament_holder:
            report["conflicts"].append(_conflict(spool_id, uid, HELD_BY_FILAMENT, filament=filament_holder[uid]))
            continue
        others = [other for other in claimants[uid] if other != spool_id]
        if others:
            report["conflicts"].append(_conflict(spool_id, uid, SHARED, holder=others[0]))
            continue
        if not dry_run:
            if unreachable:
                report["failed"].append({"spool_id": spool_id, "tag": uid, "reason": UNREACHABLE})
                continue
            try:
                await client.link_native_tag(spool_id, uid, tag_format)
            except SpoolmanNotFoundError:
                report["failed"].append({"spool_id": spool_id, "tag": uid, "reason": SPOOL_GONE})
                continue
            except SpoolmanClientError as refused:
                logger.warning("Spoolman refused native tag %s for spool %s: %s", uid, spool_id, refused)
                report["failed"].append({"spool_id": spool_id, "tag": uid, "reason": REFUSED})
                continue
            except SpoolmanUnavailableError as down:
                logger.warning("Native tag copy stopped at spool %s: %s", spool_id, down)
                unreachable = True
                report["failed"].append({"spool_id": spool_id, "tag": uid, "reason": UNREACHABLE})
                continue
            except NativeTagConflict as refused:
                if refused.spool_id is not None:
                    row = _conflict(spool_id, uid, HELD_BY_SPOOL, holder=refused.spool_id, archived=refused.archived)
                elif refused.filament_id is not None:
                    row = _conflict(spool_id, uid, HELD_BY_FILAMENT, filament=refused.filament_id)
                else:
                    row = _conflict(spool_id, uid, HELD_ELSEWHERE)
                report["conflicts"].append(row)
                continue
        report["moved"].append({"spool_id": spool_id, "tag": uid, "format": tag_format})

    logger.info(
        "Native tag copy (dry_run=%s): %d to link, %d already, %d slot ids, %d archived, %d conflicts, %d failed",
        dry_run,
        len(report["moved"]),
        report["already"],
        report["slot_ids"],
        report["archived"],
        len(report["conflicts"]),
        len(report["failed"]),
    )
    return report
