"""The lock protocol under two real sessions (WS-13 E1, spec BL / T1 scenarios a–i).

Run by ``postgres_scenario_runner`` in its own interpreter (``protocol:<name>``) —
against PostgreSQL, and scenario f also against a file-backed SQLite. Every door is
the real route function; the barriers (``lock_barriers``) stop operation A inside
itself, right after a named lock, and the coordinator lets B run to its first lock or
to a wait PostgreSQL reports before both go on. A 40P01, a 500 or a timeout is
reported as such — never folded into "ok".
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select, text


def _runner():
    from backend.tests.integration import postgres_scenario_runner as runner

    return runner


def _install(barriers) -> None:
    """Wrap every lock helper the protocol names, in every module that calls it."""
    from backend.app.api.routes import projects, stock
    from backend.app.services import (
        finished_stock,
        line_intake,
        order_from_files,
        order_fulfilment,
        part_stock,
        product_delete,
        product_gate,
        product_variants,
    )

    for module in (
        product_gate,
        line_intake,
        order_from_files,
        order_fulfilment,
        finished_stock,
        product_delete,
        product_variants,
        projects,
        stock,
    ):
        barriers.wrap(module, "product_gate")
    for name in ("lock_item", "lock_line"):
        barriers.wrap(finished_stock, name)
    barriers.wrap(part_stock, "lock_part")
    barriers.wrap(order_fulfilment, "lock_order")


def _dialect() -> str:
    from backend.app.core.database import engine

    return engine.dialect.name


async def duel(a_call, b_call, *, a_on=None, a_after=1) -> dict:
    """A runs to its barrier (``a_on`` / ``a_after``), B starts; the coordinator waits for B
    at its first lock or waiting on one; both go on. PostgreSQL only (pg_stat_activity)."""
    from backend.app.core.database import async_session, engine
    from backend.tests.integration.lock_barriers import Barriers, backend_pid, first_of

    runner = _runner()
    barriers = Barriers()
    _install(barriers)
    holds: dict[str, object] = {}
    pid_b: asyncio.Future = asyncio.get_running_loop().create_future()

    async def run(name, call, *, on=None, after=1, pid=None):
        async with async_session() as db:
            holds[name] = barriers.hold(db, after=after, on=on)
            if pid is not None:
                pid.set_result(await backend_pid(db))
            return await runner._outcome(db, lambda: call(db))

    which = "a_never_stopped"
    outcome_b = "not_started"
    try:
        task_a = asyncio.create_task(run("a", a_call, on=a_on, after=a_after))
        hold_a = await runner._hold_of(holds, "a")
        stopped = asyncio.ensure_future(hold_a.reached.wait())
        done, _pending = await asyncio.wait({task_a, stopped}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            task_b = asyncio.create_task(run("b", b_call, pid=pid_b))
            which = await first_of(await runner._hold_of(holds, "b"), engine, pid_b)
            barriers.release_all()
            outcome_a, outcome_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=60)
        else:
            stopped.cancel()
            outcome_a = await asyncio.wait_for(task_a, timeout=60)
    finally:
        barriers.restore()
    return {"which": which, "a": outcome_a, "b": outcome_b}


async def serial(a_call, b_call) -> dict:
    """A runs to its end without committing; B starts and must wait (SQLite: its write
    lock; PostgreSQL: the gate); A commits; B finishes. The freshness recipe (spec T1 4)."""
    from backend.app.core.database import async_session

    runner = _runner()
    async with async_session() as a:
        outcome_a = "ok"
        try:
            await a_call(a)
        except Exception as e:  # noqa: BLE001 — reported, not swallowed
            outcome_a = f"error:{type(e).__name__}:{str(e)[:200]}"
        b_task = None

        async def run_b():
            async with async_session() as b:
                return await runner._outcome(b, lambda: b_call(b))

        b_task = asyncio.create_task(run_b())
        await asyncio.sleep(0.4)
        b_waited = not b_task.done()
        await a.commit()
    outcome_b = await asyncio.wait_for(b_task, timeout=60)
    return {"a": outcome_a, "b": outcome_b, "b_waited": b_waited}


# ---------- setup ----------


async def shop(*, customer: bool = True, group: bool = True, line_quantity: int = 3, kits: int = 0) -> dict:
    """A lamp with two printed parts (shade, base) and a Colour group (red standard,
    blue); an active order with one line of it; free parts on the shelf."""
    from backend.app.core.database import async_session
    from backend.app.models.customer import Customer
    from backend.app.models.product import Product, ProductPart
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import line_config, part_stock, product_variants

    async with async_session() as db:
        buyer = Customer(name="Protocol customer") if customer else None
        lamp = Product(name="Protocol lamp")
        db.add_all([x for x in (buyer, lamp) if x is not None])
        await db.flush()
        shade = ProductPart(product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1)
        base = ProductPart(product_id=lamp.id, kind="printed", name="base", name_key="base", qty_per_unit=1)
        db.add_all([shade, base])
        await db.flush()
        options: dict[str, int] = {}
        group_id = None
        if group:
            # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
            from backend.tests.fixtures.sliced_sources import slice_parts

            await slice_parts(db, lamp.id, [shade, base])
            colour = await product_variants.create_group(db, lamp.id, "Colour", ["red", "blue"], record_lines=False)
            group_id = colour.id
            for option in await product_variants.groups(db, lamp.id):
                options.update({o.name: o.id for o in option.options})
        for part in (shade, base):
            await part_stock.move(db, part_id=part.id, delta=10, reason="manual", note="seed")
        order = Project(name="Protocol order", status="active", customer_id=buyer.id if buyer else None)
        db.add(order)
        await db.flush()
        line = ProjectLine(
            project_id=order.id, product_id=lamp.id, quantity=line_quantity, mode="product", sort_order=0
        )
        db.add(line)
        await db.flush()
        await line_config.seed_line(db, line, choices=None, counts=None)
        if kits:
            await part_stock.reserve_for_line(db, line, kits)
        await db.commit()
        return {
            "customer": buyer.id if buyer else None,
            "product": lamp.id,
            "shade": shade.id,
            "base": base.id,
            "group": group_id,
            "options": options,
            "order": order.id,
            "line": line.id,
        }


async def _count(model, *conditions) -> int:
    from backend.app.core.database import async_session

    async with async_session() as db:
        return int(await db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)


async def _line_choices(line_id: int) -> dict[int, int]:
    from backend.app.core.database import async_session
    from backend.app.models.line_config import ProjectLineChoice

    async with async_session() as db:
        rows = await db.execute(
            select(ProjectLineChoice.group_id, ProjectLineChoice.option_id).where(ProjectLineChoice.line_id == line_id)
        )
        return dict(rows.all())


async def _line_key_matches(line_id: int) -> bool:
    from backend.app.core.database import async_session
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import line_config
    from backend.app.services.line_composition import load_line_configs

    async with async_session() as db:
        line = await db.get(ProjectLine, line_id)
        cfg = (await load_line_configs(db, [line_id]))[line_id]
        return line.config_key == line_config.config_key(line.mode, cfg.choices, cfg.counts)


def _req():
    return _runner()._plain_request()


def _allow():
    """The runner's everything-allowed credentials (the doors' second rights)."""
    return _runner().ALLOW


# ---------- a · R07, b — the issue dialog creating the first position ----------


async def scenario_a() -> dict:
    """A assembles one unit (the line's position does not exist yet — the assembly
    creates it) and stops after locking the line; B adds a variant group."""
    from backend.app.api.routes import products as product_routes, projects as project_routes
    from backend.app.models.finished_stock import StockItem, StockItemChoice
    from backend.app.schemas.product import VariantGroupCreate
    from backend.app.schemas.project import FulfilmentIn, FulfilmentLineIn

    s = await shop(kits=2)
    data = FulfilmentIn(lines=[FulfilmentLineIn(line_id=s["line"], assemble=1)])
    result = await duel(
        lambda db: project_routes.fulfil_order(s["order"], data, _req(), db, None, creds=_allow()),
        lambda db: product_routes.create_variant_group(
            s["product"], VariantGroupCreate(name="Size", options=["S", "L"]), db, None
        ),
        a_on="lock_line",
    )
    positions = await _count(StockItem, StockItem.product_id == s["product"])
    size_on_positions = await _count(
        StockItemChoice,
        StockItemChoice.item_id.in_(select(StockItem.id).where(StockItem.product_id == s["product"])),
    )
    choices = await _line_choices(s["line"])
    return {
        **result,
        "positions": positions,
        "position_choices": size_on_positions,
        "line_groups": len(choices),
        "line_key_ok": await _line_key_matches(s["line"]),
        "consistent": await _runner()._position_ledger_consistent(
            await _ids(StockItem, StockItem.product_id == s["product"])
        ),
    }


async def _ids(model, *conditions) -> list[int]:
    from backend.app.core.database import async_session

    async with async_session() as db:
        return list(await db.scalars(select(model.id).where(*conditions)))


async def scenario_b() -> dict:
    """The same assembly against rebinding a part to an option."""
    from backend.app.api.routes import products as product_routes, projects as project_routes
    from backend.app.models.finished_stock import StockItem
    from backend.app.schemas.product import ProductPartUpdate
    from backend.app.schemas.project import FulfilmentIn, FulfilmentLineIn

    s = await shop(kits=2)
    data = FulfilmentIn(lines=[FulfilmentLineIn(line_id=s["line"], assemble=1)])
    result = await duel(
        lambda db: project_routes.fulfil_order(s["order"], data, _req(), db, None, creds=_allow()),
        lambda db: product_routes.update_part(
            s["product"], s["base"], ProductPartUpdate(variant_option_ids=[s["options"]["blue"]]), db, None
        ),
        a_on="lock_line",
    )
    return {
        **result,
        "line_key_ok": await _line_key_matches(s["line"]),
        "consistent": await _runner()._position_ledger_consistent(
            await _ids(StockItem, StockItem.product_id == s["product"])
        ),
    }


# ---------- c · R10 — completing through PATCH against completing in the issue dialog ----------


async def scenario_c() -> dict:
    from backend.app.api.routes import projects as project_routes
    from backend.app.core.database import async_session
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.schemas.project import FulfilmentIn, ProjectUpdate
    from backend.app.services import finished_stock

    s = await shop(customer=False, group=False, line_quantity=2)
    async with async_session() as db:
        item = await finished_stock.item_for(db, s["product"], {}, create=True)
        await finished_stock.receive(db, item, 2)
        line = await db.get(ProjectLine, s["line"])
        await finished_stock.reserve_for_line(db, line, 2)
        await db.commit()
    result = await duel(
        lambda db: project_routes.update_project(s["order"], ProjectUpdate(status="completed"), _req(), db, None),
        lambda db: project_routes.fulfil_order(
            s["order"], FulfilmentIn(complete=True), _req(), db, None, creds=_allow()
        ),
        a_on="lock_order",
    )
    async with async_session() as db:
        status = await db.scalar(select(Project.status).where(Project.id == s["order"]))
    return {**result, "status": status}


# ---------- d · R10 — an order copy against a line of the same product ----------


async def scenario_d() -> dict:
    from backend.app.api.routes import projects as project_routes
    from backend.app.core.database import async_session
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.schemas.project import ProjectDuplicate, ProjectLineCreate

    s = await shop()
    async with async_session() as db:
        other = Project(name="Second order", status="active")
        db.add(other)
        await db.commit()
        other_id = other.id
    result = await duel(
        lambda db: project_routes.duplicate_project(s["order"], ProjectDuplicate(), db, None),
        lambda db: project_routes.add_line(
            other_id, ProjectLineCreate(product_id=s["product"], quantity=1), db, None, creds=_allow()
        ),
        a_on="product_gate",
    )
    line_ids = await _ids(ProjectLine, ProjectLine.product_id == s["product"])
    complete = [len(await _line_choices(lid)) == 1 for lid in line_ids]
    return {**result, "lines": len(line_ids), "every_line_has_its_choices": all(complete)}


# ---------- e · R10 — deleting a one-off product's last line against configuring it ----------


async def scenario_e() -> dict:
    from backend.app.api.routes import projects as project_routes
    from backend.app.core.database import async_session
    from backend.app.models.product import Product, ProductOrigin
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.schemas.project import LineConfigurationIn
    from backend.app.services import line_config

    async with async_session() as db:
        oneoff = Product(name="One-off", origin=ProductOrigin.ADHOC_PLATE.value)
        db.add(oneoff)
        await db.flush()
        order = Project(name="One-off order", status="active")
        db.add(order)
        await db.flush()
        line = ProjectLine(project_id=order.id, product_id=oneoff.id, quantity=1, mode="product", sort_order=0)
        db.add(line)
        await db.flush()
        await line_config.seed_line(db, line, choices=None, counts=None)
        await db.commit()
        ids = {"product": oneoff.id, "order": order.id, "line": line.id}
    result = await duel(
        lambda db: project_routes.delete_line(ids["order"], ids["line"], _req(), db, None),
        lambda db: project_routes.configure_line(
            ids["order"], ids["line"], LineConfigurationIn(), db, None, creds=_allow()
        ),
        a_on="product_gate",
    )
    return {**result, "product_left": await _count(Product, Product.id == ids["product"])}


# ---------- f · R08 — freshness of the configuration writers ----------


async def scenario_f(recipe: str) -> dict:
    """Four pairs, each with its outcome. ``recipe`` — ``duel`` (PostgreSQL) or ``serial``."""
    from backend.app.api.routes import products as product_routes, projects as project_routes
    from backend.app.schemas.product import VariantGroupCreate, VariantGroupUpdate
    from backend.app.schemas.project import LineConfigurationIn, ProjectLineCreate

    async def pair(a_call, b_call):
        if recipe == "duel":
            return await duel(a_call, b_call, a_on="product_gate")
        return await serial(a_call, b_call)

    out: dict[str, dict] = {}

    # f1: a group added, then the line reconfigured — the new group's choice survives.
    s = await shop()
    size = {}

    async def add_size(db):
        await product_routes.create_variant_group(
            s["product"], VariantGroupCreate(name="Size", options=["S", "L"]), db, None
        )

    choose_blue = LineConfigurationIn(choices={s["group"]: s["options"]["blue"]})
    r = await pair(
        add_size, lambda db: project_routes.configure_line(s["order"], s["line"], choose_blue, db, None, creds=_allow())
    )
    size = await _group_by_name(s["product"], "Size")
    choices = await _line_choices(s["line"])
    kit_before = await _line_kit(s["line"])
    if size:
        await _set_standard(size["id"], size["options"]["L"], product_routes, VariantGroupUpdate, s["product"])
    out["group_then_configuration"] = {
        **r,
        "has_size": size is not None and size["id"] in choices,
        "kept_blue": choices.get(s["group"]) == s["options"]["blue"],
        "key_ok": await _line_key_matches(s["line"]),
        "kit_unchanged_by_a_new_standard": kit_before == await _line_kit(s["line"]),
    }

    # f2: the line reconfigured, then a group added — the group lands on it.
    s = await shop()
    choose_blue = LineConfigurationIn(choices={s["group"]: s["options"]["blue"]})
    r = await pair(
        lambda db: project_routes.configure_line(s["order"], s["line"], choose_blue, db, None, creds=_allow()),
        lambda db: product_routes.create_variant_group(
            s["product"], VariantGroupCreate(name="Size", options=["S", "L"]), db, None
        ),
    )
    size = await _group_by_name(s["product"], "Size")
    choices = await _line_choices(s["line"])
    out["configuration_then_group"] = {
        **r,
        "has_size": size is not None and size["id"] in choices,
        "kept_blue": choices.get(s["group"]) == s["options"]["blue"],
        "key_ok": await _line_key_matches(s["line"]),
    }

    # f3: a group added against a new line of the same product.
    s = await shop()
    r = await pair(
        lambda db: product_routes.create_variant_group(
            s["product"], VariantGroupCreate(name="Size", options=["S", "L"]), db, None
        ),
        lambda db: project_routes.add_line(
            s["order"], ProjectLineCreate(product_id=s["product"], quantity=1), db, None, creds=_allow()
        ),
    )
    from backend.app.models.project_line import ProjectLine

    size = await _group_by_name(s["product"], "Size")
    new_lines = [lid for lid in await _ids(ProjectLine, ProjectLine.project_id == s["order"]) if lid != s["line"]]
    out["group_then_new_line"] = {
        **r,
        "new_line_has_size": bool(new_lines) and size is not None and size["id"] in await _line_choices(new_lines[0]),
    }

    # f4: an option deleted against a configuration choosing it — both orders.
    s = await shop()
    choose_blue = LineConfigurationIn(choices={s["group"]: s["options"]["blue"]})
    r = await pair(
        lambda db: project_routes.configure_line(s["order"], s["line"], choose_blue, db, None, creds=_allow()),
        lambda db: product_routes.delete_variant_option(s["product"], s["group"], s["options"]["blue"], db, None),
    )
    out["choice_then_delete"] = {**r, "line_keeps_blue": (await _line_choices(s["line"])).get(s["group"])}
    s = await shop()
    choose_blue = LineConfigurationIn(choices={s["group"]: s["options"]["blue"]})
    r = await pair(
        lambda db: product_routes.delete_variant_option(s["product"], s["group"], s["options"]["blue"], db, None),
        lambda db: project_routes.configure_line(s["order"], s["line"], choose_blue, db, None, creds=_allow()),
    )
    out["delete_then_choice"] = {**r, "dangling": await _dangling_choices()}
    out["options"] = {"blue": s["options"]["blue"]}
    return out


async def _group_by_name(product_id: int, name: str) -> dict | None:
    from backend.app.core.database import async_session
    from backend.app.services import product_variants

    async with async_session() as db:
        for group in await product_variants.groups(db, product_id):
            if group.name == name:
                return {"id": group.id, "options": {o.name: o.id for o in group.options}}
    return None


async def _set_standard(group_id, option_id, product_routes, VariantGroupUpdate, product_id) -> None:  # noqa: N803
    from backend.app.core.database import async_session

    async with async_session() as db:
        await product_routes.update_variant_group(
            product_id, group_id, VariantGroupUpdate(default_option_id=option_id), db, None
        )
        await db.commit()


async def _line_kit(line_id: int) -> list[tuple[int, int]]:
    from backend.app.core.database import async_session
    from backend.app.models.product import ProductPart
    from backend.app.models.project_line import ProjectLine
    from backend.app.services.line_composition import line_composition, load_line_configs
    from backend.app.services.line_config import _defaults, load_products

    async with async_session() as db:
        line = await db.get(ProjectLine, line_id)
        product = (await load_products(db, [line.product_id]))[line.product_id]
        cfg = (await load_line_configs(db, [line_id]))[line_id]
        parts = list(await db.scalars(select(ProductPart).where(ProductPart.product_id == line.product_id)))
        return sorted((part.id, per) for part, per in line_composition(parts, line.mode, cfg, _defaults(product)))


async def _dangling_choices() -> int:
    from backend.app.core.database import async_session

    async with async_session() as db:
        return int(
            await db.scalar(
                text(
                    "SELECT count(*) FROM project_line_choices c "
                    "WHERE NOT EXISTS (SELECT 1 FROM product_variant_options o WHERE o.id = c.option_id)"
                )
            )
            or 0
        )


# ---------- g, h · R11 — every class of every footprint, held by another session ----------


async def _hold_rows(sql: str, params: dict):
    """A session holding the rows ``sql`` selects FOR UPDATE; returns (session, rollback)."""
    from backend.app.core.database import async_session

    session = async_session()
    await session.execute(text(sql + " FOR UPDATE"), params)
    return session


async def _busy_case(holder_sql: str, params: dict, door) -> dict:
    from backend.app.core.database import async_session

    holder = await _hold_rows(holder_sql, params)
    try:
        started = time.monotonic()
        async with async_session() as db:
            outcome = await asyncio.wait_for(_runner()._outcome(db, lambda: door(db)), timeout=10)
        elapsed = time.monotonic() - started
    finally:
        await holder.rollback()
        await holder.close()
    return {"outcome": outcome, "seconds": round(elapsed, 2)}


async def _busy_with_session(prepare, door) -> dict:
    """The holder is a real door run without committing (``prepare(session)``)."""
    from backend.app.core.database import async_session

    holder = async_session()
    try:
        await prepare(holder)
        started = time.monotonic()
        async with async_session() as db:
            outcome = await asyncio.wait_for(_runner()._outcome(db, lambda: door(db)), timeout=10)
        elapsed = time.monotonic() - started
    finally:
        await holder.rollback()
        await holder.close()
    return {"outcome": outcome, "seconds": round(elapsed, 2)}


async def scenario_g() -> dict:
    from backend.app.api.routes import products as product_routes
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.customer import Customer
    from backend.app.models.line_config import ProjectLinePartCount
    from backend.app.models.product import Product, ProductPart
    from backend.app.models.project_line import ProjectLine, ProjectLinePartStock, ProjectProcurement
    from backend.app.schemas.product import ProductPartMerge, ProductPartUpdate, VariantGroupCreate
    from backend.app.services import finished_stock, part_stock

    out: dict[str, dict] = {}
    s = await shop()
    async with async_session() as db:
        # A position with a changed count of «base»; a parts-mode line counting «base»;
        # a procurement row; a parts-line counter; ledger rows tied to an archive and a customer.
        item = await finished_stock.item_for(db, s["product"], {}, {s["base"]: 2}, create=True)
        await finished_stock.receive(db, item, 1)
        parts_line = ProjectLine(project_id=s["order"], product_id=s["product"], quantity=1, mode="parts", sort_order=1)
        db.add(parts_line)
        await db.flush()
        db.add(ProjectLinePartCount(line_id=parts_line.id, part_id=s["base"], qty=1))
        db.add(ProjectLinePartStock(line_id=parts_line.id, part_id=s["base"]))
        db.add(ProjectProcurement(project_id=s["order"], product_part_id=s["base"], quantity_acquired=1))
        archive = PrintArchive(filename="a.3mf", file_path="", file_size=0, status="completed")
        db.add(archive)
        await db.flush()
        await part_stock.move(db, part_id=s["base"], delta=1, reason="unfiled_print", archive_id=archive.id)
        buyer = await db.get(Customer, s["customer"])
        await finished_stock.issue(db, item, 1, customer_id=buyer.id)
        await db.commit()
        ids = {"item": item.id, "parts_line": parts_line.id, "archive": archive.id, "customer": buyer.id}

    add_group = lambda db: product_routes.create_variant_group(  # noqa: E731
        s["product"], VariantGroupCreate(name="Size", options=["S", "L"]), db, None
    )
    rebind = lambda db: product_routes.update_part(  # noqa: E731
        s["product"], s["shade"], ProductPartUpdate(variant_option_ids=[s["options"]["blue"]]), db, None
    )
    delete_base = lambda db: product_routes.delete_part(s["product"], s["base"], db, None, creds=_allow())  # noqa: E731
    merge_base_into_shade = lambda db: product_routes.merge_part(  # noqa: E731
        s["product"], s["shade"], ProductPartMerge(source_part_id=s["base"]), db, None, creds=_allow()
    )

    for door_name, door in (("add_group", add_group), ("rebind", rebind)):
        out[f"{door_name}/stock_items"] = await _busy_case(
            "SELECT id FROM stock_items WHERE id = :id", {"id": ids["item"]}, door
        )
        out[f"{door_name}/project_lines"] = await _busy_case(
            "SELECT id FROM project_lines WHERE id = :id", {"id": s["line"]}, door
        )
        out[f"{door_name}/product_parts"] = await _busy_case(
            "SELECT id FROM product_parts WHERE id = :id", {"id": s["base"]}, door
        )
    for door_name, door in (("delete_part", delete_base), ("merge/source", merge_base_into_shade)):
        out[f"{door_name}/stock_items"] = await _busy_case(
            "SELECT id FROM stock_items WHERE id = :id", {"id": ids["item"]}, door
        )
        out[f"{door_name}/parts_line"] = await _busy_case(
            "SELECT id FROM project_lines WHERE id = :id", {"id": ids["parts_line"]}, door
        )
        out[f"{door_name}/product_parts"] = await _busy_case(
            "SELECT id FROM product_parts WHERE id = :id", {"id": s["base"]}, door
        )
        out[f"{door_name}/procurement"] = await _busy_case(
            "SELECT project_id FROM project_procurement WHERE product_part_id = :id", {"id": s["base"]}, door
        )
        out[f"{door_name}/parts_line_counter"] = await _busy_case(
            "SELECT line_id FROM project_line_part_stock WHERE part_id = :id", {"id": s["base"]}, door
        )

        async def detach(db, archive_id=ids["archive"]):
            await part_stock.detach_archive(db, archive_id)

        out[f"{door_name}/ledger_by_detach_archive"] = await _busy_with_session(detach, door)
    out["merge/target"] = await _busy_case(
        "SELECT id FROM product_parts WHERE id = :id", {"id": s["shade"]}, merge_base_into_shade
    )

    # The product delete: a product of its own with every footprint class present.
    async with async_session() as db:
        doomed = Product(name="Doomed")
        db.add(doomed)
        await db.flush()
        part = ProductPart(product_id=doomed.id, kind="printed", name="p", name_key="p", qty_per_unit=1)
        db.add(part)
        await db.flush()
        await part_stock.move(db, part_id=part.id, delta=1, reason="manual", note="seed")
        position = await finished_stock.item_for(db, doomed.id, {}, create=True)
        await finished_stock.receive(db, position, 1)
        await finished_stock.issue(db, position, 1, customer_id=ids["customer"])
        await db.commit()
        doomed_ids = {"product": doomed.id, "part": part.id, "position": position.id}
    delete_doomed = lambda db: product_routes.delete_product(doomed_ids["product"], db, None, creds=_allow())  # noqa: E731
    out["delete_product/stock_items"] = await _busy_case(
        "SELECT id FROM stock_items WHERE id = :id", {"id": doomed_ids["position"]}, delete_doomed
    )
    out["delete_product/product_parts"] = await _busy_case(
        "SELECT id FROM product_parts WHERE id = :id", {"id": doomed_ids["part"]}, delete_doomed
    )
    out["delete_product/part_ledger"] = await _busy_case(
        "SELECT id FROM product_part_stock_movements WHERE product_part_id = :id",
        {"id": doomed_ids["part"]},
        delete_doomed,
    )

    async def detach_customer(db, customer_id=ids["customer"]):
        await finished_stock.detach_customer(db, customer_id)

    out["delete_product/ledger_by_detach_customer"] = await _busy_with_session(detach_customer, delete_doomed)
    out["delete_product/product_row_key_share"] = await _busy_case(
        "SELECT id FROM products WHERE id = :id FOR KEY SHARE --", {"id": doomed_ids["product"]}, delete_doomed
    )
    # Nothing was written by any refused door.
    out["after"] = {
        "groups": len(await _groups_of(s["product"])),
        "base_exists": await _count(ProductPart, ProductPart.id == s["base"]),
        "doomed_exists": await _count(Product, Product.id == doomed_ids["product"]),
    }
    return out


async def _groups_of(product_id: int) -> list:
    from backend.app.core.database import async_session
    from backend.app.services import product_variants

    async with async_session() as db:
        return await product_variants.groups(db, product_id)


# ---------- i · savepoints on PostgreSQL ----------


async def scenario_i() -> dict:
    from backend.app.api.routes import projects as project_routes
    from backend.app.core.database import async_session, engine
    from backend.app.models.archive import PrintArchive
    from backend.app.models.part_stock import ProductPartStockMovement
    from backend.app.models.product import Product, ProductOrigin, ProductPart
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import finished_stock, line_config, part_stock
    from backend.app.services.product_gate import product_gate
    from backend.tests.integration.lock_barriers import SqlRecorder

    s = await shop(group=False)
    async with async_session() as db:
        item = await finished_stock.item_for(db, s["product"], {}, create=True)
        await db.commit()
        item_id = item.id

    # (1) A lock taken only inside a rolled-back savepoint is released — and re-taken.
    recorder = SqlRecorder(engine.sync_engine)
    released = False
    relocked = False
    try:
        async with async_session() as a:
            await SqlRecorder.tag(a, "a")
            await product_gate(a, [s["product"]])
            try:
                async with a.begin_nested():
                    await finished_stock.lock_item(a, item_id)
                    raise RuntimeError("the savepoint's work fails")
            except RuntimeError:
                pass
            async with async_session() as b:
                try:
                    await b.execute(
                        text("SELECT id FROM stock_items WHERE id = :id FOR UPDATE NOWAIT"), {"id": item_id}
                    )
                    released = True
                finally:
                    await b.rollback()
            before = len(recorder.log.get("a", []))
            await finished_stock.lock_item(a, item_id)
            relocked = any(t == "stock_items" and k.startswith("LOCK") for k, t, _nw in recorder.log["a"][before:])
            await SqlRecorder.untag(a)
            await a.rollback()
    finally:
        recorder.close()

    # (2) The real caller: deleting an order whose second credit fails inside its savepoint.
    async with async_session() as db:
        oneoff = Product(name="Credit one-off", origin=ProductOrigin.ADHOC_PLATE.value)
        db.add(oneoff)
        await db.flush()
        part = ProductPart(product_id=oneoff.id, kind="printed", name="q", name_key="q", qty_per_unit=1)
        db.add(part)
        order = Project(name="Credit order", status="active")
        db.add(order)
        await db.flush()
        line = ProjectLine(project_id=order.id, product_id=oneoff.id, quantity=1, mode="product", sort_order=0)
        db.add(line)
        await db.flush()
        await line_config.seed_line(db, line, choices=None, counts=None)
        first = PrintArchive(filename="1.3mf", file_path="", file_size=0, status="completed", project_id=order.id)
        second = PrintArchive(filename="2.3mf", file_path="", file_size=0, status="completed", project_id=order.id)
        db.add_all([first, second])
        await db.commit()
        ids = {"order": order.id, "product": oneoff.id, "part": part.id, "first": first.id, "second": second.id}

    real_credit = part_stock.credit_unfiled_print

    async def credit(db, archive, *, created_by=None, note=None):
        # A real movement inside the savepoint — on a catalogue part, which outlives the
        # one-off product the cascade deletes; the second one then fails at the DATABASE.
        await part_stock.move(db, part_id=s["shade"], delta=1, reason="manual", archive_id=archive.id, note=note)
        if archive.id == ids["second"]:
            await db.execute(text("SELECT * FROM no_such_table_ws13"))
        return []

    part_stock.credit_unfiled_print = credit
    recorder = SqlRecorder(engine.sync_engine)
    try:
        async with async_session() as db:
            await SqlRecorder.tag(db, "d")
            outcome = await _runner()._outcome(
                db,
                lambda: project_routes.delete_project(
                    project_id=ids["order"], request=_req(), db=db, current_user=None
                ),
            )
            gate_statements = [
                i for i, (k, t, nw) in enumerate(recorder.log["d"]) if t == "products" and k == "LOCK NO KEY UPDATE"
            ]
    finally:
        part_stock.credit_unfiled_print = real_credit
        recorder.close()
    return {
        "released_inside_savepoint": released,
        "relocked_after_restore": relocked,
        "delete": outcome,
        "gate_statements": len(gate_statements),
        "product_left": await _count(Product, Product.id == ids["product"]),
        "first_credit": await _count(ProductPartStockMovement, ProductPartStockMovement.archive_id == ids["first"]),
        "second_credit": await _count(ProductPartStockMovement, ProductPartStockMovement.archive_id == ids["second"]),
    }


SCENARIOS = {
    "a": scenario_a,
    "b": scenario_b,
    "c": scenario_c,
    "d": scenario_d,
    "e": scenario_e,
    "f": lambda: scenario_f("duel" if _dialect() == "postgresql" else "serial"),
    "g": scenario_g,
    "i": scenario_i,
}


# ---------- T2 — the atomic variants draft under two sessions ----------


async def _revision_and_draft(product_id: int) -> tuple[str, list]:
    from backend.app.core.database import async_session
    from backend.app.services import product_variants

    async with async_session() as db:
        state = await product_variants.groups(db, product_id)
        draft = [
            {
                "id": g.id,
                "name": g.name,
                "options": [{"id": o.id, "name": o.name} for o in sorted(g.options, key=lambda o: (o.position, o.id))],
                "default": g.default_option_id,
            }
            for g in state
        ]
        return product_variants.revision(state), draft


def _apply_door(product_id: int, revision: str, draft: list):
    from backend.app.api.routes import products as product_routes
    from backend.app.schemas.product import VariantsApplyIn

    body = VariantsApplyIn(revision=revision, groups=draft)
    return lambda db: product_routes.apply_variants(product_id, body, db, None)


def _with_size(draft: list) -> list:
    return [
        *draft,
        {
            "temp_id": "size",
            "name": "Size",
            "options": [{"temp_id": "s", "name": "S"}, {"temp_id": "l", "name": "L"}],
            "default": "s",
        },
    ]


async def scenario_t2() -> dict:
    from backend.app.api.routes import projects as project_routes
    from backend.app.schemas.project import FulfilmentIn, FulfilmentLineIn, LineConfigurationIn

    out: dict[str, dict] = {}

    # Two applies with the same revision — exactly one wins.
    s = await shop()
    revision, draft = await _revision_and_draft(s["product"])
    mine = [dict(draft[0], name="Hue")]
    theirs = [dict(draft[0], name="Tint")]
    out["apply_vs_apply"] = await duel(
        _apply_door(s["product"], revision, mine), _apply_door(s["product"], revision, theirs), a_on="product_gate"
    )

    # A single route first, then an apply opened before it.
    s = await shop()
    revision, draft = await _revision_and_draft(s["product"])
    from backend.app.api.routes import products as product_routes
    from backend.app.schemas.product import VariantOptionUpdate

    out["route_vs_apply"] = await duel(
        lambda db: product_routes.update_variant_option(
            s["product"], s["group"], s["options"]["blue"], VariantOptionUpdate(name="navy"), db, None
        ),
        _apply_door(s["product"], revision, [dict(draft[0], name="Hue")]),
        a_on="product_gate",
    )

    # An apply adding a group against the issue dialog creating the first position.
    s = await shop(kits=2)
    revision, draft = await _revision_and_draft(s["product"])
    data = FulfilmentIn(lines=[FulfilmentLineIn(line_id=s["line"], assemble=1)])
    result = await duel(
        lambda db: project_routes.fulfil_order(s["order"], data, _req(), db, None, creds=_allow()),
        _apply_door(s["product"], revision, _with_size(draft)),
        a_on="lock_line",
    )
    out["apply_group_vs_first_position"] = {**result, "line_groups": len(await _line_choices(s["line"]))}

    # An apply adding a group against a configuration change of a line.
    s = await shop()
    revision, draft = await _revision_and_draft(s["product"])
    choose_blue = LineConfigurationIn(choices={s["group"]: s["options"]["blue"]})
    result = await duel(
        lambda db: project_routes.configure_line(s["order"], s["line"], choose_blue, db, None, creds=_allow()),
        _apply_door(s["product"], revision, _with_size(draft)),
        a_on="product_gate",
    )
    choices = await _line_choices(s["line"])
    out["apply_group_vs_configuration"] = {
        **result,
        "line_groups": len(choices),
        "kept_blue": choices.get(s["group"]) == s["options"]["blue"],
        "key_ok": await _line_key_matches(s["line"]),
    }
    return out


SCENARIOS["t2"] = scenario_t2


async def scenario_journal() -> dict:
    """T9 on PostgreSQL: the paged journal's ``UNION ALL`` of both books compiles, and its
    order and total are the cursor's — across a page boundary inside one instant."""
    from datetime import datetime

    from sqlalchemy import update

    from backend.app.core.database import async_session
    from backend.app.models.finished_stock import StockItemMovement
    from backend.app.models.part_stock import ProductPartStockMovement
    from backend.app.services import finished_stock, part_stock, stock_journal

    s = await shop(group=False)
    async with async_session() as db:
        item = await finished_stock.item_for(db, s["product"], {}, create=True)
        for _ in range(7):
            await finished_stock.receive(db, item, 1)
            await part_stock.move(db, part_id=s["shade"], delta=1, reason="manual", note="x")
        same = datetime(2026, 9, 27, 10, 0, 0, 123456)
        await db.execute(update(StockItemMovement).values(created_at=same))
        await db.execute(update(ProductPartStockMovement).values(created_at=same))
        await db.commit()

    async def by_pages(ascending: bool) -> tuple[list, int]:
        seen, page = [], 1
        while True:
            async with async_session() as db:
                body = await stock_journal.journal_page(db, page=page, per_page=5, ascending=ascending)
            seen += [(r.book, r.id) for r in body.items]
            if page >= body.meta.last_page:
                return seen, body.meta.total
            page += 1

    by_cursor, cursor = [], None
    while True:
        async with async_session() as db:
            body = await stock_journal.journal(db, cursor=cursor, limit=5)
        by_cursor += [(r.book, r.id) for r in body.items]
        cursor = body.next_cursor
        if not cursor:
            break
    newest, total = await by_pages(False)
    oldest, _ = await by_pages(True)
    async with async_session() as db:
        parts_only = await stock_journal.journal_page(db, book="parts", page=1, per_page=50)
        products = await stock_journal.journal_products(db, "both")
    return {
        "same_as_cursor": newest == by_cursor,
        "rows": len(newest),
        "unique": len(set(newest)),
        "total": total,
        "asc_is_reversed": oldest == list(reversed(newest)),
        "parts_total": parts_only.meta.total,
        "products": [p.name for p in products],
    }


