"""Unit tests for ASCClient JWT auth, retry, pagination, and safety guardrails."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ios_appstore_sync.asc_client import (
    ASCAPIError,
    ASCAuth,
    ASCClient,
    ASCError,
    ASCReadOnlyError,
    ASCSafetyError,
    ASCTransportError,
    HTTPResponse,
    urllib_transport,
)


def _generate_ec_keypair() -> tuple[str, Any]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return pem, private_key.public_key()


def test_es256_jwt_generation_and_no_secret_leak() -> None:
    pem, public_key = _generate_ec_keypair()
    now = 1_700_000_000.0
    auth = ASCAuth(
        key_id="KEY1234567",
        issuer_id="11111111-2222-3333-4444-555555555555",
        private_key_pem=pem,
        ttl_seconds=900,
        clock=lambda: now,
    )

    token = auth.get_token()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY1234567"
    assert header["typ"] == "JWT"

    claims = jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        audience="appstoreconnect-v1",
        options={"verify_exp": False},
    )
    assert claims["iss"] == "11111111-2222-3333-4444-555555555555"
    assert claims["iat"] == int(now)
    assert claims["exp"] == int(now) + 900
    assert claims["aud"] == "appstoreconnect-v1"

    # Repr must never leak private key or token
    repr_str = repr(auth)
    assert "BEGIN PRIVATE KEY" not in repr_str
    assert token not in repr_str


def test_submission_endpoints_blocked_by_safety_guardrail() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    called = False

    def fail_transport(*_args: Any) -> HTTPResponse:
        nonlocal called
        called = True
        return HTTPResponse(200, {}, b"{}")

    client = ASCClient(auth, transport=fail_transport)

    with pytest.raises(ASCSafetyError):
        client.post("/v1/reviewSubmissions", {"data": {}})

    with pytest.raises(ASCSafetyError):
        client.post("/v1/appStoreVersionSubmissions", {"data": {}})

    assert not called


def test_read_only_client_blocks_mutations() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    calls: list[str] = []

    def transport(method: str, _url: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        calls.append(method)
        return HTTPResponse(200, {}, b'{"data": []}')

    client = ASCClient(auth, read_only=True, transport=transport)
    assert client.get("/v1/apps") == {"data": []}
    assert calls == ["GET"]

    for fn in (
        lambda: client.post("/v1/appStoreVersions", {"data": {}}),
        lambda: client.patch("/v1/appStoreVersions/1", {"data": {}}),
        lambda: client.delete("/v1/appScreenshots/1"),
        lambda: client.upload_chunk(method="PUT", url="https://upload.example.com/1", headers={}, data=b"abc"),
    ):
        with pytest.raises(ASCReadOnlyError):
            fn()

    assert calls == ["GET"]


def test_429_retry_with_retry_after_and_backoff() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    responses = [
        HTTPResponse(429, {"retry-after": "2"}, b'{"errors":[{"code":"RATE_LIMIT"}]}'),
        HTTPResponse(503, {}, b'{"errors":[{"code":"SERVICE_UNAVAILABLE"}]}'),
        HTTPResponse(200, {}, json.dumps({"data": [{"id": "app-1"}]}).encode("utf-8")),
    ]
    sleeps: list[float] = []

    def transport(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        return responses.pop(0)

    client = ASCClient(
        auth,
        transport=transport,
        sleep_fn=sleeps.append,
        max_retries=3,
        base_backoff=1.0,
    )
    data = client.get("/v1/apps")
    assert data == {"data": [{"id": "app-1"}]}
    assert sleeps == [2.0, 2.0]  # Retry-After=2 on attempt 0, 1.0 * 2**1 = 2.0 on attempt 1


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_post_and_patch_never_retry_5xx(method: str) -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    attempts = 0
    sleeps: list[float] = []

    def transport(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        nonlocal attempts
        attempts += 1
        return HTTPResponse(503, {}, b'{"errors":[{"code":"SERVER_ERROR"}]}')

    client = ASCClient(auth, transport=transport, sleep_fn=sleeps.append, max_retries=3)
    with pytest.raises(ASCAPIError) as exc_info:
        client.request(method, "/v1/appStoreVersions", json_body={"data": {}})
    assert exc_info.value.status == 503
    assert attempts == 1
    assert sleeps == []


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_post_and_patch_retry_429(method: str) -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    responses = [
        HTTPResponse(429, {"retry-after": "1"}, b'{"errors":[{"code":"RATE_LIMIT"}]}'),
        HTTPResponse(201, {}, b'{"data":{"id":"ok"}}'),
    ]
    sleeps: list[float] = []

    def transport(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        return responses.pop(0)

    client = ASCClient(auth, transport=transport, sleep_fn=sleeps.append, max_retries=2)
    res = client.request(method, "/v1/appStoreVersions", json_body={"data": {}})
    assert res == {"data": {"id": "ok"}}
    assert sleeps == [1.0]


def test_delete_and_put_chunk_retry_5xx() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    delete_resps = [HTTPResponse(502, {}, b""), HTTPResponse(204, {}, b"")]
    put_resps = [HTTPResponse(503, {}, b""), HTTPResponse(200, {}, b"")]
    sleeps: list[float] = []

    def transport(method: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        if method == "DELETE":
            return delete_resps.pop(0)
        return put_resps.pop(0)

    client = ASCClient(auth, transport=transport, sleep_fn=sleeps.append, max_retries=2, base_backoff=0.5)
    client.delete("/v1/appScreenshots/s1")
    client.upload_chunk(method="PUT", url="https://uploads.example/1", headers={}, data=b"abc")
    assert sleeps == [0.5, 0.5]


def test_delete_retry_404_treated_as_success() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)

    # 1. DELETE returning 503 then 404 succeeds idempotently
    resps_5xx = [HTTPResponse(503, {}, b""), HTTPResponse(404, {}, b'{"errors":[{"code":"NOT_FOUND"}]}')]
    sleeps: list[float] = []

    def transport_5xx(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        return resps_5xx.pop(0)

    client_5xx = ASCClient(auth, transport=transport_5xx, sleep_fn=sleeps.append, max_retries=2, base_backoff=0.5)
    assert client_5xx.delete("/v1/appScreenshots/s1") is None
    assert sleeps == [0.5]

    # 2. DELETE raising TimeoutError then returning 404 succeeds idempotently
    events_timeout: list[Any] = [TimeoutError("timed out"), HTTPResponse(404, {}, b"")]
    sleeps_timeout: list[float] = []

    def transport_timeout(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        ev = events_timeout.pop(0)
        if isinstance(ev, Exception):
            raise ev
        return ev

    client_timeout = ASCClient(
        auth, transport=transport_timeout, sleep_fn=sleeps_timeout.append, max_retries=2, base_backoff=0.25
    )
    assert client_timeout.delete("/v1/appScreenshots/s1") is None
    assert sleeps_timeout == [0.25]

    # 3. Initial 404 on attempt 0 still raises ASCAPIError (unless allow_404=True)
    def transport_initial_404(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        return HTTPResponse(404, {}, b'{"errors":[{"code":"NOT_FOUND"}]}')

    client_404 = ASCClient(auth, transport=transport_initial_404, sleep_fn=lambda _: None, max_retries=2)
    with pytest.raises(ASCAPIError) as exc_info:
        client_404.delete("/v1/appScreenshots/missing")
    assert exc_info.value.status == 404



def test_timeout_and_urlerror_retry_on_idempotent_and_wrap_in_asc_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)

    # 1. GET retries TimeoutError and succeeds
    get_events: list[Any] = [TimeoutError("read timed out"), HTTPResponse(200, {}, b'{"data": []}')]
    sleeps: list[float] = []

    def flaky_get(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        ev = get_events.pop(0)
        if isinstance(ev, Exception):
            raise ev
        return ev

    client_get = ASCClient(auth, transport=flaky_get, sleep_fn=sleeps.append, max_retries=2, base_backoff=0.25)
    assert client_get.get("/v1/apps") == {"data": []}
    assert sleeps == [0.25]

    # 2. GET exhausting retries on TimeoutError raises ASCTransportError (subclass of ASCError)
    def always_timeout(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        raise TimeoutError("timed out")

    client_exhaust = ASCClient(auth, transport=always_timeout, sleep_fn=lambda _: None, max_retries=1)
    with pytest.raises(ASCTransportError) as exc_info:
        client_exhaust.get("/v1/apps")
    assert isinstance(exc_info.value, ASCError)

    # 3. POST does NOT retry TimeoutError and wraps in ASCError
    post_attempts = 0

    def post_timeout(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        nonlocal post_attempts
        post_attempts += 1
        raise TimeoutError("write timed out")

    client_post = ASCClient(auth, transport=post_timeout, sleep_fn=lambda _: None, max_retries=3)
    with pytest.raises(ASCError):
        client_post.post("/v1/appStoreVersions", {"data": {}})
    assert post_attempts == 1

    # 4. urllib_transport wraps both TimeoutError and URLError into ASCTransportError
    def raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("socket timed out")

    monkeypatch.setattr("ios_appstore_sync.asc_client.urlopen", raise_timeout)
    with pytest.raises(ASCTransportError, match="timed out"):
        urllib_transport("GET", "https://api.appstoreconnect.apple.com/v1/apps", {}, None, 1.0)

    def raise_urlerror(*_args: Any, **_kwargs: Any) -> Any:
        raise URLError("dns lookup failed")

    monkeypatch.setattr("ios_appstore_sync.asc_client.urlopen", raise_urlerror)
    with pytest.raises(ASCTransportError, match="Network error"):
        urllib_transport("GET", "https://api.appstoreconnect.apple.com/v1/apps", {}, None, 1.0)


def test_get_all_follows_pagination() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    seen_urls: list[str] = []

    def transport(_m: str, url: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        seen_urls.append(url)
        if "cursor=page2" in url:
            body = {"data": [{"id": "2"}], "links": {}}
        else:
            body = {
                "data": [{"id": "1"}],
                "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=page2"},
            }
        return HTTPResponse(200, {}, json.dumps(body).encode("utf-8"))

    client = ASCClient(auth, transport=transport)
    items = client.get_all("/v1/apps")
    assert [i["id"] for i in items] == ["1", "2"]
    assert len(seen_urls) == 2


@pytest.mark.parametrize(
    "bad_next",
    [
        "http://api.appstoreconnect.apple.com/v1/apps?cursor=2",
        "https://evil.example.com/v1/apps?cursor=2",
        "https://api.appstoreconnect.apple.com.evil.com/v1/apps?cursor=2",
        "https://user@api.appstoreconnect.apple.com/v1/apps?cursor=2",
        "file:///etc/passwd",
    ],
)
def test_get_all_rejects_disallowed_pagination_urls(bad_next: str) -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)
    seen_urls: list[str] = []

    def transport(_m: str, url: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        seen_urls.append(url)
        body = {"data": [{"id": "1"}], "links": {"next": bad_next}}
        return HTTPResponse(200, {}, json.dumps(body).encode("utf-8"))

    client = ASCClient(auth, transport=transport)
    with pytest.raises(ASCSafetyError) as exc_info:
        client.get_all("/v1/apps")
    assert isinstance(exc_info.value, ASCError)
    # Never sent Bearer token to the disallowed URL
    assert len(seen_urls) == 1


def test_get_all_allows_custom_base_url_host_and_rejects_others() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)

    def transport(_m: str, url: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        if "cursor=2" in url:
            return HTTPResponse(200, {}, json.dumps({"data": [{"id": "2"}], "links": {}}).encode("utf-8"))
        return HTTPResponse(
            200,
            {},
            json.dumps(
                {"data": [{"id": "1"}], "links": {"next": "https://asc-proxy.internal/v1/apps?cursor=2"}}
            ).encode("utf-8"),
        )

    client = ASCClient(auth, base_url="https://asc-proxy.internal", transport=transport)
    items = client.get_all("/v1/apps")
    assert [i["id"] for i in items] == ["1", "2"]


def test_non_retryable_error_formats_asc_details() -> None:
    pem, _ = _generate_ec_keypair()
    auth = ASCAuth("KEY1", "ISS1", pem)

    def transport(_m: str, _u: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        payload = {
            "errors": [
                {
                    "status": "409",
                    "code": "STATE_ERROR",
                    "title": "Invalid state",
                    "detail": "Version cannot be edited.",
                }
            ]
        }
        return HTTPResponse(409, {}, json.dumps(payload).encode("utf-8"))

    client = ASCClient(auth, transport=transport)
    with pytest.raises(ASCAPIError) as exc_info:
        client.patch("/v1/appStoreVersions/v1", {"data": {}})
    assert exc_info.value.status == 409
    assert "STATE_ERROR" in str(exc_info.value)
    assert "Version cannot be edited." in str(exc_info.value)
