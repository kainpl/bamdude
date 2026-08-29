"""``GET /inventory/stats`` — the Inventory stats bar, computed server-side
(Task 5, 2026-08-29 forecast-server-side).

The behavioral spec is the shipped client memo it replaces, quoted here in
full from ``frontend/src/pages/InventoryPage.tsx`` (``const stats``, :1346-1377
at the time of porting)::

    // Stats calculation (active spools only)
    const stats = useMemo(() => {
      const spools = statsSourceSpools;
      if (!spools) return null;
      let totalWeight = 0;
      let totalConsumed = 0;
      let lowStock = 0;
      let activeCount = 0;
      const byMaterial: Record<string, { count: number; weight: number }> = {};
      for (const s of spools) {
        // "Total Consumed" is the resettable lifetime counter
        // (``weight_used - baseline``). Past consumption of an archived
        // spool is real history and must stay in the running total — so
        // this aggregation happens BEFORE the archived-skip below
        // (#1390 follow-up). Pre-m075 servers don't send the baseline
        // field — ``?? 0`` falls back to the old "raw weight_used" displayed value.
        totalConsumed += Math.max(0, s.weight_used - (s.weight_used_baseline ?? 0));
        if (s.archived_at) continue;
        activeCount++;
        const remaining = Math.max(0, s.label_weight - s.weight_used);
        totalWeight += remaining;
        const pct = s.label_weight > 0 ? (remaining / s.label_weight) * 100 : 0;
        // B.8 — per-spool override falls back to the global setting when NULL.
        const threshold = s.low_stock_threshold_pct ?? lowStockThreshold;
        if (pct < threshold) lowStock++;
        const mat = s.material || 'Unknown';
        if (!byMaterial[mat]) byMaterial[mat] = { count: 0, weight: 0 };
        byMaterial[mat].count++;
        byMaterial[mat].weight += remaining;
      }
      return { totalWeight, totalConsumed, lowStock, byMaterial, totalSpools: activeCount };
    }, [statsSourceSpools, lowStockThreshold]);

ONE field on the response has no counterpart in the memo, and it exists
because the SAME full-table fetch fed a second consumer:

* ``total_spools`` counts EVERY row, archived included — the memo's
  ``totalSpools`` is the active count, served as ``active_spools``. The
  archived-inclusive count is what ``resetableSpoolIds.length`` supplied to the
  "Reset all usage" button's visibility gate and its confirmation count.

``lowStockThreshold`` in the memo is ``settings?.low_stock_threshold ?? 20``
(``InventoryPage.tsx:1323``) — the same global the server resolves through
``usage_tracker._global_low_stock_threshold``, so the card, the ``lowstock``
list filter and the ``filament_low`` notification cannot disagree. That
"cannot" is pinned by :class:`TestLowStockAgreesWithTheListFilter` below, not
merely argued: the two suites otherwise mirror each other case-for-case
without ever meeting over one farm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.services import inventory_service

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc)
URL = "/api/v1/inventory/stats"


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


async def _spool(db, **kwargs) -> Spool:
    defaults = {
        "material": "PLA",
        "brand": "Bambu",
        "color_name": "Black",
        "label_weight": 1000,
        "weight_used": 0.0,
        "weight_used_baseline": 0.0,
    }
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _set_global_threshold(db, pct: float) -> None:
    db.add(Settings(key="low_stock_threshold", value=str(pct)))
    await db.commit()


async def _filtered_ids(db, **kwargs) -> list[int]:
    """The ids the LIST filter returns — the `test_inventory_list_server_driven`
    helper, borrowed so the card and the filter can be asked the same question
    over one farm (see :class:`TestLowStockAgreesWithTheListFilter`)."""
    filters = await inventory_service.build_spool_filters(db, **kwargs)
    spools = await inventory_service.list_spools(db, filters=filters, limit=None)
    return [s.id for s in spools]


async def _limited_token(username: str, permissions: list[str]) -> str:
    """A user holding exactly ``permissions`` via a bespoke group; ``[]`` mints
    a user with no groups at all (the ``test_forecast_endpoints`` idiom)."""
    from sqlalchemy import text

    from backend.app.core.auth import create_access_token, get_password_hash
    from backend.app.core.database import async_session
    from backend.app.models.group import Group
    from backend.app.models.user import User

    async with async_session() as db:
        user = User(username=username, password_hash=get_password_hash("TestPass123!"), role="user")
        db.add(user)
        if permissions:
            group = Group(name=f"{username}-group", permissions=permissions)
            db.add(group)
        await db.commit()
        await db.refresh(user)
        if permissions:
            await db.refresh(group)
            await db.execute(
                text("INSERT INTO user_groups (user_id, group_id) VALUES (:uid, :gid)"),
                {"uid": user.id, "gid": group.id},
            )
            await db.commit()

    return create_access_token(data={"sub": username})


# ── The discriminating farm ──────────────────────────────────────────────────
#
# Every aggregate lands on its own number, so a response that crossed two of
# them cannot pass:
#
#   spool          state     label  used  baseline  remaining  consumed  pct
#   PLA  #1        live       1000   100         0        900       100   90  ← not low
#   PLA  #2        live       1000   950         0         50       950    5  ← LOW (global 20)
#   PETG #1        live        500   100         0        400       100   80  ← not low
#   ABS  #1        ARCHIVED   1000   800       300          -        500    -  ← consumption only
#
#   total_spools 4 · active_spools 3 · total_weight_g 1350 · total_consumed_g 1650
#   low_stock_count 1 · by_material PLA{2, 950} then PETG{1, 400}


async def _seed_farm(db) -> None:
    await _spool(db, material="PLA", weight_used=100.0)
    await _spool(db, material="PLA", weight_used=950.0)
    await _spool(db, material="PETG", label_weight=500, weight_used=100.0)
    await _spool(
        db,
        material="ABS",
        weight_used=800.0,
        weight_used_baseline=300.0,
        archived_at=_naive(NOW - timedelta(days=2)),
    )


class TestInventoryStatsTotals:
    async def test_the_farm_totals_match_the_memo(self, async_client: AsyncClient, db_session):
        await _seed_farm(db_session)

        rsp = await async_client.get(URL)
        assert rsp.status_code == 200, rsp.text
        body = rsp.json()

        assert body["total_spools"] == 4, "every row, archived included — the Reset-all target count"
        assert body["active_spools"] == 3, "the memo's totalSpools: activeCount"
        assert body["total_weight_g"] == pytest.approx(1350.0), "900 + 50 + 400, live only"
        assert body["total_consumed_g"] == pytest.approx(1650.0), "100 + 950 + 100 + 500 — archived history survives"
        assert body["low_stock_count"] == 1

    async def test_an_empty_inventory_is_zeros_not_nulls(self, async_client: AsyncClient):
        rsp = await async_client.get(URL)
        assert rsp.status_code == 200
        body = rsp.json()
        assert body == {
            "total_spools": 0,
            "active_spools": 0,
            "total_weight_g": 0.0,
            "total_consumed_g": 0.0,
            "by_material": [],
            "low_stock_count": 0,
        }

    async def test_an_over_reset_spool_contributes_zero_consumption_not_a_negative(
        self, async_client: AsyncClient, db_session
    ):
        """``Math.max(0, weight_used - baseline)`` is applied PER SPOOL — a
        baseline above the counter (a reset, then AMS sync corrected the weight
        down) must not eat a healthy spool's grams out of the running total."""
        await _spool(db_session, weight_used=100.0, weight_used_baseline=500.0)
        await _spool(db_session, weight_used=400.0, weight_used_baseline=0.0)

        rsp = await async_client.get(URL)
        assert rsp.json()["total_consumed_g"] == pytest.approx(400.0)

    async def test_remaining_clamps_at_zero_per_spool(self, async_client: AsyncClient, db_session):
        """``Math.max(0, label_weight - weight_used)`` — an over-consumed spool
        reads as empty, never as negative stock."""
        await _spool(db_session, label_weight=1000, weight_used=1400.0)
        await _spool(db_session, label_weight=1000, weight_used=200.0)

        rsp = await async_client.get(URL)
        assert rsp.json()["total_weight_g"] == pytest.approx(800.0)


