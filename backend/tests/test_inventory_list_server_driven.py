"""Server-driven spool list — the query layer + ``GET /inventory/spools``
(task 1, 2026-08-29).

Ports ``InventoryPage.tsx``'s deleted client-side filters/sort as the
behavioral spec — see "The predicate port table" in
``docs/superpowers/plans/2026-08-29-server-driven-lists-spools.md``. Every
test below maps to one row of that table (or one operator ruling).

Two layers are exercised:

- **Service-level** (``inventory_service.build_spool_filters`` /
  ``list_spools`` / ``count_spools`` called directly against ``db_session``)
  — the filter/sort matrix. Faster and pins the exact SQLAlchemy conditions
  without the HTTP/pydantic layer in the way (mirrors
  ``test_archive_sorting.py``'s style for ``ArchiveService.list_archives``).
- **Route-level** (``async_client`` HTTP calls) — the envelope shape, query
  param parsing (including the ``colors``/``color_rgbas`` list params), and
  the legacy-pin (bare call stays byte-for-byte the old flat full shape).
"""

from datetime import datetime, timezone

import pytest

from backend.app.models.location import Location
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.schemas.spool import SpoolResponse
from backend.app.services import inventory_service
from backend.app.services.location_service import location_name_key

pytestmark = pytest.mark.asyncio


# ── seeding helpers ──────────────────────────────────────────────────────────


async def _spool(db_session, **fields) -> Spool:
    defaults = {
        "material": "PLA",
        "brand": "Generic",
        "color_name": "Black",
        "rgba": "000000FF",
        "label_weight": 1000,
        "weight_used": 0,
    }
    defaults.update(fields)
    spool = Spool(**defaults)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


async def _location(db_session, name: str) -> Location:
    loc = Location(name=name, name_key=location_name_key(name))
    db_session.add(loc)
    await db_session.commit()
    await db_session.refresh(loc)
    return loc


async def _set_global_low_stock_threshold(db_session, value: float) -> None:
    db_session.add(Settings(key="low_stock_threshold", value=str(value)))
    await db_session.commit()


async def _filtered_ids(db_session, **kwargs) -> list[int]:
    """Apply build_spool_filters + list_spools (unsorted — legacy default
    ordering) and return the matching spool ids, in that order."""
    filters = await inventory_service.build_spool_filters(db_session, **kwargs)
    spools = await inventory_service.list_spools(db_session, filters=filters, limit=None)
    return [s.id for s in spools]


async def _count(db_session, **kwargs) -> int:
    filters = await inventory_service.build_spool_filters(db_session, **kwargs)
    return await inventory_service.count_spools(db_session, filters=filters)


# ── filters: one test per port-table row ────────────────────────────────────


class TestArchivedFilter:
    async def test_active_and_archived(self, db_session):
        active = await _spool(db_session, brand="Active")
        archived = await _spool(db_session, brand="Archived", archived_at=datetime.now(timezone.utc))

        assert set(await _filtered_ids(db_session, archived="active")) == {active.id}
        assert set(await _filtered_ids(db_session, archived="archived")) == {archived.id}


class TestUsageFilter:
    async def test_used_and_new(self, db_session):
        used = await _spool(db_session, brand="Used", weight_used=200)
        new = await _spool(db_session, brand="New", weight_used=0)

        assert set(await _filtered_ids(db_session, usage="used")) == {used.id}
        assert set(await _filtered_ids(db_session, usage="new")) == {new.id}

    async def test_lowstock_per_spool_override(self, db_session):
        """remaining = 15% for both; threshold 10% excludes it, threshold 20%
        includes it — the per-spool override, independent of the global
        setting (not set at all in this test)."""
        not_low = await _spool(
            db_session, brand="NotLow", label_weight=1000, weight_used=850, low_stock_threshold_pct=10
        )
        low = await _spool(db_session, brand="Low", label_weight=1000, weight_used=850, low_stock_threshold_pct=20)

        ids = set(await _filtered_ids(db_session, usage="lowstock"))
        assert low.id in ids
        assert not_low.id not in ids

    async def test_lowstock_global_threshold_fallback(self, db_session):
        """No per-spool override on either spool — falls back to the global
        ``low_stock_threshold`` setting (explicitly set to 30% here)."""
        await _set_global_low_stock_threshold(db_session, 30.0)
        below = await _spool(db_session, brand="BelowGlobal", label_weight=1000, weight_used=800)  # remaining 20%
        above = await _spool(db_session, brand="AboveGlobal", label_weight=1000, weight_used=100)  # remaining 90%

        ids = set(await _filtered_ids(db_session, usage="lowstock"))
        assert below.id in ids
        assert above.id not in ids

    async def test_lowstock_label_weight_zero_is_always_low(self, db_session):
        """Port table: pct is 0 when label_weight<=0 — always below any
        positive threshold, never skipped."""
        zero_label = await _spool(db_session, brand="ZeroLabel", label_weight=0, weight_used=0)
        assert zero_label.id in set(await _filtered_ids(db_session, usage="lowstock"))

    async def test_lowstock_boundary_is_strict_not_inclusive(self, db_session):
        """Review finding 7: remaining EXACTLY == threshold is NOT lowstock —
        both the client and ``_warn_if_low_stock`` (usage_tracker.py) use
        strict ``<``, the same boundary the notification re-arms on. A ``<=``
        regression would wrongly include this spool."""
        exactly_at_threshold = await _spool(
            db_session, brand="ExactlyAtThreshold", label_weight=1000, weight_used=800, low_stock_threshold_pct=20
        )  # remaining = (1000-800)/1000*100 = 20.0, threshold = 20 -> 20 < 20 is False
        assert exactly_at_threshold.id not in set(await _filtered_ids(db_session, usage="lowstock"))


class TestMaterialBrandFilters:
    async def test_material(self, db_session):
        pla = await _spool(db_session, material="PLA", brand="X")
        petg = await _spool(db_session, material="PETG", brand="X")
        assert set(await _filtered_ids(db_session, material="PLA")) == {pla.id}
        assert petg.id not in set(await _filtered_ids(db_session, material="PLA"))

    async def test_brand(self, db_session):
        a = await _spool(db_session, brand="BrandA")
        b = await _spool(db_session, brand="BrandB")
        ids = set(await _filtered_ids(db_session, brand="BrandA"))
        assert ids == {a.id}
        assert b.id not in ids


class TestColorsFilter:
    async def test_raw_pairs_including_null_name_rgba_row(self, db_session):
        named = await _spool(db_session, brand="Named", color_name="Jade White", rgba="FFFFFFFF")
        null_name_matching_rgba = await _spool(db_session, brand="NullNameMatch", color_name=None, rgba="ABCDEF12")
        null_name_other_rgba = await _spool(db_session, brand="NullNameOther", color_name=None, rgba="000000FF")
        other_named = await _spool(db_session, brand="OtherNamed", color_name="Black", rgba="000000FF")

        ids = set(await _filtered_ids(db_session, colors=["Jade White"], color_rgbas=["ABCDEF12"]))
        assert ids == {named.id, null_name_matching_rgba.id}
        assert null_name_other_rgba.id not in ids
        assert other_named.id not in ids

    async def test_colors_only_no_rgbas(self, db_session):
        named = await _spool(db_session, brand="Named", color_name="Red", rgba="FF0000FF")
        other = await _spool(db_session, brand="Other", color_name="Blue", rgba="0000FFFF")
        assert set(await _filtered_ids(db_session, colors=["Red"])) == {named.id}
        assert other.id not in set(await _filtered_ids(db_session, colors=["Red"]))


class TestCategoryFilter:
    async def test_exact_and_none_sentinel(self, db_session):
        prod = await _spool(db_session, brand="Prod", category="Production")
        none_null = await _spool(db_session, brand="NoneNull", category=None)
        none_empty = await _spool(db_session, brand="NoneEmpty", category="")

        assert set(await _filtered_ids(db_session, category="Production")) == {prod.id}
        assert set(await _filtered_ids(db_session, category="__none__")) == {none_null.id, none_empty.id}


