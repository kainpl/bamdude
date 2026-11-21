"""``test_auto_queue_tick_addresses_its_rows`` again, on the dual-dialect engine (G03 B acceptance: each case on SQLite and PostgreSQL).

The case file itself runs on the suite's SQLite engine. Here the same tests run against
``DISPATCH_TEST_POSTGRES_URL`` through ``queue_test_database.test_engine``, which every fixture of theirs
(``db_session``, the clients, ``database.async_session``) resolves to; without that address nothing runs twice.
"""

import os

import pytest

from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_auto_queue_tick_addresses_its_rows import *  # noqa: F403 -- the cases and their fixtures, collected here
from backend.tests.integration.test_auto_queue_tick_addresses_its_rows import pytestmark as _case_marks

pytestmark = [
    *(_case_marks if isinstance(_case_marks, list) else [_case_marks]),
    pytest.mark.skipif(not os.environ.get("DISPATCH_TEST_POSTGRES_URL"), reason="the PostgreSQL twin of a case file"),
]