class TestInventoryStatsByMaterial:
    async def test_buckets_count_and_weigh_live_spools_only(self, async_client: AsyncClient, db_session):
        await _seed_farm(db_session)

        buckets = {b["material"]: b for b in (await async_client.get(URL)).json()["by_material"]}
        assert set(buckets) == {"PLA", "PETG"}, "the archived ABS spool has no bucket at all"
        assert buckets["PLA"] == {"material": "PLA", "count": 2, "remaining_g": pytest.approx(950.0)}
        assert buckets["PETG"] == {"material": "PETG", "count": 1, "remaining_g": pytest.approx(400.0)}

    async def test_buckets_are_ordered_by_remaining_weight_descending(self, async_client: AsyncClient, db_session):
        """The client sorted them at render (``topMaterials``:
        ``.sort((a, b) => b[1].weight - a[1].weight)``) — served pre-sorted so
        the chip row keeps its order with no second owner of the rule."""
        await _spool(db_session, material="PLA", weight_used=900.0)  # 100 g
        await _spool(db_session, material="PETG", weight_used=0.0)  # 1000 g
        await _spool(db_session, material="ABS", weight_used=500.0)  # 500 g

        materials = [b["material"] for b in (await async_client.get(URL)).json()["by_material"]]
        assert materials == ["PETG", "ABS", "PLA"]

    async def test_a_blank_material_becomes_the_unknown_bucket(self, async_client: AsyncClient, db_session):
        """``const mat = s.material || 'Unknown'`` — a falsy material is a
        labelled bucket, never an empty chip."""
        await _spool(db_session, material="", weight_used=200.0)

        buckets = (await async_client.get(URL)).json()["by_material"]
        assert buckets == [{"material": "Unknown", "count": 1, "remaining_g": pytest.approx(800.0)}]


