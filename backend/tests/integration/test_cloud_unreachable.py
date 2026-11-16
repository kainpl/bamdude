"""An unreachable Bambu Cloud is a 502 — never "wrong credentials", never a bare 500.

Bambu Cloud is replaced below the HTTP client (``httpx.MockTransport``) with
hand-built answers; the routes, the service, the credential seam and the
MakerWorld provider run as shipped.

Three things are told apart on every path:

* Bambu could not be reached (network error, timeout, 5xx, an HTML page where
  JSON belongs) → 502, and the stored token is neither cleared nor marked dead;
* Bambu refused the credential (its expiry 401, a rejected pasted token) → 401;
* the access token expired and the refresh token renewed it → the request is
  repeated once and the caller never sees the expiry.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from backend.app.models.user import User
from backend.app.services import bambu_cloud
from backend.app.services.model_providers.makerworld import service as makerworld_service

pytestmark = pytest.mark.asyncio

EXPIRED = (401, {"code": 4, "error": "Please login.", "message": ""})
HTML = httpx.Response(200, text="<html><title>Just a moment...</title></html>")
SETTINGS = "/v1/iot-service/api/slicer/setting"
BIND = "/v1/iot-service/api/user/bind"
REFRESH = "/v1/user-service/user/refreshtoken"
LOGIN = "/v1/user-service/user/login"
PROFILE = "/v1/design-user-service/my/preference"
UNREACHABLE = "Bambu Cloud could not be reached"


class FakeBambu:
    """Answers by ``(METHOD, path)``. A value is a response, ``(status, json)``,
    an exception to raise, a callable taking the request, or a list of those
    consumed one per call (the last one repeats)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append((*key, request.headers.get("authorization")))
        if key not in self.answers:
            raise AssertionError(f"unexpected request to Bambu: {key}")
        answer = self.answers[key]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if callable(answer) and not isinstance(answer, httpx.Response):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            return httpx.Response(answer[0], json=answer[1])
        return answer

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if (m, p) == (method, path))

    def bearers(self, method: str, path: str) -> list[str | None]:
        return [auth for m, p, auth in self.calls if (m, p) == (method, path)]


@pytest.fixture
def bambu(monkeypatch):
    """Install a fake Bambu behind both services' shared HTTP client."""
    clients: list[httpx.AsyncClient] = []

    def install(answers: dict) -> FakeBambu:
        fake = FakeBambu(answers)
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        clients.append(client)
        monkeypatch.setattr(bambu_cloud, "_shared_http_client", client)
        monkeypatch.setattr(makerworld_service, "_shared_http_client", client)
        bambu_cloud.invalidate_validation_cache()
        return fake

    yield install
    bambu_cloud.invalidate_validation_cache()


def _down(request: httpx.Request):
    raise httpx.ConnectError("no route to host", request=request)


def _timeout(request: httpx.Request):
    raise httpx.ReadTimeout("timed out", request=request)


async def _sign_in(db_session, *, token="acc", refresh: str | None = "ref") -> int:
    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    admin.cloud_token = token
    admin.cloud_refresh_token = refresh
    admin.cloud_email = "me@example.com"
    admin.cloud_region = "global"
    await db_session.commit()
    return admin.id


async def _stored(db_session, user_id: int) -> tuple[str | None, str | None, bool]:
    db_session.expire_all()
    row = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    return row.cloud_token, row.cloud_refresh_token, row.cloud_token_invalid_at is not None