SCENARIOS["journal"] = scenario_journal


# ---------- implementation review round 1 (Codex): stale reads behind the gates ----------
#
# Not deadlock tests: session A is stopped BEFORE its read behind the gate (or before
# the GET's second step), session B — the real writer, its own session — commits, and
# A goes on. What A writes or answers must describe the state B left.


async def _stale_revision() -> dict:
    from fastapi import HTTPException

    from backend.app.api.routes import products as product_routes
    from backend.app.core.database import async_session
    from backend.app.models.product import Product
    from backend.app.schemas.product import VariantsApplyIn
    from backend.app.services import product_variants

    async with async_session() as db:
        product = Product(name="Stale revision")
        db.add(product)
        await db.commit()
        pid = product.id
    real = product_routes._variant_groups_out
    injected = []

    async def meanwhile(db, *args):
        result = await real(db, *args)
        if not injected:
            injected.append(True)
            async with async_session() as other:
                await product_variants.create_group(other, pid, "Unseen", ["default"], record_lines=False)
                await other.commit()
        return result

    product_routes._variant_groups_out = meanwhile
    try:
        async with async_session() as db:
            opened = await product_routes.get_product(pid, db=db, _=None)
    finally:
        product_routes._variant_groups_out = real
    outcome = "ok"
    async with async_session() as db:
        try:
            await product_routes.apply_variants(
                pid, VariantsApplyIn(revision=opened.variants_revision, groups=[]), db=db, _=None
            )
            await db.commit()
        except HTTPException as e:
            await db.rollback()
            outcome = (
                f"http:{e.status_code}:{(e.detail or {}).get('error') if isinstance(e.detail, dict) else e.detail}"
            )
    async with async_session() as db:
        left = len(await product_variants.groups(db, pid))
    return {"groups_seen": len(opened.variant_groups), "outcome": outcome, "groups_left": left}