class TestInventoryStatsLowStock:
    async def test_the_global_setting_drives_the_count(self, async_client: AsyncClient, db_session):
        await _set_global_threshold(db_session, 50.0)
        await _spool(db_session, weight_used=550.0)  # 45% — low at 50
        await _spool(db_session, weight_used=400.0)  # 60% — not low

        assert (await async_client.get(URL)).json()["low_stock_count"] == 1

    async def test_the_default_global_threshold_is_twenty_percent(self, async_client: AsyncClient, db_session):
        """No ``low_stock_threshold`` row — the client's
        ``settings?.low_stock_threshold ?? 20`` default must hold server-side."""
        await _spool(db_session, weight_used=850.0)  # 15% — low at 20
        await _spool(db_session, weight_used=700.0)  # 30% — not low

        assert (await async_client.get(URL)).json()["low_stock_count"] == 1

    async def test_a_per_spool_threshold_can_make_a_spool_low_that_the_global_would_not(
        self, async_client: AsyncClient, db_session
    ):
        """Coalesce, direction 1: override ABOVE the global."""
        await _set_global_threshold(db_session, 20.0)
        await _spool(db_session, weight_used=700.0, low_stock_threshold_pct=50)  # 30% < 50

        assert (await async_client.get(URL)).json()["low_stock_count"] == 1

    async def test_a_per_spool_threshold_can_keep_a_spool_out_that_the_global_would_flag(
        self, async_client: AsyncClient, db_session
    ):
        """Coalesce, direction 2: override BELOW the global. Same spool, same
        global — only the override moves, and the count moves with it."""
        await _set_global_threshold(db_session, 20.0)
        await _spool(db_session, weight_used=900.0, low_stock_threshold_pct=5)  # 10% >= 5

        assert (await async_client.get(URL)).json()["low_stock_count"] == 0

    async def test_a_null_override_falls_back_to_the_global(self, async_client: AsyncClient, db_session):
        """``s.low_stock_threshold_pct ?? lowStockThreshold`` — NULL is not 0."""
        await _set_global_threshold(db_session, 40.0)
        await _spool(db_session, weight_used=700.0, low_stock_threshold_pct=None)  # 30% < 40

        assert (await async_client.get(URL)).json()["low_stock_count"] == 1

    async def test_the_comparison_is_strict_so_exactly_at_the_threshold_is_not_low(
        self, async_client: AsyncClient, db_session
    ):
        """``if (pct < threshold)`` — a spool sitting exactly on the line is
        NOT low (shared with the ``lowstock`` list filter and the
        ``filament_low`` notification)."""
        await _set_global_threshold(db_session, 20.0)
        await _spool(db_session, weight_used=800.0)  # exactly 20%

        assert (await async_client.get(URL)).json()["low_stock_count"] == 0

    async def test_a_zero_label_weight_spool_reads_as_zero_percent_and_counts_as_low(
        self, async_client: AsyncClient, db_session
    ):
        """``label_weight > 0 ? … : 0`` — an unlabelled spool has 0% remaining
        by the memo's own definition, and 0 is below every legal threshold."""
        await _spool(db_session, label_weight=0, weight_used=0.0)

        assert (await async_client.get(URL)).json()["low_stock_count"] == 1

    async def test_archived_spools_are_never_low_stock(self, async_client: AsyncClient, db_session):
        """``if (s.archived_at) continue`` sits ABOVE the low-stock test — a
        retired empty spool must not keep the warning card lit forever."""
        await _spool(db_session, weight_used=990.0, archived_at=_naive(NOW - timedelta(days=1)))

        body = (await async_client.get(URL)).json()
        assert body["low_stock_count"] == 0
        assert body["total_spools"] == 1, "it is still counted as a row"


