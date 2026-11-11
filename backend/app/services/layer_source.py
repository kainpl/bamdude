"""Writer-owned, plate-scoped layer metadata. Selection never opens a file."""

MARKER = "_layer_source_v1"
SOURCE_FIELDS = ("file_path", "content_hash", "source_content_hash")


def positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def write_layer_source(archive, plates) -> None:
    """Stamp only plates parsed by this writer, alongside its final row fields."""
    extra = dict(archive.extra_data or {})
    extra.pop(MARKER, None)
    totals = {}
    seen = set()
    if isinstance(plates, list):
        for plate in plates:
            if not isinstance(plate, dict) or not positive_integer(plate.get("index")):
                totals = {}
                break
            index = plate["index"]
            if index in seen:
                totals = {}
                break
            seen.add(index)
            if positive_integer(plate.get("total_layers")):
                totals[str(index)] = plate["total_layers"]
    extra[MARKER] = {
        "version": 1,
        **{key: getattr(archive, key, None) for key in SOURCE_FIELDS},
        "plates": totals,
    }
    archive.extra_data = extra


def select_layer_total(archive, plate_index) -> int | None:
    if not positive_integer(plate_index) or not archive.file_path:
        return None
    extra = archive.extra_data
    marker = extra.get(MARKER) if isinstance(extra, dict) else None
    if not isinstance(marker, dict) or type(marker.get("version")) is not int or marker["version"] != 1:
        return None
    if any(key not in marker or marker[key] != getattr(archive, key, None) for key in SOURCE_FIELDS):
        return None
    plates = marker.get("plates")
    if not isinstance(plates, dict):
        return None
    # Reject noncanonical/ambiguous plate keys, including "01" and integer keys.
    if any(
        not isinstance(key, str) or not key.isascii() or not key.isdecimal() or key.startswith("0") for key in plates
    ):
        return None
    total = plates.get(str(plate_index))
    return total if positive_integer(total) else None
