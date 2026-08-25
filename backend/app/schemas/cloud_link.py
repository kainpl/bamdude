"""Request and response bodies for the Cloud Link settings surface.

One response shape carries the whole page. Every mutating route returns
:class:`CloudLinkStatus` rather than an acknowledgement, because each of them
changes something the page is already displaying — pairing sets three fields at
once, the toggle can be refused by a farm that is not paired — and a page that
has to re-fetch after every save shows a stale link for one round trip and gets
it wrong entirely if the re-fetch fails.

Nothing here is a secret. ``instance_secret_encrypted`` has no field on any of
these models and must never get one: it is the credential that lets something
speak for this farm, it is readable only through ``store.get_secret``, and a
settings page has no use for it.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CloudLinkStatus(BaseModel):
    """Everything the settings page shows about the link.

    ``paired`` and ``enabled`` are separate answers, and so are ``enabled`` and
    ``connected``: holding a credential, choosing to use it, and currently
    having a socket are three different states with three different repairs.
    ``revoked`` is a fourth — the portal threw the credential away, which no
    amount of enabling will fix.
    """

    enabled: bool
    paired: bool
    connected: bool
    portal_url: str
    instance_id: str | None = None
    last_connected_at: datetime | None = None
    last_error: str | None = None
    revoked: bool
    #: The allowlist **as saved**, not as published — an archived printer keeps
    #: its row, and this list drives the checkboxes the user ticked.
    published_printer_ids: list[int] = []


class CloudLinkPairRequest(BaseModel):
    """A code the user read off the portal, and optionally where to redeem it.

    ``portal_url`` is optional because re-pairing with the same portal is the
    common case; absent means "keep the one already saved" and never "reset to
    the default".
    """

    pairing_code: str
    portal_url: str | None = None


class CloudLinkPublishSetRequest(BaseModel):
    """The complete list of printers the portal may be told about.

    Complete, not a delta: the set the user saved IS the set, so an empty list
    is a valid answer meaning "publish nothing" and must not be read as "no
    change asked for".
    """

    printer_ids: list[int]


class CloudLinkEnabledRequest(BaseModel):
    """The switch. Decides whether the link runs, not whether it is paired."""

    enabled: bool


class CloudLinkAuditEntry(BaseModel):
    """One notable message that crossed the link.

    Read by a human in a table and kept for a month. There is deliberately no
    id: the row is a record, nothing addresses it, and an id in the response
    would invite an endpoint that lets somebody delete one.
    """

    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    #: ``"up"`` (we told the portal) or ``"down"`` (the portal asked us).
    direction: str
    kind: str
    summary: str
    ok: bool


class CloudLinkAuditPage(BaseModel):
    """One page of the audit, newest first, with the count of the whole.

    ``total`` is the unpaged count so the UI can size its pager. It is read
    separately from the page and the table is swept daily, so the two can
    disagree by a row — accepted: the alternative is holding a transaction open
    across both queries for a list nobody acts on.
    """

    items: list[CloudLinkAuditEntry]
    total: int = Field(..., description="Rows in the whole audit, not on this page")
    page: int
    page_size: int
