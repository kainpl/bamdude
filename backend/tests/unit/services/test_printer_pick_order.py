"""The AutoQueue printer order's sort key (C01, upstream #3480 / #2915): larger is picked first."""

from __future__ import annotations

import pytest

from backend.app.services.printer_pick_order import DEFAULT, ORDERS, PickFacts, PickOrder, advanced, pick_key


def facts(hours=0.0, last_finished=None, priority=None):
    return PickFacts(hours=hours, last_finished=last_finished, priority=priority)


def test_the_orders_and_the_default_are_upstreams():
    assert ORDERS == ("fewest_hours", "most_hours", "least_recently_used", "priority")
    assert DEFAULT == "fewest_hours"


def test_fewest_and_most_hours():
    assert pick_key("fewest_hours", facts(hours=10)) > pick_key("fewest_hours", facts(hours=20))
    assert pick_key("most_hours", facts(hours=20)) > pick_key("most_hours", facts(hours=10))


def test_lru_never_finished_goes_first():
    never, day, hour = facts(last_finished=None), facts(last_finished=-86400.0), facts(last_finished=-3600.0)
    assert pick_key("least_recently_used", never) > pick_key("least_recently_used", day)
    assert pick_key("least_recently_used", day) > pick_key("least_recently_used", hour)


def test_priority_lowest_first_none_last():
    first, fifth, none = facts(priority=0), facts(priority=5), facts(priority=None)
    assert pick_key("priority", first) > pick_key("priority", fifth) > pick_key("priority", none)


def test_an_unknown_order_keys_as_the_default():
    assert pick_key("newest", facts(hours=3)) == pick_key("fewest_hours", facts(hours=3))


@pytest.mark.parametrize("order", ORDERS)
def test_no_facts_ranks_after_facts(order):
    assert pick_key(order, None) == ()
    assert pick_key(order, None) < pick_key(order, facts(hours=1e9, last_finished=0.0, priority=100000))


def test_the_order_keys_each_printer_by_its_facts():
    order = PickOrder("fewest_hours", {1: facts(hours=5), 2: facts(hours=1)})
    assert order.key(2) > order.key(1)
    assert order.key(3) == ()


def test_advanced_adds_print_time_and_sets_the_finish():
    assert advanced(facts(hours=1.0, priority=3), seconds=3600, finished_at=50.0) == facts(2.0, 50.0, 3)
    assert advanced(None, seconds=3600, finished_at=50.0) is None
