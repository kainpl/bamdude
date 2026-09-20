"""A save replaces only the links it could have spoken for.

``PUT /spools/{id}/k-profiles`` deletes every link and recreates from the
payload. The dialog builds that payload from the printers it can offer —
connected and not archived — so every OTHER link was destroyed by a save that
never had an opinion about it: a profile on a printer you archived, or one that
merely happened to be offline at that moment. On the reporting farm that is 896
links, gone on the first save of any spool.

Silence about a printer the client could not see is not a removal.
"""

from backend.app.api.routes.inventory import k_profile_links_to_keep


class _Link:
    def __init__(self, link_id, printer_id):
        self.id = link_id
        self.printer_id = printer_id


def test_a_link_on_an_archived_printer_survives():
    kept = k_profile_links_to_keep(
        existing=[_Link(1, 5), _Link(2, 9)],
        payload_printer_ids=set(),
        archived_printer_ids={9},
        connected_printer_ids={5},
    )
    # 5 is live and was offered; 9 is retired and never was.
    assert [link.id for link in kept] == [2]


def test_a_link_on_an_offline_printer_survives():
    # Not archived, just not answering right now — the dialog offered nothing
    # for it, so the payload's silence says nothing about it either.
    kept = k_profile_links_to_keep(
        existing=[_Link(1, 5), _Link(2, 7)],
        payload_printer_ids=set(),
        archived_printer_ids=set(),
        connected_printer_ids={5},
    )
    assert [link.id for link in kept] == [2]


def test_unticking_a_live_printer_still_removes_its_link():
    # The whole point of the endpoint must keep working: the client saw printer
    # 5, sent nothing for it, and that IS a decision.
    kept = k_profile_links_to_keep(
        existing=[_Link(1, 5)],
        payload_printer_ids=set(),
        archived_printer_ids=set(),
        connected_printer_ids={5},
    )
    assert kept == []


def test_a_printer_named_in_the_payload_is_always_spoken_for():
    # Even if it dropped off the network between the dialog opening and the
    # save landing: the client clearly had an opinion, so its links are
    # replaced rather than kept alongside the new ones.
    kept = k_profile_links_to_keep(
        existing=[_Link(1, 7)],
        payload_printer_ids={7},
        archived_printer_ids=set(),
        connected_printer_ids=set(),
    )
    assert kept == []
