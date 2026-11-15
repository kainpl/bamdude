"""A Spoolman that does its own weight arithmetic, for tests of the figures.

``FakeSpoolman`` (the extra-field double) stores whatever it is sent. The
weight tests need the part of Spoolman that is NOT a store: a spool keeps
``initial_weight`` and ``used_weight`` and *derives* ``remaining_weight`` from
them, so writing one moves the other. Every rule below was measured on a
live Spoolman 0.27.1 (2026-11-15):

* ``remaining_weight`` = ``initial_weight`` − ``used_weight`` (floored at 0),
  absent while the spool has no ``initial_weight`` — so ``used_weight: 0``
  reports the spool full;
* a spool created without ``initial_weight`` is stamped with its filament's
  ``weight``, and without ``spool_weight`` with its filament's, else its
  vendor's ``empty_spool_weight``; a later relink re-stamps neither;
* a written ``remaining_weight`` becomes ``used_weight = initial − remaining``
  and is refused (400) for a spool without a size; ``initial_weight`` is
  applied before ``remaining_weight`` within one PATCH;
* an unregistered extra key is refused, a ``text`` extra field takes only a
  value that decodes to a JSON string, and a PATCHed ``extra`` is merged;
* ``GET /spool`` embeds the filament, and the filament its vendor; a field
  that is null is left out of the answer.

Native tags (0.27), measured the same day:

* a spool and a filament carry ``tags: [{uid, format?, added}]``; a ``tags``
  key in a spool body is accepted and ignored;
* ``POST /spool/{id}/tag {uid, format?}`` answers 201 with the tag, the uid
  with ``: - _`` and spaces stripped and upper-cased; again for the same spool
  it is 201 and changes nothing; a uid that is not hex is 400, an empty one
  422, a spool that does not exist 404. An all-zero uid is ACCEPTED;
* a uid another spool holds, archived or not, or a filament, is 409
  ``{message, spool_id, filament_id}`` with the holder's id in its own field;
* ``DELETE /spool/{id}/tag/{uid}`` is 204, and 404 for a uid that spool does
  not hold;
* ``GET /spool?tag=`` lists the spools holding the uid after the same
  normalisation, archived ones only with ``allow_archived=true``; a uid that
  is not hex is 400; ``GET /tag/reader`` is 200 ``[]``;
* a query parameter Spoolman does not know is ignored.

``native_tags=False`` is a Spoolman from before 0.27. NOT measured, assumed
from the last rule and from 0.27.1's 404 for an unknown route: no ``tags`` key
anywhere, every tag route 404, and ``?tag=`` ignored - so it lists EVERY spool.

``add_spool`` stages a row directly and stamps only the size, so a test can
hold a spool as an OLDER Spoolman left it — no tare of its own, or (with
``initial_weight=None``) no size of its own — which is where the filament and
vendor fallbacks still decide.
"""

from __future__ import annotations

import json

import httpx

from backend.app.services.spoolman import SpoolmanClient
from backend.tests.unit.services.test_spoolman_extra_field_registration_2903 import FakeSpoolman

BASE_URL = "http://spoolman.test:7912"


