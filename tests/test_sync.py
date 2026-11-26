"""Unit tests for idempotent App Store Connect sync engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ios_appstore_sync.asc_client import ASCAuth, ASCClient, ASCError, HTTPResponse
from ios_appstore_sync.cli import build_parser
from ios_appstore_sync.cli import main as cli_main
from ios_appstore_sync.schema import parse_metadata
from ios_appstore_sync.sync import (
    LIVE_CHANGE_TAG,
    LiveAppChangeError,
    NonEditableAppInfoError,
    NonEditableVersionError,
    SyncOptions,
    sync_metadata,
    write_step_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _dummy_auth() -> ASCAuth:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return ASCAuth("TESTKEYID", "TEST-ISSUER", pem)


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    body: dict[str, Any] | None


@dataclass
class MockASCServer:
    version_state: str = "PREPARE_FOR_SUBMISSION"
    version_state_field: str = "appStoreState"
    version_exists: bool = True
    version_copyright: str = "2025 Old Studio"
    extra_versions: list[dict[str, Any]] = field(default_factory=list)
    app_infos: list[dict[str, Any]] | None = None
    primary_category: str = "UTILITIES"
    secondary_category: str | None = None
    app_info_locs: list[dict[str, Any]] = field(default_factory=list)
    version_locs: list[dict[str, Any]] = field(default_factory=list)
    review_detail: dict[str, Any] | None = None
    availability_v2: dict[str, Any] | None = None
    territory_availability_pages: list[dict[str, Any]] = field(default_factory=list)
    app_price_points: list[dict[str, Any]] = field(default_factory=list)
    app_price_schedule: dict[str, Any] | None = None
    iaps: list[dict[str, Any]] = field(default_factory=list)
    iap_locs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    iap_price_points: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    iap_price_schedules: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    _counter: int = 100

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _json_resp(self, status: int, payload: dict[str, Any]) -> HTTPResponse:
        return HTTPResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))

    def transport(
        self,
        method: str,
        url: str,
        _headers: dict[str, str],
        body: bytes | None,
        _timeout: float,
    ) -> HTTPResponse:
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        payload = json.loads(body.decode("utf-8")) if body else None
        self.requests.append(RecordedRequest(method=method, path=path, query=query, body=payload))

        # Apps lookup
        if method == "GET" and path == "/v1/apps":
            bundle_id = (query.get("filter[bundleId]") or [""])[0]
            return self._json_resp(
                200,
                {"data": [{"id": "app-1", "type": "apps", "attributes": {"bundleId": bundle_id}}]},
            )

        # App Store Versions
        if method == "GET" and path == "/v1/apps/app-1/appStoreVersions":
            versions_out: list[dict[str, Any]] = []
            requested_ver = (query.get("filter[versionString]") or [None])[0]
            if self.version_exists:
                ver_str = requested_ver or "1.0.0"
                versions_out.append(
                    {
                        "id": "ver-1",
                        "type": "appStoreVersions",
                        "attributes": {
                            "versionString": ver_str,
                            "platform": "IOS",
                            self.version_state_field: self.version_state,
                            "copyright": self.version_copyright,
                        },
                    }
                )
            for extra in self.extra_versions:
                extra_ver = (extra.get("attributes") or {}).get("versionString")
                if requested_ver is None or extra_ver == requested_ver:
                    versions_out.append(extra)
            return self._json_resp(200, {"data": versions_out})

        if method == "POST" and path == "/v1/appStoreVersions":
            attrs = payload["data"]["attributes"]
            self.version_exists = True
            self.version_copyright = attrs.get("copyright", "")
            return self._json_resp(
                201,
                {"data": {"id": "ver-new", "type": "appStoreVersions", "attributes": attrs}},
            )

        if method == "PATCH" and path.startswith("/v1/appStoreVersions/"):
            return self._json_resp(200, {"data": payload["data"]})

        # App Infos & Categories
        if method == "GET" and path == "/v1/apps/app-1/appInfos":
            infos = (
                self.app_infos
                if self.app_infos is not None
                else [
                    {
                        "id": "info-1",
                        "type": "appInfos",
                        "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"},
                    }
                ]
            )
            return self._json_resp(200, {"data": infos})

        if method == "GET" and path.startswith("/v1/appInfos/") and path.count("/") == 3:
            info_id = path.split("/")[3]
            rels: dict[str, Any] = {
                "primaryCategory": {"data": {"type": "appCategories", "id": self.primary_category}}
            }
            if self.secondary_category:
                rels["secondaryCategory"] = {
                    "data": {"type": "appCategories", "id": self.secondary_category}
                }
            return self._json_resp(
                200,
                {"data": {"id": info_id, "type": "appInfos", "relationships": rels}},
            )

        if method == "PATCH" and path.startswith("/v1/appInfos/") and path.count("/") == 3:
            return self._json_resp(200, {"data": payload["data"]})

        # App Info Localizations
        if method == "GET" and path.endswith("/appInfoLocalizations"):
            return self._json_resp(200, {"data": self.app_info_locs})

        if method == "POST" and path == "/v1/appInfoLocalizations":
            new_id = self._next_id("ailoc")
            return self._json_resp(
                201,
                {
                    "data": {
                        "id": new_id,
                        "type": "appInfoLocalizations",
                        "attributes": payload["data"]["attributes"],
                    }
                },
            )

        if method == "PATCH" and path.startswith("/v1/appInfoLocalizations/"):
            return self._json_resp(200, {"data": payload["data"]})

        # Version Localizations
        if method == "GET" and path.endswith("/appStoreVersionLocalizations"):
            return self._json_resp(200, {"data": self.version_locs})

        if method == "POST" and path == "/v1/appStoreVersionLocalizations":
            new_id = self._next_id("vloc")
            return self._json_resp(
                201,
                {
                    "data": {
                        "id": new_id,
                        "type": "appStoreVersionLocalizations",
                        "attributes": payload["data"]["attributes"],
                    }
                },
            )

        if method == "PATCH" and path.startswith("/v1/appStoreVersionLocalizations/"):
            return self._json_resp(200, {"data": payload["data"]})

        # Review Detail
        if method == "GET" and path.endswith("/appStoreReviewDetail"):
            if self.review_detail is None:
                return self._json_resp(404, {"errors": [{"code": "NOT_FOUND"}]})
            return self._json_resp(200, {"data": self.review_detail})

        if method == "POST" and path == "/v1/appStoreReviewDetails":
            return self._json_resp(
                201,
                {
                    "data": {
                        "id": "rev-new",
                        "type": "appStoreReviewDetails",
                        "attributes": payload["data"]["attributes"],
                    }
                },
            )

        if method == "PATCH" and path.startswith("/v1/appStoreReviewDetails/"):
            return self._json_resp(200, {"data": payload["data"]})

        # Availability v2 (official two-step read)
        if method == "GET" and path == "/v1/apps/app-1/appAvailabilityV2":
            if self.availability_v2 is None:
                return self._json_resp(404, {"errors": [{"code": "NOT_FOUND"}]})
            return self._json_resp(200, self.availability_v2)

        if method == "GET" and path.startswith("/v2/appAvailabilities/") and path.endswith("/territoryAvailabilities"):
            page_idx = int((query.get("page") or ["0"])[0])
            if page_idx < len(self.territory_availability_pages):
                return self._json_resp(200, self.territory_availability_pages[page_idx])
            return self._json_resp(200, {"data": []})

        if method == "POST" and path == "/v2/appAvailabilities":
            return self._json_resp(201, {"data": {"id": "avail-new", "type": "appAvailabilities"}})

        # App Pricing
        if method == "GET" and path == "/v1/apps/app-1/appPricePoints":
            return self._json_resp(200, {"data": self.app_price_points})

        if method == "GET" and path == "/v1/apps/app-1/appPriceSchedule":
            if self.app_price_schedule is None:
                return self._json_resp(404, {"errors": [{"code": "NOT_FOUND"}]})
            return self._json_resp(200, self.app_price_schedule)

        if method == "POST" and path == "/v1/appPriceSchedules":
            return self._json_resp(201, {"data": {"id": "sched-new", "type": "appPriceSchedules"}})

        # In-App Purchases v2
        if method == "GET" and path == "/v1/apps/app-1/inAppPurchasesV2":
            return self._json_resp(200, {"data": self.iaps})

        if method == "POST" and path == "/v2/inAppPurchases":
            new_id = self._next_id("iap")
            return self._json_resp(
                201,
                {
                    "data": {
                        "id": new_id,
                        "type": "inAppPurchases",
                        "attributes": payload["data"]["attributes"],
                    }
                },
            )

        if method == "PATCH" and path.startswith("/v2/inAppPurchases/"):
            return self._json_resp(200, {"data": payload["data"]})

        if method == "GET" and path.endswith("/inAppPurchaseLocalizations"):
            iap_id = path.split("/")[3]
            return self._json_resp(200, {"data": self.iap_locs.get(iap_id, [])})

        if method == "POST" and path == "/v1/inAppPurchaseLocalizations":
            new_id = self._next_id("iaploc")
            return self._json_resp(
                201,
                {
                    "data": {
                        "id": new_id,
                        "type": "inAppPurchaseLocalizations",
                        "attributes": payload["data"]["attributes"],
                    }
                },
            )

        if method == "PATCH" and path.startswith("/v1/inAppPurchaseLocalizations/"):
            return self._json_resp(200, {"data": payload["data"]})

        if method == "GET" and path.endswith("/pricePoints") and path.startswith("/v2/inAppPurchases/"):
            iap_id = path.split("/")[3]
            points = self.iap_price_points.get(iap_id)
            if points is None:
                points = self.iap_price_points.get("*", [])
            return self._json_resp(200, {"data": points})

        if method == "GET" and path.startswith("/v1/inAppPurchasePriceSchedules/"):
            iap_id = path.split("/")[3]
            sched = self.iap_price_schedules.get(iap_id)
            if sched is None:
                return self._json_resp(404, {"errors": [{"code": "NOT_FOUND"}]})
            return self._json_resp(200, sched)

        if method == "POST" and path == "/v1/inAppPurchasePriceSchedules":
            return self._json_resp(201, {"data": {"id": "iap-sched-new", "type": "inAppPurchasePriceSchedules"}})

        raise AssertionError(f"Unexpected mock ASC request: {method} {path} ({query})")


def _sample_config(*, base_price: str = "0", territories: list[str] | None = None) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "bundleId": "com.example.focuscanvas",
        "version": "1.0.0",
        "primaryLocale": "en-US",
        "copyright": "2026 Example Studio",
        "categories": {"primary": "PRODUCTIVITY", "secondary": "UTILITIES"},
        "localizations": {
            "en-US": {
                "name": "Focus Canvas",
                "subtitle": "Calm notes & daily tasks",
                "promotionalText": "Stay focused offline.",
                "description": "Offline daily planner.",
                "keywords": "planner,agenda,offline",
                "supportUrl": "https://example.com/support",
                "marketingUrl": "https://example.com",
                "privacyPolicyUrl": "https://example.com/privacy",
            },
            "zh-Hant": {
                "name": "專注畫布",
                "subtitle": "離線筆記",
                "promotionalText": "免註冊、無干擾。",
                "description": "離線任務規劃工具。",
                "keywords": "待辦,筆記",
                "supportUrl": "https://example.com/zh/support",
                "privacyPolicyUrl": "https://example.com/zh/privacy",
            },
        },
        "availability": {
            "availableInNewTerritories": True,
            "territories": territories if territories is not None else ["USA", "AUS", "TWN"],
        },
        "reviewDetail": {
            "contactFirstName": "Alex",
            "contactLastName": "Rivera",
            "contactPhone": "+1 555 0100",
            "contactEmail": "review@example.com",
            "demoAccountRequired": False,
            "notes": "No login required.",
        },
        "appPricing": {
            "baseTerritory": "USA",
            "basePrice": base_price,
        },
        "inAppPurchases": [
            {
                "productId": "com.example.tip.small",
                "referenceName": "Small Tip",
                "type": "CONSUMABLE",
                "reviewNote": "In Settings > Support.",
                "pricing": {"baseTerritory": "USA", "price": "2.99"},
                "localizations": {
                    "en-US": {"name": "Small Tip", "description": "Support development."}
                },
            },
            {
                "productId": "com.example.pro",
                "referenceName": "Pro Unlock",
                "type": "NON_CONSUMABLE",
                "reviewNote": "Tap Upgrade in Settings.",
                "pricing": {"baseTerritory": "USA", "price": "9.99"},
                "localizations": {
                    "en-US": {"name": "Pro Unlock", "description": "Unlock all export formats."}
                },
            },
        ],
    }


def test_dry_run_issues_zero_mutating_http_calls_and_tags_live_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = MockASCServer(
        app_price_points=[
            {"id": "pp-free", "attributes": {"customerPrice": "0.0"}},
            {"id": "pp-499", "attributes": {"customerPrice": "4.99"}},
        ],
        iaps=[
            {
                "id": "iap-existing",
                "attributes": {
                    "productId": "com.example.tip.small",
                    "name": "Old Tip Name",
                    "inAppPurchaseType": "CONSUMABLE",
                },
            }
        ],
        iap_price_points={
            "iap-existing": [{"id": "iap-pp-299", "attributes": {"customerPrice": "2.99"}}]
        },
    )
    client = ASCClient(_dummy_auth(), read_only=True, transport=server.transport)
    config = parse_metadata(_sample_config())

    report = sync_metadata(
        client,
        config,
        SyncOptions(dry_run=True, sync_availability=True, sync_pricing=True, sync_iap=True),
    )

    # Zero POST/PATCH/PUT/DELETE requests!
    assert all(req.method == "GET" for req in server.requests)
    counts = report.counts()
    assert counts["CREATE"] > 0
    assert counts["UPDATE"] > 0

    # Live-affecting rows must be clearly tagged in dry-run diff table
    live_entries = [
        e
        for e in report.entries
        if e.resource in {"appAvailability", "appPriceSchedule", "inAppPurchasePriceSchedule"}
    ]
    assert len(live_entries) >= 3
    assert all(LIVE_CHANGE_TAG in e.details for e in live_entries)

    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    md = report.format_markdown()
    write_step_summary(md)
    written = summary_file.read_text(encoding="utf-8")
    assert "DRY RUN" in written
    assert LIVE_CHANGE_TAG in written
    assert "--allow-live-changes" in written
    assert "| Action | Resource | Target | Details |" in written


@pytest.mark.parametrize(
    "bad_state",
    ["IN_REVIEW", "READY_FOR_SALE", "READY_FOR_DISTRIBUTION", "WAITING_FOR_REVIEW"],
)
def test_refuses_non_editable_version(bad_state: str) -> None:
    server = MockASCServer(version_state=bad_state)
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config())

    with pytest.raises(NonEditableVersionError) as exc_info:
        sync_metadata(client, config, SyncOptions(dry_run=False))
    assert bad_state in str(exc_info.value)
    assert all(req.method == "GET" for req in server.requests)


@pytest.mark.parametrize(
    "live_state",
    [
        "READY_FOR_SALE",
        "READY_FOR_DISTRIBUTION",
        "PENDING_DEVELOPER_RELEASE",
        "PROCESSING_FOR_APP_STORE",
        "PENDING_APPLE_RELEASE",
    ],
)
@pytest.mark.parametrize("live_kind", ["availability", "pricing", "iap_pricing"])
def test_live_app_blocks_immediate_changes_without_allow_live_changes(
    live_state: str, live_kind: str
) -> None:
    server = MockASCServer(
        extra_versions=[
            {
                "id": "ver-live",
                "type": "appStoreVersions",
                "attributes": {
                    "versionString": "0.9.0",
                    "platform": "IOS",
                    "appVersionState": live_state,
                },
            }
        ],
        app_price_points=[{"id": "pp-free", "attributes": {"customerPrice": "0.0"}}],
        iaps=[
            {
                "id": "iap-tip",
                "attributes": {
                    "productId": "com.example.tip.small",
                    "name": "Small Tip",
                    "inAppPurchaseType": "CONSUMABLE",
                    "reviewNote": "In Settings > Support.",
                },
            }
        ],
        iap_price_points={
            "iap-tip": [{"id": "pp-tip-299", "attributes": {"customerPrice": "2.99"}}],
            "*": [{"id": "pp-pro-999", "attributes": {"customerPrice": "9.99"}}],
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config())
    opts = SyncOptions(
        dry_run=False,
        sync_availability=(live_kind == "availability"),
        sync_pricing=(live_kind == "pricing"),
        sync_iap=(live_kind == "iap_pricing"),
        allow_live_changes=False,
    )

    with pytest.raises(LiveAppChangeError) as exc_info:
        sync_metadata(client, config, opts)
    assert isinstance(exc_info.value, ASCError)
    assert live_state in str(exc_info.value)
    assert "--allow-live-changes" in str(exc_info.value)

    # Zero mutating requests of any kind (draft or live) were issued before preflight refused
    assert not any(r.method in {"POST", "PATCH", "PUT", "DELETE"} for r in server.requests)

    # Re-running with allow_live_changes=True succeeds and issues the live POST
    live_post_paths = {
        "/v2/appAvailabilities",
        "/v1/appPriceSchedules",
        "/v1/inAppPurchasePriceSchedules",
    }
    report = sync_metadata(client, config, opts, allow_live_changes=True)
    assert report.counts()["CREATE"] > 0
    assert any(r.method == "POST" and r.path in live_post_paths for r in server.requests)


def test_live_app_allows_draft_sync_without_live_flags_and_blocks_live_flags_preflight() -> None:
    def _make_live_server() -> MockASCServer:
        return MockASCServer(
            extra_versions=[
                {
                    "id": "ver-live",
                    "type": "appStoreVersions",
                    "attributes": {"versionString": "0.9.0", "appVersionState": "READY_FOR_SALE"},
                }
            ],
            availability_v2={
                "data": {
                    "id": "avail-1",
                    "type": "appAvailabilities",
                    "attributes": {"availableInNewTerritories": True},
                }
            },
            territory_availability_pages=[
                {
                    "data": [
                        {
                            "id": f"ta-{code}",
                            "type": "territoryAvailabilities",
                            "attributes": {"available": True},
                            "relationships": {
                                "territory": {"data": {"type": "territories", "id": code}}
                            },
                        }
                        for code in ("USA", "AUS", "TWN")
                    ]
                }
            ],
            app_price_points=[{"id": "pp-free", "attributes": {"customerPrice": "0.0"}}],
            app_price_schedule={
                "data": {
                    "id": "sched-1",
                    "relationships": {
                        "baseTerritory": {"data": {"type": "territories", "id": "USA"}}
                    },
                },
                "included": [
                    {
                        "id": "ap-1",
                        "type": "appPrices",
                        "relationships": {
                            "appPricePoint": {"data": {"type": "appPricePoints", "id": "pp-free"}}
                        },
                    }
                ],
            },
        )

    # 1. Draft-only sync on a live app succeeds without --allow-live-changes
    server_draft = _make_live_server()
    client_draft = ASCClient(_dummy_auth(), read_only=False, transport=server_draft.transport)
    config = parse_metadata(_sample_config())

    report = sync_metadata(
        client_draft,
        config,
        SyncOptions(
            dry_run=False,
            sync_availability=False,
            sync_pricing=False,
            sync_iap=False,
            allow_live_changes=False,
        ),
    )
    actions = {(e.resource, e.target): e.action for e in report.entries}
    assert actions[("appAvailability", "app-1")] == "SKIPPED"
    assert actions[("appPriceSchedule", "app-1")] == "SKIPPED"
    assert not any(
        r.method == "POST" and r.path in {"/v2/appAvailabilities", "/v1/appPriceSchedules"}
        for r in server_draft.requests
    )

    # 2. IAP sync without pricing on a live app also succeeds without --allow-live-changes
    raw_no_iap_pricing = _sample_config()
    for iap in raw_no_iap_pricing["inAppPurchases"]:
        iap.pop("pricing", None)
    server_iap_no_price = _make_live_server()
    client_iap_no_price = ASCClient(
        _dummy_auth(), read_only=False, transport=server_iap_no_price.transport
    )
    iap_report = sync_metadata(
        client_iap_no_price,
        parse_metadata(raw_no_iap_pricing),
        SyncOptions(
            dry_run=False,
            sync_availability=False,
            sync_pricing=False,
            sync_iap=True,
            allow_live_changes=False,
        ),
    )
    assert iap_report.counts()["CREATE"] > 0

    # 3. Enabling live flags without --allow-live-changes fails preflight with zero mutations
    server_blocked = _make_live_server()
    client_blocked = ASCClient(_dummy_auth(), read_only=False, transport=server_blocked.transport)
    with pytest.raises(LiveAppChangeError, match="--allow-live-changes"):
        sync_metadata(
            client_blocked,
            config,
            SyncOptions(
                dry_run=False,
                sync_availability=True,
                sync_pricing=True,
                sync_iap=False,
                allow_live_changes=False,
            ),
        )
    assert not any(r.method in {"POST", "PATCH", "PUT", "DELETE"} for r in server_blocked.requests)



def test_availability_two_step_paginated_over_50_territories_is_idempotent() -> None:
    # 65 unique 3-letter ISO-like territory codes split across 2 paginated pages (40 + 25)
    all_65 = [f"T{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(65)]
    page1_codes = all_65[:40]
    page2_codes = all_65[40:]
    unavailable_codes = ["ZZX", "ZZY", "ZZZ"]

    def _make_item(code: str, available: bool) -> dict[str, Any]:
        return {
            "id": f"ta-{code}",
            "type": "territoryAvailabilities",
            "attributes": {"available": available},
            "relationships": {"territory": {"data": {"type": "territories", "id": code}}},
        }

    server = MockASCServer(
        availability_v2={
            "data": {
                "id": "avail-99",
                "type": "appAvailabilities",
                "attributes": {"availableInNewTerritories": True},
            }
        },
        territory_availability_pages=[
            {
                "data": [_make_item(c, True) for c in page1_codes],
                "included": [{"type": "territories", "id": c, "attributes": {"currency": "USD"}} for c in page1_codes],
                "links": {
                    "next": (
                        "https://api.appstoreconnect.apple.com/v2/appAvailabilities/avail-99"
                        "/territoryAvailabilities?page=1"
                    )
                },
            },
            {
                "data": [_make_item(c, True) for c in page2_codes]
                + [_make_item(c, False) for c in unavailable_codes],
                "included": [
                    {"type": "territories", "id": c, "attributes": {"currency": "USD"}}
                    for c in page2_codes + unavailable_codes
                ],
                "links": {},
            },
        ],
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config(territories=all_65))

    report = sync_metadata(
        client,
        config,
        SyncOptions(dry_run=False, sync_availability=True, sync_pricing=False, sync_iap=False),
    )

    actions = {(e.resource, e.target): e.action for e in report.entries}
    assert actions[("appAvailability", "app-1")] == "UNCHANGED"
    assert not any(r.method == "POST" and r.path == "/v2/appAvailabilities" for r in server.requests)

    # Verify official two-step endpoints & limit=200 + include=territory params
    avail_v2_gets = [r for r in server.requests if r.method == "GET" and r.path == "/v1/apps/app-1/appAvailabilityV2"]
    terr_gets = [
        r
        for r in server.requests
        if r.method == "GET" and r.path == "/v2/appAvailabilities/avail-99/territoryAvailabilities"
    ]
    assert len(avail_v2_gets) == 1
    assert len(terr_gets) == 2
    assert terr_gets[0].query.get("limit") == ["200"]
    assert terr_gets[0].query.get("include") == ["territory"]


def test_resolve_editable_app_info_strict_without_fallback() -> None:
    server = MockASCServer(
        app_infos=[
            {"id": "info-live", "type": "appInfos", "attributes": {"state": "READY_FOR_SALE"}},
            {"id": "info-review", "type": "appInfos", "attributes": {"appStoreState": "IN_REVIEW"}},
        ]
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config())

    with pytest.raises(NonEditableAppInfoError) as exc_info:
        sync_metadata(client, config, SyncOptions(dry_run=False))
    assert isinstance(exc_info.value, ASCError)
    assert "READY_FOR_SALE" in str(exc_info.value)
    assert not any(r.method == "PATCH" and "/appInfos/" in r.path for r in server.requests)


def test_asc_33_state_fields_supported_on_version_and_app_info() -> None:
    server = MockASCServer(
        version_state="INVALID_BINARY",
        version_state_field="appVersionState",
        app_infos=[
            {"id": "info-live", "type": "appInfos", "attributes": {"state": "READY_FOR_DISTRIBUTION"}},
            {"id": "info-draft", "type": "appInfos", "attributes": {"state": "PREPARE_FOR_SUBMISSION"}},
        ],
    )
    client = ASCClient(_dummy_auth(), read_only=True, transport=server.transport)
    config = parse_metadata(_sample_config())

    report = sync_metadata(client, config, SyncOptions(dry_run=True))
    assert any(
        e.resource == "appStoreVersion" and e.details == "state=INVALID_BINARY" for e in report.entries
    )
    assert any(e.resource == "appInfo.categories" and e.target == "info-draft" for e in report.entries)

    # Non-editable appVersionState READY_FOR_DISTRIBUTION must be refused
    server_bad = MockASCServer(version_state="READY_FOR_DISTRIBUTION", version_state_field="appVersionState")
    client_bad = ASCClient(_dummy_auth(), read_only=False, transport=server_bad.transport)
    with pytest.raises(NonEditableVersionError, match="READY_FOR_DISTRIBUTION"):
        sync_metadata(client_bad, config, SyncOptions(dry_run=False))


def test_existing_iap_type_mismatch_raises_asc_error() -> None:
    server = MockASCServer(
        iaps=[
            {
                "id": "iap-tip",
                "attributes": {
                    "productId": "com.example.tip.small",
                    "name": "Small Tip",
                    "inAppPurchaseType": "NON_CONSUMABLE",  # Conflicts with CONSUMABLE in metadata
                },
            }
        ]
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config())

    with pytest.raises(ASCError, match="inAppPurchaseType='NON_CONSUMABLE'.*type='CONSUMABLE'"):
        sync_metadata(client, config, SyncOptions(dry_run=False, sync_iap=True))
    assert not any(r.method in {"POST", "PATCH"} and "/inAppPurchases" in r.path for r in server.requests)


def test_idempotent_create_update_and_unchanged() -> None:
    server = MockASCServer(
        version_copyright="2026 Example Studio",
        primary_category="PRODUCTIVITY",
        secondary_category="UTILITIES",
        app_info_locs=[
            {
                "id": "ailoc-en",
                "attributes": {
                    "locale": "en-US",
                    "name": "Focus Canvas",
                    "subtitle": "Calm notes & daily tasks",
                    "privacyPolicyUrl": "https://example.com/privacy",
                },
            },
        ],
        version_locs=[
            {
                "id": "vloc-en",
                "attributes": {
                    "locale": "en-US",
                    "promotionalText": "Old promo",
                    "description": "Offline daily planner.",
                    "keywords": "planner,agenda,offline",
                    "supportUrl": "https://example.com/support",
                    "marketingUrl": "https://example.com",
                },
            }
        ],
        review_detail={
            "id": "rev-1",
            "attributes": {
                "contactFirstName": "Alex",
                "contactLastName": "Rivera",
                "contactPhone": "+1 555 0100",
                "contactEmail": "review@example.com",
                "demoAccountRequired": False,
                "notes": "No login required.",
            },
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config())

    report = sync_metadata(
        client,
        config,
        SyncOptions(dry_run=False, sync_availability=False, sync_pricing=False, sync_iap=False),
    )

    actions = {(e.resource, e.target): e.action for e in report.entries}
    assert actions[("appStoreVersion.copyright", "1.0.0")] == "UNCHANGED"
    assert actions[("appInfo.categories", "info-1")] == "UNCHANGED"
    assert actions[("appInfoLocalization", "en-US")] == "UNCHANGED"
    assert actions[("appInfoLocalization", "zh-Hant")] == "CREATE"
    assert actions[("appStoreVersionLocalization", "en-US")] == "UPDATE"
    assert actions[("appStoreVersionLocalization", "zh-Hant")] == "CREATE"
    assert actions[("appStoreReviewDetail", "rev-1")] == "UNCHANGED"
    assert actions[("appAvailability", "app-1")] == "SKIPPED"
    assert actions[("appPriceSchedule", "app-1")] == "SKIPPED"

    # Verify PATCH on en-US version localization only sent the changed promotionalText field
    patch_vloc = [
        r for r in server.requests if r.method == "PATCH" and r.path == "/v1/appStoreVersionLocalizations/vloc-en"
    ]
    assert len(patch_vloc) == 1
    assert patch_vloc[0].body["data"]["attributes"] == {"promotionalText": "Stay focused offline."}


def test_paid_upfront_pricing_and_consumable_non_consumable_iap_sync() -> None:
    server = MockASCServer(
        version_exists=False,
        app_price_points=[
            {"id": "pp-free", "attributes": {"customerPrice": "0.00"}},
            {"id": "pp-499", "attributes": {"customerPrice": "4.99"}},
        ],
        iaps=[
            {
                "id": "iap-tip",
                "attributes": {
                    "productId": "com.example.tip.small",
                    "name": "Small Tip",
                    "inAppPurchaseType": "CONSUMABLE",
                    "reviewNote": "In Settings > Support.",
                },
            }
        ],
        iap_locs={
            "iap-tip": [
                {
                    "id": "iaploc-1",
                    "attributes": {
                        "locale": "en-US",
                        "name": "Small Tip",
                        "description": "Support development.",
                    },
                }
            ]
        },
        iap_price_points={
            "iap-tip": [{"id": "pp-tip-299", "attributes": {"customerPrice": "2.99"}}],
            "*": [{"id": "pp-pro-999", "attributes": {"customerPrice": "9.99"}}],
        },
    )
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config(base_price="4.99"))

    report = sync_metadata(
        client,
        config,
        SyncOptions(dry_run=False, sync_availability=True, sync_pricing=True, sync_iap=True),
    )

    actions = {(e.resource, e.target): e.action for e in report.entries}
    assert actions[("appStoreVersion", "1.0.0")] == "CREATE"
    assert actions[("appAvailability", "app-1")] == "CREATE"
    assert actions[("appPriceSchedule", "app-1")] == "CREATE"
    assert actions[("inAppPurchase", "com.example.tip.small")] == "UNCHANGED"
    assert actions[("inAppPurchaseLocalization", "com.example.tip.small:en-US")] == "UNCHANGED"
    assert actions[("inAppPurchasePriceSchedule", "com.example.tip.small")] == "CREATE"
    assert actions[("inAppPurchase", "com.example.pro")] == "CREATE"
    assert actions[("inAppPurchaseLocalization", "com.example.pro:en-US")] == "CREATE"

    # Verify paid-upfront price schedule picked pp-499
    price_posts = [r for r in server.requests if r.method == "POST" and r.path == "/v1/appPriceSchedules"]
    assert len(price_posts) == 1
    assert (
        price_posts[0].body["included"][0]["relationships"]["appPricePoint"]["data"]["id"]
        == "pp-499"
    )

    # Verify new NON_CONSUMABLE IAP creation payload
    iap_posts = [r for r in server.requests if r.method == "POST" and r.path == "/v2/inAppPurchases"]
    assert len(iap_posts) == 1
    assert iap_posts[0].body["data"]["attributes"]["inAppPurchaseType"] == "NON_CONSUMABLE"
    assert iap_posts[0].body["data"]["attributes"]["productId"] == "com.example.pro"


def test_cli_allow_live_changes_flag_and_clean_error_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()
    parsed_default = parser.parse_args(["sync", "--metadata", "m.json"])
    assert parsed_default.allow_live_changes is False
    parsed_enabled = parser.parse_args(["sync", "--metadata", "m.json", "--allow-live-changes"])
    assert parsed_enabled.allow_live_changes is True

    # Test clean stderr error + exit code 1 (no traceback) for OSError, ValueError, JSONDecodeError across subcommands
    def raise_os_error(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    def raise_json_error(*_args: Any, **_kwargs: Any) -> Any:
        raise json.JSONDecodeError("Expecting value", "{bad", 1)

    def raise_value_error(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("bad parameter")

    monkeypatch.setattr("ios_appstore_sync.cli.validate_metadata_file", raise_os_error)
    assert cli_main(["validate", "--metadata", "x.json"]) == 1
    assert "ERROR: disk full" in capsys.readouterr().err

    monkeypatch.setattr("ios_appstore_sync.cli.validate_metadata_file", raise_json_error)
    assert cli_main(["sync", "--metadata", "x.json"]) == 1
    assert "ERROR: Invalid JSON" in capsys.readouterr().err

    monkeypatch.setattr("ios_appstore_sync.cli.frame_screenshots", raise_value_error)
    assert (
        cli_main(["frame", "--config", "c.json", "--raw-dir", str(tmp_path), "--output-dir", str(tmp_path)])
        == 1
    )
    assert "ERROR: bad parameter" in capsys.readouterr().err


def test_action_and_example_workflow_inputs_consistent() -> None:
    action_text = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")
    # macOS runners ship /bin/bash 3.2, which rejects Bash 4+ ${VAR,,} syntax
    assert ",,}" not in action_text
    # Supply-chain hardening (#5): hash-locked binary-only install, no runtime pip upgrade, caller PATH first
    assert "--require-hashes" in action_text
    assert "--no-deps" in action_text
    assert "--only-binary=:all:" in action_text
    assert "requirements.lock" in action_text
    assert "--upgrade pip" not in action_text
    assert 'export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"' in action_text

    lock_text = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    for pkg in ("pyjwt==", "cryptography==", "pillow=="):
        assert pkg in lock_text
    assert "--hash=sha256:" in lock_text

    action = yaml.safe_load(action_text)
    assert action["inputs"]["allow-live-changes"]["default"] == "false"

    example_wf = yaml.safe_load((REPO_ROOT / "examples" / "appstore-sync.yml").read_text(encoding="utf-8"))
    on_block = example_wf.get("on") or example_wf[True]
    dispatch_inputs = on_block["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["sync_availability"]["default"] is False
    assert dispatch_inputs["allow_live_changes"]["default"] is False

    sync_steps = example_wf["jobs"]["sync"]["steps"]
    sync_step = next(s for s in sync_steps if str(s.get("uses", "")).startswith("ios-ci-apps/ios-appstore-sync"))
    assert sync_step["with"]["sync-availability"] == "${{ inputs.sync_availability }}"
    assert sync_step["with"]["allow-live-changes"] == "${{ inputs.allow_live_changes }}"


def test_cli_sync_emits_partial_report_and_step_summary_on_mid_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    meta_path = tmp_path / "metadata.json"
    meta_path.write_text(json.dumps(_sample_config()), encoding="utf-8")
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    server = MockASCServer(version_exists=True)

    def failing_transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> HTTPResponse:
        if urlparse(url).path == "/v1/apps/app-1/appInfos":
            return HTTPResponse(
                500,
                {},
                json.dumps({"errors": [{"code": "SERVER_ERROR", "detail": "boom mid-sync"}]}).encode(
                    "utf-8"
                ),
            )
        return server.transport(method, url, headers, body, timeout)

    monkeypatch.setattr("ios_appstore_sync.cli.ASCAuth.from_env", lambda **_kw: _dummy_auth())
    monkeypatch.setattr(
        "ios_appstore_sync.cli.ASCClient",
        lambda auth, read_only=False: ASCClient(
            auth=auth, read_only=read_only, transport=failing_transport, max_retries=0
        ),
    )

    rc = cli_main(["sync", "--metadata", str(meta_path)])
    assert rc == 1

    captured = capsys.readouterr()
    assert "FAILED MID-SYNC" in captured.out
    assert "appStoreVersion.copyright" in captured.out
    assert "### Error" in captured.out
    assert "boom mid-sync" in captured.out
    assert "ERROR:" in captured.err
    assert "boom mid-sync" in captured.err

    summary_text = summary_path.read_text(encoding="utf-8")
    assert "FAILED MID-SYNC" in summary_text
    assert "appStoreVersion.copyright" in summary_text
    assert "### Error" in summary_text
    assert "boom mid-sync" in summary_text