class TestCatalogIdFilter:
    async def test_exact_match(self, db_session):
        a = await _spool(db_session, brand="CatA", core_weight_catalog_id=5)
        b = await _spool(db_session, brand="CatB", core_weight_catalog_id=9)
        assert set(await _filtered_ids(db_session, catalog_id=5)) == {a.id}
        assert b.id not in set(await _filtered_ids(db_session, catalog_id=5))


class TestLocationIdFilter:
    async def test_fk_match(self, db_session):
        loc = await _location(db_session, "Drybox 1")
        fk_spool = await _spool(db_session, brand="FKMatch", location_id=loc.id)
        other = await _spool(db_session, brand="Other", location_id=None, storage_location=None)

        ids = set(await _filtered_ids(db_session, location_id=str(loc.id)))
        assert ids == {fk_spool.id}
        assert other.id not in ids

    async def test_legacy_text_fallback(self, db_session):
        """A pre-catalog spool has no ``location_id`` FK, only the free-text
        ``storage_location`` — matched case/whitespace-insensitively against
        the resolved location NAME."""
        loc = await _location(db_session, "Shelf A4")
        legacy_spool = await _spool(db_session, brand="Legacy", location_id=None, storage_location="  shelf a4  ")
        unrelated = await _spool(db_session, brand="Unrelated", location_id=None, storage_location="Somewhere Else")

        ids = set(await _filtered_ids(db_session, location_id=str(loc.id)))
        assert ids == {legacy_spool.id}
        assert unrelated.id not in ids

    async def test_none_sentinel(self, db_session):
        loc = await _location(db_session, "Drybox 2")
        with_loc = await _spool(db_session, brand="WithLoc", location_id=loc.id)
        blank = await _spool(db_session, brand="Blank", location_id=None, storage_location=None)
        blank_text = await _spool(db_session, brand="BlankText", location_id=None, storage_location="   ")
        has_legacy_text = await _spool(
            db_session, brand="HasLegacyText", location_id=None, storage_location="Some Shelf"
        )

        ids = set(await _filtered_ids(db_session, location_id="__none__"))
        assert ids == {blank.id, blank_text.id}
        assert with_loc.id not in ids
        assert has_legacy_text.id not in ids

    async def test_legacy_text_fallback_cyrillic_byte_identical(self, db_session):
        """Review finding 1 regression: the legacy-text fallback used to fold
        the column with SQL ``lower()`` (SQLite: ASCII-only) but the location
        NAME with Python ``.lower()`` (full Unicode) — a byte-identical
        Cyrillic name compared unequal on the default SQLite backend even
        though nothing differs but case-folding technique. Both sides must
        now fold through the SAME ``func.lower()``."""
        loc = await _location(db_session, "Полиця A")
        legacy_spool = await _spool(db_session, brand="CyrillicLegacy", location_id=None, storage_location="Полиця A")
        unrelated = await _spool(db_session, brand="Unrelated", location_id=None, storage_location="Інша полиця")

        ids = set(await _filtered_ids(db_session, location_id=str(loc.id)))
        assert ids == {legacy_spool.id}
        assert unrelated.id not in ids

    async def test_non_numeric_location_id_does_not_500(self, db_session):
        """Defense-in-depth companion to the route-level 422 (TestRouteEnvelope):
        even called directly (bypassing the route's Query pattern), a bogus
        ``location_id`` must raise a normal, catchable error — never something
        the caller can't anticipate."""
        with pytest.raises(ValueError):
            await inventory_service.build_spool_filters(db_session, location_id="abc")


class TestStockFilter:
    async def test_stock_and_configured(self, db_session):
        stock_null = await _spool(db_session, brand="StockNull", slicer_filament=None)
        stock_empty = await _spool(db_session, brand="StockEmpty", slicer_filament="")
        configured = await _spool(db_session, brand="Configured", slicer_filament="GFL99")

        assert set(await _filtered_ids(db_session, stock="stock")) == {stock_null.id, stock_empty.id}
        assert set(await _filtered_ids(db_session, stock="configured")) == {configured.id}


