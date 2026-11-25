"""Unit tests for offline metadata.json validator."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ios_appstore_sync.cli import main as cli_main
from ios_appstore_sync.validator import validate_metadata_dict, validate_metadata_file

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"


def _base_metadata() -> dict:
    return {
        "schemaVersion": 1,
        "bundleId": "com.example.studioapp",
        "version": "1.0.0",
        "primaryLocale": "en-US",
        "copyright": "2026 Example Studio",
        "categories": {
            "primary": "PRODUCTIVITY",
            "secondary": "UTILITIES",
        },
        "localizations": {
            "en-US": {
                "name": "Focus Canvas",
                "subtitle": "Calm notes & daily tasks",
                "promotionalText": "Stay organized offline with zero clutter.",
                "description": "A calm offline workspace for daily planning.",
                "keywords": "planner,agenda,journal,offline",
                "supportUrl": "https://example.com/support",
                "marketingUrl": "https://example.com/app",
                "privacyPolicyUrl": "https://example.com/privacy",
            },
            "zh-Hant": {
                "name": "專注畫布",
                "subtitle": "離線筆記與清單",
                "promotionalText": "免註冊、無干擾，安心整理每一天。",
                "description": "在 iPhone 上完全離線管理日常任務。",
                "keywords": "行事曆,待辦,記事本,計畫",
                "supportUrl": "https://example.com/zh-hant/support",
                "privacyPolicyUrl": "https://example.com/zh-hant/privacy",
            },
        },
        "availability": {
            "availableInNewTerritories": True,
            "territories": ["USA", "AUS", "TWN"],
        },
        "appPricing": {
            "baseTerritory": "USA",
            "basePrice": "0",
        },
        "inAppPurchases": [],
        "subscriptionGroups": [],
    }


@pytest.mark.parametrize(
    "filename",
    [
        "free-tipjar-metadata.json",
        "paid-upfront-metadata.json",
        "pro-unlock-metadata.json",
    ],
)
def test_example_configs_validate_cleanly(filename: str) -> None:
    res = validate_metadata_file(EXAMPLES_DIR / filename)
    assert res.ok, res.format_report()
    assert res.config is not None


def test_valid_base_metadata_passes() -> None:
    res = validate_metadata_dict(_base_metadata())
    assert res.ok, res.format_report()
    assert res.warnings == []


def test_apple_length_limits_enforced() -> None:
    raw = _base_metadata()
    raw["localizations"]["en-US"]["name"] = "A" * 31
    raw["localizations"]["en-US"]["subtitle"] = "B" * 31
    raw["localizations"]["en-US"]["promotionalText"] = "C" * 171
    raw["localizations"]["en-US"]["description"] = "D" * 4001

    res = validate_metadata_dict(raw)
    assert not res.ok
    paths = {issue.path for issue in res.errors}
    assert "localizations.en-US.name" in paths
    assert "localizations.en-US.subtitle" in paths
    assert "localizations.en-US.promotionalText" in paths
    assert "localizations.en-US.description" in paths


def test_cjk_keywords_utf8_byte_limit() -> None:
    # 33 CJK characters = 99 UTF-8 bytes -> passes
    raw_ok = _base_metadata()
    raw_ok["localizations"]["zh-Hant"]["keywords"] = "日" * 33
    assert len(raw_ok["localizations"]["zh-Hant"]["keywords"].encode("utf-8")) == 99
    res_ok = validate_metadata_dict(raw_ok)
    assert res_ok.ok, res_ok.format_report()

    # 45 CJK characters = 135 UTF-8 bytes -> fails despite < 100 chars
    raw_bad = _base_metadata()
    raw_bad["localizations"]["zh-Hant"]["keywords"] = "日" * 45
    assert len(raw_bad["localizations"]["zh-Hant"]["keywords"].encode("utf-8")) == 135
    res_bad = validate_metadata_dict(raw_bad)
    assert not res_bad.ok
    assert any(
        issue.path == "localizations.zh-Hant.keywords" and "100 UTF-8 byte" in issue.message
        for issue in res_bad.errors
    )


def test_keywords_warnings_are_non_blocking() -> None:
    raw = _base_metadata()
    raw["localizations"]["en-US"]["keywords"] = "focus, planner, notes"
    res = validate_metadata_dict(raw)
    assert res.ok
    warning_messages = " ".join(w.message for w in res.warnings)
    assert "comma + space" in warning_messages
    assert "already indexed from name/subtitle" in warning_messages


def test_https_urls_enforced() -> None:
    raw = _base_metadata()
    raw["localizations"]["en-US"]["supportUrl"] = "http://example.com/support"
    raw["localizations"]["en-US"]["marketingUrl"] = "not-a-url"
    raw["localizations"]["en-US"]["privacyPolicyUrl"] = "https://example.com/has space"

    res = validate_metadata_dict(raw)
    assert not res.ok
    paths = {issue.path for issue in res.errors}
    assert "localizations.en-US.supportUrl" in paths
    assert "localizations.en-US.marketingUrl" in paths
    assert "localizations.en-US.privacyPolicyUrl" in paths


def test_forbidden_patterns_detected_across_copy() -> None:
    raw = _base_metadata()
    raw["validation"] = {"forbiddenPatterns": [r"\bTODO\b", r"beta\s+build"]}
    raw["localizations"]["en-US"]["description"] = "Great app — TODO replace copy before launch."
    raw["localizations"]["en-US"]["promotionalText"] = "Internal beta build preview."

    res = validate_metadata_dict(raw)
    assert not res.ok
    paths = {issue.path for issue in res.errors}
    assert "localizations.en-US.description" in paths
    assert "localizations.en-US.promotionalText" in paths


def test_non_empty_subscription_groups_rejected_with_phase2_message() -> None:
    raw = _base_metadata()
    raw["subscriptionGroups"] = [{"referenceName": "Pro Monthly", "subscriptions": []}]

    res = validate_metadata_dict(raw)
    assert not res.ok
    assert any(
        issue.path == "subscriptionGroups" and "Phase 2" in issue.message
        for issue in res.errors
    )


def test_storekit_cross_check_pass_and_mismatch(tmp_path: Path) -> None:
    storekit_file = tmp_path / "Products.storekit"
    storekit_file.write_text(
        json.dumps(
            {
                "products": [
                    {"productID": "com.example.app.pro", "type": "NonConsumable"},
                    {"productID": "com.example.app.tip", "type": "Consumable"},
                ]
            }
        ),
        encoding="utf-8",
    )

    raw = _base_metadata()
    raw["inAppPurchases"] = [
        {
            "productId": "com.example.app.pro",
            "referenceName": "Pro Unlock",
            "type": "NON_CONSUMABLE",
            "localizations": {"en-US": {"name": "Pro Unlock", "description": "Unlock all features."}},
        },
        {
            "productId": "com.example.app.tip",
            "referenceName": "Tip",
            "type": "CONSUMABLE",
            "localizations": {"en-US": {"name": "Tip", "description": "Support the studio."}},
        },
    ]
    raw["validation"] = {"storekitConfigPath": "Products.storekit"}

    meta_path = tmp_path / "metadata.json"
    meta_path.write_text(json.dumps(raw), encoding="utf-8")

    # Matching product IDs pass
    res_ok = validate_metadata_file(meta_path)
    assert res_ok.ok, res_ok.format_report()

    # Mismatch: extra ID in metadata and missing ID from StoreKit
    raw_mismatch = copy.deepcopy(raw)
    raw_mismatch["inAppPurchases"][1]["productId"] = "com.example.app.wrong"
    meta_mismatch = tmp_path / "metadata-bad.json"
    meta_mismatch.write_text(json.dumps(raw_mismatch), encoding="utf-8")

    res_bad = validate_metadata_file(meta_mismatch)
    assert not res_bad.ok
    combined = res_bad.format_report()
    assert "com.example.app.wrong" in combined
    assert "com.example.app.tip" in combined


def test_cli_validate_exit_codes(tmp_path: Path) -> None:
    good = EXAMPLES_DIR / "paid-upfront-metadata.json"
    assert cli_main(["validate", "--metadata", str(good)]) == 0

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
    assert cli_main(["validate", "--metadata", str(bad)]) == 1
