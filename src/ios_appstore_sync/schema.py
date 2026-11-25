"""Declarative schema v1 data models for ios-appstore-sync."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

SUPPORTED_IAP_TYPES = frozenset(
    {
        "CONSUMABLE",
        "NON_CONSUMABLE",
        "NON_RENEWING_SUBSCRIPTION",
    }
)


@dataclass(frozen=True)
class CategoriesConfig:
    primary: str
    secondary: str | None = None


@dataclass(frozen=True)
class AppLocalizationConfig:
    locale: str
    name: str
    description: str
    subtitle: str | None = None
    promotional_text: str | None = None
    keywords: str | None = None
    support_url: str | None = None
    marketing_url: str | None = None
    privacy_policy_url: str | None = None


@dataclass(frozen=True)
class AvailabilityConfig:
    available_in_new_territories: bool = True
    territories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewDetailConfig:
    contact_first_name: str | None = None
    contact_last_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    demo_account_required: bool = False
    demo_account_name: str | None = None
    demo_account_password: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class AppPricingConfig:
    base_territory: str
    base_price: str


@dataclass(frozen=True)
class IAPPricingConfig:
    base_territory: str
    price: str


@dataclass(frozen=True)
class IAPLocalizationConfig:
    locale: str
    name: str
    description: str


@dataclass(frozen=True)
class InAppPurchaseConfig:
    product_id: str
    reference_name: str
    type: str
    localizations: dict[str, IAPLocalizationConfig] = field(default_factory=dict)
    review_note: str | None = None
    review_screenshot: str | None = None
    pricing: IAPPricingConfig | None = None


@dataclass(frozen=True)
class ValidationConfig:
    storekit_config_path: str | None = None
    forbidden_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScreenshotsSyncConfig:
    display_type: str = "APP_IPHONE_69"
    directory: str | None = None


@dataclass(frozen=True)
class MetadataConfig:
    schema_version: int
    bundle_id: str
    version: str
    primary_locale: str
    localizations: dict[str, AppLocalizationConfig]
    copyright: str | None = None
    categories: CategoriesConfig | None = None
    availability: AvailabilityConfig | None = None
    review_detail: ReviewDetailConfig | None = None
    app_pricing: AppPricingConfig | None = None
    in_app_purchases: tuple[InAppPurchaseConfig, ...] = ()
    subscription_groups: tuple[Any, ...] = ()
    screenshots: ScreenshotsSyncConfig | None = None
    validation: ValidationConfig | None = None
    source_path: Path | None = None


def load_raw_json(path: str | Path) -> dict[str, Any]:
    """Load a JSON object from disk, raising ValueError on malformed input."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read '{file_path}': {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in '{file_path}': {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected top-level JSON object in '{file_path}'.")
    return data