class TestAssignedFilter:
    async def test_exists_both_ways(self, db_session, printer_factory):
        printer = await printer_factory()
        assigned = await _spool(db_session, brand="Assigned")
        unassigned = await _spool(db_session, brand="Unassigned")

        db_session.add(SpoolAssignment(spool_id=assigned.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        assert set(await _filtered_ids(db_session, assigned="assigned")) == {assigned.id}
        assert set(await _filtered_ids(db_session, assigned="unassigned")) == {unassigned.id}


class TestSearchFilter:
    async def test_tokenised_over_six_columns(self, db_session):
        match = await _spool(db_session, brand="SUNLU", material="PETG", color_name="Black")
        # No token below appears (case-insensitively) in ANY of the six
        # searched columns (brand/material/color_name/subtype/note/
        # slicer_filament_name) — a true negative, not an accidental hit.
        no_match = await _spool(db_session, brand="Devil Design", material="PLA", color_name="White")

        ids = set(await _filtered_ids(db_session, q="SUN Bl"))
        assert ids == {match.id}
        assert no_match.id not in ids

    async def test_empty_query_applies_no_filter(self, db_session):
        a = await _spool(db_session, brand="A")
        b = await _spool(db_session, brand="B")
        ids = set(await _filtered_ids(db_session, q="   "))
        assert ids == {a.id, b.id}


class TestCombinedFilters:
    async def test_and_semantics(self, db_session):
        keep = await _spool(
            db_session, material="PLA", brand="KeepBrand", category="Production", weight_used=500, label_weight=1000
        )
        await _spool(
            db_session, material="PETG", brand="KeepBrand", category="Production", weight_used=500, label_weight=1000
        )
        await _spool(
            db_session, material="PLA", brand="KeepBrand", category="Prototype", weight_used=500, label_weight=1000
        )
        await _spool(
            db_session, material="PLA", brand="KeepBrand", category="Production", weight_used=0, label_weight=1000
        )

        ids = set(await _filtered_ids(db_session, material="PLA", category="Production", usage="used"))
        assert ids == {keep.id}


class TestCount:
    async def test_count_matches_filtered_ids(self, db_session):
        for i in range(3):
            await _spool(db_session, brand=f"X{i}", material="PLA")
        await _spool(db_session, brand="Other", material="PETG")

        assert await _count(db_session, material="PLA") == 3
        assert await _count(db_session) == 4


# ── sort map: one assertion pair (asc/desc) per real-column key ─────────────

# Every key from ``columnSortValues`` (InventoryPage.tsx:514-565) EXCEPT
# ``display_name``'s composite is folded in via `material` differing (see
# below), `location` (its own dedicated join-based tests further down), and
# `rgba`/`color_combined` (operator ruling — remapped client-side to
# `color_name`, the server never learns them).
_SORT_KEYS = [
    "id",
    "added_time",
    "purchase_date",
    "encode_time",
    "last_used_time",
    "material",
    "subtype",
    "color_name",
    "brand",
    "slicer_filament",
    "storage_location",
    "purchase_location",
    "label_weight",
    "net",
    "gross",
    "used",
    "remaining",
    "note",
    "data_origin",
    "tag_type",
    "stock",
    "spool_name",
    "cost_per_kg",
    "filament_diameter",
    "lot",
    "weight_check",
    "display_name",
]


@pytest.fixture
async def sort_matrix_spools(db_session):
    """Two spools, A before B on EVERY key in ``_SORT_KEYS`` simultaneously —
    one fixture pins all ~27 real-column sort keys (asc: [A, B], desc: [B,
    A])."""
    a = await _spool(
        db_session,
        material="ABS",
        subtype="Basic",
        color_name="Black",
        brand="AAA_Brand",
        slicer_filament=None,
        slicer_filament_name="AAA_Preset",
        storage_location="Shelf A",
        purchase_location="AAA Store",
        label_weight=500,
        weight_used=100,
        core_weight=250,
        note="AAA note",
        data_origin="manual",
        tag_type="bambulab",
        core_weight_catalog_id=1,
        cost_per_kg=10.0,
        filament_diameter="1.75",
        lot=1,
        last_scale_weight=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        purchase_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        encode_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_used=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    b = await _spool(
        db_session,
        material="PLA",
        subtype="Silk",
        color_name="White",
        brand="ZZZ_Brand",
        slicer_filament="GFL99",
        slicer_filament_name="ZZZ_Preset",
        storage_location="Shelf Z",
        purchase_location="ZZZ Store",
        label_weight=1000,
        weight_used=150,
        core_weight=250,
        note="ZZZ note",
        data_origin="rfid_auto",
        tag_type="generic",
        core_weight_catalog_id=5,
        cost_per_kg=20.0,
        filament_diameter="2.85",
        lot=5,
        last_scale_weight=1150,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        purchase_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        encode_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        last_used=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    return a, b


@pytest.mark.parametrize("key", _SORT_KEYS)
async def test_sort_key_asc_and_desc(db_session, sort_matrix_spools, key):
    a, b = sort_matrix_spools
    filters = await inventory_service.build_spool_filters(db_session)

    asc = await inventory_service.list_spools(db_session, filters=filters, sort_by=f"{key}_asc", limit=None)
    assert [s.id for s in asc] == [a.id, b.id], f"{key}_asc"

    desc = await inventory_service.list_spools(db_session, filters=filters, sort_by=f"{key}_desc", limit=None)
    assert [s.id for s in desc] == [b.id, a.id], f"{key}_desc"


class TestLocationSort:
    async def test_shelf_first_then_printer_name_then_slot(self, db_session, printer_factory):
        shelf_spool = await _spool(db_session, brand="Shelf")  # unassigned
        printer_a = await printer_factory(name="Printer A")
        printer_z = await printer_factory(name="Printer Z")
        on_a = await _spool(db_session, brand="OnPrinterA")
        on_z = await _spool(db_session, brand="OnPrinterZ")

        db_session.add(SpoolAssignment(spool_id=on_a.id, printer_id=printer_a.id, ams_id=0, tray_id=0))
        db_session.add(SpoolAssignment(spool_id=on_z.id, printer_id=printer_z.id, ams_id=0, tray_id=0))
        await db_session.commit()

        filters = await inventory_service.build_spool_filters(db_session)

        asc = await inventory_service.list_spools(db_session, filters=filters, sort_by="location_asc", limit=None)
        ids_asc = [s.id for s in asc]
        assert ids_asc.index(shelf_spool.id) < ids_asc.index(on_a.id) < ids_asc.index(on_z.id)

        desc = await inventory_service.list_spools(db_session, filters=filters, sort_by="location_desc", limit=None)
        ids_desc = [s.id for s in desc]
        assert ids_desc.index(on_z.id) < ids_desc.index(on_a.id) < ids_desc.index(shelf_spool.id)

    async def test_no_row_duplication_when_a_spool_has_two_assignments(self, db_session, printer_factory):
        """SpoolAssignment's only UniqueConstraint is (printer_id, ams_id,
        tray_id) — a SLOT, not a spool — and assign_spool never removes a
        spool's OTHER assignments. A spool can therefore end up with more
        than one assignment row; the location-sort join must not duplicate
        its list row."""
        printer = await printer_factory()
        spool = await _spool(db_session, brand="DoubleAssigned")
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await db_session.commit()

        filters = await inventory_service.build_spool_filters(db_session)
        rows = await inventory_service.list_spools(db_session, filters=filters, sort_by="location_asc", limit=None)
        assert [s.id for s in rows].count(spool.id) == 1


class TestPagingTiebreak:
    async def test_page_2_tiebreak_on_low_cardinality_sort(self, db_session):
        """Every spool shares the same material (a low-cardinality sort) —
        without the mandatory Spool.id tiebreak, paging by material has no
        defined order between the two queries and could repeat or skip a
        row."""
        spools = [await _spool(db_session, brand=f"Brand{i}", material="PLA") for i in range(4)]
        filters = await inventory_service.build_spool_filters(db_session)

        page1 = await inventory_service.list_spools(
            db_session, filters=filters, sort_by="material_asc", limit=2, offset=0
        )
        page2 = await inventory_service.list_spools(
            db_session, filters=filters, sort_by="material_asc", limit=2, offset=2
        )

        all_ids = [s.id for s in page1] + [s.id for s in page2]
        assert sorted(all_ids) == sorted(s.id for s in spools)
        assert len(set(all_ids)) == 4


class TestNullableSortColumnsMatchClientCoalescing:
    """Review finding 5: the sort matrix fixture never exercised a bare NULL
    sort value (``sort_matrix_spools``'s nullable fields are always masked by
    a non-null value or routed through a CASE that already handles NULL).
    Without an explicit fix, NULL placement here is dialect-dependent
    (SQLite defaults NULLS FIRST on asc, PostgreSQL NULLS LAST) — the same
    request would order differently per backend. The client coalesces every
    nullable extractor to ``''``/``0``, which sorts NULL first on asc/last on
    desc deterministically; the server must match."""

    async def test_nullable_text_column_coalesces_like_the_client(self, db_session):
        has_subtype = await _spool(db_session, brand="HasSubtype", subtype="Zzz")
        null_subtype = await _spool(db_session, brand="NullSubtype", subtype=None)
        filters = await inventory_service.build_spool_filters(db_session)

        asc = await inventory_service.list_spools(db_session, filters=filters, sort_by="subtype_asc", limit=None)
        assert [s.id for s in asc] == [null_subtype.id, has_subtype.id]

        desc = await inventory_service.list_spools(db_session, filters=filters, sort_by="subtype_desc", limit=None)
        assert [s.id for s in desc] == [has_subtype.id, null_subtype.id]

    async def test_nullable_datetime_column_pins_null_first_on_asc_last_on_desc(self, db_session):
        has_date = await _spool(
            db_session, brand="HasPurchaseDate", purchase_date=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        null_date = await _spool(db_session, brand="NullPurchaseDate", purchase_date=None)
        filters = await inventory_service.build_spool_filters(db_session)

        asc = await inventory_service.list_spools(db_session, filters=filters, sort_by="purchase_date_asc", limit=None)
        assert [s.id for s in asc] == [null_date.id, has_date.id]

        desc = await inventory_service.list_spools(
            db_session, filters=filters, sort_by="purchase_date_desc", limit=None
        )
        assert [s.id for s in desc] == [has_date.id, null_date.id]


class TestStockSortTreatsEmptyStringAsUnconfigured:
    async def test_empty_string_slicer_filament_sorts_as_unconfigured(self, db_session):
        """Review finding 4: the ``stock`` SORT used to disagree with the
        ``stock`` FILTER (and the client extractor) about an empty-string
        ``slicer_filament`` — the filter already treats it as unconfigured
        (seeded by ``TestStockFilter``); the sort must agree."""
        empty_string = await _spool(db_session, brand="EmptyString", slicer_filament="")
        configured = await _spool(db_session, brand="Configured", slicer_filament="GFL99")
        filters = await inventory_service.build_spool_filters(db_session)

        asc = await inventory_service.list_spools(db_session, filters=filters, sort_by="stock_asc", limit=None)
        assert [s.id for s in asc] == [empty_string.id, configured.id]


class TestUnrecognizedSortFallsBackGracefully:
    async def test_missing_and_garbage_sort_by(self, db_session):
        a = await _spool(db_session, material="ABS", brand="A")
        b = await _spool(db_session, material="PLA", brand="B")
        filters = await inventory_service.build_spool_filters(db_session)

        default = await inventory_service.list_spools(db_session, filters=filters, sort_by=None, limit=None)
        garbage = await inventory_service.list_spools(db_session, filters=filters, sort_by="nonsense", limit=None)
        assert [s.id for s in default] == [s.id for s in garbage] == [a.id, b.id]


# ── route level: envelope, params, and the legacy pin ───────────────────────


class TestRouteEnvelope:
    async def test_paged_envelope_shape_and_meta_total(self, async_client, db_session):
        for i in range(5):
            await _spool(db_session, brand=f"Brand{i}", material="PLA")
        await _spool(db_session, brand="Other", material="PETG")

        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "per_page": 2, "material": "PLA"})
        assert resp.status_code == 200
        body = resp.json()

        assert set(body.keys()) == {"items", "meta"}
        assert body["meta"]["total"] == 5
        assert body["meta"]["current_page"] == 1
        assert body["meta"]["per_page"] == 2
        assert body["meta"]["last_page"] == 3
        assert len(body["items"]) == 2
        # Default paged rows: ``k_profiles`` is null (present-but-None), never
        # the nested array — the ``include_k_profiles`` opt-in (task 4) is the
        # only thing that fills it. Same spirit as the original "not in"
        # assertion: the slim projection carries no per-profile payload unless
        # explicitly asked.
        assert body["items"][0]["k_profiles"] is None
        assert "k_profile_count" in body["items"][0]

    async def test_all_true_skips_pagination(self, async_client, db_session):
        for i in range(5):
            await _spool(db_session, brand=f"Brand{i}", material="PLA")

        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "all": "true"})
        body = resp.json()
        assert len(body["items"]) == 5
        assert body["meta"]["total"] == 5
        assert body["meta"]["last_page"] == 1

    async def test_colors_and_color_rgbas_list_params(self, async_client, db_session):
        named = await _spool(db_session, brand="Named", color_name="Red", rgba="FF0000FF")
        await _spool(db_session, brand="Other", color_name="Blue", rgba="0000FFFF")

        resp = await async_client.get(
            "/api/v1/inventory/spools",
            params=[("page", 1), ("colors", "Red")],
        )
        body = resp.json()
        ids = [i["id"] for i in body["items"]]
        assert ids == [named.id]

    async def test_k_profile_count_reflects_real_profiles(self, async_client, db_session, printer_factory):
        """Check (a)'s finding, exercised end-to-end: InventoryPage.tsx's
        ``pa_k`` table column reads ``k_profiles`` off list rows for a
        presence badge — ``k_profile_count`` must carry the real count, not
        just be present-and-zero."""
        from backend.app.models.filament_calibration import FilamentCalibration
        from backend.app.models.spool_k_profile import SpoolKProfile

        printer = await printer_factory()
        spool = await _spool(db_session, brand="HasProfile")

        calibration = FilamentCalibration(
            printer_id=printer.id,
            filament_id="GFL99",
            nozzle_diameter=0.4,
            nozzle_volume_type="Standard",
            cali_mode="pa",
            source="manual",
            name="Test PA",
        )
        db_session.add(calibration)
        await db_session.commit()
        await db_session.refresh(calibration)

        db_session.add(SpoolKProfile(spool_id=spool.id, printer_id=printer.id, filament_calibration_id=calibration.id))
        await db_session.commit()

        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1})
        body = resp.json()
        item = next(i for i in body["items"] if i["id"] == spool.id)
        assert item["k_profile_count"] == 1

    async def test_non_numeric_location_id_is_422_not_500(self, async_client, db_session):
        """Review finding 3: ``location_id=abc`` used to reach ``int(location_id)``
        uncaught and 500. The Query pattern must reject it as a normal 4xx —
        reachable by any INVENTORY_READ consumer via a hand-edited/truncated
        deep-link (the page writes ``?location_id=`` into the shareable URL)."""
        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "location_id": "abc"})
        assert 400 <= resp.status_code < 500
        assert resp.status_code != 500

        # The two legitimate shapes still work fine.
        ok_sentinel = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "location_id": "__none__"})
        assert ok_sentinel.status_code == 200
        ok_numeric = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "location_id": "1"})
        assert ok_numeric.status_code == 200


