"""A hand-built Manyfold, answering the v0 API as its source code does.

Built from the API study of ``manyfold3d/manyfold`` (serializers, controllers, Doorkeeper set-up) —
never recorded from, or checked against, a live instance. What it reproduces on purpose:

* absolute ``@id`` / ``contentUrl`` built from ``PUBLIC_HOSTNAME`` — here ``http://localhost:3214``,
  which is NOT the address the instance is reached at;
* a 401 that is the plain text ``Failed to Login`` under the API content type;
* a file list without file names or sizes; ``updateContentUrl`` (a signed write address) in a file;
* pages of the owner's size (2 here) with relative ``view`` links and ``previous`` (not ``prev``);
* a token endpoint that counts its requests (the real one allows 10 in 3 minutes).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, quote

import httpx

BASE = "http://manyfold.lan:3214"
PUBLIC = "http://localhost:3214"  # what Manyfold believes its own address is
API = "application/vnd.manyfold.v0+json"
CLIENT_ID = "client-abc"
CLIENT_SECRET = "s3cr3t-value"
STORAGE = "https://bucket.s3.example.com"
DNS = {"manyfold.lan": ["192.168.1.20"], "bucket.s3.example.com": ["52.216.1.1"]}
CONTEXT = ["https://schema.org/", {"hydra": "http://www.w3.org/ns/hydra/core#", "spdx": "http://spdx.org/rdf/terms#"}]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
STL = b"solid clip\nendsolid clip\n" + b" " * 100

MODEL = "x7q3k2pdbm4z"
FILES = {
    # id: (title, mime, path inside the model, bytes)
    "f1c2d3g4h5j6": ("Clip", "model/stl", "clip.stl", STL),
    "k7l8m9n0p1q2": ("Clip", "model/stl", "supported/clip v2.stl", STL + b"supports"),
    "r3s4t5v6w7x8": ("Plate", "model/3mf", "plate.3mf", b"PK\x03\x04" + b"\x00" * 60),
    "z9b0c1d2f3g4": ("Photo", "image/jpeg", "photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 60),
    "h5j6k7l8m9n0": ("Readme", "application/pdf", "readme.pdf", b"%PDF-1.4"),
    "p1q2r3s4t5v6": ("All Parts", "application/zip", "all parts.zip", b"PK\x03\x04zip"),
}


def openapi(*, raw: bool = True) -> dict:
    paths = {"/models": {}, "/models/{id}": {}, "/models/{model_id}/model_files/{id}": {}, "/creators/{id}": {}}
    if raw:
        paths["/models/{model_id}/raw/{filename}"] = {}
    return {"openapi": "3.0.1", "info": {"title": "Manyfold API", "version": "0"}, "paths": paths}


class FakeManyfold:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.sign_ins = 0
        self.token_serial = 0
        self.valid_tokens: set[str] = set()
        self.granted_scope = "public read"
        self.token_status: int | None = None  # force a status on the token endpoint (429, 301…)
        self.spec: dict | None = openapi()
        self.storage_redirect = False
        self.storage_status = 200
        self.models = {
            MODEL: {"name": "Cable Clip", "files": dict(FILES), "preview": "z9b0c1d2f3g4", "creator": "jane-doe"},
            "b4n8tq1zk0fw": {"name": "Тримач котушки", "files": {}, "preview": None, "creator": None},
            "m0d3l3aaaaaa": {"name": "Third", "files": {}, "preview": None, "creator": None},
        }
        self.creators = {"jane-doe": "Jane Doe"}
        self.sizes: dict[str, int] = {}
        self.content_url: dict[str, str] = {}
        self.render: dict[str, bytes] = {}

    # ---- helpers -------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def paths(self, host: str = "manyfold.lan") -> list[str]:
        return [request.url.path for request in self.requests if request.url.host == host]

    def _api(self, status: int, body) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode(), headers={"Content-Type": API})

    def _unauthorised(self) -> httpx.Response:
        return httpx.Response(401, content=b"Failed to Login", headers={"Content-Type": API})

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.valid_tokens

    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    # ---- the server ----------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "bucket.s3.example.com":
            return self._storage(request)
        if request.url.host != "manyfold.lan" or request.url.port != 3214:
            return httpx.Response(599, text="not this server")
        path = request.url.path
        if path == "/api/v0/openapi.json":
            if self.spec is None:
                return httpx.Response(404, text="<html>Not Found</html>")
            return httpx.Response(200, json=self.spec)
        if path == "/oauth/token" and request.method == "POST":
            return self._token(request)
        if not self._authorised(request):
            return self._unauthorised()
        api = request.headers.get("Accept") == API
        parts = path.strip("/").split("/")
        if parts == ["models"] and api:
            return self._list(request)
        if len(parts) == 2 and parts[0] == "models" and api:
            return self._model(parts[1])
        if len(parts) == 2 and parts[0] == "creators" and api:
            name = self.creators.get(parts[1])
            if name is None:
                return httpx.Response(404, headers={"Content-Type": API})
            return self._api(
                200, {"@id": f"{PUBLIC}/creators/{parts[1]}", "@type": "Organization", "name": name, "slug": parts[1]}
            )
        if len(parts) == 4 and parts[0] == "models" and parts[2] == "model_files":
            if api and "." not in parts[3]:
                return self._file(parts[1], parts[3])
            return self._derivative(parts[1], parts[3], request)
        if len(parts) >= 4 and parts[0] == "models" and parts[2] == "raw":
            return self._raw(parts[1], "/".join(parts[3:]), request)
        return httpx.Response(404, text="<html>")

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.sign_ins += 1
        if self.token_status == 429:
            return httpx.Response(429)
        if self.token_status in (301, 302):
            return httpx.Response(self.token_status, headers={"Location": "https://manyfold.lan/oauth/token"})
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") != "client_credentials":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client auth failed"})
        asked = set((form.get("scope") or "public").split())
        if not asked <= set(self.granted_scope.split()):
            return httpx.Response(400, json={"error": "invalid_scope", "error_description": "not allowed"})
        self.token_serial += 1
        token = f"token-{self.token_serial}"
        self.valid_tokens = {token}
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 7200,
                "scope": " ".join(sorted(asked)),
                "created_at": 1794000000,
            },
        )

    def _list(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        if "@" in query or query.startswith("http"):
            return httpx.Response(302, headers={"Location": f"{PUBLIC}/follows/new"})
        ids = [model_id for model_id, model in self.models.items() if query.lower() in model["name"].lower()]
        size = 2
        pages = max(1, -(-len(ids) // size))
        page = int(request.url.params.get("page", "1"))
        members = [
            {"@id": f"{PUBLIC}/models/{model_id}", "@type": "3DModel", "name": self.models[model_id]["name"]}
            for model_id in ids[(page - 1) * size : page * size]
        ]
        view = {
            "@id": f"/models?page={page}",
            "@type": "hydra:PartialCollectionView",
            "first": "/models?page=1",
            "last": f"/models?page={pages}",
        }
        if page > 1:
            view["previous"] = f"/models?page={page - 1}"
        if page < pages:
            view["next"] = f"/models?page={page + 1}"
        return self._api(
            200,
            {
                "@context": CONTEXT,
                "@id": "/models",
                "@type": "hydra:Collection",
                "totalItems": len(ids),
                "member": members,
                "view": view,
            },
        )

    def _model(self, model_id: str) -> httpx.Response:
        model = self.models.get(model_id)
        if model is None:
            return httpx.Response(404, headers={"Content-Type": API})
        body = {
            "@id": f"{PUBLIC}/models/{model_id}",
            "@type": "3DModel",
            "@context": CONTEXT,
            "name": model["name"],
            "hasPart": [
                {
                    "@id": f"{PUBLIC}/models/{model_id}/model_files/{file_id}",
                    "@type": "3DModel",
                    "name": title,
                    "encodingFormat": mime,
                }
                for file_id, (title, mime, _path, _data) in model["files"].items()
            ],
            "isPartOf": [],
            "sensitive": False,
            "keywords": ["clip", "material:petg"] if model["files"] else [],
            "links": [],
        }
        if model["files"]:
            body["caption"] = "Clip for 6 mm cables"
            body["spdx:license"] = {
                "@type": "spdx:License",
                "@id": "http://spdx.org/licenses/CC-BY-4.0",
                "licenseId": "CC-BY-4.0",
            }
        if model["creator"]:
            body["creator"] = {"@id": f"{PUBLIC}/creators/{model['creator']}", "@type": "Organization"}
        if model["preview"]:
            body["preview_file"] = {
                "@id": f"{PUBLIC}/models/{model_id}/model_files/{model['preview']}",
                "@type": "3DModel",
            }
        return self._api(200, body)

    def _file(self, model_id: str, file_id: str) -> httpx.Response:
        model = self.models.get(model_id)
        if model is None or file_id not in model["files"]:
            return httpx.Response(404, headers={"Content-Type": API})
        title, mime, path, data = model["files"][file_id]
        content_url = self.content_url.get(file_id, f"{PUBLIC}/models/{model_id}/raw/{quote(path)}")
        return self._api(
            200,
            {
                "@id": f"{PUBLIC}/models/{model_id}/model_files/{file_id}",
                "@type": "3DModel",
                "@context": CONTEXT,
                "name": title,
                "isPartOf": {"@id": f"{PUBLIC}/models/{model_id}", "@type": "3DModel"},
                "contentUrl": content_url,
                # A signed address with WRITE access: nothing may keep or log it.
                "updateContentUrl": f"{content_url}?sig=WRITE-SIGNATURE-DO-NOT-KEEP",
                "encodingFormat": mime,
                "contentSize": self.sizes.get(file_id, len(data)),
                "presupported": path.startswith("supported/"),
                "up": "+z",
            },
        )

    def _find(self, model_id: str, path: str):
        model = self.models.get(model_id) or {"files": {}}
        return next((entry for entry in model["files"].values() if entry[2] == path), None)

    def _raw(self, model_id: str, path: str, request: httpx.Request) -> httpx.Response:
        entry = self._find(model_id, path)
        if entry is None:
            return httpx.Response(404)
        if self.storage_redirect:
            signed = f"{STORAGE}/library/{model_id}/{quote(path)}?X-Amz-Signature=abc%2Fdef&X-Amz-Expires=900"
            return httpx.Response(302, headers={"Location": signed})
        return httpx.Response(200, content=entry[3], headers={"Content-Type": entry[1]})

    def _derivative(self, model_id: str, name: str, request: httpx.Request) -> httpx.Response:
        file_id = name.split(".", 1)[0]
        model = self.models.get(model_id)
        if model is None or file_id not in model["files"]:
            return httpx.Response(404)
        _title, mime, _path, data = model["files"][file_id]
        derivative = request.url.params.get("derivative")
        if derivative == "render" and file_id in self.render:
            # The render lies beside the file under ITS extension: the content type is the model's.
            return httpx.Response(200, content=self.render[file_id], headers={"Content-Type": mime})
        # No derivative: Manyfold sends the original file.
        return httpx.Response(200, content=data, headers={"Content-Type": mime})

    def _storage(self, request: httpx.Request) -> httpx.Response:
        if self.storage_status != 200:
            return httpx.Response(self.storage_status, text="<Error><Code>AccessDenied</Code></Error>")
        parts = request.url.path.strip("/").split("/", 2)  # library/<model>/<path>
        entry = self._find(parts[1], parts[2]) if len(parts) == 3 else None
        if entry is None or "X-Amz-Signature=abc%2Fdef" not in request.url.query.decode():
            return httpx.Response(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")
        return httpx.Response(200, content=entry[3], headers={"Content-Type": "binary/octet-stream"})
