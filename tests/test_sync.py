"""Unit tests for idempotent App Store Connect sync engine."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from ios_appstore_sync.asc_client import ASCAuth, ASCClient, HTTPResponse
from ios_appstore_sync.schema import parse_metadata
from ios_appstore_sync.sync import (
    NonEditableVersionError,
    SyncOptions,
    sync_metadata,
    write_step_summary,
)


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
    version_exists: bool = True
    version_copyright: str = "2025 Old Studio"
    primary_category: str = "UTILITIES"
    secondary_category: str | None = None
    app_info_locs: list[dict[str, Any]] = field(default_factory=list)
    version_locs: list[dict[str, Any]] = field(default_factory=list)
    review_detail: dict[str, Any] | None = None
    availability: dict[str, Any] | None = None
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
            if not self.version_exists:
                return self._json_resp(200, {"data": []})
            version_str = (query.get("filter[versionString]") or ["1.0.0"])[0]
            return self._json_resp(
                200,
                {
                    "data": [
                        {
                            "id": "ver-1",
                            "type": "appStoreVersions",
                            "attributes": {
                                "versionString": version_str,
                                "platform": "IOS",
                                "appStoreState": self.version_state,
                                "copyright": self.version_copyright,
                            },
                        }
                    ]
                },
            )

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
            return self._json_resp(
                200,
                {
                    "data": [
                        {
                            "id": "info-1",
                            "type": "appInfos",
                            "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"},
                        }
                    ]
                },
            )

        if method == "GET" and path == "/v1/appInfos/info-1":
            rels: dict[str, Any] = {
                "primaryCategory": {"data": {"type": "appCategories", "id": self.primary_category}}
            }
            if self.secondary_category:
                rels["secondaryCategory"] = {
                    "data": {"type": "appCategories", "id": self.secondary_category}
                }
            return self._json_resp(
                200,
                {"data": {"id": "info-1", "type": "appInfos", "relationships": rels}},
            )

        if method == "PATCH" and path == "/v1/appInfos/info-1":
            return self._json_resp(200, {"data": payload["data"]})

        # App Info Localizations
        if method == "GET" and path == "/v1/appInfos/info-1/appInfoLocalizations":
            return self._json_resp(200, {"data": self.app_info_locs})

        if method == "POST" and path == "/v1/appInfoLocalizations":
            new_id = self._next_id("ailoc")
            return self._json_resp(
                201, {"data": {"id": new_id, "type": "appInfoLocalizations", "attributes": payload["data"]["attributes"]}}
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
                {"data": {"id": new_id, "type": "appStoreVersionLocalizations", "attributes": payload["data"]["attributes"]}},
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
                201, {"data": {"id": "rev-new", "type": "appStoreReviewDetails", "attributes": payload["data"]["attributes"]}}
            )

        if method == "PATCH" and path.startswith("/v1/appStoreReviewDetails/"):
            return self._json_resp(200, {"data": payload["data"]})

        # Availability v2
        if method == "GET" and path == "/v2/appAvailabilities/app-1":
            if self.availability is None:
                return self._json_resp(404, {"errors": [{"code": "NOT_FOUND"}]})
            return self._json_resp(200, self.availability)

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
                201, {"data": {"id": new_id, "type": "inAppPurchases", "attributes": payload["data"]["attributes"]}}
            )

        if method == "PATCH" and path.startswith("/v2/inAppPurchases/"):
            return self._json_resp(200, {"data": payload["data"]})

        if method == "GET" and path.endswith("/inAppPurchaseLocalizations"):
            iap_id = path.split("/")[3]
            return self._json_resp(200, {"data": self.iap_locs.get(iap_id, [])})

        if method == "POST" and path == "/v1/inAppPurchaseLocalizations":
            new_id = self._next_id("iaploc")
            return self._json_resp(
                201, {"data": {"id": new_id, "type": "inAppPurchaseLocalizations", "attributes": payload["data"]["attributes"]}}
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


def _sample_config(*, base_price: str = "0") -> dict[str, Any]:
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
            "territories": ["USA", "AUS", "TWN"],
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


def test_dry_run_issues_zero_mutating_http_calls_and_writes_summary(
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

    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    md = report.format_markdown()
    write_step_summary(md)
    written = summary_file.read_text(encoding="utf-8")
    assert "DRY RUN" in written
    assert "| Action | Resource | Target | Details |" in written


@pytest.mark.parametrize("bad_state", ["IN_REVIEW", "READY_FOR_SALE", "WAITING_FOR_REVIEW"])
def test_refuses_non_editable_version(bad_state: str) -> None:
    server = MockASCServer(version_state=bad_state)
    client = ASCClient(_dummy_auth(), read_only=False, transport=server.transport)
    config = parse_metadata(_sample_config())

    with pytest.raises(NonEditableVersionError) as exc_info:
        sync_metadata(client, config, SyncOptions(dry_run=False))
    assert bad_state in str(exc_info.value)
    assert all(req.method == "GET" for req in server.requests)


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
