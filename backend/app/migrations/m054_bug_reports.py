"""Bug-report submission audit table (in-app bug-report UI).

In-app bug-report flow lives in ``api/routes/bug_report.py`` + the floating red
Bug bubble in the frontend ``Layout``. Every submission attempt — successful or
failed — records one row in ``bug_reports`` so operators can see what was sent
and whether the GitHub issue was created. The relay handles the actual GitHub
write; BamDude never holds a PAT.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` makes re-runs no-ops. Fresh installs
go through ``Base.metadata.create_all`` which creates the same shape from
``backend/app/models/bug_report.py`` — the migration is for existing DBs that
upgrade through 0.4.4.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 54
name = "bug_reports"


async def upgrade(conn):
    if await table_exists(conn, "bug_reports"):
        return

    if is_postgres():
        await conn.execute(
            text(
                """
                CREATE TABLE bug_reports (
                    id SERIAL PRIMARY KEY,
                    description TEXT NOT NULL,
                    reporter_email VARCHAR(255),
                    github_issue_number INTEGER,
                    github_issue_url VARCHAR(500),
                    status VARCHAR(20) NOT NULL DEFAULT 'submitted',
                    error_message TEXT,
                    email_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                )
                """
            )
        )
    else:
        await conn.execute(
            text(
                """
                CREATE TABLE bug_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    description TEXT NOT NULL,
                    reporter_email VARCHAR(255),
                    github_issue_number INTEGER,
                    github_issue_url VARCHAR(500),
                    status VARCHAR(20) NOT NULL DEFAULT 'submitted',
                    error_message TEXT,
                    email_sent BOOLEAN NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                )
                """
            )
        )