class TestIncludeKProfilesOptIn:
    """Task 4's cards-view opt-in: ``include_k_profiles=true`` serializes the
    full per-profile array on paged rows (and on grouped representatives);
    omitted, ``k_profiles`` stays null. Serialization-only — the relationship
    is eager-loaded either way (see ``_spool_to_list_item``)."""

    async def _seed_profiled_spool(self, db_session, printer_factory):
        from backend.app.models.filament_calibration import FilamentCalibration
        from backend.app.models.spool_k_profile import SpoolKProfile

        printer = await printer_factory()
        spool = await _spool(db_session, brand="HasProfile")
        calibration = FilamentCalibration(
            printer_id=printer.id,
            filament_id="GFL99",
            nozzle_diameter=0.4,
            nozzle_volume_type="Standard",
            cali_mode="pa",
            source="manual",
            name="Opt-in PA",
        )
        db_session.add(calibration)
        await db_session.commit()
        await db_session.refresh(calibration)
        db_session.add(SpoolKProfile(spool_id=spool.id, printer_id=printer.id, filament_calibration_id=calibration.id))
        await db_session.commit()
        return spool

    async def test_flat_opt_in_carries_the_full_array(self, async_client, db_session, printer_factory):
        spool = await self._seed_profiled_spool(db_session, printer_factory)
        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "include_k_profiles": "true"})
        assert resp.status_code == 200
        item = next(i for i in resp.json()["items"] if i["id"] == spool.id)
        assert item["k_profile_count"] == 1
        assert isinstance(item["k_profiles"], list)
        assert len(item["k_profiles"]) == 1
        assert item["k_profiles"][0]["name"] == "Opt-in PA"

    async def test_flat_default_stays_null_even_with_profiles(self, async_client, db_session, printer_factory):
        spool = await self._seed_profiled_spool(db_session, printer_factory)
        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1})
        item = next(i for i in resp.json()["items"] if i["id"] == spool.id)
        assert item["k_profile_count"] == 1
        assert item["k_profiles"] is None

    async def test_grouped_representative_honors_the_opt_in(self, async_client, db_session, printer_factory):
        spool = await self._seed_profiled_spool(db_session, printer_factory)
        resp = await async_client.get(
            "/api/v1/inventory/spools",
            params={"page": 1, "group_similar": "true", "include_k_profiles": "true"},
        )
        assert resp.status_code == 200
        rep = next(g["representative"] for g in resp.json()["items"] if spool.id in g["ids"])
        assert isinstance(rep["k_profiles"], list)
        assert len(rep["k_profiles"]) == 1

    async def test_legacy_bare_call_ignores_the_param(self, async_client, db_session):
        await _spool(db_session, brand="Legacy")
        resp = await async_client.get("/api/v1/inventory/spools", params={"include_k_profiles": "true"})
        body = resp.json()
        assert isinstance(body, list)  # still the flat legacy shape
        assert "k_profile_count" not in body[0]


