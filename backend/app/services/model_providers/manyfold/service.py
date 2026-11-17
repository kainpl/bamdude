"""Manyfold API client (upstream 3480d3943, #1471 — rebuilt on the configured-origin transport).

Talks to a self-hosted Manyfold through its ``v0`` API: list and search
models, read a model and its files, download one file, fetch a model's
preview. BamDude signs in with an OAuth application's client id and secret
(client credentials) and asks for the ``public read`` scopes; it never writes
to Manyfold.

Rules this client keeps, each for a reason found in Manyfold's own code:

* **Every request is built from the configured base and validated ids.** The
  absolute URLs in Manyfold's answers come from its ``PUBLIC_HOSTNAME`` (often
  ``http://localhost:3214``): they are read for the ids and the file name they
  carry, never fetched.
* **Capabilities, not versions.** An instance says nowhere which version it is;
  its OpenAPI document does say whether the raw-file path exists (Manyfold
  v0.149; the practical minimum is v0.150). Read once per base, before the
  first sign-in.
* **The token endpoint allows 10 requests in 3 minutes**, so the token is cached
  until shortly before it expires, one sign-in runs at a time, and a refusal is
  remembered briefly instead of being asked for again by every tile of a page.
* **No ``order`` parameter**: Manyfold stores it in the application owner's
  profile. The page size is that owner's setting too; we page with what comes.
* A file answer may carry ``updateContentUrl`` — a signed address with write
  access. Only named fields are ever copied out of an answer, and bodies are
  never logged.

All network I/O goes through :class:`GuardedTransport`; this module imports no
HTTP library (``test_model_providers_fetch_only_through_the_guard.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.base import (
    BrowsableService,
    ProviderDownload,
    ProviderDownloadInfo,
    ProviderError,
    ProviderResolvedModel,
    ProviderResourceRef,
    ProviderService,
    ProviderStatus,
)
from backend.app.services.model_providers.manyfold.config import ManyfoldConfig
from backend.app.services.model_providers.outbound import (
    GuardedAnswer,
    GuardedTransport,
    OutboundRefused,
    PinnedOrigin,
)

logger = logging.getLogger(__name__)

SOURCE_TYPE = "manyfold"
API_MEDIA_TYPE = "application/vnd.manyfold.v0+json"
SCOPES = "public read"
MINIMUM_VERSION = "0.150"

# Manyfold's public ids are 12 characters of a reduced alphabet; a creator is addressed by a slug.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,128}$")
_MODEL_ID_IN_URL = re.compile(r"/models/([A-Za-z0-9_-]{1,64})(?:/|$)")
_FILE_ID_IN_URL = re.compile(r"/model_files/([A-Za-z0-9_-]{1,64})(?:[/.?]|$)")
_CREATOR_IN_URL = re.compile(r"/creators/([^/?#]+)/?$")
_RAW_PATH_IN_SPEC = re.compile(r"^/models/\{[^}/]+\}/raw/\{[^}/]+\}$")
_PAGE_IN_LINK = re.compile(r"[?&]page=(\d{1,9})(?:&|$)")

# What the library can slice or show, by Manyfold's MIME type (it derives it from the extension)
# and by the extension of the real file name.
IMPORTABLE_MIME_TYPES = frozenset({"model/3mf", "model/stl", "model/step", "model/obj"})
IMPORTABLE_EXTENSIONS = (".3mf", ".stl", ".step", ".stp", ".obj")

MAX_FILE_BYTES = 1024 * 1024 * 1024  # Manyfold's own default upload limit
MAX_PREVIEW_BYTES = 10 * 1024 * 1024
#: How many of a model's importable files get their name and size read when the model is opened.
FILE_DETAIL_LIMIT = 60
FILE_DETAIL_CONCURRENCY = 4
JSON_DEADLINE_SECONDS = 30.0
PREVIEW_DEADLINE_SECONDS = 30.0
DOWNLOAD_DEADLINE_SECONDS = 30 * 60.0
MAX_QUERY_LENGTH = 200

# Renew this long before Manyfold's own expiry, so a request never starts with a dying token.
_TOKEN_MARGIN_SECONDS = 120
_REFUSED_FOR_SECONDS = 30.0
_CAPABLE_FOR_SECONDS = 3600.0

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# (base, client id, secret digest) -> (token, monotonic expiry)
_token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}
# A sign-in Manyfold just refused (or rate-limited), and until when that answer is reused.
_refused: dict[tuple[str, str, str], tuple[ManyfoldError, float]] = {}
# base -> monotonic time until which its capabilities are known good
_capable: dict[str, float] = {}
#: How long a request waits for a sign-in somebody else is making before it gives up.
SIGN_IN_WAIT_SECONDS = 20.0
#: An unreachable instance is not asked again by every tile of a page for this long.
_UNREACHABLE_FOR_SECONDS = 10.0
_token_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_token_lock_loop: asyncio.AbstractEventLoop | None = None


def _sign_in_lock(key: tuple[str, str, str]) -> asyncio.Lock:
    """One sign-in at a time PER connection (a draft being tested never holds up the saved one);
    bound to the running loop, recreated if it changes."""
    global _token_lock_loop
    loop = asyncio.get_running_loop()
    if _token_lock_loop is not loop:
        _token_locks.clear()
        _token_lock_loop = loop
    if key not in _token_locks:
        if len(_token_locks) > 64:  # drafts come and go; an idle lock is nobody's
            for stale in [known for known, lock in _token_locks.items() if not lock.locked()]:
                del _token_locks[stale]
        _token_locks[key] = asyncio.Lock()
    return _token_locks[key]


class ManyfoldError(ProviderError):
    """A Manyfold failure. ``code`` names it for the frontend; ``status`` is what our API answers.

    Never 401 or 403: those are OUR session's words (``api/client.ts`` reads a 401 as "signed out"),
    and a refusal by somebody else's server must not look like one.
    """

    code = "manyfold_failed"
    status = 502

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status


class ManyfoldNotConfigured(ManyfoldError):
    code = "manyfold_not_configured"
    status = 409


class ManyfoldCredentials(ManyfoldError):
    """Manyfold refused the client id / secret, or the application lacks the scope."""

    code = "manyfold_credentials"
    status = 409


class ManyfoldNotFound(ManyfoldError):
    """Not there, or not visible to the application's owner — Manyfold answers both the same."""

    code = "manyfold_not_found"
    status = 404