async def _stale_binding() -> dict:
    from sqlalchemy import insert

    from backend.app.api.routes import products as product_routes
    from backend.app.core.database import async_session
    from backend.app.models.product import ProductPart
    from backend.app.models.product_variant import ProductPartOption
    from backend.app.models.project_line import ProjectLine
    from backend.app.schemas.product import ProductPartUpdate
    from backend.app.services import line_composition, product_gate as gate_module

    s = await shop(customer=False)
    red, blue = s["options"]["red"], s["options"]["blue"]
    async with async_session() as db:
        await db.execute(
            insert(ProductPartOption.__table__).values(part_id=s["shade"], option_id=red, group_id=s["group"])
        )
        await db.commit()

    async def kit_of_line() -> dict[int, int]:
        async with async_session() as db:
            line = await db.get(ProjectLine, s["line"])
            parts = list(await db.scalars(select(ProductPart).where(ProductPart.product_id == s["product"])))
            comp = await line_composition.compositions_for_lines(db, [line], {s["product"]: parts})
            return {part.id: per for part, per in comp[line.id]}

    before = await kit_of_line()
    real = gate_module.product_gate
    injected = []

    async def meanwhile(db, ids):
        if not injected:
            injected.append(True)
            async with async_session() as other:
                await product_routes.update_part(
                    s["product"], s["shade"], ProductPartUpdate(variant_option_ids=[blue]), db=other, _=None
                )
                await other.commit()
        return await real(db, ids)

    gate_module.product_gate = meanwhile
    try:
        async with async_session() as db:
            result = await product_routes.update_part(
                s["product"], s["shade"], ProductPartUpdate(variant_option_ids=[red]), db=db, _=None
            )
            await db.commit()
    finally:
        gate_module.product_gate = real
    async with async_session() as db:
        stored = list(
            await db.scalars(select(ProductPartOption.option_id).where(ProductPartOption.part_id == s["shade"]))
        )
    return {
        "requested": [red],
        "answered": result.variant_option_ids,
        "stored": stored,
        "kit_before": before,
        "kit_after": await kit_of_line(),
    }


