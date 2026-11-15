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
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
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

    def bodies(self, method: str, path: str) -> list[dict]:
        return [body for m, p, body in self.calls if m == method and p == path]

    # -- what Spoolman answers --------------------------------------------
    def _filament_view(self, filament_id: int) -> dict:
        filament = dict(self.filaments[filament_id])
        vendor_id = filament.pop("vendor_id", None)
        if vendor_id is not None:
            filament["vendor"] = dict(self.vendors[vendor_id])
        return filament

    def view(self, spool_id: int) -> dict:
        stored = {key: value for key, value in self.spools[spool_id].items() if value is not None}
        filament_id = stored.pop("filament_id")
        stored["filament"] = self._filament_view(filament_id)
        initial = stored.get("initial_weight")
        if initial is not None:
            stored["remaining_weight"] = max(float(initial) - float(stored["used_weight"]), 0.0)
        return stored

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

        if path == "/spool" and method == "GET":
            archived = request.url.params.get("allow_archived") == "true"
            return httpx.Response(
                200, json=[self.view(sid) for sid, s in self.spools.items() if archived or not s.get("archived")]
            )

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


def client_for(fake: WeighingSpoolman) -> SpoolmanClient:
    client = SpoolmanClient(BASE_URL)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return client
