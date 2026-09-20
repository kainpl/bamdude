"""Parse once per revision and requested plate, independently of fleet/copy count."""

import pytest

from backend.app.services import filament_requirements as reader
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf


@pytest.mark.asyncio
async def test_parse_count_scales_with_revision_and_plate_not_printers_or_copies(tmp_path, monkeypatch):
    filament = [{"id": 3, "type": "PETG", "used_g": "0.0001"}]
    path = write_routing_3mf(tmp_path / "many.3mf", {2: filament, 4: filament})
    original, calls = reader.read_print_requirements, []

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "read_print_requirements", counted)
    cache = reader.PrintRequirementsCache()
    for _copy in range(100):
        for _printer in range(23):
            assert (await cache.read(path, 2)).status == "ok"
    assert calls == [2]
    assert (await cache.read(path, 4)).status == "ok"
    assert calls == [2, 4]
    with path.open("ab") as stream:
        stream.write(b"new revision")
    assert (await cache.read(path, 2)).status == "ok"
    assert calls == [2, 4, 2]


@pytest.mark.asyncio
async def test_whole_file_resolution_seeds_exact_plate_cache_without_overriding_explicit_zero(tmp_path, monkeypatch):
    path = write_routing_3mf(tmp_path / "one.3mf", {15: [{"id": 1, "type": "PLA", "used_g": "1"}]})
    cache = reader.PrintRequirementsCache()
    whole = await cache.read(path, 0, archive_plate_id=1)
    assert whole.resolved_plate_id == 15
    assert await cache.read(path, 15) is whole