class TestLegacyPin:
    async def test_bare_call_stays_flat_full_shape_with_k_profiles_in_order(self, async_client, db_session):
        """The bare call (no ``page``) must keep the historical response: a
        flat array whose items carry the EXACT ``SpoolResponse`` key set — no
        key gained, none lost (``k_profiles`` present, never a
        ``k_profile_count``) — ordered by material/brand/color_name. Values
        are pinned by the schema itself; this guards the serialization path,
        which changed to ``response_model=None`` in the paged rework."""
        archived = await _spool(
            db_session, material="ABS", brand="A", color_name="Red", archived_at=datetime.now(timezone.utc)
        )
        active = await _spool(db_session, material="PETG", brand="B", color_name="Blue")

        resp = await async_client.get("/api/v1/inventory/spools", params={"include_archived": "true"})
        assert resp.status_code == 200
        body = resp.json()

        assert isinstance(body, list)
        ids = [s["id"] for s in body]
        assert active.id in ids
        assert archived.id in ids
        expected_keys = set(SpoolResponse.model_fields.keys())
        for s in body:
            assert set(s.keys()) == expected_keys
            assert "k_profiles" in s
            assert "k_profile_count" not in s

        by_id = {s["id"]: s for s in body}
        assert [by_id[archived.id]["material"], by_id[active.id]["material"]] == ["ABS", "PETG"]
        index_by_id = {s["id"]: i for i, s in enumerate(body)}
        assert index_by_id[archived.id] < index_by_id[active.id]

    async def test_bare_call_default_excludes_archived(self, async_client, db_session):
        active = await _spool(db_session, brand="Active")
        archived = await _spool(db_session, brand="Archived", archived_at=datetime.now(timezone.utc))

        resp = await async_client.get("/api/v1/inventory/spools")
        body = resp.json()
        ids = [s["id"] for s in body]
        assert active.id in ids
        assert archived.id not in ids


# ── task 2: ids + facets endpoints ──────────────────────────────────────────
#
# ``GET /spools/ids`` powers "Select all N matching the filter" (spec §3.4) —
# it must answer over the SAME filter semantics as the paged list (both ride
# ``build_spool_filters``), so a selection can never include a row the list
# wouldn't show. ``GET /spools/facets`` feeds the filter dropdowns (spec §3.6)
# — distinct raw values under the active archived tab only; colour pairs stay
# RAW (the client owns the catalog and resolves/groups them).


class TestListSpoolIdsService:
    async def test_returns_ids_ascending_and_respects_limit(self, db_session):
        s1 = await _spool(db_session, brand="IdsA")
        s2 = await _spool(db_session, brand="IdsB")
        s3 = await _spool(db_session, brand="IdsC")

        ids = await inventory_service.list_spool_ids(db_session, filters=[])
        assert ids == sorted([s1.id, s2.id, s3.id])

        capped = await inventory_service.list_spool_ids(db_session, filters=[], limit=2)
        assert capped == sorted([s1.id, s2.id, s3.id])[:2]

    async def test_honours_the_shared_filters(self, db_session):
        used = await _spool(db_session, brand="IdsUsed", weight_used=100)
        await _spool(db_session, brand="IdsNew", weight_used=0)

        filters = await inventory_service.build_spool_filters(db_session, usage="used")
        assert await inventory_service.list_spool_ids(db_session, filters=filters) == [used.id]


class TestIdsEndpoint:
    async def test_ids_honor_filters_and_q(self, async_client, db_session):
        match = await _spool(db_session, material="PLA", brand="SUNLU", color_name="Black")
        await _spool(db_session, material="PETG", brand="SUNLU")
        await _spool(db_session, material="PLA", brand="Overture")

        resp = await async_client.get("/api/v1/inventory/spools/ids", params={"material": "PLA", "q": "SUN"})
        assert resp.status_code == 200
        assert resp.json() == {"ids": [match.id]}

    async def test_ids_agree_with_the_paged_list_over_the_same_filters(self, async_client, db_session):
        """The entire point of the endpoint: the id set it materializes must be
        exactly the rows the paged list answers with under identical params —
        both ride ``build_spool_filters``, so this can only break if one of
        them stops doing so."""
        a = await _spool(db_session, brand="ParityA", weight_used=100)
        b = await _spool(db_session, brand="ParityB", weight_used=50)
        await _spool(db_session, brand="ParityC", weight_used=0)

        list_resp = await async_client.get(
            "/api/v1/inventory/spools", params={"page": 1, "all": "true", "usage": "used"}
        )
        ids_resp = await async_client.get("/api/v1/inventory/spools/ids", params={"usage": "used"})
        assert ids_resp.status_code == 200

        listed = {item["id"] for item in list_resp.json()["items"]}
        assert set(ids_resp.json()["ids"]) == listed == {a.id, b.id}

    async def test_over_the_cap_is_400(self, async_client, db_session, monkeypatch):
        """Spec §3.4's sanity cap: refuse a pathological select-all rather than
        materialize an unbounded id list. Cap lowered via monkeypatch — seeding
        50 001 real rows would test nothing extra, slowly."""
        from backend.app.api.routes import inventory as inventory_routes

        monkeypatch.setattr(inventory_routes, "_SPOOL_IDS_CAP", 2)
        for i in range(3):
            await _spool(db_session, brand=f"Cap{i}")

        resp = await async_client.get("/api/v1/inventory/spools/ids")
        assert resp.status_code == 400

    async def test_exactly_at_the_cap_is_200(self, async_client, db_session, monkeypatch):
        """The refusal boundary is OVER the cap, not at it."""
        from backend.app.api.routes import inventory as inventory_routes

        monkeypatch.setattr(inventory_routes, "_SPOOL_IDS_CAP", 3)
        seeded = [await _spool(db_session, brand=f"AtCap{i}") for i in range(3)]

        resp = await async_client.get("/api/v1/inventory/spools/ids")
        assert resp.status_code == 200
        assert set(resp.json()["ids"]) == {s.id for s in seeded}

    async def test_non_numeric_location_id_is_422_not_500(self, async_client, db_session):
        """T1 review carry-over: every endpoint taking ``location_id`` reuses
        the same ``Query(pattern=...)`` guard, or ``int("abc")`` in the shared
        builder re-opens the fixed 500."""
        await _spool(db_session)

        resp = await async_client.get("/api/v1/inventory/spools/ids", params={"location_id": "abc"})
        assert 400 <= resp.status_code < 500
        assert resp.status_code != 500

        ok_sentinel = await async_client.get("/api/v1/inventory/spools/ids", params={"location_id": "__none__"})
        assert ok_sentinel.status_code == 200
        ok_numeric = await async_client.get("/api/v1/inventory/spools/ids", params={"location_id": "1"})
        assert ok_numeric.status_code == 200