def _opt_str(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def parse_metadata(raw: dict[str, Any], source_path: Path | None = None) -> MetadataConfig:
    """Convert a validated raw dict into a typed MetadataConfig."""
    categories: CategoriesConfig | None = None
    raw_categories = raw.get("categories")
    if isinstance(raw_categories, dict):
        categories = CategoriesConfig(
            primary=str(raw_categories.get("primary", "")),
            secondary=_opt_str(raw_categories, "secondary"),
        )

    localizations: dict[str, AppLocalizationConfig] = {}
    raw_locs = raw.get("localizations") or {}
    if isinstance(raw_locs, dict):
        for locale, entry in raw_locs.items():
            if not isinstance(entry, dict):
                continue
            localizations[locale] = AppLocalizationConfig(
                locale=locale,
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                subtitle=_opt_str(entry, "subtitle"),
                promotional_text=_opt_str(entry, "promotionalText"),
                keywords=_opt_str(entry, "keywords"),
                support_url=_opt_str(entry, "supportUrl"),
                marketing_url=_opt_str(entry, "marketingUrl"),
                privacy_policy_url=_opt_str(entry, "privacyPolicyUrl"),
            )

    availability: AvailabilityConfig | None = None
    raw_avail = raw.get("availability")
    if isinstance(raw_avail, dict):
        territories = tuple(str(t) for t in (raw_avail.get("territories") or ()))
        availability = AvailabilityConfig(
            available_in_new_territories=bool(raw_avail.get("availableInNewTerritories", True)),
            territories=territories,
        )

    review_detail: ReviewDetailConfig | None = None
    raw_review = raw.get("reviewDetail")
    if isinstance(raw_review, dict):
        review_detail = ReviewDetailConfig(
            contact_first_name=_opt_str(raw_review, "contactFirstName"),
            contact_last_name=_opt_str(raw_review, "contactLastName"),
            contact_phone=_opt_str(raw_review, "contactPhone"),
            contact_email=_opt_str(raw_review, "contactEmail"),
            demo_account_required=bool(raw_review.get("demoAccountRequired", False)),
            demo_account_name=_opt_str(raw_review, "demoAccountName"),
            demo_account_password=_opt_str(raw_review, "demoAccountPassword"),
            notes=_opt_str(raw_review, "notes"),
        )

    app_pricing: AppPricingConfig | None = None
    raw_pricing = raw.get("appPricing")
    if isinstance(raw_pricing, dict):
        app_pricing = AppPricingConfig(
            base_territory=str(raw_pricing.get("baseTerritory", "")),
            base_price=str(raw_pricing.get("basePrice", "")),
        )

    iaps: list[InAppPurchaseConfig] = []
    raw_iaps = raw.get("inAppPurchases") or []
    if isinstance(raw_iaps, list):
        for item in raw_iaps:
            if not isinstance(item, dict):
                continue
            iap_pricing: IAPPricingConfig | None = None
            raw_iap_pricing = item.get("pricing")
            if isinstance(raw_iap_pricing, dict):
                iap_pricing = IAPPricingConfig(
                    base_territory=str(raw_iap_pricing.get("baseTerritory", "")),
                    price=str(raw_iap_pricing.get("price", "")),
                )
            iap_locs: dict[str, IAPLocalizationConfig] = {}
            raw_iap_locs = item.get("localizations") or {}
            if isinstance(raw_iap_locs, dict):
                for loc, loc_val in raw_iap_locs.items():
                    if not isinstance(loc_val, dict):
                        continue
                    iap_locs[loc] = IAPLocalizationConfig(
                        locale=loc,
                        name=str(loc_val.get("name", "")),
                        description=str(loc_val.get("description", "")),
                    )
            iaps.append(
                InAppPurchaseConfig(
                    product_id=str(item.get("productId", "")),
                    reference_name=str(item.get("referenceName", "")),
                    type=str(item.get("type", "")),
                    localizations=iap_locs,
                    review_note=_opt_str(item, "reviewNote"),
                    review_screenshot=_opt_str(item, "reviewScreenshot"),
                    pricing=iap_pricing,
                )
            )

    raw_subs = raw.get("subscriptionGroups") or []
    subscription_groups = tuple(raw_subs) if isinstance(raw_subs, list) else (raw_subs,)

    screenshots: ScreenshotsSyncConfig | None = None
    raw_shots = raw.get("screenshots")
    if isinstance(raw_shots, dict):
        screenshots = ScreenshotsSyncConfig(
            display_type=str(raw_shots.get("displayType", "APP_IPHONE_69")),
            directory=_opt_str(raw_shots, "directory"),
        )

    validation: ValidationConfig | None = None
    raw_val = raw.get("validation")
    if isinstance(raw_val, dict):
        patterns = tuple(str(p) for p in (raw_val.get("forbiddenPatterns") or ()))
        validation = ValidationConfig(
            storekit_config_path=_opt_str(raw_val, "storekitConfigPath"),
            forbidden_patterns=patterns,
        )

    return MetadataConfig(
        schema_version=int(raw.get("schemaVersion", 0)),
        bundle_id=str(raw.get("bundleId", "")),
        version=str(raw.get("version", "")),
        primary_locale=str(raw.get("primaryLocale", "")),
        localizations=localizations,
        copyright=_opt_str(raw, "copyright"),
        categories=categories,
        availability=availability,
        review_detail=review_detail,
        app_pricing=app_pricing,
        in_app_purchases=tuple(iaps),
        subscription_groups=subscription_groups,
        screenshots=screenshots,
        validation=validation,
        source_path=source_path,
    )
