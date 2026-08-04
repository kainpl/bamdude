"""Which project a print belongs to when the caller did not say.

The rule already existed in the queue and auto-queue routes, written out
twice. The direct-print route never got it, so printing a project-linked file
straight to a printer produced an archive with ``project_id`` NULL — the same
file queued instead landed in its project correctly.
"""

from types import SimpleNamespace

from backend.app.services.library_helpers import project_for_library_file


def file_with(*project_ids):
    return SimpleNamespace(projects=[SimpleNamespace(id=pid) for pid in project_ids])


def test_an_explicit_project_wins():
    # The operator naming a project is the whole point of the parameter.
    assert project_for_library_file(7, file_with(3)) == 7


def test_a_linked_file_supplies_the_project():
    # The reported bug: this returned None on the direct-print path.
    assert project_for_library_file(None, file_with(3)) == 3


def test_a_multi_project_file_takes_the_first():
    # Matches the rule the queue and auto-queue routes have always applied:
    # rows are single-project by design, and the pivot reads in insertion
    # order, so [0] is deterministic rather than arbitrary.
    assert project_for_library_file(None, file_with(5, 9)) == 5


def test_an_unlinked_file_leaves_it_unset():
    assert project_for_library_file(None, file_with()) is None


def test_no_file_leaves_it_unset():
    # Reprints from an archive pass no library file at all.
    assert project_for_library_file(None, None) is None


def test_an_explicit_project_survives_an_unlinked_file():
    assert project_for_library_file(7, file_with()) == 7