class TestFacetsEndpoint:
    async def test_distinct_values_across_all_five_dimensions(self, async_client, db_session):
        await _spool(db_session, material="PLA", brand="SUNLU", category="Prod", core_weight_catalog_id=None)
        await _spool(db_session, material="PLA", brand="SUNLU", category="Prod", core_weight_catalog_id=None)
        await _spool(
            db_session,
            material="PETG",
            brand="eSun",
            category="Proto",
            core_weight_catalog_id=7,
            color_name="Jade White",
            rgba="FFFFFFFF",
        )

        resp = await async_client.get("/api/v1/inventory/spools/facets")
        assert resp.status_code == 200
        body = resp.json()

        assert set(body.keys()) == {"materials", "brands", "categories", "catalog_ids", "colors"}
        assert set(body["materials"]) == {"PLA", "PETG"}
        assert set(body["brands"]) == {"SUNLU", "eSun"}
        assert set(body["categories"]) == {"Prod", "Proto"}
        assert body["catalog_ids"] == [7]
        # The default-seeded pair appears ONCE despite two spools carrying it.
        pairs = {(c["color_name"], c["rgba"]) for c in body["colors"]}
        assert pairs == {("Black", "000000FF"), ("Jade White", "FFFFFFFF")}

    async def test_scoped_to_the_archived_tab_param(self, async_client, db_session):
        """Spec §3.6: facets answer "under the active archived tab" — an
        archived spool's values must not appear as active-tab dropdown options
        (and vice versa). Omitting the param spans both, per the endpoint
        family's usual omit-a-param-get-no-filter contract."""
        await _spool(db_session, material="PLA", brand="ActiveBrand")
        await _spool(db_session, material="ASA", brand="ArchivedBrand", archived_at=datetime.now(timezone.utc))

        active = (await async_client.get("/api/v1/inventory/spools/facets", params={"archived": "active"})).json()
        assert set(active["materials"]) == {"PLA"}
        assert set(active["brands"]) == {"ActiveBrand"}

        archived = (await async_client.get("/api/v1/inventory/spools/facets", params={"archived": "archived"})).json()
        assert set(archived["materials"]) == {"ASA"}
        assert set(archived["brands"]) == {"ArchivedBrand"}

        both = (await async_client.get("/api/v1/inventory/spools/facets")).json()
        assert set(both["materials"]) == {"PLA", "ASA"}

    async def test_brand_null_and_empty_are_excluded(self, async_client, db_session):
        """The client dropdown filtered ``.filter(Boolean)`` — NULL/'' brands
        were never options."""
        await _spool(db_session, brand=None)
        await _spool(db_session, brand="")
        await _spool(db_session, brand="RealBrand")

        body = (await async_client.get("/api/v1/inventory/spools/facets")).json()
        assert body["brands"] == ["RealBrand"]

    async def test_categories_are_trimmed_and_blank_excluded(self, async_client, db_session):
        """Client: ``s.category?.trim()`` then ``.filter(Boolean)`` — ' Prod'
        and 'Prod' merge into one option; NULL/''/whitespace-only vanish."""
        await _spool(db_session, category=" Prod")
        await _spool(db_session, category="Prod")
        await _spool(db_session, category="   ")
        await _spool(db_session, category=None)

        body = (await async_client.get("/api/v1/inventory/spools/facets")).json()
        assert body["categories"] == ["Prod"]

    async def test_colors_keep_null_name_pairs_but_drop_the_double_null(self, async_client, db_session):
        """A NULL-name+rgba pair is a real, filterable option (the raw-pairs
        colour design exists exactly for it); a pair with BOTH sides NULL can
        never be filtered on and resolves to nothing client-side — dropped."""
        await _spool(db_session, color_name=None, rgba="FF0000FF")
        await _spool(db_session, color_name=None, rgba=None)

        body = (await async_client.get("/api/v1/inventory/spools/facets")).json()
        pairs = {(c["color_name"], c["rgba"]) for c in body["colors"]}
        assert (None, "FF0000FF") in pairs
        assert (None, None) not in pairs

    async def test_catalog_ids_are_distinct_and_non_null(self, async_client, db_session):
        await _spool(db_session, core_weight_catalog_id=3)
        await _spool(db_session, core_weight_catalog_id=3)
        await _spool(db_session, core_weight_catalog_id=1)
        await _spool(db_session, core_weight_catalog_id=None)

        body = (await async_client.get("/api/v1/inventory/spools/facets")).json()
        assert body["catalog_ids"] == [1, 3]


class TestCategoryFacetFilterRoundTrip:
    async def test_every_advertised_category_option_matches_at_least_one_spool(self, async_client, db_session):
        """T2 review minor 1: the facet advertises TRIMMED category options
        (' Production' and 'Production' are one dropdown entry), so the shared
        category filter must trim too — a padded-category spool (the
        CSV-import shape) used to contribute the option yet never match it,
        making list/count/ids disagree with the facet that advertised it.
        Round-trip contract: every option facets returns must, fed straight
        back as the filter, match at least one spool."""
        padded = await _spool(db_session, category=" Production")
        plain = await _spool(db_session, category="Prototype")

        facets = (await async_client.get("/api/v1/inventory/spools/facets")).json()
        assert set(facets["categories"]) == {"Production", "Prototype"}

        for option in facets["categories"]:
            resp = await async_client.get("/api/v1/inventory/spools/ids", params={"category": option})
            assert resp.status_code == 200
            assert len(resp.json()["ids"]) >= 1, f"facet option {option!r} matched nothing"

        # And specifically: the padded spool IS matched by the trimmed option
        # it advertised, without bleeding into the other category.
        prod = await async_client.get("/api/v1/inventory/spools/ids", params={"category": "Production"})
        assert prod.json()["ids"] == [padded.id]
        proto = await async_client.get("/api/v1/inventory/spools/ids", params={"category": "Prototype"})
        assert proto.json()["ids"] == [plain.id]


# ── task 3: grouped mode ("group similar spools" server-side) ────────────────
#
# ``group_similar=true`` on ``GET /spools`` — rows become GROUPS: the 7-column
# key (material | subtype | brand | color_name | rgba | label_weight | lot,
# the exact port of the deleted client's ``spoolGroupKey``,
# InventoryPage.tsx:83-87) + ``group_count`` + complete member ``ids`` + the
# min(id) row as representative, paged over GROUPS under the same
# ``build_spool_filters`` list. The CLIENT code is the behavioral spec,
# including its consumers (:1402-1439): used (``weight_used > 0``) or
# assigned spools are NEVER merged — each stays its own singleton group.


async def _groups(db_session, sort_by=None, limit=None, offset=0, **filter_kwargs):
    filters = await inventory_service.build_spool_filters(db_session, **filter_kwargs)
    return await inventory_service.list_spool_groups(
        db_session, filters=filters, sort_by=sort_by, limit=limit, offset=offset
    )


async def _group_total(db_session, **filter_kwargs) -> int:
    filters = await inventory_service.build_spool_filters(db_session, **filter_kwargs)
    return await inventory_service.count_spool_groups(db_session, filters=filters)


