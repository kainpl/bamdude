"""list_paging: the arithmetic and the sort split the three list routes share."""

import pytest

from backend.app.services.list_paging import SortSpec, page_meta, resolve_sort, slice_page, sort_computed


def test_page_meta_counts_pages_and_never_reports_zero_pages():
    assert page_meta(0, 1, 24, False).last_page == 1
    assert page_meta(49, 3, 24, False).model_dump() == {"total": 49, "current_page": 3, "per_page": 24, "last_page": 3}


def test_all_reports_one_page_of_everything():
    meta = page_meta(7, 5, 24, True)
    assert (meta.current_page, meta.per_page, meta.last_page) == (1, 7, 1)
    assert page_meta(0, 1, 24, True).per_page == 1


def test_slice_page_is_exact_and_all_is_identity():
    rows = list(range(10))
    assert slice_page(rows, 2, 4, False) == [4, 5, 6, 7]
    assert slice_page(rows, 9, 4, False) == []
    assert slice_page(rows, 9, 4, True) == rows


SPEC = SortSpec(
    sql={"name": ("name_col", True), "updated": ("updated_col", False)}, computed={"kits"}, default="name-asc"
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("name-desc", ("name", "desc", False)),
        ("kits-asc", ("kits", "asc", True)),
        ("nonsense", ("name", "asc", False)),
        (None, ("name", "asc", False)),
        ("kits", ("name", "asc", False)),  # no direction → default, not a guess
        ("total_price-desc", ("name", "asc", False)),  # a key of another list is unknown here
    ],
)
def test_resolve_sort_is_closed_and_falls_back(raw, expected):
    assert resolve_sort(SPEC, raw) == expected


def test_a_key_with_a_dash_in_its_name_still_resolves():
    spec = SortSpec(sql={"name": ("c", False)}, computed={"total_price"}, default="name-asc")
    assert resolve_sort(spec, "total_price-desc") == ("total_price", "desc", True)


def test_sort_computed_is_stable_by_id_on_ties():
    rows = [{"id": 3, "n": 1}, {"id": 1, "n": 1}, {"id": 2, "n": 5}]
    assert [r["id"] for r in sort_computed(rows, lambda r: r["n"], "desc", id_fn=lambda r: r["id"])] == [2, 1, 3]
    assert [r["id"] for r in sort_computed(rows, lambda r: r["n"], "asc", id_fn=lambda r: r["id"])] == [1, 3, 2]


def test_sort_computed_puts_missing_figures_last_both_ways():
    """A row whose figure is unknown (None) is never compared with a number —
    it sorts after every known one, ascending or descending."""
    rows = [{"id": 1, "n": None}, {"id": 2, "n": 5}, {"id": 3, "n": 1}]
    assert [r["id"] for r in sort_computed(rows, lambda r: r["n"], "asc", id_fn=lambda r: r["id"])] == [3, 2, 1]
    assert [r["id"] for r in sort_computed(rows, lambda r: r["n"], "desc", id_fn=lambda r: r["id"])] == [2, 3, 1]
