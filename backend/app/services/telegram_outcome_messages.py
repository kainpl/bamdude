"""Which print a Telegram completion or reminder message is about, for a reaction on it.

A reaction update names a chat and a message id and nothing else, so the two
message kinds a reaction means something on — the print-complete notification
and the «result not checked» reminder — are remembered here as they are sent:
(bot account, chat, message) -> (archive id, the archive's immutable identity).

Memory only, on purpose: bounded per bot and aged out, and lost on a restart —
a reaction on a message sent before the restart then does nothing, and the
message's own buttons still work (they carry the archive in their callback).
The id alone is never trusted when the reaction arrives: SQLite hands a deleted
print's id to the next one, so the handler checks the identity too.
"""

from __future__ import annotations

import time
from collections import OrderedDict

MAX_PER_BOT = 500
MAX_AGE_SECONDS = 7 * 24 * 3600

# bot account id -> {(chat id, message id): (archive id, identity, remembered at)}
_messages: dict[int, OrderedDict[tuple[int, int], tuple[int, str, float]]] = {}


def remember(bot_account_id: int, chat_id: int, message_id: int, archive_id: int, identity: str | None) -> None:
    """Note that this message is about this print. A print without an identity is not remembered."""
    if not identity:
        return
    now = time.monotonic()
    of_bot = _messages.setdefault(bot_account_id, OrderedDict())
    of_bot.pop((chat_id, message_id), None)
    of_bot[(chat_id, message_id)] = (archive_id, identity, now)
    _prune(of_bot, now)


def lookup(bot_account_id: int, chat_id: int, message_id: int) -> tuple[int, str] | None:
    """The print this message is about, or None when it is not remembered (any more)."""
    of_bot = _messages.get(bot_account_id)
    if not of_bot:
        return None
    _prune(of_bot, time.monotonic())
    found = of_bot.get((chat_id, message_id))
    return (found[0], found[1]) if found is not None else None


def _prune(of_bot: OrderedDict, now: float) -> None:
    # Insertion order is age order, so the oldest entries are at the front.
    while of_bot:
        key, (_archive_id, _identity, at) = next(iter(of_bot.items()))
        if len(of_bot) > MAX_PER_BOT or now - at > MAX_AGE_SECONDS:
            of_bot.pop(key, None)
        else:
            break


def clear() -> None:
    _messages.clear()
