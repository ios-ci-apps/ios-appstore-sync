"""Unit tests for ASCClient JWT auth, retry, pagination, and safety guardrails."""

from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt
import pytest

from ios_appstore_sync.asc_client import (
    ASCAPIError,
    ASCAuth,
    ASCClient,
    ASCReadOnlyError,
    ASCSafetyError,
    HTTPResponse,
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