class TestGroupedModeService:
    async def test_identical_unused_unassigned_spools_form_one_group(self, db_session):
        a = await _spool(db_session)
        b = await _spool(db_session)
        c = await _spool(db_session)

        groups = await _groups(db_session)
        assert len(groups) == 1
        (group,) = groups
        assert group["group_count"] == 3
        assert group["ids"] == sorted([a.id, b.id, c.id])
        # Representative is the min(id) row (plan ruling), carried as the ORM
        # row itself so the route can serialize it as a SpoolListItem.
        assert group["representative"].id == min(a.id, b.id, c.id)
        assert await _group_total(db_session) == 1

    async def test_null_and_empty_string_text_key_fields_group_together(self, db_session):
        """The client key coalesces subtype/brand/color_name/rgba with
        ``|| ''`` — NULL and '' are the SAME key value (pinned by the client's
        own test 'treats null and empty string subtype the same')."""
        await _spool(db_session, material="PLA", subtype=None)
        await _spool(db_session, material="PLA", subtype="")
        await _spool(db_session, material="PETG", brand=None)
        await _spool(db_session, material="PETG", brand="")

        groups = await _groups(db_session)
        assert len(groups) == 2
        assert all(g["group_count"] == 2 for g in groups)

    async def test_spools_differing_only_in_lot_are_two_groups(self, db_session):
        """The plan's named seed: lot is part of the key so sequential-lot
        copies of a purchase bundle stay distinct."""
        await _spool(db_session, lot=1)
        await _spool(db_session, lot=2)

        groups = await _groups(db_session)
        assert len(groups) == 2
        assert all(g["group_count"] == 1 for g in groups)

    async def test_lot_zero_is_distinct_from_null_and_nulls_merge(self, db_session):
        """The client uses ``lot ?? ''`` (nullish), NOT ``|| ''``: lot=0 keys
        as '0' while NULL keys as '' — 0 and NULL are DIFFERENT groups, and
        all-NULL lots are ONE group (SQL GROUP BY treats NULLs as equal on
        both dialects, which is exactly the ``?? ''`` fold)."""
        zero = await _spool(db_session, lot=0)
        null_a = await _spool(db_session, lot=None)
        null_b = await _spool(db_session, lot=None)

        groups = await _groups(db_session)
        assert len(groups) == 2
        by_count = {g["group_count"]: g for g in groups}
        assert by_count[1]["ids"] == [zero.id]
        assert by_count[2]["ids"] == sorted([null_a.id, null_b.id])

    async def test_used_spool_never_merges(self, db_session):
        """Client consumers (InventoryPage.tsx:1407-1424): only unused spools
        are eligible — a used spool with an identical key stays its own row."""
        fresh_a = await _spool(db_session, weight_used=0)
        fresh_b = await _spool(db_session, weight_used=0)
        used = await _spool(db_session, weight_used=500)

        groups = await _groups(db_session)
        assert len(groups) == 2
        by_count = {g["group_count"]: g for g in groups}
        assert by_count[2]["ids"] == sorted([fresh_a.id, fresh_b.id])
        assert by_count[1]["ids"] == [used.id]

    async def test_assigned_spool_never_merges(self, db_session, printer_factory):
        printer = await printer_factory()
        fresh_a = await _spool(db_session)
        fresh_b = await _spool(db_session)
        assigned = await _spool(db_session)
        db_session.add(SpoolAssignment(spool_id=assigned.id, printer_id=printer.id, ams_id=0, tray_id=0))
        await db_session.commit()

        groups = await _groups(db_session)
        assert len(groups) == 2
        by_count = {g["group_count"]: g for g in groups}
        assert by_count[2]["ids"] == sorted([fresh_a.id, fresh_b.id])
        assert by_count[1]["ids"] == [assigned.id]

    async def test_two_identical_ineligible_spools_stay_two_singles(self, db_session):
        """Two USED spools sharing the whole key must NOT merge with each
        other either (the client renders every ineligible spool individually)
        — the eligibility discriminator has to key them apart, not just apart
        from the eligible group."""
        used_a = await _spool(db_session, weight_used=100)
        used_b = await _spool(db_session, weight_used=100)

        groups = await _groups(db_session)
        assert len(groups) == 2
        assert sorted(g["ids"][0] for g in groups) == sorted([used_a.id, used_b.id])
        assert await _group_total(db_session) == 2

    async def test_filters_apply_before_grouping(self, db_session):
        pla_a = await _spool(db_session, material="PLA")
        pla_b = await _spool(db_session, material="PLA")
        await _spool(db_session, material="PETG")
        await _spool(db_session, material="PETG")

        groups = await _groups(db_session, material="PLA")
        assert len(groups) == 1
        assert groups[0]["ids"] == sorted([pla_a.id, pla_b.id])
        assert await _group_total(db_session, material="PLA") == 1

    async def test_group_key_fields_carry_the_coalesced_key(self, db_session):
        """The key fields on a group row are the KEY values — '' where the
        client's ``|| ''`` folded a NULL — while ``lot`` stays its raw value
        (None reported as null; the ``?? ''`` fold has no '' to collide with
        on an integer column)."""
        await _spool(db_session, subtype=None, brand=None, color_name=None, rgba=None, lot=None)

        (group,) = await _groups(db_session)
        assert group["material"] == "PLA"
        assert group["subtype"] == ""
        assert group["brand"] == ""
        assert group["color_name"] == ""
        assert group["rgba"] == ""
        assert group["label_weight"] == 1000
        assert group["lot"] is None

    async def test_group_paging_pages_groups_not_spools(self, db_session):
        # 5 groups of 2 members each (distinct by lot), one shared sort value.
        for lot in range(1, 6):
            await _spool(db_session, lot=lot)
            await _spool(db_session, lot=lot)

        assert await _group_total(db_session) == 5
        page_one = await _groups(db_session, limit=2, offset=0)
        page_three = await _groups(db_session, limit=2, offset=4)
        assert len(page_one) == 2
        assert len(page_three) == 1
        assert all(g["group_count"] == 2 for g in page_one + page_three)


class TestGroupedSort:
    async def test_material_asc_and_desc(self, db_session):
        await _spool(db_session, material="PLA")
        await _spool(db_session, material="ABS")
        await _spool(db_session, material="PETG")

        asc = [g["material"] for g in await _groups(db_session, sort_by="material_asc")]
        assert asc == ["ABS", "PETG", "PLA"]
        desc = [g["material"] for g in await _groups(db_session, sort_by="material_desc")]
        assert desc == ["PLA", "PETG", "ABS"]

    async def test_brand_sorts_null_as_empty_first(self, db_session):
        await _spool(db_session, brand="Zeta")
        await _spool(db_session, brand=None)

        asc = [g["brand"] for g in await _groups(db_session, sort_by="brand_asc")]
        assert asc == ["", "Zeta"]

    async def test_color_name_sort_folds_case(self, db_session):
        """The flat sort map lowercases color_name (a pre-existing client
        extractor behaviour, NOT a new fold — the stage-C guard is about
        ADDING folds) — grouped mode must agree with flat mode here."""
        await _spool(db_session, color_name="apple")
        await _spool(db_session, color_name="Banana")

        asc = [g["color_name"] for g in await _groups(db_session, sort_by="color_name_asc")]
        assert asc == ["apple", "Banana"]

    async def test_display_name_composite(self, db_session):
        await _spool(db_session, material="PLA", brand="Beta")
        await _spool(db_session, material="PLA", brand="Alpha")
        await _spool(db_session, material="ABS", brand="Zeta")

        asc = [(g["material"], g["brand"]) for g in await _groups(db_session, sort_by="display_name_asc")]
        assert asc == [("ABS", "Zeta"), ("PLA", "Alpha"), ("PLA", "Beta")]

    async def test_low_cardinality_sort_pages_without_dup_or_skip(self, db_session):
        """Every grouped ordering ends with a stable tiebreak — walk an
        identical-sort-value set page by page and the union must be complete
        and duplicate-free (the flat list's TestPagingTiebreak, over groups)."""
        for lot in range(1, 7):
            await _spool(db_session, lot=lot)

        seen: list[int] = []
        for offset in (0, 2, 4):
            page = await _groups(db_session, sort_by="material_asc", limit=2, offset=offset)
            seen.extend(g["representative"].id for g in page)
        assert len(seen) == 6
        assert len(set(seen)) == 6

    async def test_unsupported_sort_key_raises_value_error(self, db_session):
        """Grouped mode deliberately REFUSES sort keys outside the group key
        (plan ruling: 'others 400') instead of the flat list's permissive
        fallback — a silent fallback would page groups under an order the
        caller didn't ask for."""
        await _spool(db_session)
        with pytest.raises(ValueError):
            await _groups(db_session, sort_by="last_used_time_asc")
        with pytest.raises(ValueError):
            await _groups(db_session, sort_by="material")  # malformed: no _asc/_desc
        # And the route-facing validator agrees (same single source of truth).
        with pytest.raises(ValueError):
            inventory_service.assert_group_sort_supported("location_desc")
        inventory_service.assert_group_sort_supported(None)
        inventory_service.assert_group_sort_supported("display_name_desc")