async def _stale_customer(to_none: bool) -> dict:
    from backend.app.api.routes import projects as project_routes
    from backend.app.core.database import async_session
    from backend.app.models.customer import Customer
    from backend.app.models.finished_stock import StockItemMovement
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.models.stock_issue import StockIssue
    from backend.app.schemas.project import ProjectUpdate
    from backend.app.services import finished_stock, order_fulfilment
    from backend.app.services.stock_issues import Recipient

    s = await shop(group=False)
    async with async_session() as db:
        other = Customer(name="Protocol customer 2")
        db.add(other)
        await db.commit()
        target = None if to_none else other.id
    async with async_session() as db:
        item = await finished_stock.item_for(db, s["product"], {}, create=True)
        await finished_stock.receive(db, item, 2)
        await db.commit()
    async with async_session() as db:
        line = await db.get(ProjectLine, s["line"])
        await finished_stock.reserve_for_line(db, line, 2)
        await db.commit()
    movements = await _count(StockItemMovement)
    real = order_fulfilment.lock_order
    injected = []

    async def meanwhile(db, project_id):
        if not injected:
            injected.append(True)
            async with async_session() as other_db:
                await project_routes.update_project(
                    project_id,
                    ProjectUpdate(customer_id=target, contact_id=None),
                    _req(),
                    db=other_db,
                    current_user=None,
                )
                await other_db.commit()
        return await real(db, project_id)

    order_fulfilment.lock_order = meanwhile
    outcome = "ok"
    try:
        async with async_session() as db:
            project = await db.get(Project, s["order"])
            try:
                await order_fulfilment.apply(
                    db,
                    project,
                    [order_fulfilment.LineRequest(line_id=s["line"], issue=1)],
                    recipient=Recipient(name="Typed by hand"),
                    waybill=None,
                    note=None,
                    complete=False,
                    actor=None,
                )
                await db.commit()
            except order_fulfilment.FulfilmentError as e:
                await db.rollback()
                outcome = f"refused:{e.status}:{e}"
    finally:
        order_fulfilment.lock_order = real
    async with async_session() as db:
        current = await db.scalar(select(Project.customer_id).where(Project.id == s["order"]))
    return {
        "outcome": outcome,
        "issues": await _count(StockIssue),
        "movements_moved": await _count(StockItemMovement) - movements,
        "customer_now": current,
        "customer_wanted": target,
    }


