"""Cancel one failed object without throwing away the plate, from the bot.

The operator learns a part has come loose from a notification, on a phone,
somewhere other than the workshop. Until this existed the only control the bot
offered was Stop — which discards the nineteen parts that are still fine.

The screen is a photo of the plate seen from above with a numbered pin on each
object, and a keyboard of those same numbers. Both come from the server: the
pins from :mod:`plate_markers` (the placement the browser also reads) drawn by
:mod:`plate_marker_render`, and the objects from the printers route, which is
called rather than reimplemented so that both of BambuStudio's gates and the
MQTT freshness check apply here exactly as they do in the web.

⚠️ **Skipping cannot be undone.** The object is excluded for the rest of the
print and no command brings it back. Two consequences run through this file:
the press asks for confirmation *by name*, and an object that is already
skipped stays on the screen — greyed, unpressable, in place. Removing it would
shift every button beside it, so the spot the operator was about to press
means a different part between one screen and the next.

A refusal is **said, not hidden**. The entry button gates only on what is true
straight after a reconnect; anything that needs the 3MF read is answered on
the screen behind it, where the objects have been loaded and the reason can be
given. :func:`entry_button` records what gating the other way cost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.printer_manager import printer_manager
from backend.app.services.telegram_handlers.common import NS, deny_out_of_scope, has_perm
from backend.app.services.telegram_handlers.pagination import build_page_nav

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

BUTTONS_PER_ROW = 5

# Objects per page.
#
# The keyboard could hold more; the CAPTION cannot. Every object on the page
# needs a "number — name" line, because the picture only carries numbers, and
# Telegram caps a photo caption at 1024 characters. Fifteen lines of a
# truncated name leave room for the header and the warnings above them.
PAGE_SIZE = 15

# Long enough to tell two parts apart, short enough that fifteen of them plus
# the header fit the cap above.
NAME_CHARS = 28


def entry_button(printer_id: int, lang: str) -> InlineKeyboardButton | None:
    """The Skip-objects button for the printer detail screen, or ``None``.

    ⚠️ **Gates only on what survives a restart.** The obvious gate —
    ``state.skip_objects_supported`` — does not: it is written in exactly three
    places, two branches of ``GET /print/objects`` and ``on_print_start``. A
    backend restarted mid-print therefore holds ``False`` and an empty
    ``printable_objects`` until somebody opens the Skip dialog **in the web**.
    Gating on it hid this button in precisely the situation the feature exists
    for — the operator who is not at the computer. Measured: restarted with a
    print running, no button.

    So the substantive answers ("this plate has no object labels", "only one
    object left") are given INSIDE the screen, where pressing the button has
    loaded the objects and the refusal can be explained. That is the same call
    the web preview modal already records: hiding a control explains nothing,
    and this is exactly the case where somebody flips the wrong slicer switch
    for want of being told which one.

    What is kept here is the one gate that comes from telemetry rather than
    from a 3MF read, so it is true immediately after a reconnect.
    """
    client = printer_manager.get_client(printer_id)
    if not client:
        return None

    # BS's first gate — ``fun`` bit 49. ``partskip`` False is a refusal by the
    # machine itself; ``partskip`` absent is "not reported yet" and keeps
    # working, the same reading the endpoint uses.
    if (getattr(client.state, "print_option_support", None) or {}).get("partskip") is False:
        return None

    return InlineKeyboardButton(
        text=f"✂️ {t(lang, NS, 'skip_objects.btn_entry')}",
        callback_data=f"skipobj:show:{printer_id}:0",
    )


def _refusal(printer_id: int, objects: list[dict], lang: str) -> str | None:
    """Why this plate cannot be skipped from here, or ``None`` if it can.

    Read AFTER the payload load, because that load is what fills
    ``skip_objects_supported`` — see :func:`entry_button` for why it cannot be
    consulted before.
    """
    client = printer_manager.get_client(printer_id)
    if client and not getattr(client.state, "skip_objects_supported", False):
        return t(lang, NS, "skip_objects.unsupported")

    if len([o for o in objects if not o["skipped"]]) < 2:
        # Skipping the last one is Stop with extra steps and none of Stop's
        # confirmation, so it is refused — but said out loud, with the control
        # that does mean "end this print" named.
        return t(lang, NS, "skip_objects.only_one_left")

    return None


async def _objects_payload(printer_id: int) -> dict:
    """The printers route's own answer, session and all.

    Calling the endpoint function keeps one implementation of the object
    reload, the marker placement and the printing/connected checks. A second
    one here would drift, and a drifting marker points at the wrong part.
    """
    from backend.app.api.routes.printers import get_printable_objects
    from backend.app.core.database import async_session

    async with async_session() as db:
        return await get_printable_objects(printer_id, db=db)


async def _plate_picture(printer_id: int, objects: list[dict]) -> bytes | None:
    """The top-down plate render with the pins drawn on, or ``None``.

    ``None`` is an ordinary outcome: no archive on disk yet, or a 3MF that
    carries no ``Metadata/top_N.png``. The caller then shows the list without
    a picture and says so. ⚠️ It must never fall back to ``plate_N.png`` — see
    :mod:`plate_marker_render` for what that costs.
    """
    from sqlalchemy import select

    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.services.plate_marker_render import PlateMarker, render_markers, top_view_png

    async with async_session() as db:
        archive = (
            (
                await db.execute(
                    select(PrintArchive)
                    .where(
                        PrintArchive.printer_id == printer_id,
                        PrintArchive.status == "printing",
                        PrintArchive.file_path != "",
                    )
                    .order_by(PrintArchive.id.desc())
                )
            )
            .scalars()
            .first()
        )

    if not archive or not archive.file_path:
        return None

    source = top_view_png(settings.base_dir / archive.file_path, archive.plate_index or 1)
    if source is None:
        return None

    markers = [
        PlateMarker(id=o["id"], x=o["marker"]["x"], y=o["marker"]["y"], skipped=o["skipped"])
        for o in objects
        if o.get("marker")
    ]
    try:
        return render_markers(source, markers)
    except Exception:  # noqa: BLE001 — a broken PNG costs the picture, not the feature
        return None


async def _perform_skip(printer_id: int, object_id: int) -> None:
    """Skip via the endpoint, so its gates and validation apply here too."""
    from backend.app.api.routes.printers import skip_objects
    from backend.app.core.database import async_session

    async with async_session() as db:
        await skip_objects(printer_id, [object_id], db=db)


def _label(obj: dict) -> str:
    name = obj["name"] or "?"
    return name if len(name) <= NAME_CHARS else name[: NAME_CHARS - 1] + "…"


async def _screen(printer_id: int, offset: int, lang: str) -> tuple[str, InlineKeyboardMarkup, bytes | None]:
    """Caption, keyboard and picture for one page of the picker."""
    payload = await _objects_payload(printer_id)
    objects = sorted(payload["objects"], key=lambda o: o["id"])

    refusal = _refusal(printer_id, objects, lang)
    if refusal:
        # No picture and no number keyboard: every one of them would be a
        # control that does nothing. The sentence and the way back, only.
        back = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"◀️ {t(lang, NS, 'printers.btn_back')}", callback_data=f"skipobj:back:{printer_id}"
                    )
                ]
            ]
        )
        return f"✂️ *{escape_md(t(lang, NS, 'skip_objects.title'))}*\n\n{escape_md(refusal)}", back, None

    picture = await _plate_picture(printer_id, objects)

    page = objects[offset : offset + PAGE_SIZE]

    lines = [f"✂️ *{escape_md(t(lang, NS, 'skip_objects.title'))}*"]
    if payload.get("positions_approximate"):
        lines.append(escape_md(t(lang, NS, "skip_objects.approximate")))
    if picture is None:
        lines.append(escape_md(t(lang, NS, "skip_objects.no_picture")))
    lines.append("")
    for obj in page:
        mark = "\U0001f6ab" if obj["skipped"] else "•"
        lines.append(f"{mark} *{obj['id']}* — {escape_md(_label(obj))}")

    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(page), BUTTONS_PER_ROW):
        rows.append(
            [
                InlineKeyboardButton(
                    # A skipped object keeps its place and its number and goes
                    # nowhere when pressed — see the module docstring.
                    text=f"\U0001f6ab{obj['id']}" if obj["skipped"] else str(obj["id"]),
                    callback_data="noop" if obj["skipped"] else f"skipobj:pick:{printer_id}:{obj['id']}",
                )
                for obj in page[start : start + BUTTONS_PER_ROW]
            ]
        )

    nav = build_page_nav(len(objects), offset, PAGE_SIZE, f"skipobj:show:{printer_id}:", lang)
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text=f"◀️ {t(lang, NS, 'printers.btn_back')}", callback_data=f"skipobj:back:{printer_id}")]
    )

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), picture


async def _render(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup, picture: bytes | None) -> None:
    """Put a screen on the operator's phone, editing in place where possible.

    A photo message cannot become a text message or the reverse, so which edit
    applies is decided by what the message already is rather than by what we
    would like it to be.
    """
    if callback.message.photo:
        if picture is not None:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=BufferedInputFile(picture, "plate.png"), caption=text),
                reply_markup=markup,
            )
        else:
            await callback.message.edit_caption(caption=text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("skipobj:show:"))
async def cb_show(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """The picker: the plate, its pins, and a number per object."""
    lang = await get_language()
    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    printer_id, offset = int(parts[2]), int(parts[3])
    await callback.answer()

    try:
        text, markup, picture = await _screen(printer_id, offset, lang)
    except Exception as e:  # noqa: BLE001 — HTTPException detail or anything else, said once
        await callback.answer(_reason(e, lang), show_alert=True)
        return

    if callback.message.photo:
        await _render(callback, text, markup, picture)
    elif picture is not None:
        # The printer detail above is a text message and stays one; the picker
        # arrives as a new photo beneath it.
        await callback.message.answer_photo(
            photo=BufferedInputFile(picture, "plate.png"), caption=text, reply_markup=markup
        )
    else:
        await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("skipobj:back:"))
async def cb_back(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Leave the picker.

    ⚠️ Not a plain ``printer:{id}`` button, which is what it was at first and
    why it did nothing. That callback lands in ``show_printer_detail``, which
    ends in ``edit_text`` — and Telegram refuses to edit text into a PHOTO
    message ("there is no text in the message to edit"). The picker is a photo
    whenever the plate has a top view, so the button silently failed on
    exactly the screens that work best.

    Which way out is right depends on how the picker arrived:

    * as a **new photo** below the printer card — the card is still on screen
      with its own buttons, so deleting the photo puts the operator back
      exactly where they were;
    * as an **edit of the card itself** (no top view, so it stayed text) —
      there is nothing above to go back to, so redraw the card in place.
    """
    printer_id = int(callback.data.split(":")[2])
    if await deny_out_of_scope(callback, tg_chat, printer_id):
        return
    await callback.answer()

    if callback.message.photo:
        try:
            await callback.message.delete()
            return
        except Exception:  # noqa: BLE001 — too old to delete (48h) or already gone
            pass

    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)


