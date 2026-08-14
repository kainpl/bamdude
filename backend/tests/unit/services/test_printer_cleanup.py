"""Which files on the printer may be removed after a print.

One upload becomes several files on the machine: we send ``Cube.3mf`` and the
printer writes ``Cube.gcode.3mf`` into ``/cache`` and, when "store sent files to
storage" is on, into internal storage as well. Cleanup looked for exactly the
name it had uploaded, so it saw neither and reported "nothing to delete" — true
by its own rule, and the rule was the bug.

The property that matters most here is the one that is expensive to get wrong:
a file whose name matches but whose bytes do not is somebody else's, and
deleting it is the only mistake in this module that cannot be undone.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from backend.app.services.printer_cleanup import (
    archive_hashes,
    derived_copy_names,
    remove_verified_copies,
)


class TestWhatThePrinterMakesOfOurUpload:
    def test_a_plain_3mf_gains_a_gcode_sibling(self) -> None:
        assert derived_copy_names("Cube.3mf") == ["Cube.gcode.3mf"]

    def test_a_path_is_reduced_to_its_name(self) -> None:
        # The two media disagree about what a path looks like; the name is the
        # part they share.
        assert derived_copy_names("/cache/Cube.3mf") == ["Cube.gcode.3mf"]

    def test_the_derived_form_does_not_derive_again(self) -> None:
        """⚠️ Feeding this its own output must not invent Cube.gcode.gcode.3mf.

        The candidate list is built by looping, and a second pass over it is
        exactly the kind of change somebody makes later.
        """
        assert derived_copy_names("Cube.gcode.3mf") == []

    def test_something_that_is_not_a_3mf_yields_nothing(self) -> None:
        assert derived_copy_names("Cube.gcode") == []
        assert derived_copy_names("plate_1.png") == []

    def test_case_is_ignored_for_the_test_but_kept_in_the_answer(self) -> None:
        # Printers have shipped both spellings; the name we ask for has to be
        # the one on the machine.
        assert derived_copy_names("CUBE.3MF") == ["CUBE.gcode.3mf"]


class TestWhichDigestsIdentifyThisPrint:
    def test_both_are_taken(self) -> None:
        """They differ exactly when a 3MF patch was applied, and the printer's
        copy is a copy of the bytes it RECEIVED — so which one matches depends
        on whether this print was patched."""
        archive = SimpleNamespace(source_content_hash="AAA", content_hash="bbb")
        assert archive_hashes(archive) == {"aaa", "bbb"}

    def test_missing_and_empty_are_not_digests(self) -> None:
        archive = SimpleNamespace(source_content_hash=None, content_hash="   ")
        assert archive_hashes(archive) == set()


def _entry(name: str, path: str | None = None, is_dir: bool = False) -> dict:
    return {"name": name, "path": path or f"/cache/{name}", "is_directory": is_dir}


class TestRemovingThePrintersCopies:
    @pytest.mark.asyncio
    async def test_a_matching_copy_is_read_and_removed(self) -> None:
        payload = b"the 3mf we sent"
        digest = hashlib.sha256(payload).hexdigest()
        deleted: list[str] = []

        removed = await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes={digest},
            read_bytes=lambda p: _async(payload),
            delete=lambda p: _async(deleted.append(p)),
            label="test",
        )

        assert removed == 1
        assert deleted == ["/cache/Cube.gcode.3mf"]

    @pytest.mark.asyncio
    async def test_the_same_name_with_other_bytes_survives(self) -> None:
        """⚠️ The whole reason this function reads before it deletes.

        ``Cube.gcode.3mf`` is exactly what a print sent from BambuStudio leaves
        behind, so the name alone cannot authorise a delete.
        """
        deleted: list[str] = []

        removed = await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes={hashlib.sha256(b"ours").hexdigest()},
            read_bytes=lambda p: _async(b"somebody else's"),
            delete=lambda p: _async(deleted.append(p)),
            label="test",
        )

        assert removed == 0
        assert deleted == []

    @pytest.mark.asyncio
    async def test_a_name_nobody_asked_for_is_never_read(self) -> None:
        # Cheap filter first: hashing every file in /cache would mean
        # downloading the whole directory on every print.
        reads: list[str] = []

        await remove_verified_copies(
            entries=[_entry("SomeoneElse.gcode.3mf"), _entry("plate_1.png")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes={"whatever"},
            read_bytes=lambda p: _async(reads.append(p) or b""),
            delete=lambda p: _async(None),
            label="test",
        )

        assert reads == []

    @pytest.mark.asyncio
    async def test_a_directory_is_never_a_candidate(self) -> None:
        deleted: list[str] = []

        await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf", is_dir=True)],
            wanted={"Cube.gcode.3mf"},
            expected_hashes={"whatever"},
            read_bytes=lambda p: _async(b""),
            delete=lambda p: _async(deleted.append(p)),
            label="test",
        )

        assert deleted == []

    @pytest.mark.asyncio
    async def test_a_printer_that_cannot_be_read_keeps_its_file(self) -> None:
        """Best-effort: every failure path leaves the file, never removes it."""
        deleted: list[str] = []

        async def _boom(_path):
            raise OSError("connection reset")

        removed = await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes={"whatever"},
            read_bytes=_boom,
            delete=lambda p: _async(deleted.append(p)),
            label="test",
        )

        assert removed == 0
        assert deleted == []

    @pytest.mark.asyncio
    async def test_with_no_digest_nothing_happens_by_default(self) -> None:
        """A print we cannot identify is not a licence to delete by name."""
        deleted: list[str] = []

        removed = await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes=set(),
            read_bytes=lambda p: _async(b"anything"),
            delete=lambda p: _async(deleted.append(p)),
            label="test",
        )

        assert removed == 0
        assert deleted == []

    @pytest.mark.asyncio
    async def test_but_the_job_that_just_finished_may_opt_out_of_the_proof(self) -> None:
        """A print BamDude picked up rather than sent — from the slicer or the
        printer's own screen — whose 3MF was never recovered has no digest at
        all. Its name is still bound to the job that has just ended on this
        machine, which is a narrower claim than "any file called this".
        """
        deleted: list[str] = []

        removed = await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes=set(),
            read_bytes=lambda p: _async(b"never read"),
            delete=lambda p: _async(deleted.append(p)),
            label="test",
            allow_unverified=True,
        )

        assert removed == 1
        assert deleted == ["/cache/Cube.gcode.3mf"]

    @pytest.mark.asyncio
    async def test_the_unverified_path_does_not_download_what_it_cannot_check(self) -> None:
        reads: list[str] = []

        await remove_verified_copies(
            entries=[_entry("Cube.gcode.3mf")],
            wanted={"Cube.gcode.3mf"},
            expected_hashes=set(),
            read_bytes=lambda p: _async(reads.append(p) or b""),
            delete=lambda p: _async(None),
            label="test",
            allow_unverified=True,
        )

        assert reads == []


async def _async(value):
    return value