async def scenario_stale_reads() -> dict:
    return {
        "revision": await _stale_revision(),
        "binding": await _stale_binding(),
        "customer": await _stale_customer(to_none=False),
        "customer_removed": await _stale_customer(to_none=True),
    }


SCENARIOS["stale_reads"] = scenario_stale_reads


# ---------- WS-13 E10 R10 — a part born bound against deleting its option ----------
#
# The product gate WAITS (FOR NO KEY UPDATE, no NOWAIT): whichever door takes it first
# finishes, and the other — behind the gate — reads the state the first one left.


async def _part_named(product_id: int, name: str) -> dict | None:
    from backend.app.core.database import async_session
    from backend.app.models.product import ProductPart

    async with async_session() as db:
        part = await db.scalar(
            select(ProductPart).where(ProductPart.product_id == product_id, ProductPart.name == name)
        )
        if part is None:
            return None
        return {
            "variant_option_ids": [link.option_id for link in part.option_links],
            "aliases": list(part.aliases or []),
        }


async def _option_exists(option_id: int) -> bool:
    from backend.app.models.product_variant import ProductVariantOption

    return await _count(ProductVariantOption, ProductVariantOption.id == option_id) == 1


async def scenario_bound_part_first() -> dict:
    """A creates a part bound to «blue» and stops right after taking the gate; B deletes
    «blue» and must wait; A commits — B, behind the gate, sees the bound part and refuses."""
    from backend.app.api.routes import products as product_routes
    from backend.app.schemas.product import ProductPartCreate

    s = await shop()
    blue = s["options"]["blue"]
    data = ProductPartCreate(kind="printed", name="Blue shade", aliases=["blue_shade_v2"], variant_option_ids=[blue])
    result = await duel(
        lambda db: product_routes.create_part(s["product"], data, db, None),
        lambda db: product_routes.delete_variant_option(s["product"], s["group"], blue, db, None),
        a_on="product_gate",
    )
    return {
        **result,
        "part": await _part_named(s["product"], "Blue shade"),
        "option_exists": await _option_exists(blue),
    }


