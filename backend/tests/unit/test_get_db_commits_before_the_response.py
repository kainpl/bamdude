"""A write is committed before its answer leaves, and a failed commit is not an answer of success.

Upstream #3320. ``get_db`` used to be a yield dependency at FastAPI's default scope, whose exit --
the commit -- runs after the response has been sent: a client could read the old value right
after a 200, and a commit that failed was rolled back behind a 200 the client already had.
These tests drive the REAL ``get_db`` (no override) through a real FastAPI app over a file
database, and look at the database from a second session at the moment the response starts.
"""

import inspect

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.database import get_db
from backend.app.models.settings import Settings


@pytest.fixture
async def farm(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Settings.__table__.create)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", sessions)

    app = FastAPI()

    @app.post("/write")
    async def write(db: AsyncSession = Depends(get_db)):
        db.add(Settings(key="written", value="1"))  # never commits itself, like the Workshop's routes
        return {"ok": True}

    @app.post("/twice")
    async def twice(db: AsyncSession = Depends(get_db)):
        db.add(Settings(key="dup", value="1"))
        db.add(Settings(key="dup", value="2"))  # the unique key is refused only when the session flushes
        return {"ok": True}

    @app.post("/refuse")
    async def refuse(db: AsyncSession = Depends(get_db)):
        db.add(Settings(key="refused", value="1"))
        await db.flush()
        raise HTTPException(409, "no")

    async def reader(key):
        async with sessions() as other:
            return (await other.execute(select(Settings.value).where(Settings.key == key))).scalar_one_or_none()

    seen = {}

    async def asgi(scope, receive, send):
        """What a second connection reads at the instant the response starts."""

        async def watching(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
                seen["written"] = await reader("written")
            await send(message)

        await app(scope, receive, watching)

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False), base_url="http://t")
    yield client, seen, reader
    await client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_row_is_committed_when_the_response_starts(farm):
    client, seen, _reader = farm
    assert (await client.post("/write")).status_code == 200
    assert seen == {"status": 200, "written": "1"}


@pytest.mark.asyncio
async def test_a_commit_that_fails_is_never_answered_with_success(farm):
    client, _seen, reader = farm
    response = await client.post("/twice")
    assert response.status_code == 500
    assert await reader("dup") is None


@pytest.mark.asyncio
async def test_a_refused_request_is_rolled_back(farm):
    client, _seen, reader = farm
    assert (await client.post("/refuse")).status_code == 409
    assert await reader("refused") is None


@pytest.mark.asyncio
async def test_the_database_is_released_before_the_response_starts(farm):
    """SQLite has one writer: were the request still holding it, this write would wait and fail."""
    client, _seen, _reader = farm
    await client.post("/write")
    async with database.async_session() as other:
        await other.execute(text("PRAGMA busy_timeout = 0"))
        other.add(Settings(key="after", value="1"))
        await other.commit()


def test_get_db_is_not_a_yield_dependency():
    """A yield dependency exits after the response unless every call site says otherwise; ours has
    hundreds of call sites, so the dependency itself decides -- and a generator here would undo it."""
    assert not inspect.isasyncgenfunction(get_db)
    assert not inspect.isgeneratorfunction(get_db)


@pytest.mark.asyncio
async def test_without_fastapis_function_scope_it_refuses_loudly():
    """Never a quiet return to committing after the response."""

    class _Connection:
        scope = {}

    with pytest.raises(RuntimeError, match="function"):
        await get_db(_Connection())


def test_the_test_suites_client_closes_the_transaction_the_same_way():
    """The suite overrides ``get_db``; an override that exits after the response would hide exactly
    what this file is about from every other test."""
    import ast
    from pathlib import Path

    source = (Path(__file__).parents[1] / "conftest.py").read_text(encoding="utf-8")
    override = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "override_get_db"
    )
    assert not any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(override))
    assert "enter_before_response" in ast.unparse(override)
