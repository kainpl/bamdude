"""One failure-reason vocabulary: keys, never labels or sentences (upstream 5211fd45, #2974).

Three writers put three spellings of one cause into ``failure_reason`` — English
labels from the backend, the translated label older editors saved, and prose from
the stale paths — and the Failure Analysis widget groups on the raw value, so one
cause occupied several buckets and the label form could never be translated. The
writers now produce keys, and m186 folds the history onto them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.utils.failure_reasons import FAILURE_REASON_KEYS

REPO = Path(__file__).resolve().parents[3]


def test_the_backend_vocabulary_is_the_editors():
    source = (REPO / "frontend/src/components/EditArchiveModal.tsx").read_text(encoding="utf-8")
    block = source[source.index("const FAILURE_REASON_KEYS = [") : source.index("] as const;")]
    assert set(re.findall(r"'([A-Za-z]+)'", block)) == set(FAILURE_REASON_KEYS)


def test_every_key_has_a_label_in_both_locales():
    for locale in ("en", "uk"):
        source = (REPO / f"frontend/src/i18n/locales/{locale}.ts").read_text(encoding="utf-8")
        block = source[source.index("    failureReasons: {") :]
        block = block[: block.index("}")]
        assert set(re.findall(r"^\s*(\w+):", block, re.M)) >= set(FAILURE_REASON_KEYS), locale


def test_the_derived_reasons_are_keys():
    from backend.app.main import _HMS_FAILURE_REASONS, derive_failure_reason

    assert set(_HMS_FAILURE_REASONS.values()) <= set(FAILURE_REASON_KEYS)
    assert derive_failure_reason("cancelled", []) in FAILURE_REASON_KEYS


@pytest.mark.asyncio
async def test_m186_folds_the_history_onto_the_keys(db_session, printer_factory, test_engine):
    from backend.app.migrations import m186_failure_reason_vocabulary as m186

    printer = await printer_factory()
    values = [
        "User cancelled",  # the backend's English label
        "Скасовано користувачем",  # our Ukrainian label, saved by an older editor
        "Annulé par l'utilisateur",  # a Bambuddy database imported as is
        "Stale - reconciled after reconnect, end time unknown",
        "Outcome uncertain after reconnect; inspect the printer and plate before resuming",
        "Stopped by user (printer was offline)",
        "layerShift",  # already a key
        "the belt snapped",  # free text: left alone, never guessed
    ]
    for value in values:
        db_session.add(
            PrintArchive(
                printer_id=printer.id,
                filename="x.3mf",
                file_path="",
                file_size=0,
                status="failed",
                failure_reason=value,
            )
        )
    await db_session.commit()

    await m186.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))

    rows = (await db_session.execute(text("SELECT failure_reason FROM print_archives ORDER BY id"))).scalars().all()
    assert rows == [
        "userCancelled",
        "userCancelled",
        "userCancelled",
        "noStatusUpdate",
        "noStatusUpdate",
        "userCancelled",
        "layerShift",
        "the belt snapped",
    ]


def test_the_frozen_map_never_maps_one_label_to_two_keys_and_only_onto_keys():
    from backend.app.migrations import m186_failure_reason_vocabulary as m186

    assert set(m186.LEGACY_FAILURE_REASON_LABELS.values()) <= set(FAILURE_REASON_KEYS)


def test_the_backend_labels_cover_every_key_in_both_languages():
    """Notifications name a reason in words; a key without a label would go out raw."""
    import json

    for lang in ("en", "uk"):
        labels = json.loads((REPO / f"backend/app/data/failure_reasons_{lang}.json").read_text(encoding="utf-8"))
        assert set(labels) == set(FAILURE_REASON_KEYS), lang
