"""The disposable worker of "Assemble project": meshes in, packed project 3MFs and plate pictures out.

Started by the assembly service through the worker guardian, one process per job. It reads a single
bootstrap line ``{"root", "deadline"}`` and then works only inside ``root``:

    job.json            what to do (see ``run``)
    src/…               the source files, copied there by main
    <key>.settings.json the project settings of each printer row, composed by main
    progress.json       rewritten while packing
    cancel              when present, the packing stops at its next step (an in-process run; the service
                        stops the whole process instead and never writes it)
    out/…               results: 3MF files and plate pictures
    result.json         written last

User files are parsed here and nowhere else. The module imports no application code beyond the pure
record helpers, opens no network connection and no database.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

MAX_TRIANGLES = 2_000_000  # per source mesh
JSON_BYTES = 256 * 1024
_PROGRESS_SECONDS = 0.5


class _Stop(Exception):
    def __init__(self, outcome: str, why: str, parts: list | None = None):
        super().__init__(why)
        self.outcome = outcome
        self.why = why
        self.parts = parts or []


def _write_json(path: Path, payload: dict) -> None:
    part = path.with_suffix(path.suffix + ".part")
    with part.open("w", encoding="utf-8") as output:
        json.dump(payload, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(part, path)


def _inside(root: Path, member: str) -> Path:
    path = (root / member).resolve()
    path.relative_to(root.resolve())
    return path


def _orient(vertices, orientation: str):
    import numpy as np

    x, y, z = vertices[:, 0], vertices[:, 1], vertices[:, 2]
    turned = {
        "as_is": (x, y, z),
        "x+90": (x, -z, y),
        "x-90": (x, z, -y),
        "y+90": (z, y, -x),
        "y-90": (-z, y, x),
        "x180": (x, -y, -z),
    }[orientation]
    out = np.column_stack(turned).astype(np.float64)
    return out - out.min(axis=0)  # on the bed, bounding box starting at the origin


def _load_mesh(path: Path, fmt: str):
    import numpy as np
    import trimesh

    # an OBJ may name material and texture files beside it: only the geometry of this one file is read
    extra = {"skip_materials": True} if fmt == "obj" else {}
    mesh = trimesh.load(str(path), file_type=fmt, force="mesh", process=False, **extra)
    vertices = np.asarray(getattr(mesh, "vertices", ()), dtype=np.float64)
    triangles = np.asarray(getattr(mesh, "faces", ()), dtype=np.int64)
    if vertices.ndim != 2 or triangles.ndim != 2 or len(vertices) == 0 or len(triangles) == 0:
        raise ValueError("no geometry")
    if len(triangles) > MAX_TRIANGLES:
        raise OverflowError("too many triangles")
    if not np.isfinite(vertices).all():
        raise ValueError("non-finite coordinate")
    mesh.merge_vertices()  # STL repeats every vertex per triangle; the 3MF should not
    return np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.faces, dtype=np.int64)


def _load_parts(root: Path, job: dict) -> list[tuple]:
    """``[(vertices, triangles)]`` in part order, oriented and on the bed."""
    import numpy as np

    parts_spec = job["parts"]
    meshes: list[tuple | None] = [None] * len(parts_spec)
    if job["mode"] == "relayout":
        from platenest.bambu3mf import read_project

        for source in job["sources"]:
            try:
                project = read_project(_inside(root, source["member"]).read_bytes())
            except (ValueError, OSError):
                raise _Stop("failed", "unreadable_project") from None
            for index, obj in enumerate(project.objects[: len(meshes)]):
                if meshes[index] is None and len(obj.vertices) and len(obj.triangles):
                    meshes[index] = (np.asarray(obj.vertices, np.float64), np.asarray(obj.triangles, np.int64))
    else:
        if len(job["sources"]) != len(parts_spec):
            raise _Stop("failed", "bad_job")
        for index, source in enumerate(job["sources"]):
            try:
                meshes[index] = _load_mesh(_inside(root, source["member"]), source["format"])
            except OverflowError:
                bad = [{"index": index, "name": parts_spec[index]["name"], "reason": "mesh_too_large"}]
                raise _Stop("failed", "mesh_too_large", bad) from None
            except Exception:  # a parser may raise anything on a hostile file; no message text (it can carry paths)
                bad = [{"index": index, "name": parts_spec[index]["name"], "reason": "unreadable_mesh"}]
                raise _Stop("failed", "unreadable_mesh", bad) from None
    missing = [
        {"index": i, "name": parts_spec[i]["name"], "reason": "mesh_missing"} for i, m in enumerate(meshes) if m is None
    ]
    if missing:
        raise _Stop("failed", "mesh_missing", missing)
    return [(_orient(m[0], parts_spec[i]["orientation"]), m[1]) for i, m in enumerate(meshes)]


def _pack_printer(
    root: Path,
    job: dict,
    printer: dict,
    index: int,
    meshes: list,
    deadline_ns: int | None,
    search_seconds: float = 0,
) -> dict:
    import cv2
    from platenest import Cancelled, Options, Part, Zone, pack, render_plate
    from platenest.bambu3mf import Instance, MeshObject, write_project

    from backend.app.services.assembly import record as rec

    layout, specs = job["layout"], job["parts"]
    zone_spec = printer["zone"]
    zone = Zone(
        zone_spec["width"],
        zone_spec["depth"],
        allowed=[tuple(map(tuple, polygon)) for polygon in zone_spec.get("allowed", [])],
        excluded=[tuple(map(tuple, polygon)) for polygon in zone_spec.get("excluded", [])],
        max_height=zone_spec.get("max_height"),
    )
    options = Options(
        spacing=float(layout["spacing"]),
        rotations=rec.rotations(layout["rotation_step"]),
        centre=bool(layout["centre"]),
    )
    parts = [
        Part(v, t, quantity=int(spec["quantity"]), fill=bool(spec["fill"]), name=spec["name"])
        for (v, t), spec in zip(meshes, specs, strict=True)
    ]

    last = [0.0]

    def progress(p) -> None:
        now = time.monotonic()
        diagnostic = getattr(p, "diagnostic", None)
        if now - last[0] < _PROGRESS_SECONDS and not diagnostic:
            return
        last[0] = now
        payload = {
            "printer": printer["key"],
            "printer_index": index,
            "printer_count": len(job["printers"]),
        }
        if hasattr(p, "best"):
            payload.update(
                search_best_copies=len(p.best.placements),
                search_best_plates=p.best.plate_count,
                search_elapsed_ms=int(p.elapsed * 1000),
                search_attempt=p.attempt,
                search_engine=1 if p.engine == "sparrow" else 0,
            )
            if diagnostic:
                payload["search_diagnostic"] = diagnostic
        else:
            payload.update(run=p.run, runs=p.runs, placed=p.placed, total=p.total, fill_part=p.fill_part)
        try:
            _write_json(root / "progress.json", payload)
        except OSError:
            # progress is a courtesy: on Windows the reader may hold the file open at the moment it is replaced
            pass

    def cancel() -> bool:
        if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
            raise _Stop("timeout", "deadline")
        return (root / "cancel").exists()

    try:
        if layout.get("long_search", False):
            from platenest import optimize

            result = optimize(
                parts,
                zone,
                options,
                seconds=search_seconds,
                free_rotation=layout["rotation_step"] == -1,
                progress=progress,
                cancel=cancel,
                finish=lambda: (root / "finish").exists(),
            )
        else:
            result = pack(parts, zone, options, progress=progress, cancel=cancel)
    except Cancelled:
        raise _Stop("canceled", "cancel") from None
    except ValueError:
        return {"key": printer["key"], "status": "failed", "reason": "bad_geometry", "parts": []}

    if result.rejected:
        rejected = [{"index": r.part, "name": specs[r.part]["name"], "reason": r.reason} for r in result.rejected]
        return {"key": printer["key"], "status": "failed", "reason": "part_rejected", "parts": rejected}

    plates = []
    for plate in range(result.plate_count):
        counts: dict[str, int] = {}
        for placement in result.placements:
            if placement.plate == plate:
                counts[str(placement.part)] = counts.get(str(placement.part), 0) + 1
        fill = any(specs[int(part)]["fill"] for part in counts)
        plates.append({"plate": plate, "counts": counts, "fill": fill})

    record = rec.AssemblyRecord(
        set_id=uuid.uuid4().hex,
        base_name=job["base_name"],
        # the meshes written below are already oriented, so a later re-layout starts from "as_is"
        parts=tuple(rec.PartInput(s["name"], int(s["quantity"]), bool(s["fill"]), "as_is") for s in specs),
        printer=rec.PrinterInput(**printer["record_printer"]),
        layout=rec.LayoutInput(
            float(layout["spacing"]),
            layout["rotation_step"],
            bool(layout["centre"]),
            bool(layout["split_plates"]),
            bool(layout.get("long_search", False)),
        ),
        plates=tuple(plates),
    )
    extra = {rec.RECORD_MEMBER: rec.dump(record)}
    objects = [MeshObject(spec["name"], v, t) for (v, t), spec in zip(meshes, specs, strict=True)]
    settings = _inside(root, printer["settings"]).read_bytes()
    bed_size = (float(printer["bed_size"][0]), float(printer["bed_size"][1]))
    out = root / "out"
    out.mkdir(exist_ok=True)

    def write(member: str, instances: list, title: str) -> None:
        data = write_project(
            objects, instances, bed_size=bed_size, project_settings=settings, title=title, extra_files=extra
        )
        (out / member).write_bytes(data)

    files = []
    if layout["split_plates"]:
        for plate in range(result.plate_count):
            member = f"{printer['key']}_{plate + 1}.3mf"
            # The plate's number is in the title so that no two files of a set are the same bytes: the library
            # reuses a row for content it already holds, and two full plates of one part are laid out alike.
            title = f"{job['base_name']} ({plate + 1})"
            write(
                member, [Instance(p.part, 0, p.x, p.y, p.angle) for p in result.placements if p.plate == plate], title
            )
            files.append({"member": f"out/{member}", "plate": plate})
    else:
        member = f"{printer['key']}.3mf"
        write(member, [Instance(p.part, p.plate, p.x, p.y, p.angle) for p in result.placements], job["base_name"])
        files.append({"member": f"out/{member}", "plate": None})

    for plate in plates:
        picture = f"{printer['key']}_plate_{plate['plate'] + 1}.png"
        ok, encoded = cv2.imencode(".png", render_plate(result, parts, zone, options, plate["plate"], scale=1))
        if ok:
            (out / picture).write_bytes(encoded.tobytes())
            plate["picture"] = f"out/{picture}"
        else:
            plate["picture"] = None

    return {
        "key": printer["key"],
        "status": "ok",
        "reason": "",
        "parts": [],
        "files": files,
        "plates": plates,
        "fill_counts": {str(k): v for k, v in result.fill_counts.items()},
        "fill_limited": sorted(result.fill_limited),
    }


def run(root: Path, deadline_ns: int | None = None) -> dict:
    """Do the job described by ``root/job.json`` and return what ``result.json`` will hold."""
    result: dict = {"outcome": "failed", "why": "unknown", "parts": [], "printers": []}
    try:
        raw = (root / "job.json").read_bytes()
        if len(raw) > JSON_BYTES:
            raise _Stop("failed", "bad_job")
        try:
            job = json.loads(raw)
            if job["mode"] not in ("new", "relayout") or not job["printers"] or not job["parts"]:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise _Stop("failed", "bad_job") from None
        import cv2

        cv2.setNumThreads(1)  # the worker environment pins BLAS threads, not OpenCV's own
        meshes = _load_parts(root, job)
        search_deadline = time.monotonic() + 300
        for index, printer in enumerate(job["printers"]):
            share = max(0.0, search_deadline - time.monotonic()) / (len(job["printers"]) - index)
            result["printers"].append(_pack_printer(root, job, printer, index, meshes, deadline_ns, share))
        result.update(outcome="ok", why="")
    except _Stop as stop:
        result.update(outcome=stop.outcome, why=stop.why, parts=stop.parts)
    except MemoryError:
        result.update(outcome="resource_limit", why="memory")
    except Exception as error:  # closed: the type only, never the message
        result.update(outcome="failed", why=type(error).__name__)
    if result["outcome"] != "ok":  # an unfinished run leaves nothing a reader could mistake for a result
        for leftover in (root / "out").glob("*") if (root / "out").is_dir() else ():
            leftover.unlink(missing_ok=True)
        result["printers"] = []
    return result


def main() -> int:
    request = json.loads(sys.stdin.buffer.readline(16385))
    root = Path(request["root"]).resolve(strict=True)
    os.chdir(root)
    started = time.monotonic()
    result = run(root, int(request["deadline"]) if request.get("deadline") else None)
    result["pid"] = os.getpid()
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    try:
        _write_json(root / "result.json", result)
    except Exception:
        return 3
    return 0 if result["outcome"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