class ManyfoldRefused(ManyfoldError):
    """Our own refusal of a request (a bad id, a file that is not importable)."""

    code = "manyfold_refused"
    status = 400


class ManyfoldUnavailable(ManyfoldError):
    """Unreachable, failing, too old, or answering something unexpected."""


def clear_caches() -> None:
    """Forget tokens, refusals and capabilities — after the connection was edited or restored."""
    _token_cache.clear()
    _refused.clear()
    _capable.clear()
    for key in [known for known, lock in _token_locks.items() if not lock.locked()]:
        del _token_locks[key]


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ManyfoldNotFound("That is not a Manyfold id")
    return value


def _id_from(url: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        match = pattern.search(urlsplit(url).path)
    except ValueError:
        return None
    return match.group(1) if match else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def image_type(head: bytes) -> str | None:
    for signature, mime in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_importable_filename(filename: str) -> bool:
    return filename.lower().endswith(IMPORTABLE_EXTENSIONS)


def _from_transport(exc: OutboundRefused) -> ManyfoldError:
    if exc.reason == "too_large":
        return ManyfoldUnavailable("The file is larger than BamDude accepts from Manyfold", "manyfold_too_large", 413)
    if exc.reason == "unreachable":
        return ManyfoldUnavailable(f"Could not reach Manyfold. {exc}", "manyfold_unreachable")
    if exc.reason == "encoded":
        return ManyfoldUnavailable(
            "Manyfold sent a compressed answer although BamDude asked for a plain one. "
            "Switch off response compression for BamDude on the proxy in front of Manyfold.",
            "manyfold_incompatible",
        )
    if exc.reason == "transport_disabled":
        return ManyfoldUnavailable(
            "Manyfold is unavailable: this BamDude installation cannot pin its connections to checked "
            "addresses. Reinstall its Python packages from requirements.txt.",
            "manyfold_transport_unavailable",
            503,
        )
    if exc.reason in {"foreign_refused", "redirect_chain", "bad_redirect", "address_refused"}:
        return ManyfoldUnavailable(
            f"Manyfold keeps this file in storage BamDude will not fetch from. {exc}", "manyfold_storage_refused"
        )
    return ManyfoldUnavailable("Manyfold could not be asked for that")


class ManyfoldService(ProviderService, BrowsableService):
    """Per-request Manyfold client."""

    streams_downloads = True

    def __init__(self, config: ManyfoldConfig, *, transport: GuardedTransport | None = None):
        self._config = config
        self._transport = transport
        self._base = config.url.rstrip("/")
        if transport is None and config.configured:
            try:
                self._transport = GuardedTransport(PinnedOrigin.admit(self._base, label="Manyfold URL"))
            except ValueError:
                self._transport = None  # a stored address that no longer passes: reported on use
        # Per request: a model's files are read once however many callers ask.
        self._files: dict[tuple[str, str], dict[str, Any]] = {}
        self._models: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()

    # ---- readiness ---------------------------------------------------

    def _ready(self) -> GuardedTransport:
        if not self._config.configured:
            raise ManyfoldNotConfigured("Manyfold is not connected. An administrator sets it up in Settings.")
        if not self._config.enabled:
            raise ManyfoldNotConfigured("Manyfold is switched off in Settings.", "manyfold_disabled")
        if self._transport is None:
            raise ManyfoldNotConfigured("The saved Manyfold address is not usable. Save the connection again.")
        return self._transport

    async def _ask(self, call: Callable[[], Any], deadline: float = JSON_DEADLINE_SECONDS) -> GuardedAnswer:
        try:
            async with asyncio.timeout(deadline):
                return await call()
        except OutboundRefused as exc:
            raise _from_transport(exc) from exc
        except TimeoutError as exc:
            raise ManyfoldUnavailable("Manyfold did not answer in time", "manyfold_unreachable") from exc

    async def capabilities(self, *, fresh: bool = False) -> None:
        """Raise unless this instance serves what we need. Read without a token."""
        transport = self._ready()
        if not fresh and _capable.get(self._base, 0.0) > time.monotonic():
            return
        answer = await self._ask(lambda: transport.get("/api/v0/openapi.json", accept="application/json"))
        if answer.redirect == "scheme_upgrade":
            raise ManyfoldUnavailable(
                "Manyfold redirects to https. Enter its address with https://.", "manyfold_wrong_scheme"
            )
        spec = answer.json() if answer.status == 200 and answer.redirect is None else None
        paths = spec.get("paths") if isinstance(spec, dict) else None
        if not isinstance(paths, dict):
            raise ManyfoldUnavailable(
                f"This address does not answer like Manyfold {MINIMUM_VERSION} or newer.", "manyfold_not_manyfold"
            )
        if "/models" not in paths or not any(_RAW_PATH_IN_SPEC.match(str(path)) for path in paths):
            raise ManyfoldUnavailable(
                f"This Manyfold is too old (needs {MINIMUM_VERSION} or newer).", "manyfold_too_old"
            )
        _capable[self._base] = time.monotonic() + _CAPABLE_FOR_SECONDS

    # ---- sign-in -------------------------------------------------------

    def _cache_key(self) -> tuple[str, str, str]:
        digest = hashlib.sha256(self._config.client_secret.encode()).hexdigest()
        return (self._base, self._config.client_id, digest)

    async def _token(self, *, refused: str | None = None) -> str:
        """The connection's token. ``refused`` is one Manyfold just answered 401 to: it is replaced
        — by ONE sign-in, however many requests met that 401 (the rest take the new token).

        The cache is read before the lock, so a request with a good token never waits for a
        sign-in; waiting for one has a deadline.
        """
        self._ready()
        key = self._cache_key()

        def cached() -> str | None:
            entry = _token_cache.get(key)
            return entry[0] if entry and entry[0] != refused and entry[1] > time.monotonic() else None

        token = cached()
        if token:
            return token
        lock = _sign_in_lock(key)
        try:
            async with asyncio.timeout(SIGN_IN_WAIT_SECONDS):
                await lock.acquire()
        except TimeoutError as exc:
            raise ManyfoldUnavailable("Manyfold is still answering an earlier sign-in", "manyfold_unreachable") from exc
        try:
            token = cached()
            if token:
                return token
            earlier = _refused.get(key)
            if earlier and earlier[1] > time.monotonic():
                raise type(earlier[0])(str(earlier[0]), earlier[0].code, earlier[0].status)
            try:
                await self.capabilities()
                return await self._sign_in(key)
            except ManyfoldError as exc:
                if isinstance(exc, ManyfoldCredentials) or exc.code == "manyfold_rate_limited":
                    _refused[key] = (exc, time.monotonic() + _REFUSED_FOR_SECONDS)
                elif exc.code == "manyfold_unreachable":
                    _refused[key] = (exc, time.monotonic() + _UNREACHABLE_FOR_SECONDS)
                raise
        finally:
            lock.release()

    async def _sign_in(self, key: tuple[str, str, str]) -> str:
        """Ask Manyfold for a token; the caller holds the sign-in lock."""
        transport = self._ready()
        form = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "scope": SCOPES,
        }
        answer = await self._ask(lambda: transport.post_form("/oauth/token", form))
        if answer.redirect is not None:
            # Never followed: a redirected POST loses its body, and its body is the secret.
            if answer.redirect == "scheme_upgrade":
                raise ManyfoldUnavailable(
                    "Manyfold redirects to https. Enter its address with https://.", "manyfold_wrong_scheme"
                )
            raise ManyfoldUnavailable("Manyfold redirected the sign-in. Check its address.", "manyfold_redirected")
        if answer.status == 429:
            raise ManyfoldUnavailable(
                "Manyfold is limiting sign-ins. Try again in a few minutes.", "manyfold_rate_limited"
            )
        body = answer.json()
        error = body.get("error") if isinstance(body, dict) else None
        scope_message = (
            "The Manyfold application lacks the 'read' scope. Create an application with the public and read scopes."
        )
        if error == "invalid_scope":
            raise ManyfoldCredentials(scope_message, "manyfold_scope")
        if answer.status in (400, 401) or error in ("invalid_client", "unauthorized_client"):
            raise ManyfoldCredentials("Manyfold did not accept the client ID or secret.")
        if answer.status != 200 or not isinstance(body, dict):
            raise ManyfoldUnavailable(f"Manyfold sign-in failed with HTTP {answer.status}")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise ManyfoldUnavailable("Manyfold sign-in returned no token")
        granted = str(body.get("scope") or "").split()
        if granted and "read" not in granted:
            raise ManyfoldCredentials(scope_message, "manyfold_scope")
        try:
            lifetime = float(body.get("expires_in") or 7200)
        except (TypeError, ValueError):
            lifetime = 7200.0
        _token_cache[key] = (token, time.monotonic() + max(0.0, lifetime - _TOKEN_MARGIN_SECONDS))
        return token

    # ---- requests ------------------------------------------------------

    @staticmethod
    def _raise_for(answer: GuardedAnswer, what: str) -> None:
        status = answer.status
        if answer.redirect is not None:
            if answer.redirect == "scheme_upgrade":
                raise ManyfoldUnavailable(
                    "Manyfold redirects to https. Enter its address with https://.", "manyfold_wrong_scheme"
                )
            # Manyfold's own redirect of an API request: a sign-in page (no access) or a hijacked search.
            raise ManyfoldNotFound(f"Manyfold would not show {what}", "manyfold_redirected")
        if status == 401:
            raise ManyfoldCredentials("Manyfold did not accept BamDude's sign-in.")
        if status == 403:
            raise ManyfoldCredentials(
                f"Manyfold refused access to {what}. The application needs the 'read' scope.", "manyfold_scope"
            )
        if status == 404:
            raise ManyfoldNotFound(f"Manyfold could not find {what}")
        if status == 429:
            raise ManyfoldUnavailable("Manyfold is limiting requests. Try again shortly.", "manyfold_rate_limited")
        if status >= 400:
            raise ManyfoldUnavailable(f"Manyfold answered HTTP {status} for {what}")

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> GuardedAnswer:
        """GET with the token; a 401 drops the cached token and asks once more."""
        transport = self._ready()
        answer: GuardedAnswer | None = None
        token: str | None = None
        for attempt in range(2):
            token = await self._token(refused=token)
            answer = await self._ask(lambda: transport.get(path, params=params, accept=API_MEDIA_TYPE, bearer=token))  # noqa: B023
            if answer.status != 401 or attempt:
                break
        return answer

    async def _get_json(self, path: str, what: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await self._get(path, params=params)
        self._raise_for(answer, what)
        body = answer.json()
        if not isinstance(body, dict):
            raise ManyfoldUnavailable(f"Manyfold sent an unexpected answer for {what}", "manyfold_incompatible")
        return body

    # ---- catalogue -----------------------------------------------------

    async def list_models(self, *, query: str = "", page: int = 1) -> dict[str, Any]:
        page = max(1, int(page))
        params: dict[str, Any] = {"page": page}
        query = (query or "").strip()
        if len(query) > MAX_QUERY_LENGTH:
            raise ManyfoldRefused("That search is too long")
        if query:
            params["q"] = query
        try:
            body = await self._get_json("/models", "the model list", params)
        except ManyfoldNotFound as exc:
            if query and exc.code == "manyfold_redirected":
                # A query Manyfold cannot parse, an address or a ``name@host`` is answered with a redirect.
                raise ManyfoldRefused("Manyfold did not understand that search.", "manyfold_query_refused") from exc
            raise
        members = body.get("member") if isinstance(body.get("member"), list) else []
        models = []
        for member in members:
            if not isinstance(member, dict):
                continue
            model_id = _id_from(member.get("@id"), _MODEL_ID_IN_URL)
            if model_id:
                models.append({"id": model_id, "name": str(member.get("name") or model_id)})
        view = body.get("view") if isinstance(body.get("view"), dict) else {}
        total = body.get("totalItems")
        last = _PAGE_IN_LINK.search(str(view.get("last") or ""))
        return {
            "total": total if isinstance(total, int) and total >= 0 else None,
            "page": page,
            "pages": int(last.group(1)) if last else None,
            "has_next": bool(view.get("next")),
            "has_previous": bool(view.get("previous")),
            "models": models,
        }

    async def _creator_name(self, creator: Any) -> tuple[str | None, str | None]:
        """``(slug, name)`` of a model's creator; the name is one more request, best effort."""
        raw = creator.get("@id") if isinstance(creator, dict) else None
        try:
            match = _CREATOR_IN_URL.search(urlsplit(raw).path) if isinstance(raw, str) else None
        except ValueError:
            match = None
        slug = unquote(match.group(1)) if match else None
        if not slug or not _SLUG_RE.match(slug):
            return None, None
        try:
            body = await self._get_json(f"/creators/{quote(slug, safe='')}", "the creator")
        except ManyfoldError:
            return slug, None  # not visible to the application's owner: the model still is
        return slug, _text(body.get("name"))

    async def _model_body(self, model_id: str) -> dict[str, Any]:
        if model_id not in self._models:
            self._models[model_id] = await self._get_json(f"/models/{model_id}", "this model")
        return self._models[model_id]

    async def get_model(self, model_id: str, *, details: bool = True) -> dict[str, Any]:
        """A model and its files. ``details`` reads each importable file's name and size (bounded)."""
        model_id = valid_id(model_id)
        body = await self._model_body(model_id)
        files: list[dict[str, Any]] = []
        for part in body.get("hasPart") or []:
            if not isinstance(part, dict):
                continue
            file_id = _id_from(part.get("@id"), _FILE_ID_IN_URL)
            if not file_id:
                continue
            mime = str(part.get("encodingFormat") or "")
            files.append(
                {
                    "id": file_id,
                    "name": str(part.get("name") or file_id),
                    "mime": mime,
                    "importable": mime in IMPORTABLE_MIME_TYPES,
                    "filename": None,
                    "size": None,
                    "presupported": None,
                    "available": True,
                }
            )
        license_info = body.get("spdx:license")
        keywords = body.get("keywords")
        preview = body.get("preview_file")
        model: dict[str, Any] = {
            "id": model_id,
            "name": str(body.get("name") or model_id),
            "caption": _text(body.get("caption")),
            "description": _text(body.get("description")),
            "license": _text(license_info.get("licenseId")) if isinstance(license_info, dict) else None,
            "creator": None,
            "creator_slug": None,
            "tags": [str(tag) for tag in keywords if isinstance(tag, str)] if isinstance(keywords, list) else [],
            "sensitive": body.get("sensitive") is True,
            # The model's page, built from the configured address — never Manyfold's own ``@id``.
            "url": f"{self._base}/models/{model_id}",
            "preview_file_id": _id_from(preview.get("@id"), _FILE_ID_IN_URL) if isinstance(preview, dict) else None,
            "files": files,
            "files_detailed": False,
        }
        if details:
            model["creator_slug"], model["creator"] = await self._creator_name(body.get("creator"))
            await self._detail_files(model_id, files)
            model["files_detailed"] = True
        return model

    async def _detail_files(self, model_id: str, files: list[dict[str, Any]]) -> None:
        """Name, size and presupported flag of the importable files — the list itself carries none."""
        gate = asyncio.Semaphore(FILE_DETAIL_CONCURRENCY)

        async def detail(entry: dict[str, Any]) -> None:
            async with gate:
                try:
                    file = await self.get_file(model_id, entry["id"])
                except ManyfoldNotFound:
                    # Listed, but not downloadable by this application (a preview-only model).
                    entry.update(importable=False, available=False)
                    return
                except ManyfoldError:
                    return  # the entry stays as the list gave it; importing asks again
            entry.update(
                filename=file["filename"],
                size=file["size"],
                presupported=file["presupported"],
                importable=is_importable_filename(file["filename"]),
            )

        wanted = [entry for entry in files if entry["importable"]][:FILE_DETAIL_LIMIT]
        await asyncio.gather(*(detail(entry) for entry in wanted))

    async def get_file(self, model_id: str, file_id: str) -> dict[str, Any]:
        """One file's details, with the raw-download path rebuilt from its decoded segments."""
        model_id, file_id = valid_id(model_id), valid_id(file_id)
        cached = self._files.get((model_id, file_id))
        if cached is not None:
            return cached
        body = await self._get_json(f"/models/{model_id}/model_files/{file_id}", "this file")
        try:
            content_path = urlsplit(str(body.get("contentUrl") or "")).path
        except ValueError:
            content_path = ""
        marker = f"/models/{model_id}/raw/"
        position = content_path.find(marker)
        if position < 0:
            if "/model_files/" in content_path:
                raise ManyfoldUnavailable(
                    f"This Manyfold is too old (needs {MINIMUM_VERSION} or newer).", "manyfold_too_old"
                )
            raise ManyfoldUnavailable("Manyfold sent no usable download link for this file", "manyfold_incompatible")
        segments = [unquote(segment) for segment in content_path[position + len(marker) :].split("/")]
        if any(segment in ("", ".", "..") or "/" in segment or "\\" in segment for segment in segments) or any(
            ord(char) < 32 for segment in segments for char in segment
        ):
            raise ManyfoldUnavailable("Manyfold sent no usable download link for this file", "manyfold_incompatible")
        size = body.get("contentSize")
        file = {
            "id": file_id,
            "model_id": model_id,
            "name": str(body.get("name") or file_id),
            "filename": segments[-1],
            "mime": str(body.get("encodingFormat") or ""),
            "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            "presupported": body.get("presupported") if isinstance(body.get("presupported"), bool) else None,
            "raw_path": marker + "/".join(quote(segment, safe="") for segment in segments),
        }
        self._files[(model_id, file_id)] = file
        return file

    async def check(self) -> dict[str, Any]:
        """Test connection: capabilities, a sign-in, the first page. Returns what the form shows."""
        earlier = _refused.get(self._cache_key())
        if earlier and earlier[0].code == "manyfold_unreachable":
            _refused.pop(self._cache_key(), None)  # a test is always a fresh try at reaching it
        await self.capabilities(fresh=True)
        listing = await self.list_models()
        return {"models": listing["total"], "minimum_version": MINIMUM_VERSION}

    # ---- bytes ---------------------------------------------------------

    async def _stream_to(self, path: str, sink: Callable[[bytes], Any], *, cap: int, params=None, what: str) -> int:
        """GET ``path`` (one storage hop allowed) into ``sink``; returns the byte count."""
        transport = self._ready()
        token: str | None = None
        for attempt in range(2):
            token = await self._token(refused=token)
            try:
                async with transport.stream(path, params=params, bearer=token, foreign_hop=True) as answer:
                    # Only Manyfold's own 401 means the token was refused; storage never saw it.
                    if answer.status == 401 and not answer.foreign and attempt == 0:
                        continue
                    if answer.foreign and answer.status >= 400:
                        raise ManyfoldUnavailable(
                            f"The file storage behind Manyfold answered HTTP {answer.status}", "manyfold_storage_failed"
                        )
                    self._raise_for(answer, what)
                    size = 0
                    async for chunk in answer.chunks(cap):
                        size += len(chunk)
                        result = sink(chunk)
                        if asyncio.iscoroutine(result):
                            await result
                    return size
            except OutboundRefused as exc:
                raise _from_transport(exc) from exc
        raise ManyfoldCredentials("Manyfold did not accept BamDude's sign-in.")

    async def download_file(self, file: dict[str, Any], destination: Path) -> int:
        """Stream one file to ``destination`` (never into memory); returns its size."""
        if file["size"] is not None and file["size"] > MAX_FILE_BYTES:
            raise ManyfoldUnavailable(
                f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB", "manyfold_too_large", 413
            )
        try:
            async with asyncio.timeout(DOWNLOAD_DEADLINE_SECONDS):
                with open(destination, "wb") as handle:

                    async def write(chunk: bytes) -> None:
                        await asyncio.to_thread(handle.write, chunk)

                    return await self._stream_to(file["raw_path"], write, cap=MAX_FILE_BYTES, what="this file")
        except TimeoutError as exc:
            raise ManyfoldUnavailable("The download from Manyfold took too long", "manyfold_unreachable") from exc

    async def fetch_preview(self, model_id: str) -> tuple[bytes, str]:
        """The model's preview as an image, from Manyfold's own derivatives.

        An image comes as its small "preview" copy, a 3D file as its render when Manyfold made one.
        Without a derivative Manyfold sends the ORIGINAL file, and the content type of a render is
        the model's — so the bytes are sniffed, and anything that does not start like an image is
        dropped after its first bytes.
        """
        no_preview = ManyfoldNotFound("This model has no preview", "manyfold_no_preview")
        model = await self.get_model(model_id, details=False)
        file_id = model["preview_file_id"]
        if not file_id:
            raise no_preview
        file = await self.get_file(model["id"], file_id)
        extension = os.path.splitext(file["filename"])[1].lower()
        if file["mime"].startswith("image/"):
            derivative = "preview"
        elif file["mime"].startswith("model/") or file["mime"].startswith("text/x-gcode"):
            derivative = "render"
        else:
            raise no_preview
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            raise no_preview
        data = bytearray()
        kind: list[str | None] = [None]

        def collect(chunk: bytes) -> None:
            data.extend(chunk)
            if kind[0] is None and len(data) >= 12:
                kind[0] = image_type(bytes(data[:12]))
                if kind[0] is None:
                    raise no_preview

        try:
            async with asyncio.timeout(PREVIEW_DEADLINE_SECONDS):
                await self._stream_to(
                    f"/models/{model['id']}/model_files/{file_id}{extension}",
                    collect,
                    cap=MAX_PREVIEW_BYTES,
                    params={"derivative": derivative},
                    what="the preview",
                )
        except TimeoutError as exc:
            raise ManyfoldUnavailable("Manyfold did not answer in time", "manyfold_unreachable") from exc
        except ManyfoldUnavailable as exc:
            if exc.code == "manyfold_too_large":
                raise no_preview from exc
            raise
        content_type = kind[0] or image_type(bytes(data[:12]))
        if content_type is None:
            raise no_preview
        return bytes(data), content_type

    # ---- ProviderService -----------------------------------------------

    async def get_status(self, db) -> ProviderStatus:
        usable = (
            self._config.configured
            and self._config.enabled
            and self._transport is not None
            and not outbound.transport_disabled()
        )
        return ProviderStatus(authenticated=usable, can_download=usable, configured=self._config.configured)

    async def resolve(self, ref: ProviderResourceRef) -> ProviderResolvedModel:
        model = await self.get_model(ref.external_id)
        return ProviderResolvedModel(ref=ref, design=model, instances=model["files"])

    async def get_download(self, ref: ProviderResourceRef) -> ProviderDownloadInfo:
        if not ref.sub_id:
            raise ManyfoldRefused("Name the file to import")
        file = await self.get_file(ref.external_id, ref.sub_id)
        if not is_importable_filename(file["filename"]):
            raise ManyfoldRefused(
                "BamDude imports 3MF, STL, STEP and OBJ files from Manyfold.", "manyfold_not_importable"
            )
        if file["size"] is not None and file["size"] > MAX_FILE_BYTES:
            raise ManyfoldUnavailable(
                f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB", "manyfold_too_large", 413
            )
        page = f"{self._base}/models/{file['model_id']}"
        enriched = ProviderResourceRef(
            source_type=SOURCE_TYPE, external_id=file["model_id"], sub_id=file["id"], original_url=page
        )
        return ProviderDownloadInfo(ref=enriched, url=page, suggested_filename=file["filename"], size=file["size"])

    async def download_to(self, info: ProviderDownloadInfo, destination: Path) -> int:
        # The path comes from the ids of ``info.ref`` again — ``info.url`` is never fetched.
        file = await self.get_file(info.ref.external_id, info.ref.sub_id or "")
        return await self.download_file(file, destination)

    async def download(self, info: ProviderDownloadInfo) -> ProviderDownload:
        raise ManyfoldUnavailable("Manyfold files are streamed to disk, never held in memory")

    async def fetch_thumbnail(self, url: str) -> tuple[bytes, str]:
        # A preview is addressed by model id (``fetch_preview``); no address from a client is fetched.
        raise ManyfoldRefused("Manyfold previews are addressed by model, not by URL")

    async def origin_of(self, ref: ProviderResourceRef):
        """Title, creator and licence of the model a file came from, as they are now."""
        from backend.app.services.library_import_meta import ImportOrigin

        model = await self.get_model(ref.external_id, details=False)
        slug, name = await self._creator_name((await self._model_body(model["id"])).get("creator"))
        return ImportOrigin(
            source_model_id=ref.external_id,
            source_file_id=ref.sub_id,
            title=model["name"],
            creator=name or slug,
            license=model["license"],
            canonical_url=model["url"],
        )
