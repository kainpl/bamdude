"""The Manyfold client against a hand-built Manyfold (``tests/_fixtures/manyfold.py``). No network."""

from __future__ import annotations

import logging

import httpx
import pytest

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.base import ProviderResourceRef
from backend.app.services.model_providers.manyfold import service as manyfold
from backend.app.services.model_providers.manyfold.config import ManyfoldConfig
from backend.app.services.model_providers.manyfold.service import (
    ManyfoldCredentials,
    ManyfoldError,
    ManyfoldNotConfigured,
    ManyfoldNotFound,
    ManyfoldRefused,
    ManyfoldService,
    ManyfoldUnavailable,
)
from backend.app.services.model_providers.outbound import GuardedTransport, PinnedOrigin
from backend.tests._fixtures.manyfold import (
    API,
    BASE,
    CLIENT_ID,
    CLIENT_SECRET,
    DNS,
    MODEL,
    PNG,
    STL,
    FakeManyfold,
    openapi,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def resolve(host, port):
        if host not in DNS:
            raise OSError("unscripted name")
        return list(DNS[host])

    monkeypatch.setattr(outbound, "resolve_host", resolve)
    manyfold.clear_caches()
    yield
    manyfold.clear_caches()


@pytest.fixture
def fake():
    return FakeManyfold()


def service_for(fake: FakeManyfold, *, secret: str = CLIENT_SECRET, enabled: bool = True) -> ManyfoldService:
    config = ManyfoldConfig(url=BASE, client_id=CLIENT_ID, client_secret=secret, enabled=enabled)
    transport = GuardedTransport(PinnedOrigin.admit(BASE, label="Manyfold URL"), client=fake.client())
    return ManyfoldService(config, transport=transport)


class TestCatalogue:
    async def test_a_page_of_models_comes_with_the_sources_own_paging(self, fake):
        listing = await service_for(fake).list_models()
        assert listing == {
            "total": 3,
            "page": 1,
            "pages": 2,
            "has_next": True,
            "has_previous": False,
            "models": [{"id": MODEL, "name": "Cable Clip"}, {"id": "b4n8tq1zk0fw", "name": "Тримач котушки"}],
        }
        second = await service_for(fake).list_models(page=2)
        assert (second["has_next"], second["has_previous"], len(second["models"])) == (False, True, 1)

    async def test_every_request_goes_to_the_configured_address_never_to_the_one_manyfold_reports(self, fake):
        service = service_for(fake)
        await service.list_models()
        model = await service.get_model(MODEL)
        assert {request.url.host for request in fake.requests} == {"manyfold.lan"}
        assert model["url"] == f"{BASE}/models/{MODEL}"

    async def test_the_api_is_asked_with_its_media_type_and_never_for_an_order(self, fake):
        await service_for(fake).list_models(query="clip", page=1)
        listing = next(request for request in fake.requests if request.url.path == "/models")
        assert listing.headers["Accept"] == API
        assert dict(listing.url.params) == {"page": "1", "q": "clip"}  # ``order`` would rewrite the owner's profile

    async def test_a_search_manyfold_answers_with_a_redirect_is_a_refused_search(self, fake):
        with pytest.raises(ManyfoldRefused) as caught:
            await service_for(fake).list_models(query="someone@example.social")
        assert caught.value.code == "manyfold_query_refused"
        assert all(request.url.path != "/follows/new" for request in fake.requests)

    async def test_a_model_lists_its_files_with_names_and_sizes_for_the_importable_ones(self, fake):
        model = await service_for(fake).get_model(MODEL)
        assert (model["name"], model["license"], model["creator"], model["tags"]) == (
            "Cable Clip",
            "CC-BY-4.0",
            "Jane Doe",
            ["clip", "material:petg"],
        )
        files = {entry["id"]: entry for entry in model["files"]}
        assert files["k7l8m9n0p1q2"] == {
            "id": "k7l8m9n0p1q2",
            "name": "Clip",
            "mime": "model/stl",
            "importable": True,
            "filename": "clip v2.stl",
            "size": len(STL) + 8,
            "presupported": True,
            "available": True,
            "detailed": True,
        }
        assert files["r3s4t5v6w7x8"]["importable"] is True
        # Pictures, documents and archives are listed and never importable — nor asked about.
        for file_id in ("z9b0c1d2f3g4", "h5j6k7l8m9n0", "p1q2r3s4t5v6"):
            assert files[file_id]["importable"] is False and files[file_id]["filename"] is None
        detail_requests = [path for path in fake.paths() if "/model_files/" in path]
        assert len(detail_requests) == 3

    async def test_the_signed_write_address_of_a_file_is_never_kept(self, fake, caplog):
        caplog.set_level(logging.DEBUG)
        service = service_for(fake)
        model = await service.get_model(MODEL)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        assert "WRITE-SIGNATURE" not in repr(model) + repr(file) + caplog.text
        assert "updateContentUrl" not in repr(file)

    async def test_a_model_without_files_is_an_empty_model_not_an_error(self, fake):
        model = await service_for(fake).get_model("b4n8tq1zk0fw")
        assert (model["files"], model["preview_file_id"], model["creator"], model["license"]) == ([], None, None, None)

    async def test_a_file_listed_but_not_downloadable_is_marked_unavailable(self, fake):
        del fake.models[MODEL]["files"]["f1c2d3g4h5j6"]
        fake.models[MODEL]["files"]["f1c2d3g4h5j6"] = ("Clip", "model/stl", "clip.stl", STL)
        service = service_for(fake)
        original = fake._file
        fake._file = lambda model_id, file_id: (
            original(model_id, "missing") if file_id == "f1c2d3g4h5j6" else original(model_id, file_id)
        )
        files = {entry["id"]: entry for entry in (await service.get_model(MODEL))["files"]}
        assert (files["f1c2d3g4h5j6"]["importable"], files["f1c2d3g4h5j6"]["available"]) == (False, False)

    @pytest.mark.parametrize("bad", ["", "../admin", "a/b", "x" * 65, "id?x=1", None])
    async def test_an_id_that_is_not_an_id_never_reaches_manyfold(self, fake, bad):
        with pytest.raises(ManyfoldNotFound):
            await service_for(fake).get_model(bad)
        assert fake.requests == []


class TestAModelWithManyFiles:
    """Review MB-M11: one model view asked Manyfold up to 62 times. Details come a page at a time."""

    def crowded(self, fake: FakeManyfold, count: int = 47) -> None:
        fake.models[MODEL]["files"] = {
            f"file{index:03d}": (f"Part {index}", "model/stl", f"part-{index}.stl", STL) for index in range(count)
        }

    async def test_the_first_view_details_one_page_and_says_there_is_more(self, fake):
        self.crowded(fake)
        model = await service_for(fake).get_model(MODEL)
        assert len(model["files"]) == 47, "every file is listed"
        detailed = [entry for entry in model["files"] if entry["detailed"]]
        assert [entry["id"] for entry in detailed] == [f"file{index:03d}" for index in range(20)]
        assert all(entry["filename"] and entry["size"] == len(STL) for entry in detailed)
        assert all(entry["filename"] is None for entry in model["files"][20:])
        assert (model["files_page"], model["files_pages"], model["files_next_page"]) == (1, 3, 2)
        upstream = [path for path in fake.paths() if path.startswith("/models/") or path.startswith("/creators/")]
        assert len(upstream) == 22, f"{len(upstream)} upstream requests for one model view"

    async def test_the_next_page_reads_only_its_own_files(self, fake):
        self.crowded(fake)
        await service_for(fake).get_model(MODEL)
        before = len(fake.requests)
        model = await service_for(fake).get_model(MODEL, files_page=3)  # another request, another service
        detailed = [entry["id"] for entry in model["files"] if entry["detailed"]]
        # Page 1 is still known (the short cache), page 3 was just read, page 2 nobody asked for.
        assert detailed == [f"file{index:03d}" for index in (*range(20), *range(40, 47))]
        assert (model["files_page"], model["files_pages"], model["files_next_page"]) == (3, 3, None)
        assert len(fake.requests) - before == 7, "the model, its creator and page 1 were not read again"

    async def test_a_page_past_the_end_is_an_empty_page_not_an_error(self, fake):
        self.crowded(fake, 3)
        model = await service_for(fake).get_model(MODEL, files_page=9)
        assert (model["files_page"], model["files_pages"], model["files_next_page"]) == (9, 1, None)

    async def test_a_model_without_importable_files_has_no_pages(self, fake):
        model = await service_for(fake).get_model("b4n8tq1zk0fw")
        assert (model["files_pages"], model["files_next_page"]) == (0, None)

    async def test_a_page_of_previews_reads_each_model_once(self, fake):
        first = await service_for(fake).fetch_preview(MODEL)
        before = len(fake.requests)
        assert await service_for(fake).fetch_preview(MODEL) == first
        assert len(fake.requests) - before == 1, "only the picture itself is fetched again"

    async def test_the_short_memory_ends_with_the_connection(self, fake):
        await service_for(fake).get_model(MODEL)
        manyfold.clear_caches()
        before = len(fake.requests)
        await service_for(fake).get_model(MODEL)
        assert len(fake.requests) - before > 3

    async def test_details_that_take_too_long_leave_the_rest_for_another_ask(self, fake, monkeypatch):
        import asyncio

        self.crowded(fake, 6)
        real = ManyfoldService.get_file

        async def slow(self, model_id, file_id):
            if file_id >= "file003":
                await asyncio.sleep(30)
            return await real(self, model_id, file_id)

        monkeypatch.setattr(ManyfoldService, "get_file", slow)
        monkeypatch.setattr(manyfold, "FILE_DETAIL_DEADLINE_SECONDS", 0.2, raising=False)
        model = await asyncio.wait_for(service_for(fake).get_model(MODEL), 5)
        assert [entry["detailed"] for entry in model["files"]] == [True, True, True, False, False, False]


class TestCapabilities:
    async def test_an_instance_without_the_raw_file_path_is_too_old(self, fake):
        fake.spec = openapi(raw=False)
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service_for(fake).list_models()
        assert caught.value.code == "manyfold_too_old"
        assert "0.150" in str(caught.value)
        assert fake.sign_ins == 0  # asked before the first sign-in

    async def test_an_address_that_is_not_manyfold_says_so(self, fake):
        fake.spec = None
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service_for(fake).check()
        assert caught.value.code == "manyfold_not_manyfold"

    async def test_capabilities_are_read_once_and_again_for_a_connection_test(self, fake):
        await service_for(fake).list_models()
        await service_for(fake).get_model(MODEL)
        assert fake.paths().count("/api/v0/openapi.json") == 1
        assert (await service_for(fake).check()) == {"models": 3, "minimum_version": "0.150"}
        assert fake.paths().count("/api/v0/openapi.json") == 2

    async def test_a_file_answer_in_the_old_shape_is_too_old(self, fake):
        fake.content_url["f1c2d3g4h5j6"] = f"/models/{MODEL}/model_files/f1c2d3g4h5j6.stl"
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service_for(fake).get_file(MODEL, "f1c2d3g4h5j6")
        assert caught.value.code == "manyfold_too_old"


class TestSignIn:
    async def test_one_token_serves_every_request_and_every_service(self, fake):
        for _ in range(3):
            service = service_for(fake)
            await service.list_models()
            await service.get_model(MODEL)
        assert fake.sign_ins == 1

    async def test_a_refused_secret_is_not_tried_again_by_the_next_request(self, fake):
        for _ in range(5):
            with pytest.raises(ManyfoldCredentials) as caught:
                await service_for(fake, secret="wrong").list_models()
            assert caught.value.code == "manyfold_credentials"
        assert fake.sign_ins == 1  # the other four would have eaten Manyfold's 10 sign-ins per 3 minutes
        # The corrected secret is another credential: it signs in at once.
        await service_for(fake).list_models()
        assert fake.sign_ins == 2

    async def test_a_rate_limited_sign_in_is_not_hammered(self, fake):
        fake.token_status = 429
        for _ in range(3):
            with pytest.raises(ManyfoldUnavailable) as caught:
                await service_for(fake).list_models()
            assert caught.value.code == "manyfold_rate_limited"
        assert fake.sign_ins == 1

    async def test_an_application_without_the_read_scope_is_named(self, fake):
        fake.granted_scope = "public"
        with pytest.raises(ManyfoldCredentials) as caught:
            await service_for(fake).list_models()
        assert caught.value.code == "manyfold_scope"

    async def test_a_redirected_sign_in_is_never_followed_with_the_secret(self, fake):
        fake.token_status = 301
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service_for(fake).list_models()
        assert caught.value.code == "manyfold_wrong_scheme"  # http -> https on the same host
        token_requests = [request for request in fake.requests if request.url.path == "/oauth/token"]
        assert len(token_requests) == 1 and token_requests[0].url.scheme == "http"

    async def test_a_plain_text_401_drops_the_token_and_asks_once_more(self, fake):
        service = service_for(fake)
        await service.list_models()
        fake.expire_tokens()  # revoked on Manyfold's side; our cache still holds it
        listing = await service_for(fake).list_models()
        assert listing["total"] == 3 and fake.sign_ins == 2

    async def test_a_401_that_persists_is_reported_not_looped(self, fake):
        service = service_for(fake)
        await service.list_models()
        fake._authorised = lambda request: False
        with pytest.raises(ManyfoldCredentials):
            await service.list_models()
        assert fake.sign_ins == 2

    async def test_neither_the_secret_nor_the_token_is_logged_or_in_an_error(self, fake, caplog):
        caplog.set_level(logging.DEBUG)
        service = service_for(fake)
        await service.list_models()
        with pytest.raises(ManyfoldError) as caught:
            await service_for(fake, secret="another-wrong-secret").list_models()
        text = caplog.text + str(caught.value) + repr(service._config)
        assert CLIENT_SECRET not in text and "another-wrong-secret" not in text and "token-1" not in text

    async def test_nothing_is_asked_while_not_connected_or_switched_off(self, fake):
        with pytest.raises(ManyfoldNotConfigured) as caught:
            await ManyfoldService(ManyfoldConfig()).list_models()
        assert caught.value.code == "manyfold_not_configured"
        with pytest.raises(ManyfoldNotConfigured) as caught:
            await service_for(fake, enabled=False).list_models()
        assert caught.value.code == "manyfold_disabled"
        assert fake.requests == []


class TestSignInUnderLoad:
    """Review MB-M3 / MB-M4: one sign-in for everybody, and nobody waits for it without a deadline."""

    async def test_a_revoked_token_is_renewed_once_for_every_request_that_was_refused(self, fake):
        import asyncio

        await service_for(fake).list_models()
        fake.expire_tokens()
        # A catalogue page: every tile's request meets the 401 at once.
        listings = await asyncio.gather(*(service_for(fake).list_models() for _ in range(40)))
        assert all(listing["total"] == 3 for listing in listings)
        assert fake.sign_ins == 2, (
            f"{fake.sign_ins - 1} sign-ins for one revoked token (Manyfold allows 10 in 3 minutes)"
        )

    async def test_a_download_refused_with_the_old_token_shares_the_renewal_too(self, fake, tmp_path):
        import asyncio

        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        fake.expire_tokens()
        sizes = await asyncio.gather(
            *(service_for(fake).download_file(file, tmp_path / f"{index}.stl") for index in range(6))
        )
        assert sizes == [len(STL)] * 6 and fake.sign_ins == 2

    async def test_waiting_for_somebody_elses_sign_in_has_a_deadline(self, fake, monkeypatch):
        import asyncio

        never = asyncio.Event()

        async def stuck(self, key):
            await never.wait()

        monkeypatch.setattr(ManyfoldService, "_sign_in", stuck)
        monkeypatch.setattr(manyfold, "SIGN_IN_WAIT_SECONDS", 0.05, raising=False)
        first = asyncio.create_task(service_for(fake).list_models())
        await asyncio.sleep(0.02)
        try:
            with pytest.raises(ManyfoldUnavailable) as caught:
                await asyncio.wait_for(service_for(fake).list_models(), 2)
            assert caught.value.code == "manyfold_unreachable"
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def test_a_request_with_a_good_token_never_waits_for_a_sign_in(self, fake, monkeypatch):
        import asyncio

        await service_for(fake).list_models()  # the installation's token is cached
        never = asyncio.Event()
        real = ManyfoldService._sign_in

        async def stuck(self, key):
            if self._config.client_secret == "draft":
                await never.wait()  # an administrator's draft test against an address that never answers
            return await real(self, key)

        monkeypatch.setattr(ManyfoldService, "_sign_in", stuck)
        draft = asyncio.create_task(service_for(fake, secret="draft").list_models())
        await asyncio.sleep(0.02)
        try:
            listing = await asyncio.wait_for(service_for(fake).list_models(), 2)
            assert listing["total"] == 3
            # ... and a sign-in for the saved connection does not queue behind the draft's either.
            fake.expire_tokens()
            assert (await asyncio.wait_for(service_for(fake).list_models(), 2))["total"] == 3
        finally:
            draft.cancel()
            await asyncio.gather(draft, return_exceptions=True)

    async def test_an_unreachable_manyfold_is_not_asked_again_by_every_tile(self, fake, monkeypatch):
        asked: list[str] = []

        async def down(host, port):
            asked.append(host)
            raise OSError("no route")

        monkeypatch.setattr(outbound, "resolve_host", down)
        for _ in range(5):
            with pytest.raises(ManyfoldUnavailable) as caught:
                await service_for(fake).fetch_preview(MODEL)
            assert caught.value.code == "manyfold_unreachable"
        assert len(asked) == 1
        # "Test connection" is always a fresh try.
        with pytest.raises(ManyfoldUnavailable):
            await service_for(fake).check()
        assert len(asked) == 2


class TestFiles:
    async def test_the_download_path_is_rebuilt_from_the_decoded_segments(self, fake):
        file = await service_for(fake).get_file(MODEL, "k7l8m9n0p1q2")
        assert file["raw_path"] == f"/models/{MODEL}/raw/supported/clip%20v2.stl"
        assert file["filename"] == "clip v2.stl"

    @pytest.mark.parametrize(
        "content_url",
        [
            f"http://localhost:3214/models/{MODEL}/raw/../../../etc/passwd",
            f"http://localhost:3214/models/{MODEL}/raw/a/%2e%2e/b.stl",
            f"http://localhost:3214/models/{MODEL}/raw/a%2Fb.stl",
            f"http://localhost:3214/models/{MODEL}/raw/",
            f"http://localhost:3214/models/{MODEL}/raw/a//b.stl",
            "http://evil.example/steal",
            "",
        ],
    )
    async def test_a_download_link_that_could_leave_the_model_is_refused(self, fake, content_url):
        fake.content_url["f1c2d3g4h5j6"] = content_url
        with pytest.raises(ManyfoldUnavailable):
            await service_for(fake).get_file(MODEL, "f1c2d3g4h5j6")

    async def test_a_file_is_streamed_to_disk(self, fake, tmp_path):
        service = service_for(fake)
        file = await service.get_file(MODEL, "k7l8m9n0p1q2")
        target = tmp_path / "out.stl"
        assert await service.download_file(file, target) == len(STL) + 8
        assert target.read_bytes() == STL + b"supports"
        raw = next(request for request in fake.requests if "/raw/" in request.url.path)
        assert "accept" not in raw.headers or raw.headers["Accept"] != API

    async def test_a_file_in_object_storage_is_fetched_there_without_the_token(self, fake, tmp_path):
        fake.storage_redirect = True
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        target = tmp_path / "out.stl"
        await service.download_file(file, target)
        assert target.read_bytes() == STL
        (storage,) = [request for request in fake.requests if request.url.host == "bucket.s3.example.com"]
        assert "authorization" not in storage.headers

    @pytest.mark.parametrize(
        "storage",
        [
            "https://10.0.0.5:9000",  # MinIO on the LAN, by address
            "https://minio.lan",  # ... by a name that resolves to a private address
            "http://bucket.s3.example.com",  # public, but plain http
            "http://192.168.1.30:9000",
        ],
    )
    async def test_storage_on_the_lan_or_over_plain_http_is_fetched_without_the_token(
        self, fake, tmp_path, monkeypatch, storage
    ):
        """Owner, 2026-11-17 (as upstream): MinIO beside Manyfold is the normal home topology."""
        monkeypatch.setitem(DNS, "minio.lan", ["192.168.1.30"])
        signed = f"{storage}/library/{MODEL}/clip.stl?X-Amz-Signature=abc%2Fdef&X-Amz-Expires=900"
        fake._raw = lambda model_id, path, request: httpx.Response(302, headers={"Location": signed})
        serve = fake.handle

        def handle(request):
            if request.url.host == "manyfold.lan":
                return serve(request)
            fake.requests.append(request)
            return fake._storage(request)

        fake.handle = handle
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        target = tmp_path / "out.stl"
        await service.download_file(file, target)
        assert target.read_bytes() == STL
        (fetched,) = [request for request in fake.requests if request.url.host != "manyfold.lan"]
        assert "authorization" not in fetched.headers

    async def test_a_redirect_to_a_metadata_address_is_a_refused_storage(self, fake, tmp_path):
        fake._raw = lambda model_id, path, request: httpx.Response(
            302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service.download_file(file, tmp_path / "out.stl")
        assert caught.value.code == "manyfold_storage_refused"
        assert all(request.url.host == "manyfold.lan" for request in fake.requests)

    async def test_storage_bamdude_cannot_reach_is_named_as_storage_not_as_manyfold(self, fake, tmp_path):
        """Live check, 2026-11-17: a storage endpoint only Manyfold can resolve (a compose service
        name) read as "Could not reach Manyfold"."""
        from backend.app.i18n.api_errors import translate_sentence

        fake._raw = lambda model_id, path, request: httpx.Response(
            302, headers={"Location": "http://minio:9000/models/clip.stl?X-Amz-Signature=abc"}
        )
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service.download_file(file, tmp_path / "out.stl")
        assert caught.value.code == "manyfold_storage_unreachable"
        assert "storage" in str(caught.value) and "reach Manyfold" not in str(caught.value)
        assert translate_sentence(str(caught.value), "uk") != str(caught.value)

    async def test_another_storage_refusal_does_not_claim_a_private_address(self, fake, tmp_path):
        fake._raw = lambda model_id, path, request: httpx.Response(
            302, headers={"Location": "https://user:pw@bucket.s3.example.com/library/clip.stl"}
        )
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service.download_file(file, tmp_path / "out.stl")
        assert caught.value.code == "manyfold_storage_refused" and "private address" not in str(caught.value)

    async def test_a_refusal_by_storage_does_not_renew_the_token(self, fake, tmp_path):
        fake.storage_redirect, fake.storage_status = True, 401
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service.download_file(file, tmp_path / "out.stl")
        assert caught.value.code == "manyfold_storage_failed" and fake.sign_ins == 1

    async def test_a_file_manyfold_says_is_too_large_is_not_downloaded(self, fake, tmp_path, monkeypatch):
        monkeypatch.setattr(manyfold, "MAX_FILE_BYTES", 64)
        fake.sizes["f1c2d3g4h5j6"] = 65
        service = service_for(fake)
        ref = ProviderResourceRef(source_type="manyfold", external_id=MODEL, sub_id="f1c2d3g4h5j6")
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service.get_download(ref)
        assert (caught.value.code, caught.value.status) == ("manyfold_too_large", 413)
        assert not any("/raw/" in path for path in fake.paths())

    async def test_a_file_larger_than_it_claimed_is_cut_off(self, fake, tmp_path, monkeypatch):
        monkeypatch.setattr(manyfold, "MAX_FILE_BYTES", 64)
        fake.sizes["f1c2d3g4h5j6"] = 10  # the answer lies
        service = service_for(fake)
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        with pytest.raises(ManyfoldUnavailable) as caught:
            await service.download_file(file, tmp_path / "out.stl")
        assert caught.value.code == "manyfold_too_large"

    async def test_only_printable_files_are_offered_for_download(self, fake):
        service = service_for(fake)
        for file_id in ("z9b0c1d2f3g4", "h5j6k7l8m9n0", "p1q2r3s4t5v6"):
            with pytest.raises(ManyfoldRefused) as caught:
                await service.get_download(ProviderResourceRef("manyfold", MODEL, file_id))
            assert caught.value.code == "manyfold_not_importable"
        info = await service.get_download(ProviderResourceRef("manyfold", MODEL, "k7l8m9n0p1q2"))
        assert info.suggested_filename == "clip v2.stl"
        assert info.ref.original_url == f"{BASE}/models/{MODEL}"

    async def test_the_origin_of_an_import_is_the_model_its_creator_and_its_licence(self, fake):
        origin = await service_for(fake).origin_of(ProviderResourceRef("manyfold", MODEL, "f1c2d3g4h5j6"))
        assert (origin.title, origin.creator, origin.license, origin.canonical_url, origin.source_file_id) == (
            "Cable Clip",
            "Jane Doe",
            "CC-BY-4.0",
            f"{BASE}/models/{MODEL}",
            "f1c2d3g4h5j6",
        )


class TestPreviews:
    async def test_an_image_preview_is_served_as_the_image_it_is(self, fake):
        data, content_type = await service_for(fake).fetch_preview(MODEL)
        assert content_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
        derivative = next(request for request in fake.requests if ".jpg" in request.url.path)
        assert derivative.url.params["derivative"] == "preview"

    async def test_a_render_is_recognised_by_its_bytes_not_its_content_type(self, fake):
        fake.models[MODEL]["preview"] = "f1c2d3g4h5j6"
        fake.render["f1c2d3g4h5j6"] = PNG
        data, content_type = await service_for(fake).fetch_preview(MODEL)
        assert (content_type, data) == ("image/png", PNG)

    async def test_the_original_file_sent_for_a_missing_render_is_not_a_preview(self, fake):
        fake.models[MODEL]["preview"] = "f1c2d3g4h5j6"  # an STL, and Manyfold made no render
        with pytest.raises(ManyfoldNotFound) as caught:
            await service_for(fake).fetch_preview(MODEL)
        assert caught.value.code == "manyfold_no_preview"

    async def test_a_model_without_a_preview_file_has_no_preview(self, fake):
        with pytest.raises(ManyfoldNotFound):
            await service_for(fake).fetch_preview("b4n8tq1zk0fw")

    async def test_a_picture_address_from_a_client_is_never_fetched(self, fake):
        with pytest.raises(ManyfoldRefused):
            await service_for(fake).fetch_thumbnail(f"{BASE}/models/{MODEL}")
        assert fake.requests == []
