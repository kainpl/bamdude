"""The attempt-end cases and G03 B2's archive-close recovery on PostgreSQL.

The case file itself runs on the suite's SQLite engine. Here the same tests run against
``DISPATCH_TEST_POSTGRES_URL`` through ``queue_test_database.test_engine``, which every fixture of theirs
(``db_session``, the clients, ``database.async_session``) resolves to; without that address nothing runs twice.
"""

import os

import pytest

from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_attempt_end_under_the_lane import *  # noqa: F403 -- the cases and their fixtures, collected here
from backend.tests.integration.test_attempt_end_under_the_lane import pytestmark as _case_marks
from backend.tests.integration.test_dispatch_attempt_outcome import (  # noqa: F401 -- case and fixtures
    clean_spool_state,
    sessions,
    test_attempt_end_closes_a_prepared_archive_after_the_runner_commit_fails,
)

pytestmark = [
    *(_case_marks if isinstance(_case_marks, list) else [_case_marks]),
    pytest.mark.skipif(not os.environ.get("DISPATCH_TEST_POSTGRES_URL"), reason="the PostgreSQL twin of a case file"),
]
