#!/bin/sh

ruff check backend/ && ruff format --check backend/

# ⚠️ From the repo root, never `cd backend`: some tests resolve paths against
# the repository and one spawns a child that must import `backend.app`.
# ⚠️ conftest ignores `.env` via BAMDUDE_IGNORE_DOTENV. Camera ownership is
# worker-only, so there is no CAMERA_RUNTIME prefix or inline test mode.
# Limit parallelism on developer hosts; see CONTRIBUTING.md#testing.
# Addressed historical PG gate (disposable TEST_POSTGRES_URL only):
# python -m pytest backend/tests/integration/test_postgres_scenarios.py -k g02_historical --timeout=120 -n 0
# This default suite does not provision PostgreSQL; CI's existing scenarios job does.
venv/bin/python3 -m pytest backend/tests/ -v -n 4 --timeout=300 --timeout-method=thread