class TestGroupedModeRoute:
    async def test_grouped_envelope_shape_and_meta_total_counts_groups(self, async_client, db_session):
        for _ in range(3):
            await _spool(db_session)
        await _spool(db_session, material="PETG")

        resp = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "group_similar": "true"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"items", "meta"}
        assert body["meta"]["total"] == 2

        by_material = {item["material"]: item for item in body["items"]}
        group = by_material["PLA"]
        assert group["group_count"] == 3
        assert len(group["ids"]) == 3
        assert group["subtype"] == ""  # coalesced key value, seeded NULL
        assert group["label_weight"] == 1000
        assert group["lot"] is None
        # Representative rides the slim list projection: k_profile_count,
        # and k_profiles null unless the include_k_profiles opt-in (task 4)
        # asks for the nested array.
        assert group["representative"]["id"] == min(group["ids"])
        assert "k_profile_count" in group["representative"]
        assert group["representative"]["k_profiles"] is None

    async def test_grouped_mode_rides_the_same_filters(self, async_client, db_session):
        pla = await _spool(db_session, material="PLA")
        await _spool(db_session, material="PETG")

        resp = await async_client.get(
            "/api/v1/inventory/spools", params={"page": 1, "group_similar": "true", "material": "PLA"}
        )
        body = resp.json()
        assert body["meta"]["total"] == 1
        assert [i["ids"] for i in body["items"]] == [[pla.id]]

    async def test_grouped_paging_slices_groups(self, async_client, db_session):
        for lot in range(1, 4):  # 3 groups x 2 members
            await _spool(db_session, lot=lot)
            await _spool(db_session, lot=lot)

        resp = await async_client.get(
            "/api/v1/inventory/spools", params={"page": 2, "per_page": 2, "group_similar": "true"}
        )
        body = resp.json()
        assert body["meta"]["total"] == 3
        assert body["meta"]["last_page"] == 2
        assert len(body["items"]) == 1
        assert body["items"][0]["group_count"] == 2

    async def test_grouped_all_true_returns_every_group(self, async_client, db_session):
        for lot in range(1, 4):
            await _spool(db_session, lot=lot)

        resp = await async_client.get(
            "/api/v1/inventory/spools", params={"page": 1, "all": "true", "group_similar": "true"}
        )
        body = resp.json()
        assert len(body["items"]) == 3
        assert body["meta"]["last_page"] == 1
        # Group rows, not flat spools — the escape hatch stays in grouped mode.
        assert all(item["group_count"] == 1 for item in body["items"])

    async def test_unsupported_sort_is_400_supported_is_200(self, async_client, db_session):
        await _spool(db_session)

        bad = await async_client.get(
            "/api/v1/inventory/spools",
            params={"page": 1, "group_similar": "true", "sort_by": "last_used_time_desc"},
        )
        assert bad.status_code == 400

        for good_sort in ("display_name_asc", "material_desc", "brand_asc", "color_name_desc"):
            ok = await async_client.get(
                "/api/v1/inventory/spools", params={"page": 1, "group_similar": "true", "sort_by": good_sort}
            )
            assert ok.status_code == 200, good_sort

        omitted = await async_client.get("/api/v1/inventory/spools", params={"page": 1, "group_similar": "true"})
        assert omitted.status_code == 200

    async def test_group_similar_without_page_is_400(self, async_client, db_session):
        """Grouped rows only exist in the paged envelope — silently answering
        the legacy flat shape to a caller that asked for groups would be a
        response-shape surprise, so it's refused loudly."""
        await _spool(db_session)
        resp = await async_client.get("/api/v1/inventory/spools", params={"group_similar": "true"})
        assert resp.status_code == 400

        # The bare legacy call itself stays untouched.
        legacy = await async_client.get("/api/v1/inventory/spools")
        assert legacy.status_code == 200
        assert isinstance(legacy.json(), list)

    async def test_grouped_location_id_pattern_still_applies(self, async_client, db_session):
        """T1/T2 carry-over: every surface taking location_id shares
        _LOCATION_ID_PATTERN — the grouped branch is the same endpoint, pin it
        anyway so a future split can't silently drop the 422."""
        await _spool(db_session)
        bad = await async_client.get(
            "/api/v1/inventory/spools", params={"page": 1, "group_similar": "true", "location_id": "abc"}
        )
        assert 400 <= bad.status_code < 500
        ok = await async_client.get(
            "/api/v1/inventory/spools", params={"page": 1, "group_similar": "true", "location_id": "__none__"}
        )
        assert ok.status_code == 200
        # And the 200 really is the grouped shape (the seeded spool has no
        # location, so it survives the __none__ filter as one group).
        assert [item["group_count"] for item in ok.json()["items"]] == [1]


# ── The naming template is part of the search (2026-09-01) ──────────────────
#
# The list shows a name built from a user template, and the browser used to
# search that rendered name character for character. The server-driven rewrite
# degraded ``q`` to the raw columns, so a template mentioning anything else went
# unsearchable while staying perfectly visible. The template is a setting, so
# the server composes it in SQL and matches it like any other column.


async def _set_template(db_session, template: str) -> None:
    db_session.add(Settings(key="spool_display_template", value=template))
    await db_session.commit()


async def test_every_naming_placeholder_has_a_sql_expression():
    """Drift guard. ``spoolName.ts`` draws the name, ``label_context`` prints it
    and ``inventory_service`` searches it — three renderers of one vocabulary. A
    placeholder added to the registry without an expression here would go
    silently unsearchable, which is exactly the failure this whole change fixes.
    """
    from backend.app.services.label_template import NAMING_PLACEHOLDERS

    assert {p.key for p in NAMING_PLACEHOLDERS} == set(inventory_service._display_name_columns())


async def test_the_id_and_the_lot_are_searchable_whatever_the_template_says(db_session):
    """Asked for directly: an operator looks a spool up by the number written on
    it. These are a FLOOR — narrowing the naming template must not cost search
    reach — so the default template, which mentions neither, still finds them."""
    target = await _spool(db_session, brand="SUNLU", lot=7714)
    await _spool(db_session, brand="Polymaker", lot=3)

    assert await _filtered_ids(db_session, q="7714") == [target.id]
    assert await _filtered_ids(db_session, q=str(target.id)) == [target.id]


async def test_a_template_only_field_becomes_searchable(db_session):
    """The bug, in one test: with ``{lot}`` on screen, searching for it found
    nothing, because the server matched columns rather than the name."""
    await _set_template(db_session, "{brand} {material} #{lot}")
    target = await _spool(db_session, brand="SUNLU", material="PETG", lot=42, filament_diameter="2.85")
    await _spool(db_session, brand="SUNLU", material="PETG", lot=None)

    assert await _filtered_ids(db_session, q="#42") == [target.id]


async def test_a_token_may_straddle_the_boundary_between_two_fields(db_session):
    """What the browser search could do and the column search could not.

    ⚠️ Only across a boundary the query itself can contain: the search splits on
    whitespace, so a token never spans a SPACE — it is the template's own
    punctuation that used to be unmatchable. "LU/PET" exists in no column; it
    exists only in the name.
    """
    await _set_template(db_session, "{brand}/{material}")
    target = await _spool(db_session, brand="SUNLU", material="PETG")
    await _spool(db_session, brand="Polymaker", material="PETG")

    assert await _filtered_ids(db_session, q="LU/PET") == [target.id]


async def test_the_literal_text_of_the_template_is_searchable_too(db_session):
    """A template's own words are part of the name the operator reads."""
    await _set_template(db_session, "Shelf {brand} {material}")
    target = await _spool(db_session, brand="SUNLU", material="PETG")

    assert await _filtered_ids(db_session, q="Shelf") == [target.id]


async def test_a_spool_with_empty_fields_is_still_findable(db_session):
    """⚠️ ``NULL || 'x'`` is NULL on both backends — one un-coalesced piece would
    make the whole composed name NULL and the ilike would match NOTHING. A spool
    with no lot must not make every spool unfindable."""
    await _set_template(db_session, "{brand} {material} {lot} {cost_per_kg} {purchase_date}")
    target = await _spool(db_session, brand="SUNLU", material="PETG", lot=None, cost_per_kg=None)

    assert await _filtered_ids(db_session, q="SUNLU") == [target.id]


async def test_an_unknown_placeholder_survives_verbatim_and_is_findable(db_session):
    """The same choice ``label_template.resolve`` and the frontend make: a typo
    shows up as ``{colour_name}`` in the name rather than as a silent gap — so
    searching for it has to find the rows that display it."""
    await _set_template(db_session, "{brand} {colour_name}")
    target = await _spool(db_session, brand="SUNLU")

    assert await _filtered_ids(db_session, q="{colour_name}") == [target.id]


async def test_the_search_still_reaches_the_note_a_template_never_shows(db_session):
    """The raw columns are a floor under the name, not an alternative to it."""
    await _set_template(db_session, "{brand}")
    target = await _spool(db_session, brand="SUNLU", note="kitchen shelf")
    await _spool(db_session, brand="SUNLU", note=None)

    assert await _filtered_ids(db_session, q="kitchen") == [target.id]
