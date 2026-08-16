"""The one 3MF downloader that lives outside the retry service still takes its lock.

``on_print_start`` creates the archive row before fetching the 3MF, so for the
whole length of that fetch the row looks exactly like one waiting to be filled:
``status='printing'`` with an empty ``file_path``. That is precisely what both
automatic retry triggers select on, and a printer reconnect mid-download is not
an edge case — the dispatcher causes reconnects itself.

⚠️ **A second downloader cannot notice the first.** ``_do_retry`` reads
``file_path`` *before* its own download, so its "already has a file"
early-return has long since passed by the time the handler attaches. It would
attach on top: a second archive directory cut, the first orphaned on disk.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services.archive_download_retry import ArchiveDownloadRetryService


@pytest.mark.asyncio
class TestClaim:
    async def test_a_claimed_archive_is_refused_to_the_retry_triggers(self):
        service = ArchiveDownloadRetryService()

        async with service.claim(7):
            assert await service.retry_archive(7) == "in_progress"

    async def test_the_claim_is_released_on_the_way_out(self):
        service = ArchiveDownloadRetryService()

        async with service.claim(7):
            pass

        assert 7 not in service._in_progress

    async def test_the_claim_is_released_even_when_the_download_raises(self):
        """A leaked claim is silent and permanent: that archive would never be
        retried again, by any trigger, including the manual one."""
        service = ArchiveDownloadRetryService()

        with pytest.raises(RuntimeError):
            async with service.claim(7):
                raise RuntimeError("FTP fell over")

        assert 7 not in service._in_progress

    async def test_it_never_releases_a_claim_it_did_not_take(self):
        """Releasing on someone else's behalf would reopen the very window this
        guard closes — while the first downloader is still running."""
        service = ArchiveDownloadRetryService()

        async with service.claim(7):
            async with service.claim(7):
                pass
            assert 7 in service._in_progress, "the inner block released the outer block's claim"

    async def test_other_archives_are_unaffected(self):
        service = ArchiveDownloadRetryService()

        async with service.claim(7):
            assert 8 not in service._in_progress

    async def test_a_concurrent_claim_does_not_deadlock_the_holder(self):
        """The guard is a set behind a lock, not a queue — a would-be second
        downloader is turned away immediately rather than waiting out a
        download that can take minutes."""
        service = ArchiveDownloadRetryService()

        async with service.claim(7):
            result = await asyncio.wait_for(service.retry_archive(7), timeout=1.0)

        assert result == "in_progress"
