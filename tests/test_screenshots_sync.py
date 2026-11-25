"""Unit tests for ASC screenshot validation, chunked upload, MD5 commit, and IAP review screenshots."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from PIL import Image
import pytest

from ios_appstore_sync.asc_client import ASCAuth, ASCClient, HTTPResponse
from ios_appstore_sync.schema import parse_metadata
from ios_appstore_sync.screenshots_sync import (
    ScreenshotValidationError,
    inspect_png,
    sync_app_screenshots,
    sync_iap_review_screenshots,
)
from ios_appstore_sync.sync import SyncOptions, SyncReport, sync_metadata


def _dummy_auth() -> ASCAuth:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return ASCAuth("SHOTKEY", "SHOT-ISSUER", pem)


def _save_png(path: Path, size: tuple[int, int], mode: str = "RGB", color: Any = (20, 40, 60)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new(mode, size, color)
    img.save(path, format="PNG")
    return path.read_bytes()


def test_inspect_png_validates_dimensions_and_opaque_rgb(tmp_path: Path) -> None:
    good = tmp_path / "good.png"
    data = _save_png(good, (1320, 2868), mode="RGB")
    info = inspect_png(good, display_type="APP_IPHONE_69")
    assert info.width == 1320
    assert info.height == 2868
    assert info.mode == "RGB"
    assert info.file_size == len(data)
    assert info.md5 == hashlib.md5(data).hexdigest()

    # Alpha channel (RGBA) must fail
    rgba = tmp_path / "alpha.png"
    _save_png(rgba, (1320, 2868), mode="RGBA", color=(20, 40, 60, 128))
    with pytest.raises(ScreenshotValidationError) as exc_alpha:
        inspect_png(rgba, display_type="APP_IPHONE_69")
    assert "opaque RGB" in str(exc_alpha.value)

    # Wrong dimensions must fail
    wrong_dim = tmp_path / "small.png"
    _save_png(wrong_dim, (800, 600), mode="RGB")
    with pytest.raises(ScreenshotValidationError) as exc_dim:
        inspect_png(wrong_dim, display_type="APP_IPHONE_69")
    assert "800x600" in str(exc_dim.value)


@dataclass
class MockUploadServer:
    existing_sets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    existing_shots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    existing_iap_review_shot: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    api_calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    uploaded_chunks: dict[str, bytes] = field(default_factory=dict)
    upload_headers: list[dict[str, str]] = field(default_factory=list)
    _counter: int = 1

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _json_resp(self, status: int, payload: dict[str, Any]) -> HTTPResponse:
        return HTTPResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))

    def transport(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        _timeout: float,
    ) -> HTTPResponse:
        parsed = urlparse(url)
        # Presigned chunk upload target
        if parsed.netloc == "uploads.apple.example":
            assert "Authorization" not in headers  # Never send ASC Bearer JWT to presigned URL
            self.upload_headers.append(dict(headers))
            key = parsed.path
            self.uploaded_chunks[key] = self.uploaded_chunks.get(key, b"") + (body or b"")
            return HTTPResponse(200, {}, b"")

        path = parsed.path
        payload = json.loads(body.decode("utf-8")) if body else None
        self.api_calls.append((method, path, payload))

        if method == "GET" and path.endswith("/appScreenshotSets"):
            loc_id = path.split("/")[3]
            return self._json_resp(200, {"data": self.existing_sets.get(loc_id, [])})

        if method == "POST" and path == "/v1/appScreenshotSets":
            return self._json_resp(
                201,
                {"data": {"id": self._next_id("set"), "type": "appScreenshotSets", "attributes": payload["data"]["attributes"]}},
            )

        if method == "GET" and path.endswith("/appScreenshots"):
            set_id = path.split("/")[3]
            return self._json_resp(200, {"data": self.existing_shots.get(set_id, [])})

        if method == "DELETE" and (
            path.startswith("/v1/appScreenshots/")
            or path.startswith("/v1/inAppPurchaseAppStoreReviewScreenshots/")
        ):
            return HTTPResponse(204, {}, b"")

        if method == "POST" and path in (
            "/v1/appScreenshots",
            "/v1/inAppPurchaseAppStoreReviewScreenshots",
        ):
            shot_id = self._next_id("shot")
            file_size = int(payload["data"]["attributes"]["fileSize"])
            # Split into two upload chunks to exercise chunked upload logic
            mid = file_size // 2
            ops = [
                {
                    "method": "PUT",
                    "url": f"https://uploads.apple.example/{shot_id}",
                    "offset": 0,
                    "length": mid,
                    "requestHeaders": [{"name": "Content-Type", "value": "image/png"}],
                },
                {
                    "method": "PUT",
                    "url": f"https://uploads.apple.example/{shot_id}",
                    "offset": mid,
                    "length": file_size - mid,
                    "requestHeaders": [{"name": "Content-Type", "value": "image/png"}],
                },
            ]
            return self._json_resp(
                201,
                {
                    "data": {
                        "id": shot_id,
                        "type": payload["data"]["type"],
                        "attributes": {"uploadOperations": ops},
                    }
                },
            )

        if method == "PATCH" and (
            path.startswith("/v1/appScreenshots/")
            or path.startswith("/v1/inAppPurchaseAppStoreReviewScreenshots/")
            or path.endswith("/relationships/appScreenshots")
        ):
            return self._json_resp(200, {"data": payload["data"]})

        if method == "GET" and path.endswith("/appStoreReviewScreenshot"):
            iap_id = path.split("/")[3]
            existing = self.existing_iap_review_shot.get(iap_id)
            if existing is None:
                return self._json_resp(404, {"errors": [{"code": "NOT_FOUND"}]})
            return self._json_resp(200, {"data": existing})

        raise AssertionError(f"Unexpected request: {method} {url}")


def test_chunked_upload_and_md5_commit_and_idempotency(tmp_path: Path) -> None:
    shots_dir = tmp_path / "framed"
    png1 = _save_png(shots_dir / "en-US" / "01-home.png", (1320, 2868), color=(10, 20, 30))
    png2 = _save_png(shots_dir / "en-US" / "02-detail.png", (1320, 2868), color=(40, 50, 60))
    md5_1 = hashlib.md5(png1).hexdigest()
    md5_2 = hashlib.md5(png2).hexdigest()

    # 1. Dry-run first: zero mutations
    server_dry = MockUploadServer()
    client_dry = ASCClient(_dummy_auth(), read_only=True, transport=server_dry.transport)
    report_dry = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=True)

    sync_app_screenshots(
        client=client_dry,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_IPHONE_69",
        dry_run=True,
        report=report_dry,
    )
    assert all(m == "GET" for m, _, _ in server_dry.api_calls)
    assert len(server_dry.uploaded_chunks) == 0
    assert report_dry.counts()["CREATE"] == 2

    # 2. Live upload: reserve -> 2 chunks each -> MD5 commit -> reorder
    server_live = MockUploadServer()
    client_live = ASCClient(_dummy_auth(), read_only=False, transport=server_live.transport)
    report_live = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    sync_app_screenshots(
        client=client_live,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_IPHONE_69",
        dry_run=False,
        report=report_live,
    )

    # Reassembled chunks must match exact PNG bytes
    uploaded_blobs = list(server_live.uploaded_chunks.values())
    assert uploaded_blobs == [png1, png2]

    # Verify MD5 checksums sent in PATCH commits
    commits = [
        body["data"]["attributes"]
        for m, path, body in server_live.api_calls
        if m == "PATCH" and path.startswith("/v1/appScreenshots/")
    ]
    assert commits == [
        {"uploaded": True, "sourceFileChecksum": md5_1},
        {"uploaded": True, "sourceFileChecksum": md5_2},
    ]

    # 3. Second run with matching MD5 checksums -> UNCHANGED and zero mutations!
    server_same = MockUploadServer(
        existing_sets={
            "vloc-en": [
                {"id": "set-1", "attributes": {"screenshotDisplayType": "APP_IPHONE_69"}}
            ]
        },
        existing_shots={
            "set-1": [
                {"id": "s1", "attributes": {"sourceFileChecksum": md5_1}},
                {"id": "s2", "attributes": {"sourceFileChecksum": md5_2}},
            ]
        },
    )
    client_same = ASCClient(_dummy_auth(), read_only=False, transport=server_same.transport)
    report_same = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)
    sync_app_screenshots(
        client=client_same,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_IPHONE_69",
        dry_run=False,
        report=report_same,
    )
    assert all(m == "GET" for m, _, _ in server_same.api_calls)
    assert report_same.counts()["UNCHANGED"] == 2


def test_iap_review_screenshot_upload_and_dry_run(tmp_path: Path) -> None:
    review_png = tmp_path / "review" / "tip.png"
    review_bytes = _save_png(review_png, (1024, 1024), mode="RGB", color=(120, 80, 90))
    review_md5 = hashlib.md5(review_bytes).hexdigest()

    meta_path = tmp_path / "metadata.json"
    config = parse_metadata(
        {
            "schemaVersion": 1,
            "bundleId": "com.example.app",
            "version": "1.0.0",
            "primaryLocale": "en-US",
            "localizations": {"en-US": {"name": "App", "description": "Desc"}},
            "inAppPurchases": [
                {
                    "productId": "com.example.tip",
                    "referenceName": "Small Tip",
                    "type": "CONSUMABLE",
                    "reviewScreenshot": "review/tip.png",
                    "localizations": {
                        "en-US": {"name": "Small Tip", "description": "Support the app."}
                    },
                }
            ],
        },
        source_path=meta_path,
    )

    # Dry-run: zero mutations
    server_dry = MockUploadServer()
    client_dry = ASCClient(_dummy_auth(), read_only=True, transport=server_dry.transport)
    report_dry = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=True)
    sync_iap_review_screenshots(
        client=client_dry,
        iap_ids={"com.example.tip": "iap-1"},
        config=config,
        dry_run=True,
        report=report_dry,
    )
    assert all(m == "GET" for m, _, _ in server_dry.api_calls)
    assert report_dry.entries[0].action == "CREATE"

    # Live replace of stale review screenshot: DELETE old -> POST reserve -> PUT chunks -> PATCH commit
    server_live = MockUploadServer(
        existing_iap_review_shot={
            "iap-1": {"id": "old-rev-shot", "attributes": {"sourceFileChecksum": "00000000"}}
        }
    )
    client_live = ASCClient(_dummy_auth(), read_only=False, transport=server_live.transport)
    report_live = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)
    sync_iap_review_screenshots(
        client=client_live,
        iap_ids={"com.example.tip": "iap-1"},
        config=config,
        dry_run=False,
        report=report_live,
    )
    methods_and_paths = [(m, p) for m, p, _ in server_live.api_calls]
    assert ("DELETE", "/v1/inAppPurchaseAppStoreReviewScreenshots/old-rev-shot") in methods_and_paths
    assert ("POST", "/v1/inAppPurchaseAppStoreReviewScreenshots") in methods_and_paths
    assert list(server_live.uploaded_chunks.values()) == [review_bytes]
    patches = [
        body["data"]["attributes"]
        for m, p, body in server_live.api_calls
        if m == "PATCH" and p.startswith("/v1/inAppPurchaseAppStoreReviewScreenshots/")
    ]
    assert patches == [{"uploaded": True, "sourceFileChecksum": review_md5}]
    assert report_live.entries[0].action == "UPDATE"


def test_sync_metadata_screenshot_gating_and_dry_run(tmp_path: Path) -> None:
    shots_dir = tmp_path / "framed"
    _save_png(shots_dir / "en-US" / "01-home.png", (1320, 2868), color=(30, 60, 90))

    config = parse_metadata(
        {
            "schemaVersion": 1,
            "bundleId": "com.example.app",
            "version": "1.0.0",
            "primaryLocale": "en-US",
            "localizations": {"en-US": {"name": "App", "description": "Desc"}},
            "screenshots": {"displayType": "APP_IPHONE_69", "directory": "framed"},
        },
        source_path=tmp_path / "metadata.json",
    )

    methods: list[str] = []

    def transport(method: str, url: str, _h: dict, _b: bytes | None, _t: float) -> HTTPResponse:
        methods.append(method)
        path = urlparse(url).path
        if path == "/v1/apps":
            body = {"data": [{"id": "app-1", "attributes": {"bundleId": "com.example.app"}}]}
        elif path == "/v1/apps/app-1/appStoreVersions":
            body = {
                "data": [
                    {
                        "id": "ver-1",
                        "attributes": {
                            "versionString": "1.0.0",
                            "appStoreState": "PREPARE_FOR_SUBMISSION",
                        },
                    }
                ]
            }
        elif path == "/v1/apps/app-1/appInfos":
            body = {"data": [{"id": "info-1", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}}]}
        else:
            body = {"data": []}
        return HTTPResponse(200, {}, json.dumps(body).encode("utf-8"))

    client = ASCClient(_dummy_auth(), read_only=True, transport=transport)
    report_skipped = sync_metadata(
        client,
        config,
        SyncOptions(dry_run=True, sync_screenshots=False),
    )
    assert any(e.resource == "screenshots" and e.action == "SKIPPED" for e in report_skipped.entries)
    assert all(m == "GET" for m in methods)
