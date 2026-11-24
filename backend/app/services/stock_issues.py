"""Issues of goods — the ONE writer of ``stock_issues`` (spec workshop-order-issue, rule 10).

An issue is one «Виконати» of an order's «Склад і видача» that hands units over,
or a manual issue off the stock page. It is written with a SNAPSHOT — the
customer's name, the recipient, the delivery — because the contact and the
delivery directory may change or go; afterwards only the waybill number and the
note may change. What it handed over are the issue movements of both ledgers that
name it — and, once sealed, the lines of its dispatch note, which is what is shown.
Since WS-12 the issue IS the dispatch note ``DN-<id>``: :func:`seal` writes its snapshot
(lines, supplier, basis, performer, units) once, in the issuing transaction, after the
movements (spec workshop-dispatch-notes).
``tests/unit/test_stock_issues_have_one_writer.py`` keeps every other module out.

Never commits. Refusals are :class:`StockIssueError` — an English sentence the
route forwards verbatim (translated at the boundary) with its HTTP status.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.customer import Customer, CustomerContact
from backend.app.models.finished_stock import StockItem, StockItemMovement
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.settings import Settings
from backend.app.models.stock_issue import WAYBILL_MAX, StockIssue, StockIssueLine
from backend.app.models.user import User
from backend.app.services.entity_codes import code_for
from backend.app.services.finished_stock_views import configuration_refs

#: «Leave it alone» for :func:`update` — ``None`` is a real value there (clear it).
UNSET = object()


class StockIssueError(Exception):
    """An issue the writer refuses; ``status`` is the HTTP answer."""

    def __init__(self, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True, slots=True)
class Recipient:
    """Who takes the goods and how they travel — copied into the issue as text."""

    name: str | None = None
    phone: str | None = None
    delivery_method: str | None = None
    delivery_details: str | None = None


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def _waybill(value: str | None) -> str | None:
    value = _clean(value)
    if value is not None and len(value) > WAYBILL_MAX:
        raise StockIssueError("A waybill number is at most 24 characters", 422)
    return value


async def _contact(db: AsyncSession, contact_id: int | None, customer_id: int) -> CustomerContact | None:
    """The order's contact when it is still this customer's, else the customer's main one."""
    load = selectinload(CustomerContact.delivery_method)
    if contact_id is not None:
        found = (
            await db.execute(select(CustomerContact).options(load).where(CustomerContact.id == contact_id))
        ).scalar_one_or_none()
        if found is not None and found.customer_id == customer_id:
            return found
    return (
        await db.execute(
            select(CustomerContact)
            .options(load)
            .where(CustomerContact.customer_id == customer_id)
            .order_by(CustomerContact.position, CustomerContact.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def default_recipient(db: AsyncSession, *, project: Project | None, customer_id: int) -> Recipient:
    """The order's contact person, else the customer's main contact (spec rule 18)."""
    contact = await _contact(db, project.contact_id if project is not None else None, customer_id)
    if contact is None:
        return Recipient()
    method = contact.delivery_method.name if contact.delivery_method is not None else None
    return Recipient(contact.name, contact.phone, method, contact.delivery_details)


async def create(
    db: AsyncSession,
    *,
    customer_id: int,
    project_id: int | None,
    recipient: Recipient,
    waybill: str | None,
    note: str | None,
    actor: User | None,
) -> StockIssue:
    """A new issue with its snapshot. The customer is required (spec rule 16)."""
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise StockIssueError("Customer not found", 404)
    issue = StockIssue(
        project_id=project_id,
        customer_id=customer.id,
        customer_name=customer.name,
        recipient_name=_clean(recipient.name),
        recipient_phone=_clean(recipient.phone),
        delivery_method=_clean(recipient.delivery_method),
        delivery_details=_clean(recipient.delivery_details),
        waybill=_waybill(waybill),
        note=_clean(note),
        created_by=actor.id if actor is not None else None,
    )
    db.add(issue)
    await db.flush()
    return issue


async def update(db: AsyncSession, issue: StockIssue, *, waybill: object = UNSET, note: object = UNSET) -> None:
    """The waybill number and the note — the only things an issue lets change (spec rule 21)."""
    if waybill is not UNSET:
        issue.waybill = _waybill(waybill)  # type: ignore[arg-type]
    if note is not UNSET:
        issue.note = _clean(note)  # type: ignore[arg-type]
    await db.flush()


#: The supplier's details a note copies — ``settings`` keys ``document_supplier_<key>`` (rule 10).
SUPPLIER_KEYS = ("name", "address", "phone", "code", "iban")


async def supplier_snapshot(db: AsyncSession) -> dict[str, str]:
    """The supplier's details as they are set now; an unset one is ``""``.

    The settings route stores a JSON null as the text ``"None"`` — that is no detail
    either, never a word on paper (final review M5).
    """
    keys = {f"document_supplier_{key}": key for key in SUPPLIER_KEYS}
    rows = dict((await db.execute(select(Settings.key, Settings.value).where(Settings.key.in_(keys)))).all())
    out = {}
    for setting, key in keys.items():
        value = (rows.get(setting) or "").strip()
        out[key] = "" if value == "None" else value
    return out


async def _by_id(db: AsyncSession, model, ids) -> dict:
    ids = sorted(set(ids))
    if not ids:
        return {}
    return {row.id: row for row in (await db.execute(select(model).where(model.id.in_(ids)))).scalars()}


async def seal(db: AsyncSession, issue: StockIssue, *, actor: User | None) -> list[StockIssueLine]:
    """Write the dispatch note of ``issue`` from its movements — once (spec workshop-dispatch-notes, rule 6).

    Ready units first, one line per stock position in the order they moved;
    then a parts line's parts, one line per part. The configuration, the names
    and the SKU are copied as they are now; the document never reads them again.
    """
    if await db.scalar(select(func.count(StockIssueLine.id)).where(StockIssueLine.issue_id == issue.id)):
        raise StockIssueError("This issue already has its dispatch note")
    finished = (
        await db.execute(
            select(StockItemMovement.item_id, func.sum(StockItemMovement.delta_on_hand))
            .where(StockItemMovement.stock_issue_id == issue.id)
            .group_by(StockItemMovement.item_id)
            .order_by(func.min(StockItemMovement.id))
        )
    ).all()
    parts = (
        await db.execute(
            select(ProductPartStockMovement.product_part_id, func.sum(ProductPartStockMovement.delta))
            .where(
                ProductPartStockMovement.stock_issue_id == issue.id,
                ProductPartStockMovement.reason == "issued_for_order",
            )
            .group_by(ProductPartStockMovement.product_part_id)
            .order_by(func.min(ProductPartStockMovement.id))
        )
    ).all()
    items = await _by_id(db, StockItem, [item_id for item_id, _ in finished])
    part_rows = await _by_id(db, ProductPart, [part_id for part_id, _ in parts])
    products = await _by_id(
        db, Product, [i.product_id for i in items.values()] + [p.product_id for p in part_rows.values()]
    )
    configurations = await configuration_refs(db, list(items.values()))

    lines: list[StockIssueLine] = []
    for item_id, delta in finished:
        quantity = -int(delta or 0)
        if quantity <= 0:
            continue
        product = products[items[item_id].product_id]
        lines.append(
            StockIssueLine(
                product_id=product.id,
                product_name=product.name,
                sku=product.sku,
                configuration=configurations[item_id][1].model_dump(include={"choices", "changed_parts"}),
                quantity=quantity,
            )
        )
    for part_id, delta in parts:
        quantity = -int(delta or 0)
        if quantity <= 0:
            continue
        part = part_rows[part_id]
        product = products[part.product_id]
        lines.append(
            StockIssueLine(
                product_id=product.id,
                product_name=product.name,
                sku=product.sku,
                configuration={},
                part_name=part.name,
                quantity=quantity,
            )
        )
    if not lines:
        raise StockIssueError("An issue that hands nothing over has no dispatch note")
    for position, line in enumerate(lines, start=1):
        line.issue_id = issue.id
        line.position = position
    db.add_all(lines)
    issue.units = sum(line.quantity for line in lines)
    issue.supplier = await supplier_snapshot(db)
    issue.created_by_name = actor.username if actor is not None else None
    if issue.project_id is not None:
        project = await db.get(Project, issue.project_id)
        if project is not None:
            issue.order_code = code_for("order", project.id)
            issue.order_name = project.name
    await db.flush()
    return lines


async def detach_customer(db: AsyncSession, customer_id: int) -> None:
    """A deleted customer leaves its issues with the snapshot of its name (SQLite runs no FK actions)."""
    await db.execute(sa_update(StockIssue).where(StockIssue.customer_id == customer_id).values(customer_id=None))


async def detach_project(db: AsyncSession, project_id: int) -> None:
    """A deleted order leaves its issues with the customer."""
    await db.execute(sa_update(StockIssue).where(StockIssue.project_id == project_id).values(project_id=None))


async def detach_user(db: AsyncSession, user_id: int) -> None:
    """A deleted user leaves the issues it wrote without a performer."""
    await db.execute(sa_update(StockIssue).where(StockIssue.created_by == user_id).values(created_by=None))


async def detach_product(db: AsyncSession, product_id: int) -> None:
    """A deleted product leaves the notes' lines with their text (SQLite runs no FK actions, ids repeat)."""
    await db.execute(sa_update(StockIssueLine).where(StockIssueLine.product_id == product_id).values(product_id=None))
