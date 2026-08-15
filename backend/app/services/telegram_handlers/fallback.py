"""The last word: a message nothing else claimed.

Two of the wizards take free text — the IP and access code when adding a
printer, the number when editing machine hours — and they claim it through a
handler bound to their own FSM state. State lives in aiogram's in-memory
storage, so a restart of the backend wipes it while the operator is still
mid-conversation.

What follows is worse than an error. The wizard's handler no longer matches, no
other handler wants a bare IP address either, and aiogram simply drops the
message. The operator types, the bot says nothing, and there is no way to tell
that from the bot being down.

⚠️ **Registered LAST** (``telegram_bot.py``), which is the whole design: every
command, every reply-keyboard button and every active scene handler is asked
first, and this only ever sees what none of them wanted. Registering it any
earlier would swallow the bot.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import NS

router = Router()


@router.message()
async def msg_unclaimed(message: Message, state: FSMContext | None = None) -> None:
    """Say something, rather than nothing.

    ⚠️ Clears any leftover state as well. Reaching here while a state is set
    means the scene that owned it did not want this message either — which it
    cannot, since its handler is bound to that state and would have run. So the
    state is stale by definition, and leaving it set would keep eating messages.
    """
    lang = await get_language()
    if state is not None and await state.get_state() is not None:
        await state.clear()

    await message.answer(escape_md(t(lang, NS, "start.not_understood")))