class TestLowStockAgreesWithTheListFilter:
    """The card and the ``usage=lowstock`` filter beside it, over ONE farm.

    Both resolve the threshold through the same
    ``usage_tracker._global_low_stock_threshold`` and build the same
    ``coalesce(low_stock_threshold_pct, global)`` comparison against the same
    ``_remaining_pct_expr()`` — one owner, no copy. But the two test suites
    mirror each other case-for-case and never MEET, so a future edit to that
    resolver could keep both green while the number on the card and the rows
    the filter returns disagree by one. This is the meeting point.

    The filter is asked with ``archived="active"`` because that is the card's
    own scope (the memo's ``if (s.archived_at) continue``); comparing against
    an unscoped filter would be comparing two different questions.
    """

    async def test_the_count_and_the_filtered_rows_are_the_same_spools(self, async_client: AsyncClient, db_session):
        await _set_global_threshold(db_session, 20.0)
        # Exactly ON the line — strict ``<`` puts it in NEITHER answer. This is
        # the row a ``<=`` regression would move, and it would move it in only
        # one of the two places if they ever stopped sharing the expression.
        boundary = await _spool(db_session, brand="Boundary", weight_used=800.0)  # 20.0%
        just_under = await _spool(db_session, brand="JustUnder", weight_used=801.0)  # 19.9% → low
        override_low = await _spool(  # 30% remaining, but its own threshold is 50 → low
            db_session, brand="Override", weight_used=700.0, low_stock_threshold_pct=50
        )
        await _spool(db_session, brand="Healthy", weight_used=100.0)  # 90% → not low
        # Archived and nearly empty: outside BOTH answers, for two different
        # reasons (the card live-gates, the filter is asked for the active tab).
        await _spool(db_session, brand="Retired", weight_used=950.0, archived_at=_naive(NOW - timedelta(days=1)))

        served = (await async_client.get(URL)).json()["low_stock_count"]
        filtered = set(await _filtered_ids(db_session, usage="lowstock", archived="active"))

        assert filtered == {just_under.id, override_low.id}
        assert boundary.id not in filtered
        assert served == len(filtered) == 2


class TestInventoryStatsPermissions:
    async def test_inventory_read_opens_the_endpoint(self, async_client: AsyncClient):
        token = await _limited_token("statsreader", ["inventory:read"])
        rsp = await async_client.get(URL, headers={"Authorization": f"Bearer {token}"})
        assert rsp.status_code == 200, rsp.text

    async def test_forecast_read_alone_is_403_because_stats_is_not_a_forecast_surface(self, async_client: AsyncClient):
        """DELIBERATE, not an oversight. Spec §2.4 and the plan's Interfaces
        block both specify ``RequirePermission(INVENTORY_READ)`` here — NOT the
        ``RequireAnyPermission(INVENTORY_READ, INVENTORY_FORECAST_READ)`` pair
        the four forecast endpoints carry. The stats bar is an inventory
        surface that happens to ride along in this cycle.

        The consequence is benign and unchanged from before the endpoint
        existed: such a user's `serverStats` stays undefined, `stats` is null
        and the bar simply does not render — the full-table feed this replaced
        also required ``inventory:read``. Without this case, a later "make the
        forecast endpoints consistent" sweep would widen the permission with no
        test turning red.
        """
        token = await _limited_token("statsforecastonly", ["inventory:forecast_read"])
        rsp = await async_client.get(URL, headers={"Authorization": f"Bearer {token}"})
        assert rsp.status_code == 403, rsp.text

    async def test_no_permission_is_403(self, async_client: AsyncClient):
        token = await _limited_token("statsnoperms", [])
        rsp = await async_client.get(URL, headers={"Authorization": f"Bearer {token}"})
        assert rsp.status_code == 403, rsp.text