class WeighingSpoolman(FakeSpoolman):
    def __init__(self, *, native_tags: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.native_tags = native_tags
        # uid -> {"kind": "spool" | "filament", "id", "format"}: one holder per uid.
        self.tags: dict[str, dict] = {}
        self.vendors: dict[int, dict] = {}
        self.filaments: dict[int, dict] = {}
        # Every request body, in order, as (method, path, body).
        self.calls: list[tuple[str, str, dict]] = []
        self._next_filament_id = 100

    # -- staging ----------------------------------------------------------
    def add_vendor(self, vendor_id: int, name: str, **fields) -> None:
        self.vendors[vendor_id] = {"id": vendor_id, "name": name, **fields}

    def add_filament(self, filament_id: int, *, vendor_id: int | None = None, **fields) -> None:
        self.filaments[filament_id] = {"id": filament_id, "vendor_id": vendor_id, **fields}

    def add_spool(self, spool_id: int, filament_id: int, *, used_weight: float = 0.0, **fields) -> None:
        fields.setdefault("initial_weight", self.filaments[filament_id].get("weight"))
        self.spools[spool_id] = {
            "id": spool_id,
            "filament_id": filament_id,
            "used_weight": used_weight,
            "archived": False,
            "extra": {},
            "registered": "2026-01-01T00:00:00Z",
            **fields,
        }
        self._next_id = max(self._next_id, spool_id + 1)

    def tag_spool(self, spool_id: int, uid: str, tag_format: str | None = None) -> None:
        """Stage a native tag as Spoolman's own UI or another tool would have linked it."""
        self.tags[_normalize_uid(uid)] = {"kind": "spool", "id": spool_id, "format": tag_format}

    def tag_filament(self, filament_id: int, uid: str) -> None:
        self.tags[_normalize_uid(uid)] = {"kind": "filament", "id": filament_id, "format": None}

    def tags_of(self, spool_id: int) -> list[str]:
        return sorted(uid for uid, t in self.tags.items() if t["kind"] == "spool" and t["id"] == spool_id)

    def _tag_views(self, kind: str, holder_id: int) -> list[dict]:
        views = []
        for uid, tag in self.tags.items():
            if tag["kind"] != kind or tag["id"] != holder_id:
                continue
            view = {"uid": uid, "added": "2026-11-15T12:00:00Z"}
            if tag["format"]:
                view["format"] = tag["format"]
            views.append(view)
        return views

    def bodies(self, method: str, path: str) -> list[dict]:
        return [body for m, p, body in self.calls if m == method and p == path]

    # -- what Spoolman answers --------------------------------------------
    def _filament_view(self, filament_id: int) -> dict:
        filament = dict(self.filaments[filament_id])
        vendor_id = filament.pop("vendor_id", None)
        if vendor_id is not None:
            filament["vendor"] = dict(self.vendors[vendor_id])
        if self.native_tags:
            filament["tags"] = self._tag_views("filament", filament_id)
        return filament

    def view(self, spool_id: int) -> dict:
        stored = {key: value for key, value in self.spools[spool_id].items() if value is not None}
        filament_id = stored.pop("filament_id")
        stored["filament"] = self._filament_view(filament_id)
        initial = stored.get("initial_weight")
        if initial is not None:
            stored["remaining_weight"] = max(float(initial) - float(stored["used_weight"]), 0.0)
        if self.native_tags:
            stored["tags"] = self._tag_views("spool", spool_id)
        return stored

    def _tag_route(self, method: str, path: str, body: dict) -> httpx.Response | None:
        """Answer a native-tag route, or None when ``path`` is not one."""
        parts = path.strip("/").split("/")
        if parts[0] == "tag":
            if not self.native_tags:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=[]) if parts == ["tag", "reader"] else None
        if len(parts) < 3 or parts[0] != "spool" or parts[2] != "tag":
            return None
        if not self.native_tags:
            return httpx.Response(404, json={"detail": "Not Found"})
        spool_id = int(parts[1])
        if spool_id not in self.spools:
            return httpx.Response(404, json={"message": f"No spool with ID {spool_id} found."})
        if method == "POST" and len(parts) == 3:
            raw = body.get("uid")
            if not isinstance(raw, str) or raw == "":
                return httpx.Response(422, json={"detail": [{"type": "string_too_short", "loc": ["body", "uid"]}]})
            uid = _normalize_uid(raw)
            if not _is_hex(uid):
                return httpx.Response(
                    400, json={"message": f'"{raw}" is not a valid tag UID. A UID must be hexadecimal.'}
                )
            held = self.tags.get(uid)
            if held is not None and (held["kind"], held["id"]) != ("spool", spool_id):
                return httpx.Response(
                    409,
                    json={
                        "message": f"Tag {uid} is already linked to {held['kind']} {held['id']}.",
                        "spool_id": held["id"] if held["kind"] == "spool" else None,
                        "filament_id": held["id"] if held["kind"] == "filament" else None,
                    },
                )
            if held is None:
                self.tags[uid] = {"kind": "spool", "id": spool_id, "format": body.get("format")}
            return httpx.Response(201, json=next(t for t in self._tag_views("spool", spool_id) if t["uid"] == uid))
        if method == "DELETE" and len(parts) == 4:
            uid = _normalize_uid(parts[3])
            held = self.tags.get(uid)
            if held is None or (held["kind"], held["id"]) != ("spool", spool_id):
                return httpx.Response(404, json={"message": f"Spool {spool_id} has no tag with UID {uid}."})
            del self.tags[uid]
            return httpx.Response(204)
        return httpx.Response(405, json={"detail": "Method Not Allowed"})

    def _reject_bad_extra(self, body: dict) -> httpx.Response | None:
        if (rejection := self._reject_unknown_extra(body)) is not None:
            return rejection
        for value in (body.get("extra") or {}).values():
            try:
                decoded = json.loads(value) if isinstance(value, str) else None
            except ValueError:
                decoded = None
            if not isinstance(decoded, str):
                return httpx.Response(400, json={"message": "Value is not a string."})
        return None

    def _apply(self, spool: dict, body: dict) -> httpx.Response | None:
        if "filament_id" in body:
            if body["filament_id"] not in self.filaments:
                return httpx.Response(404, json={"message": "No filament with that ID."})
            spool["filament_id"] = body["filament_id"]
        if "initial_weight" in body:
            spool["initial_weight"] = body["initial_weight"]
        if "used_weight" in body:
            spool["used_weight"] = float(body["used_weight"])
        if "remaining_weight" in body:
            initial = spool.get("initial_weight")
            if initial is None:
                return httpx.Response(400, json={"message": "remaining_weight needs an initial weight."})
            spool["used_weight"] = max(float(initial) - float(body["remaining_weight"]), 0.0)
        for key in ("price", "comment", "location", "spool_weight", "archived", "lot_nr", "last_used"):
            if key in body:
                spool[key] = body[key]
        if "extra" in body:
            spool["extra"] = {**(spool.get("extra") or {}), **body["extra"]}
        return None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else {}
        method = request.method
        if path.startswith(("/spool", "/filament", "/vendor")):
            self.calls.append((method, path, body))

        if (answer := self._tag_route(method, path, body)) is not None:
            self.log.append(f"{method} {path}")
            if not path.startswith("/spool"):
                self.calls.append((method, path, body))
            return answer

        if path == "/spool" and method == "GET":
            archived = request.url.params.get("allow_archived") == "true"
            listed = [sid for sid, s in self.spools.items() if archived or not s.get("archived")]
            if self.native_tags and "tag" in request.url.params:
                raw = request.url.params["tag"]
                uid = _normalize_uid(raw)
                if not uid:
                    return httpx.Response(400, json={"message": "A tag UID must not be empty."})
                if not _is_hex(uid):
                    return httpx.Response(
                        400, json={"message": f'"{raw}" is not a valid tag UID. A UID must be hexadecimal.'}
                    )
                held = self.tags.get(uid)
                listed = [sid for sid in listed if held is not None and (held["kind"], held["id"]) == ("spool", sid)]
            return httpx.Response(200, json=[self.view(sid) for sid in listed])

        if path == "/spool" and method == "POST":
            if (rejection := self._reject_bad_extra(body)) is not None:
                return rejection
            filament = self.filaments.get(body.get("filament_id"))
            if filament is None:
                return httpx.Response(404, json={"message": "No filament with that ID."})
            spool_id = self._next_id
            self._next_id += 1
            spool = {
                "id": spool_id,
                "filament_id": filament["id"],
                "used_weight": 0.0,
                "archived": False,
                "extra": {},
                "registered": "2026-01-01T00:00:00Z",
                "initial_weight": filament.get("weight"),
            }
            tare = filament.get("spool_weight")
            if tare is None and filament.get("vendor_id") is not None:
                tare = self.vendors[filament["vendor_id"]].get("empty_spool_weight")
            if tare is not None:
                spool["spool_weight"] = tare
            if (rejection := self._apply(spool, body)) is not None:
                return rejection
            self.spools[spool_id] = spool
            return httpx.Response(200, json=self.view(spool_id))

        if path.startswith("/spool/"):
            spool_id = int(path.rsplit("/", 1)[-1])
            if spool_id not in self.spools:
                return httpx.Response(404, json={"message": "No spool with that ID."})
            if method == "GET":
                return httpx.Response(200, json=self.view(spool_id))
            if method == "PATCH":
                if (rejection := self._reject_bad_extra(body)) is not None:
                    return rejection
                draft = dict(self.spools[spool_id])
                if (rejection := self._apply(draft, body)) is not None:
                    return rejection
                self.spools[spool_id] = draft
                return httpx.Response(200, json=self.view(spool_id))

        if path == "/filament" and method == "GET":
            return httpx.Response(200, json=[self._filament_view(fid) for fid in self.filaments])

        if path == "/filament" and method == "POST":
            filament_id = self._next_filament_id
            self._next_filament_id += 1
            self.filaments[filament_id] = {"id": filament_id, **body}
            return httpx.Response(200, json=self._filament_view(filament_id))

        if path.startswith("/filament/"):
            filament_id = int(path.rsplit("/", 1)[-1])
            if filament_id not in self.filaments:
                return httpx.Response(404, json={"message": "No filament with that ID."})
            if method == "PATCH":
                self.filaments[filament_id].update(body)
            return httpx.Response(200, json=self._filament_view(filament_id))

        if path == "/vendor" and method == "GET":
            return httpx.Response(200, json=list(self.vendors.values()))

        return await super().handler(request)


def _normalize_uid(raw: str) -> str:
    return "".join(ch for ch in raw if ch not in ":-_ ").upper()


def _is_hex(uid: str) -> bool:
    return bool(uid) and all(ch in "0123456789ABCDEF" for ch in uid)


def client_for(fake: WeighingSpoolman) -> SpoolmanClient:
    client = SpoolmanClient(BASE_URL)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return client
