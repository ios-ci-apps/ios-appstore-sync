"""Unit tests for ASC screenshot validation, chunked upload, MD5 commit, and IAP review screenshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from PIL import Image

from ios_appstore_sync.asc_client import ASCAuth, ASCClient, ASCError, HTTPResponse
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
    # Per-shot-id (or "*" default) sequence of assetDeliveryState dicts returned on GET polling
    delivery_states: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Optional fileName -> assetDeliveryState sequence
    delivery_states_by_filename: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    shot_filenames: dict[str, str] = field(default_factory=dict)
    api_calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    uploaded_chunks: dict[str, bytes] = field(default_factory=dict)
    upload_headers: list[dict[str, str]] = field(default_factory=list)
    _counter: int = 1

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _json_resp(self, status: int, payload: dict[str, Any]) -> HTTPResponse:
        return HTTPResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))

    def _next_delivery_state(self, shot_id: str) -> dict[str, Any]:
        fname = self.shot_filenames.get(shot_id, "")
        seq = self.delivery_states.get(shot_id)
        if seq is None and fname in self.delivery_states_by_filename:
            seq = self.delivery_states_by_filename[fname]
        if seq is None:
            seq = self.delivery_states.get("*")
        if not seq:
            return {"state": "COMPLETE"}
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

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
                {
                    "data": {
                        "id": self._next_id("set"),
                        "type": "appScreenshotSets",
                        "attributes": payload["data"]["attributes"],
                    }
                },
            )

        if method == "GET" and path.endswith("/appScreenshots"):
            set_id = path.split("/")[3]
            return self._json_resp(200, {"data": self.existing_shots.get(set_id, [])})

        if method == "GET" and path.startswith(("/v1/appScreenshots/", "/v1/inAppPurchaseAppStoreReviewScreenshots/")):
            shot_id = path.split("/")[3]
            res_type = (
                "appScreenshots"
                if path.startswith("/v1/appScreenshots/")
                else "inAppPurchaseAppStoreReviewScreenshots"
            )
            return self._json_resp(
                200,
                {
                    "data": {
                        "id": shot_id,
                        "type": res_type,
                        "attributes": {"assetDeliveryState": self._next_delivery_state(shot_id)},
                    }
                },
            )

        if method == "DELETE" and path.startswith(
            ("/v1/appScreenshots/", "/v1/inAppPurchaseAppStoreReviewScreenshots/")
        ):
            return HTTPResponse(204, {}, b"")

        if method == "POST" and path in (
            "/v1/appScreenshots",
            "/v1/inAppPurchaseAppStoreReviewScreenshots",
        ):
            shot_id = self._next_id("shot")
            file_name = str(payload["data"]["attributes"]["fileName"])
            self.shot_filenames[shot_id] = file_name
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
            path.startswith(("/v1/appScreenshots/", "/v1/inAppPurchaseAppStoreReviewScreenshots/"))
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
        poll_interval=0.0,
    )
    assert all(m == "GET" for m, _, _ in server_dry.api_calls)
    assert len(server_dry.uploaded_chunks) == 0
    assert report_dry.counts()["CREATE"] == 2

    # 2. Live upload: reserve -> 2 chunks each -> MD5 commit -> poll COMPLETE -> reorder
    server_live = MockUploadServer(
        delivery_states_by_filename={
            "01-home.png": [{"state": "UPLOAD_COMPLETE"}, {"state": "COMPLETE"}],
            "02-detail.png": [{"state": "COMPLETE"}],
        }
    )
    client_live = ASCClient(_dummy_auth(), read_only=False, transport=server_live.transport)
    report_live = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    sync_app_screenshots(
        client=client_live,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_IPHONE_69",
        dry_run=False,
        report=report_live,
        poll_interval=0.0,
        sleep_fn=lambda _: None,
    )

    # Reassembled chunks must match exact PNG bytes
    uploaded_blobs = list(server_live.uploaded_chunks.values())
    assert uploaded_blobs == [png1, png2]

    # Verify MD5 checksums sent in PATCH commits and polling occurred
    commits = [
        body["data"]["attributes"]
        for m, path, body in server_live.api_calls
        if m == "PATCH" and path.startswith("/v1/appScreenshots/")
    ]
    assert commits == [
        {"uploaded": True, "sourceFileChecksum": md5_1},
        {"uploaded": True, "sourceFileChecksum": md5_2},
    ]
    poll_gets = [p for m, p, _ in server_live.api_calls if m == "GET" and p.startswith("/v1/appScreenshots/")]
    assert len(poll_gets) == 3  # 2 polls for 01-home.png + 1 poll for 02-detail.png

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
        poll_interval=0.0,
    )
    assert all(m == "GET" for m, _, _ in server_same.api_calls)
    assert report_same.counts()["UNCHANGED"] == 2


def test_upload_before_delete_reorders_then_deletes_old_screenshots(tmp_path: Path) -> None:
    shots_dir = tmp_path / "framed"
    _save_png(shots_dir / "en-US" / "01-new.png", (1320, 2868), color=(11, 22, 33))
    _save_png(shots_dir / "en-US" / "02-new.png", (1320, 2868), color=(44, 55, 66))

    server = MockUploadServer(
        existing_sets={"vloc-en": [{"id": "set-1", "attributes": {"screenshotDisplayType": "APP_IPHONE_69"}}]},
        existing_shots={
            "set-1": [
                {"id": "old-1", "attributes": {"sourceFileChecksum": "aaaa"}},
                {"id": "old-2", "attributes": {"sourceFileChecksum": "bbbb"}},
            ]
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    sync_app_screenshots(
        client=client,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_IPHONE_69",
        dry_run=False,
        report=report,
        poll_interval=0.0,
    )

    # Verify ordering: all uploads/commits/polls happen BEFORE reorder PATCH, and all DELETEs happen AFTER reorder!
    mutating_seq = [(m, p) for m, p, _ in server.api_calls if m in {"POST", "PATCH", "DELETE"}]
    reorder_idx = next(
        i for i, (m, p) in enumerate(mutating_seq) if m == "PATCH" and p.endswith("/relationships/appScreenshots")
    )
    delete_indices = [i for i, (m, _) in enumerate(mutating_seq) if m == "DELETE"]
    post_indices = [i for i, (m, p) in enumerate(mutating_seq) if m == "POST" and p == "/v1/appScreenshots"]

    assert len(post_indices) == 2
    assert len(delete_indices) == 2
    assert max(post_indices) < reorder_idx < min(delete_indices)
    assert [mutating_seq[i][1] for i in delete_indices] == ["/v1/appScreenshots/old-1", "/v1/appScreenshots/old-2"]


def test_upload_before_delete_pre_deletes_only_minimum_excess_over_10(tmp_path: Path) -> None:
    shots_dir = tmp_path / "framed"
    for idx in range(5):
        _save_png(shots_dir / "en-US" / f"{idx + 1:02d}-new.png", (1320, 2868), color=(idx * 10, 20, 30))

    # 7 existing + 5 desired = 12 -> must pre-delete ONLY 2 before uploading, then delete remaining 5 after reorder!
    server = MockUploadServer(
        existing_sets={"vloc-en": [{"id": "set-1", "attributes": {"screenshotDisplayType": "APP_IPHONE_69"}}]},
        existing_shots={
            "set-1": [
                {"id": f"old-{i}", "attributes": {"sourceFileChecksum": f"deadbeef{i}"}}
                for i in range(1, 8)
            ]
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    sync_app_screenshots(
        client=client,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_IPHONE_69",
        dry_run=False,
        report=report,
        poll_interval=0.0,
    )

    mutating_seq = [(m, p) for m, p, _ in server.api_calls if m in {"POST", "PATCH", "DELETE"}]
    first_upload_idx = next(i for i, (m, p) in enumerate(mutating_seq) if m == "POST" and p == "/v1/appScreenshots")
    reorder_idx = next(
        i for i, (m, p) in enumerate(mutating_seq) if m == "PATCH" and p.endswith("/relationships/appScreenshots")
    )

    pre_deletes = [p for m, p in mutating_seq[:first_upload_idx] if m == "DELETE"]
    mid_deletes = [p for m, p in mutating_seq[first_upload_idx:reorder_idx] if m == "DELETE"]
    post_deletes = [p for m, p in mutating_seq[reorder_idx + 1 :] if m == "DELETE"]

    assert len(pre_deletes) == 2
    assert mid_deletes == []
    assert len(post_deletes) == 5


def test_asset_delivery_state_failed_reports_recovery_details_and_keeps_existing(tmp_path: Path) -> None:
    shots_dir = tmp_path / "framed"
    _save_png(shots_dir / "en-US" / "01-ok.png", (1320, 2868), color=(10, 20, 30))
    _save_png(shots_dir / "en-US" / "02-bad.png", (1320, 2868), color=(40, 50, 60))

    server = MockUploadServer(
        existing_sets={"vloc-en": [{"id": "set-77", "attributes": {"screenshotDisplayType": "APP_IPHONE_69"}}]},
        existing_shots={
            "set-77": [
                {"id": "old-1", "attributes": {"sourceFileChecksum": "1111"}},
                {"id": "old-2", "attributes": {"sourceFileChecksum": "2222"}},
            ]
        },
        delivery_states_by_filename={
            "01-ok.png": [{"state": "COMPLETE"}],
            "02-bad.png": [
                {
                    "state": "FAILED",
                    "errors": [{"code": "INVALID_BINARY", "description": "Corrupt PNG data"}],
                }
            ],
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    with pytest.raises(ASCError) as exc_info:
        sync_app_screenshots(
            client=client,
            version_loc_ids={"en-US": "vloc-en"},
            screenshots_dir=shots_dir,
            display_type="APP_IPHONE_69",
            dry_run=False,
            report=report,
            poll_interval=0.0,
        )

    msg = str(exc_info.value)
    assert "Corrupt PNG data" in msg
    assert "screenshot_set_id='set-77'" in msg
    assert "existing_ids=['old-1', 'old-2']" in msg
    assert "newly_uploaded_ids=" in msg
    assert "failed_file='en-US/02-bad.png'" in msg
    # Existing screenshots must NOT have been deleted!
    assert not any(m == "DELETE" for m, _, _ in server.api_calls)


def test_asset_delivery_state_timeout_raises_with_recovery_details(tmp_path: Path) -> None:
    shots_dir = tmp_path / "framed"
    _save_png(shots_dir / "en-US" / "01-slow.png", (1320, 2868), color=(10, 20, 30))

    server = MockUploadServer(
        existing_sets={"vloc-en": [{"id": "set-9", "attributes": {"screenshotDisplayType": "APP_IPHONE_69"}}]},
        existing_shots={"set-9": [{"id": "old-1", "attributes": {"sourceFileChecksum": "9999"}}]},
        delivery_states={"*": [{"state": "UPLOAD_COMPLETE"}]},
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    clock_ticks = iter([0.0, 0.5, 1.5])
    with pytest.raises(ASCError) as exc_info:
        sync_app_screenshots(
            client=client,
            version_loc_ids={"en-US": "vloc-en"},
            screenshots_dir=shots_dir,
            display_type="APP_IPHONE_69",
            dry_run=False,
            report=report,
            poll_interval=0.1,
            poll_timeout=1.0,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: next(clock_ticks),
        )
    msg = str(exc_info.value)
    assert "Timed out" in msg
    assert "screenshot_set_id='set-9'" in msg
    assert "failed_file='en-US/01-slow.png'" in msg
    assert not any(m == "DELETE" for m, _, _ in server.api_calls)


def test_iap_review_screenshot_upload_polling_and_dry_run(tmp_path: Path) -> None:
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
        poll_interval=0.0,
    )
    assert all(m == "GET" for m, _, _ in server_dry.api_calls)
    assert report_dry.entries[0].action == "CREATE"

    # Live replace of stale review screenshot: DELETE old -> POST reserve -> PUT chunks -> PATCH commit -> GET poll
    server_live = MockUploadServer(
        existing_iap_review_shot={
            "iap-1": {"id": "old-rev-shot", "attributes": {"sourceFileChecksum": "00000000"}}
        },
        delivery_states_by_filename={
            "tip.png": [{"state": "UPLOAD_COMPLETE"}, {"state": "COMPLETE"}],
        },
    )
    client_live = ASCClient(_dummy_auth(), read_only=False, transport=server_live.transport)
    report_live = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)
    sync_iap_review_screenshots(
        client=client_live,
        iap_ids={"com.example.tip": "iap-1"},
        config=config,
        dry_run=False,
        report=report_live,
        poll_interval=0.0,
        sleep_fn=lambda _: None,
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
    poll_gets = [
        p
        for m, p, _ in server_live.api_calls
        if m == "GET" and p.startswith("/v1/inAppPurchaseAppStoreReviewScreenshots/")
    ]
    assert len(poll_gets) == 2
    assert report_live.entries[0].action == "UPDATE"


def test_iap_review_screenshot_failure_includes_deleted_old_id_recovery_info(tmp_path: Path) -> None:
    review_png = tmp_path / "review" / "tip.png"
    _save_png(review_png, (1024, 1024), mode="RGB", color=(120, 80, 90))

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

    server = MockUploadServer(
        existing_iap_review_shot={
            "iap-1": {"id": "iap-shot-old-9", "attributes": {"sourceFileChecksum": "stale000"}}
        },
        delivery_states_by_filename={
            "tip.png": [
                {"state": "FAILED", "errors": [{"description": "Invalid screenshot dimensions"}]}
            ],
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=False)

    with pytest.raises(ASCError) as exc_info:
        sync_iap_review_screenshots(
            client=client,
            iap_ids={"com.example.tip": "iap-1"},
            config=config,
            dry_run=False,
            report=report,
            poll_interval=0.0,
            sleep_fn=lambda _: None,
        )

    msg = str(exc_info.value)
    assert "Invalid screenshot dimensions" in msg
    assert "iap_id='iap-1'" in msg
    assert "product_id='com.example.tip'" in msg
    assert "deleted_old_screenshot_id='iap-shot-old-9'" in msg
    assert "newly_uploaded_id=" in msg
    assert "failed_file='com.example.tip/tip.png'" in msg
    assert ("DELETE", "/v1/inAppPurchaseAppStoreReviewScreenshots/iap-shot-old-9") in [
        (m, p) for m, p, _ in server.api_calls
    ]


def test_path_containment_blocks_traversal_and_escaping_symlinks(tmp_path: Path) -> None:
    appstore_dir = tmp_path / "appstore"
    outside_dir = tmp_path / "outside"
    _save_png(outside_dir / "secret.png", (1320, 2868), color=(90, 90, 90))
    _save_png(outside_dir / "en-US" / "01.png", (1320, 2868), color=(90, 90, 90))

    # 1. screenshots.directory escaping base_dir via '..'
    server = MockUploadServer()
    client = ASCClient(_dummy_auth(), read_only=True, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=True)
    with pytest.raises(ScreenshotValidationError, match="escapes allowed base directory"):
        sync_app_screenshots(
            client=client,
            version_loc_ids={"en-US": "vloc-en"},
            screenshots_dir=appstore_dir / ".." / "outside",
            display_type="APP_IPHONE_69",
            dry_run=True,
            report=report,
            base_dir=appstore_dir,
        )

    # 2. Escaping symlink inside screenshots_dir/<locale>/
    shots_dir = appstore_dir / "framed"
    (shots_dir / "en-US").mkdir(parents=True, exist_ok=True)
    (shots_dir / "en-US" / "01-escape.png").symlink_to(outside_dir / "secret.png")
    with pytest.raises(ScreenshotValidationError, match="escapes allowed base directory"):
        sync_app_screenshots(
            client=client,
            version_loc_ids={"en-US": "vloc-en"},
            screenshots_dir=shots_dir,
            display_type="APP_IPHONE_69",
            dry_run=True,
            report=report,
        )

    # 3. IAP reviewScreenshot traversal via '..'
    meta_path = appstore_dir / "metadata.json"
    cfg_traversal = parse_metadata(
        {
            "schemaVersion": 1,
            "bundleId": "com.example.app",
            "version": "1.0.0",
            "primaryLocale": "en-US",
            "localizations": {"en-US": {"name": "App", "description": "Desc"}},
            "inAppPurchases": [
                {
                    "productId": "com.example.tip",
                    "referenceName": "Tip",
                    "type": "CONSUMABLE",
                    "reviewScreenshot": "../outside/secret.png",
                    "localizations": {"en-US": {"name": "Tip", "description": "Desc"}},
                }
            ],
        },
        source_path=meta_path,
    )
    with pytest.raises(ScreenshotValidationError, match="escapes allowed base directory"):
        sync_iap_review_screenshots(
            client=client,
            iap_ids={"com.example.tip": "iap-1"},
            config=cfg_traversal,
            dry_run=True,
            report=report,
        )

    # 4. IAP reviewScreenshot escaping symlink
    symlink_review = appstore_dir / "review-link.png"
    symlink_review.symlink_to(outside_dir / "secret.png")
    cfg_symlink = parse_metadata(
        {
            "schemaVersion": 1,
            "bundleId": "com.example.app",
            "version": "1.0.0",
            "primaryLocale": "en-US",
            "localizations": {"en-US": {"name": "App", "description": "Desc"}},
            "inAppPurchases": [
                {
                    "productId": "com.example.tip",
                    "referenceName": "Tip",
                    "type": "CONSUMABLE",
                    "reviewScreenshot": "review-link.png",
                    "localizations": {"en-US": {"name": "Tip", "description": "Desc"}},
                }
            ],
        },
        source_path=meta_path,
    )
    with pytest.raises(ScreenshotValidationError, match="escapes allowed base directory"):
        sync_iap_review_screenshots(
            client=client,
            iap_ids={"com.example.tip": "iap-1"},
            config=cfg_symlink,
            dry_run=True,
            report=report,
        )


def test_unknown_screenshot_display_type_emits_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shots_dir = tmp_path / "framed"
    _save_png(shots_dir / "en-US" / "01.png", (1000, 2000), color=(20, 30, 40))
    _save_png(shots_dir / "en-US" / "02.png", (1000, 2000), color=(50, 60, 70))

    server = MockUploadServer()
    client = ASCClient(_dummy_auth(), read_only=True, transport=server.transport)
    report = SyncReport(bundle_id="com.example.app", version="1.0.0", dry_run=True)

    sync_app_screenshots(
        client=client,
        version_loc_ids={"en-US": "vloc-en"},
        screenshots_dir=shots_dir,
        display_type="APP_VISION_PRO",
        dry_run=True,
        report=report,
    )

    assert len(report.warnings) == 1
    assert "APP_VISION_PRO" in report.warnings[0]
    assert "### Warnings" in report.format_markdown()
    assert "APP_VISION_PRO" in report.format_markdown()
    err = capsys.readouterr().err
    assert err.count("APP_VISION_PRO") == 1


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
