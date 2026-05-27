"""Per-printer FTP serialization + exponential backoff (P2S WRONG_VERSION_NUMBER fix).

Bambu printers' FTPS allows ~one session; overlapping connects (post-print
SD-cleanup + archive-download to the same printer) trigger a plaintext rejection
that implicit-TLS surfaces as WRONG_VERSION_NUMBER. The decorator serializes
all FTP ops per IP; with_ftp_retry backs off exponentially.
"""

import asyncio

import pytest

from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import _ftp_serialized, with_ftp_retry


@pytest.mark.asyncio
async def test_same_ip_serializes_no_overlap():
    order: list[str] = []

    @_ftp_serialized
    async def op(ip_address: str, tag: str):
        order.append(f"start-{tag}")
        await asyncio.sleep(0.05)
        order.append(f"end-{tag}")
        return tag

    await asyncio.gather(op("1.2.3.4", "A"), op("1.2.3.4", "B"))

    # Serialized: one op fully completes before the other starts (no interleave).
    assert order in (
        ["start-A", "end-A", "start-B", "end-B"],
        ["start-B", "end-B", "start-A", "end-A"],
    ), order


@pytest.mark.asyncio
async def test_different_ips_run_concurrently():
    order: list[str] = []

    @_ftp_serialized
    async def op(ip_address: str, tag: str):
        order.append(f"start-{tag}")
        await asyncio.sleep(0.05)
        order.append(f"end-{tag}")

    await asyncio.gather(op("1.1.1.1", "A"), op("2.2.2.2", "B"))

    # Different printers don't block each other → both start before either ends.
    assert set(order[:2]) == {"start-A", "start-B"}, order


@pytest.mark.asyncio
async def test_decorator_passes_args_kwargs_and_ip_as_kwarg():
    @_ftp_serialized
    async def op(ip_address: str, a: int, b: int = 0):
        return (ip_address, a, b)

    assert await op("1.2.3.4", 1, b=2) == ("1.2.3.4", 1, 2)
    assert await op(ip_address="1.2.3.4", a=9) == ("1.2.3.4", 9, 0)


@pytest.mark.asyncio
async def test_with_ftp_retry_exponential_backoff(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(bambu_ftp.asyncio, "sleep", fake_sleep)

    async def always_fail(ip_address):
        return False  # falsy → treated as failure → retried

    result = await with_ftp_retry(always_fail, "1.2.3.4", max_retries=3, retry_delay=2.0, operation_name="t")
    assert result is None
    # 3 retries → exponential 2, 4, 8
    assert waits == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_with_ftp_retry_backoff_capped(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(bambu_ftp.asyncio, "sleep", fake_sleep)

    async def always_fail(ip_address):
        return False

    await with_ftp_retry(always_fail, "1.2.3.4", max_retries=2, retry_delay=20.0, operation_name="t")
    # 20, then min(40, 30) = 30 (capped)
    assert waits == [20.0, 30.0]
