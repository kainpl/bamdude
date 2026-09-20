#!/bin/sh

ruff check backend/ && ruff format --check backend/

# ⚠️ From the repo root, never `cd backend`: some tests resolve paths against
# the repository and one spawns a child that must import `backend.app`.
# ⚠️ Since 2026-09-18 conftest itself ignores `.env` (BAMDUDE_IGNORE_DOTENV),
# so this prefix only covers a CAMERA_RUNTIME exported in the shell. Before
# that it was the whole defence: with
# `worker` the virtual-printer startup tests fail and one hangs forever.
# ⚠️ `-n auto`, not `-n 30`: sixty processes on a developer box stalled the
# suite at ~98% with idle CPU. Details: CONTRIBUTING.md#testing.
CAMERA_RUNTIME=inline venv/bin/python3 -m pytest backend/tests/ -v -n auto     --timeout=300 --timeout-method=thread