# Every data route that asks Bambu: (HTTP method, our path, JSON body, Bambu's first call).
DATA_ROUTES = [
    ("GET", "/api/v1/cloud/settings", None, ("GET", SETTINGS)),
    ("GET", "/api/v1/cloud/settings/PFUS1", None, ("GET", f"{SETTINGS}/PFUS1")),
    ("GET", "/api/v1/cloud/devices", None, ("GET", BIND)),
    ("GET", "/api/v1/cloud/firmware-updates", None, ("GET", BIND)),
    (
        "POST",
        "/api/v1/cloud/settings",
        {"type": "filament", "name": "Mine", "base_id": "GFSA00", "setting": {}},
        ("POST", SETTINGS),
    ),
    ("PUT", "/api/v1/cloud/settings/PFUS1", {"name": "Mine", "setting": {}}, ("GET", f"{SETTINGS}/PFUS1")),
    ("DELETE", "/api/v1/cloud/settings/PFUS1", None, ("DELETE", f"{SETTINGS}/PFUS1")),
]
ROUTE_IDS = ["list", "detail", "devices", "firmware", "create", "update", "delete"]


class TestADataRouteWhenBambuIsDown:
    @pytest.mark.parametrize(("method", "path", "body", "first"), DATA_ROUTES, ids=ROUTE_IDS)
    @pytest.mark.parametrize(
        "failure",
        [_down, _timeout, (503, {"error": "upstream"}), httpx.Response(502, text="<html>Bad gateway</html>"), HTML],
        ids=["network", "timeout", "503-json", "502-html", "200-html"],
    )
    async def test_it_is_a_502_and_the_sign_in_is_untouched(
        self, async_client, db_session, bambu, method, path, body, first, failure
    ):
        if method == "DELETE" and failure is HTML:
            pytest.skip("a delete reads no body: Bambu's 200 is its whole answer")
        user_id = await _sign_in(db_session)
        fake = bambu({first: failure})

        resp = await async_client.request(method, path, json=body)

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]
        assert await _stored(db_session, user_id) == ("acc", "ref", False)
        assert fake.count("POST", REFRESH) == 0

    async def test_the_sentence_names_bambu_cloud(self, async_client, db_session, bambu):
        await _sign_in(db_session)
        bambu({("GET", BIND): _down})

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.json()["detail"].startswith(UNREACHABLE)