@router.callback_query(F.data.startswith("skipobj:pick:"))
async def cb_pick(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Confirm, by name.

    ⚠️ The only control in the bot that asks twice, and deliberately so: pause,
    resume and light are all reversible. A bare "are you sure?" would confirm
    only that a button was pressed, so the object's name is in the question.
    """
    lang = await get_language()
    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    printer_id, object_id = int(parts[2]), int(parts[3])
    await callback.answer()

    try:
        payload = await _objects_payload(printer_id)
    except Exception as e:  # noqa: BLE001
        await callback.answer(_reason(e, lang), show_alert=True)
        return

    obj = next((o for o in payload["objects"] if o["id"] == object_id), None)
    name = _label(obj) if obj else str(object_id)

    text = "\n".join(
        [
            f"✂️ *{escape_md(t(lang, NS, 'skip_objects.confirm_title'))}*",
            "",
            f"*{object_id}* — {escape_md(name)}",
            "",
            escape_md(t(lang, NS, "skip_objects.confirm_irreversible")),
        ]
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✂️ {t(lang, NS, 'skip_objects.btn_confirm')}",
                    callback_data=f"skipobj:do:{printer_id}:{object_id}",
                ),
                InlineKeyboardButton(
                    text=f"❌ {t(lang, NS, 'printers.btn_cancel')}",
                    callback_data=f"skipobj:show:{printer_id}:0",
                ),
            ]
        ]
    )
    await _render(callback, text, markup, None)


@router.callback_query(F.data.startswith("skipobj:do:"))
async def cb_do(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Confirmed — skip it, then redraw from what the printer says now."""
    lang = await get_language()
    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    printer_id, object_id = int(parts[2]), int(parts[3])

    try:
        await _perform_skip(printer_id, object_id)
    except Exception as e:  # noqa: BLE001 — the part is still printing either way, and that is the news
        await callback.answer(_reason(e, lang), show_alert=True)
        return

    await callback.answer(f"✂️ {t(lang, NS, 'skip_objects.skipped', id=object_id)}")

    # Redrawn from a fresh payload rather than by editing the screen we already
    # have: the printer is the authority on what is skipped, including anything
    # skipped from the web while this dialog sat open.
    try:
        text, markup, picture = await _screen(printer_id, 0, lang)
    except Exception:  # noqa: BLE001 — the skip landed; failing to redraw must not read as a failure
        return
    await _render(callback, text, markup, picture)


def _reason(exc: Exception, lang: str) -> str:
    """What to put in the alert.

    ⚠️ An ``HTTPException`` detail is shown verbatim. Those strings were
    written for a person — "This plate was not sliced with object labels, so
    its parts cannot be skipped" — and a second vocabulary for the same
    refusals would only be a vaguer one.
    """
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return t(lang, NS, "skip_objects.failed")