async def scenario_option_delete_first() -> dict:
    """A deletes «blue» and stops right after taking the gate; B creates a part bound to
    it and must wait; A commits — B, behind the gate, finds no such option: 422, and no
    part (nor its aliases) is written."""
    from backend.app.api.routes import products as product_routes
    from backend.app.schemas.product import ProductPartCreate

    s = await shop()
    blue = s["options"]["blue"]
    data = ProductPartCreate(kind="printed", name="Blue shade", aliases=["blue_shade_v2"], variant_option_ids=[blue])
    result = await duel(
        lambda db: product_routes.delete_variant_option(s["product"], s["group"], blue, db, None),
        lambda db: product_routes.create_part(s["product"], data, db, None),
        a_on="product_gate",
    )
    return {
        **result,
        "part": await _part_named(s["product"], "Blue shade"),
        "option_exists": await _option_exists(blue),
    }


SCENARIOS["bound_part_first"] = scenario_bound_part_first
SCENARIOS["option_delete_first"] = scenario_option_delete_first


# ---------- print_exits · WS-13 E13 V04 — a print's exits against each other ----------


async def scenario_print_exits() -> dict:
    """The archive editor and the trash lock the order and then the print, as the batch exits
    and receiving do. Each crossing stops A right after its first order lock; B starts and
    must WAIT on that order — never take the print first and meet A coming the other way
    (before the fix: the editor held the print, remove-archives the order, and PostgreSQL
    broke the cycle with a deadlock). A batch that finds the print in the trash files nothing
    (WS-13 E13 V08)."""
    from backend.app.api.routes import archives as archive_routes, projects as project_routes
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.project import Project
    from backend.app.schemas.archive import ArchiveUpdate
    from backend.app.schemas.project import BatchAddArchives

    s = await shop(customer=False, group=False, line_quantity=1)
    async with async_session() as db:
        other = Project(name="Protocol destination", status="active")
        db.add(other)
        await db.commit()
        other_id = other.id
    admin = (None, True)

    async def a_print() -> int:
        async with async_session() as db:
            archive = PrintArchive(
                project_id=s["order"], filename="exit", file_path="", file_size=0, status="completed", quantity=1
            )
            db.add(archive)
            await db.commit()
            return archive.id

    doors = {
        "patch": lambda a: (
            lambda db: archive_routes.update_archive(a, ArchiveUpdate(project_id=None), db, admin, _allow())
        ),
        "trash": lambda a: lambda db: archive_routes.delete_archive(a, db, admin, _allow()),
        "remove": lambda a: (
            lambda db: project_routes.remove_archives_from_project(
                s["order"], BatchAddArchives(archive_ids=[a]), db, None, _allow()
            )
        ),
        "add": lambda a: (
            lambda db: project_routes.add_archives_to_project(
                other_id, BatchAddArchives(archive_ids=[a]), db, None, _allow()
            )
        ),
    }
    out: dict[str, dict] = {"order": s["order"], "other": other_id}
    for first, second in (("remove", "patch"), ("patch", "remove"), ("add", "trash"), ("trash", "add")):
        archive_id = await a_print()
        result = await duel(doors[first](archive_id), doors[second](archive_id), a_on="lock_order")
        async with async_session() as db:
            row = await db.get(PrintArchive, archive_id)
            result.update(project=row.project_id, trashed=row.deleted_at is not None)
        out[f"{first}_then_{second}"] = result
    return out


SCENARIOS["print_exits"] = scenario_print_exits