class TestAnExpiredAccessToken:
    async def test_a_renewed_token_repeats_the_request_once_and_the_caller_sees_nothing(
        self, async_client, db_session, bambu
    ):
        user_id = await _sign_in(db_session)
        fake = bambu(
            {
                ("GET", BIND): [EXPIRED, (200, {"devices": [{"dev_id": "01P", "name": "P1S", "online": True}]})],
                ("POST", REFRESH): (200, {"accessToken": "acc-2", "refreshToken": "ref-2"}),
            }
        )

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.status_code == 200, resp.text
        assert [d["dev_id"] for d in resp.json()] == ["01P"]
        assert fake.bearers("GET", BIND) == ["Bearer acc", "Bearer acc-2"]
        assert fake.count("POST", REFRESH) == 1
        assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)

    @pytest.mark.parametrize(("method", "path", "body", "first"), DATA_ROUTES, ids=ROUTE_IDS)
    async def test_a_refused_renewal_is_a_401_and_the_token_is_kept_but_marked_dead(
        self, async_client, db_session, bambu, method, path, body, first
    ):
        """Never a 500, and never ``clear_token``: the email and region stay
        for the next sign-in, and the status says "expired"."""
        user_id = await _sign_in(db_session)
        bambu({first: EXPIRED, ("POST", REFRESH): (401, {"error": "Please login."})})

        resp = await async_client.request(method, path, json=body)

        assert resp.status_code == 401, resp.text
        assert await _stored(db_session, user_id) == ("acc", "ref", True)

    async def test_without_a_refresh_token_it_is_a_401_and_marked_dead(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session, refresh=None)
        fake = bambu({("GET", BIND): EXPIRED})

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.status_code == 401, resp.text
        assert fake.count("POST", REFRESH) == 0
        assert await _stored(db_session, user_id) == ("acc", None, True)

    @pytest.mark.parametrize("failure", [_down, _timeout, (503, {}), HTML], ids=["network", "timeout", "503", "html"])
    async def test_a_renewal_that_could_not_reach_bambu_is_a_502_and_the_token_stays_alive(
        self, async_client, db_session, bambu, failure
    ):
        user_id = await _sign_in(db_session)
        bambu({("GET", BIND): EXPIRED, ("POST", REFRESH): failure})

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"].startswith(UNREACHABLE)
        assert await _stored(db_session, user_id) == ("acc", "ref", False)

    async def test_the_request_is_repeated_only_once(self, async_client, db_session, bambu):
        """A renewed token Bambu refuses again is an expired sign-in, not a loop."""
        await _sign_in(db_session)
        fake = bambu(
            {
                ("GET", BIND): EXPIRED,
                ("POST", REFRESH): [
                    (200, {"accessToken": "acc-2", "refreshToken": "ref-2"}),
                    (401, {"error": "Please login."}),
                ],
            }
        )

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.status_code == 401, resp.text
        assert fake.count("GET", BIND) == 2

    async def test_a_401_without_bambus_expiry_signature_signs_nobody_out(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        fake = bambu({("GET", BIND): (401, {"error": "nope"})})

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.status_code == 502, resp.text
        assert fake.count("POST", REFRESH) == 0
        assert await _stored(db_session, user_id) == ("acc", "ref", False)

    async def test_firmware_updates_reports_the_expiry_instead_of_unknown_firmware(
        self, async_client, db_session, bambu
    ):
        await _sign_in(db_session, refresh=None)
        bambu(
            {
                ("GET", BIND): (200, {"devices": [{"dev_id": "01P", "name": "P1S"}]}),
                ("GET", "/v1/iot-service/api/user/device/version"): EXPIRED,
            }
        )

        resp = await async_client.get("/api/v1/cloud/firmware-updates")

        assert resp.status_code == 401, resp.text

    async def test_firmware_updates_keeps_a_device_whose_firmware_bambu_would_not_tell(
        self, async_client, db_session, bambu
    ):
        await _sign_in(db_session)
        bambu(
            {
                ("GET", BIND): (200, {"devices": [{"dev_id": "01P", "name": "P1S"}]}),
                ("GET", "/v1/iot-service/api/user/device/version"): (404, {"error": "no such device"}),
            }
        )

        resp = await async_client.get("/api/v1/cloud/firmware-updates")

        assert resp.status_code == 200, resp.text
        assert resp.json()["updates"][0]["current_version"] is None


class TestSigningIn:
    CREDENTIALS = {"email": "me@example.com", "password": "pw", "region": "global"}

    @pytest.mark.parametrize(
        "failure",
        [_down, _timeout, (503, {"message": "Login failed"}), httpx.Response(502, text="<html></html>"), HTML],
        ids=["network", "timeout", "503-json", "502-html", "200-html"],
    )
    async def test_an_unreachable_bambu_is_a_502_not_a_wrong_password(self, async_client, bambu, failure):
        bambu({("POST", LOGIN): failure})

        resp = await async_client.post("/api/v1/cloud/login", json=self.CREDENTIALS)

        assert resp.status_code == 502, resp.text

    async def test_a_wrong_password_is_still_bambus_own_answer(self, async_client, bambu):
        bambu({("POST", LOGIN): (400, {"code": 2, "error": "Incorrect password"})})

        resp = await async_client.post("/api/v1/cloud/login", json=self.CREDENTIALS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is False
        assert resp.json()["message"] == "Incorrect password"

    @pytest.mark.parametrize("failure", [_down, _timeout, (500, {}), HTML], ids=["network", "timeout", "500", "html"])
    async def test_the_email_code_step_is_a_502_not_a_wrong_code(self, async_client, bambu, failure):
        bambu({("POST", LOGIN): failure})

        resp = await async_client.post(
            "/api/v1/cloud/verify", json={"email": "me@example.com", "code": "123456", "region": "global"}
        )

        assert resp.status_code == 502, resp.text

    async def test_a_wrong_email_code_is_still_bambus_own_answer(self, async_client, bambu):
        bambu({("POST", LOGIN): (400, {"message": "Incorrect code"})})

        resp = await async_client.post(
            "/api/v1/cloud/verify", json={"email": "me@example.com", "code": "123456", "region": "global"}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "success": False,
            "needs_verification": False,
            "message": "Incorrect code",
            "verification_type": None,
            "tfa_key": None,
            "reason": None,
        }

    @pytest.mark.parametrize("failure", [_down, _timeout, (500, {})], ids=["network", "timeout", "500"])
    async def test_the_authenticator_step_is_a_502_not_a_wrong_code(self, async_client, bambu, failure):
        bambu(
            {
                ("GET", "/api/csrf"): httpx.Response(200, json={}, headers={"set-cookie": "bbl_csrf_token=c1; Path=/"}),
                ("POST", "/api/sign-in/tfa"): failure,
            }
        )

        resp = await async_client.post(
            "/api/v1/cloud/verify",
            json={"email": "me@example.com", "code": "123456", "tfa_key": "k", "region": "global"},
        )

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"].startswith(UNREACHABLE)


class TestAPastedToken:
    async def _paste(self, async_client, token="pasted"):
        return await async_client.post("/api/v1/cloud/token", json={"access_token": token, "region": "global"})

    @pytest.mark.parametrize(
        "failure",
        [_down, _timeout, (503, {}), HTML, httpx.Response(403, text="<html>Just a moment...</html>")],
        ids=["network", "timeout", "503", "200-html", "cloudflare-403"],
    )
    async def test_an_unreachable_bambu_is_a_502_not_an_invalid_token(self, async_client, db_session, bambu, failure):
        user_id = await _sign_in(db_session, token="old", refresh="old-ref")
        bambu({("GET", PROFILE): failure})

        resp = await self._paste(async_client)

        assert resp.status_code == 502, resp.text
        assert await _stored(db_session, user_id) == ("old", "old-ref", False)

    @pytest.mark.parametrize("answer", [EXPIRED, (401, {"error": "nope"}), (403, {"error": "forbidden"})])
    async def test_a_token_bambu_refuses_is_invalid(self, async_client, db_session, bambu, answer):
        user_id = await _sign_in(db_session, token="old", refresh="old-ref")
        bambu({("GET", PROFILE): answer})

        resp = await self._paste(async_client)

        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == "Invalid token"
        assert await _stored(db_session, user_id) == ("old", "old-ref", False)

    @pytest.mark.parametrize("token", ["", "   ", "two words", "line\nbreak", "tab\there", "нелатинка"])
    async def test_a_token_that_cannot_be_a_token_is_refused_without_asking_bambu(self, async_client, bambu, token):
        fake = bambu({})

        resp = await self._paste(async_client, token)

        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == "Invalid token"
        assert fake.calls == []

    async def test_the_whitespace_around_a_pasted_token_is_dropped(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session, token="old", refresh=None)
        fake = bambu({("GET", PROFILE): (200, {})})

        resp = await self._paste(async_client, "  pasted\n")

        assert resp.status_code == 200, resp.text
        assert fake.bearers("GET", PROFILE) == ["Bearer pasted"]
        assert (await _stored(db_session, user_id))[0] == "pasted"


def RENEWED_EACH_TIME(request: httpx.Request) -> httpx.Response:
    """A refresh endpoint that rotates the pair on every call (acc-2, acc-3, …)."""
    import json

    spent = json.loads(request.content)["refreshToken"]
    n = 2 if spent == "ref" else int(spent.rsplit("-", 1)[1]) + 1
    return httpx.Response(200, json={"accessToken": f"acc-{n}", "refreshToken": f"ref-{n}"})


class TestOneRenewalPerRequest:
    """An endpoint that answers the expiry body for a reason other than expiry
    must not spend a second refresh token: the repeat is not renewed again,
    and nothing is marked invalid: the request alone is refused (401)."""

    async def test_a_cloud_route_renews_once(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        fake = bambu({("GET", BIND): EXPIRED, ("POST", REFRESH): RENEWED_EACH_TIME})

        resp = await async_client.get("/api/v1/cloud/devices")

        assert resp.status_code == 401, resp.text
        assert fake.count("POST", REFRESH) == 1
        assert fake.bearers("GET", BIND) == ["Bearer acc", "Bearer acc-2"]
        assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)


class TestTheStatus:
    async def test_an_expired_token_the_refresh_renews_is_still_signed_in(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        bambu(
            {
                ("GET", PROFILE): EXPIRED,
                ("POST", REFRESH): (200, {"accessToken": "acc-2", "refreshToken": "ref-2"}),
            }
        )

        resp = await async_client.get("/api/v1/cloud/status")

        assert resp.json()["is_authenticated"] is True
        assert resp.json()["sign_in_expired"] is False
        assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)

    async def test_a_renewal_that_could_not_reach_bambu_does_not_mark_the_token_dead(
        self, async_client, db_session, bambu
    ):
        user_id = await _sign_in(db_session)
        bambu({("GET", PROFILE): EXPIRED, ("POST", REFRESH): _down})

        resp = await async_client.get("/api/v1/cloud/status")

        assert resp.status_code == 200, resp.text
        assert await _stored(db_session, user_id) == ("acc", "ref", False)

    async def test_a_refused_renewal_is_an_expired_sign_in(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        bambu({("GET", PROFILE): EXPIRED, ("POST", REFRESH): (401, {})})

        resp = await async_client.get("/api/v1/cloud/status")

        assert resp.json()["is_authenticated"] is False
        assert resp.json()["sign_in_expired"] is True
        assert await _stored(db_session, user_id) == ("acc", "ref", True)

    async def test_a_data_call_whose_renewal_could_not_reach_bambu_does_not_poison_the_status(
        self, async_client, db_session, bambu
    ):
        """The expiry verdict used to be cached before the renewal ran: after a
        network-failed renewal every status poll said "sign-in expired" for
        five minutes without asking Bambu."""
        user_id = await _sign_in(db_session)
        fake = bambu({("GET", BIND): EXPIRED, ("GET", PROFILE): EXPIRED, ("POST", REFRESH): _down})

        failed = await async_client.get("/api/v1/cloud/devices")
        status = await async_client.get("/api/v1/cloud/status")

        assert failed.status_code == 502, failed.text
        assert failed.json()["detail"].startswith(UNREACHABLE)
        # Nothing is known about the credential: the last known state stands.
        assert status.json()["is_authenticated"] is True
        assert status.json()["sign_in_expired"] is False
        assert fake.count("GET", PROFILE) == 1  # the status asked Bambu, not a cached verdict
        assert await _stored(db_session, user_id) == ("acc", "ref", False)

    async def test_the_next_successful_call_after_a_failed_renewal_clears_everything(
        self, async_client, db_session, bambu
    ):
        user_id = await _sign_in(db_session)
        fake = bambu(
            {
                ("GET", BIND): [EXPIRED, EXPIRED, (200, {"devices": []})],
                ("GET", PROFILE): (200, {}),
                ("POST", REFRESH): [_down, (200, {"accessToken": "acc-2", "refreshToken": "ref-2"})],
            }
        )

        first = await async_client.get("/api/v1/cloud/devices")
        second = await async_client.get("/api/v1/cloud/devices")
        status = await async_client.get("/api/v1/cloud/status")

        assert (first.status_code, second.status_code) == (502, 200), (first.text, second.text)
        assert status.json()["is_authenticated"] is True
        assert status.json()["sign_in_expired"] is False
        assert fake.count("POST", REFRESH) == 2
        assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)

    async def test_a_refused_renewal_is_remembered_as_expired(self, async_client, db_session, bambu):
        """A real expiry that was not renewed is still cached as rejected."""
        await _sign_in(db_session)
        bambu({("GET", BIND): EXPIRED, ("POST", REFRESH): (401, {})})

        await async_client.get("/api/v1/cloud/devices")

        cloud = bambu_cloud.BambuCloudService(region="global")
        cloud.set_token("acc")
        assert await cloud.validate_token() is False  # from the cache: PROFILE is not answered here


class TestMakerWorldOnTheSameToken:
    DESIGN = "/v1/design-service/design/1400373"
    INSTANCES = "/v1/design-service/design/1400373/instances"
    URL = {"url": "https://makerworld.com/en/models/1400373-seed-starter"}

    async def test_an_expired_token_is_renewed_and_the_request_repeated(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        fake = bambu(
            {
                ("GET", self.DESIGN): [EXPIRED, (200, {"id": 1400373, "title": "Seed Starter", "instances": []})],
                ("GET", self.INSTANCES): (200, {"hits": []}),
                ("POST", REFRESH): (200, {"accessToken": "acc-2", "refreshToken": "ref-2"}),
            }
        )

        resp = await async_client.post("/api/v1/makerworld/resolve", json=self.URL)

        assert resp.status_code == 200, resp.text
        assert fake.bearers("GET", self.DESIGN) == ["Bearer acc", "Bearer acc-2"]
        assert fake.bearers("GET", self.INSTANCES) == ["Bearer acc-2"]
        assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)

    async def test_a_refused_renewal_is_a_401_and_marks_the_token_dead(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        bambu({("GET", self.DESIGN): EXPIRED, ("POST", REFRESH): (401, {"error": "Please login."})})

        resp = await async_client.post("/api/v1/makerworld/resolve", json=self.URL)

        assert resp.status_code == 401, resp.text
        assert await _stored(db_session, user_id) == ("acc", "ref", True)

    async def test_without_a_refresh_token_it_is_a_401_and_marks_the_token_dead(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session, refresh=None)
        bambu({("GET", self.DESIGN): EXPIRED})

        resp = await async_client.post("/api/v1/makerworld/resolve", json=self.URL)

        assert resp.status_code == 401, resp.text
        assert await _stored(db_session, user_id) == ("acc", None, True)

    @pytest.mark.parametrize("failure", [_down, (503, {})], ids=["network", "503"])
    async def test_a_renewal_that_could_not_reach_bambu_is_a_502_and_the_token_stays_alive(
        self, async_client, db_session, bambu, failure
    ):
        user_id = await _sign_in(db_session)
        bambu({("GET", self.DESIGN): EXPIRED, ("POST", REFRESH): failure})

        resp = await async_client.post("/api/v1/makerworld/resolve", json=self.URL)

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"].startswith(UNREACHABLE)
        assert await _stored(db_session, user_id) == ("acc", "ref", False)

    async def test_an_endpoint_that_answers_expired_to_the_renewed_token_renews_once(
        self, async_client, db_session, bambu
    ):
        user_id = await _sign_in(db_session)
        fake = bambu({("GET", self.DESIGN): EXPIRED, ("POST", REFRESH): RENEWED_EACH_TIME})

        resp = await async_client.post("/api/v1/makerworld/resolve", json=self.URL)

        assert resp.status_code == 401, resp.text
        assert fake.count("POST", REFRESH) == 1
        assert fake.bearers("GET", self.DESIGN) == ["Bearer acc", "Bearer acc-2"]
        assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)

    async def test_a_network_failure_never_touches_the_token(self, async_client, db_session, bambu):
        user_id = await _sign_in(db_session)
        fake = bambu({("GET", self.DESIGN): _down})

        resp = await async_client.post("/api/v1/makerworld/resolve", json=self.URL)

        assert resp.status_code == 502, resp.text
        assert fake.count("POST", REFRESH) == 0
        assert await _stored(db_session, user_id) == ("acc", "ref", False)
