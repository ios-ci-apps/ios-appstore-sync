"""Offline validator for declarative App Store Connect metadata.json (schema v1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ios_appstore_sync.schema import (
    SCHEMA_VERSION,
    SUPPORTED_IAP_TYPES,
    MetadataConfig,
    load_raw_json,
    parse_metadata,
)

LIMIT_APP_NAME_CHARS = 30
LIMIT_SUBTITLE_CHARS = 30
LIMIT_PROMOTIONAL_TEXT_CHARS = 170
LIMIT_DESCRIPTION_CHARS = 4000
LIMIT_KEYWORDS_UTF8_BYTES = 100
LIMIT_IAP_NAME_CHARS = 30
LIMIT_IAP_DESCRIPTION_CHARS = 55

_TERRITORY_RE = re.compile(r"^[A-Z]{3}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){1,2}$")


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # "error" or "warning"
    path: str
    message: str

    def format(self) -> str:
        prefix = "ERROR" if self.severity == "error" else "WARN"
        return f"[{prefix}] {self.path}: {self.message}"


@dataclass
class ValidationResult:
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    config: MetadataConfig | None = None

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, path: str, message: str) -> None:
        self.errors.append(ValidationIssue("error", path, message))

    def add_warning(self, path: str, message: str) -> None:
        self.warnings.append(ValidationIssue("warning", path, message))

    def format_report(self) -> str:
        lines: list[str] = []
        for issue in self.errors:
            lines.append(issue.format())
        for issue in self.warnings:
            lines.append(issue.format())
        if self.ok:
            summary = f"Validation passed ({len(self.warnings)} warning(s))."
        else:
            summary = (
                f"Validation failed with {len(self.errors)} error(s) "
                f"and {len(self.warnings)} warning(s)."
            )
        lines.append(summary)
        return "\n".join(lines)


def _is_valid_https_url(value: str) -> bool:
    if any(ch.isspace() for ch in value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and "." in parsed.netloc


def _validate_non_negative_price(value: Any, path: str, result: ValidationResult) -> None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        result.add_error(path, "must be a numeric price string such as '0' or '4.99'.")
        return
    text = str(value).strip()
    if not text:
        result.add_error(path, "must not be empty.")
        return
    try:
        dec = Decimal(text)
    except InvalidOperation:
        result.add_error(path, f"'{value}' is not a valid decimal price.")
        return
    if dec.is_nan() or dec.is_infinite() or dec < Decimal("0"):
        result.add_error(path, f"'{value}' must be a non-negative decimal price.")


def _validate_territory(value: Any, path: str, result: ValidationResult) -> None:
    if not isinstance(value, str) or not _TERRITORY_RE.match(value):
        result.add_error(
            path,
            f"'{value}' must be a 3-letter uppercase ISO 3166-1 alpha-3 territory code (e.g. 'USA', 'AUS', 'TWN').",
        )


def _extract_tokens(text: str) -> set[str]:
    """Extract lowercase word tokens for keyword overlap warnings."""
    return {tok for tok in re.findall(r"[^\W_]+", text.casefold()) if len(tok) >= 2}


def _check_keywords(
    locale: str,
    keywords: str,
    name: str,
    subtitle: str,
    result: ValidationResult,
) -> None:
    path = f"localizations.{locale}.keywords"
    utf8_len = len(keywords.encode("utf-8"))
    if utf8_len > LIMIT_KEYWORDS_UTF8_BYTES:
        result.add_error(
            path,
            f"exceeds Apple's {LIMIT_KEYWORDS_UTF8_BYTES} UTF-8 byte limit "
            f"(got {utf8_len} bytes across {len(keywords)} characters).",
        )

    if ", " in keywords:
        result.add_warning(
            path,
            "contains ', ' (comma + space); remove spaces after commas to avoid wasting keyword bytes.",
        )

    title_tokens = _extract_tokens(name) | _extract_tokens(subtitle)
    title_compact = f"{name} {subtitle}".casefold()
    seen_overlaps: list[str] = []
    for raw_kw in keywords.split(","):
        kw = raw_kw.strip()
        if not kw:
            continue
        kw_cf = kw.casefold()
        is_overlap = kw_cf in title_tokens or (
            len(kw_cf) >= 2 and kw_cf in title_compact and not kw_cf.isascii()
        )
        if is_overlap and kw not in seen_overlaps:
            seen_overlaps.append(kw)
    if seen_overlaps:
        result.add_warning(
            path,
            f"duplicates terms already indexed from name/subtitle: {', '.join(seen_overlaps)}.",
        )


def _extract_storekit_product_ids(storekit_path: Path) -> set[str]:
    text = storekit_path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("StoreKit config must be a JSON object.")

    product_ids: set[str] = set()
    for section in ("products", "nonRenewingSubscriptions"):
        items = data.get(section)
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict):
                    pid = entry.get("productID")
                    if isinstance(pid, str) and pid.strip():
                        product_ids.add(pid.strip())
    return product_ids


def validate_metadata_dict(
    raw: dict[str, Any],
    *,
    base_dir: Path | None = None,
    source_path: Path | None = None,
) -> ValidationResult:
    """Validate raw metadata dictionary against schema v1 and Apple App Store rules."""
    result = ValidationResult()

    if not isinstance(raw, dict):
        result.add_error("$", "metadata root must be a JSON object.")
        return result

    # 1. Schema version
    schema_version = raw.get("schemaVersion")
    if isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        result.add_error(
            "schemaVersion",
            f"must be {SCHEMA_VERSION} (got {schema_version!r}).",
        )

    # 2. App identity
    bundle_id = raw.get("bundleId")
    if not isinstance(bundle_id, str) or not _BUNDLE_ID_RE.match(bundle_id.strip()):
        result.add_error(
            "bundleId",
            "must be a valid reverse-DNS bundle identifier (e.g. 'com.example.app').",
        )

    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version.strip()):
        result.add_error(
            "version",
            "must be a valid App Store version string (e.g. '1.0' or '1.0.0').",
        )

    primary_locale = raw.get("primaryLocale")
    if not isinstance(primary_locale, str) or not primary_locale.strip():
        result.add_error("primaryLocale", "must be a non-empty locale code (e.g. 'en-US').")

    copyright_text = raw.get("copyright")
    if copyright_text is not None and not isinstance(copyright_text, str):
        result.add_error("copyright", "must be a string when provided.")

    categories = raw.get("categories")
    if categories is not None:
        if not isinstance(categories, dict):
            result.add_error("categories", "must be an object with 'primary' and optional 'secondary'.")
        else:
            primary_cat = categories.get("primary")
            if not isinstance(primary_cat, str) or not primary_cat.strip():
                result.add_error("categories.primary", "must be a non-empty App Store category ID.")
            secondary_cat = categories.get("secondary")
            if secondary_cat is not None and (not isinstance(secondary_cat, str) or not secondary_cat.strip()):
                result.add_error("categories.secondary", "must be a non-empty string when provided.")

    # 3. Localizations & Apple limits
    localizations = raw.get("localizations")
    user_facing_fields: list[tuple[str, str]] = []
    if isinstance(copyright_text, str) and copyright_text:
        user_facing_fields.append(("copyright", copyright_text))

    if not isinstance(localizations, dict) or not localizations:
        result.add_error("localizations", "must be a non-empty object mapping locale codes to copy fields.")
    else:
        if isinstance(primary_locale, str) and primary_locale and primary_locale not in localizations:
            result.add_error(
                "primaryLocale",
                f"'{primary_locale}' must exist as a key in 'localizations'.",
            )

        for locale, entry in localizations.items():
            prefix = f"localizations.{locale}"
            if not isinstance(entry, dict):
                result.add_error(prefix, "must be a JSON object.")
                continue

            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                result.add_error(f"{prefix}.name", "is required and must be a non-empty string.")
                name_str = ""
            else:
                name_str = name
                if len(name) > LIMIT_APP_NAME_CHARS:
                    result.add_error(
                        f"{prefix}.name",
                        f"exceeds Apple's {LIMIT_APP_NAME_CHARS}-character limit (got {len(name)}).",
                    )
                user_facing_fields.append((f"{prefix}.name", name))

            subtitle = entry.get("subtitle")
            subtitle_str = ""
            if subtitle is not None:
                if not isinstance(subtitle, str):
                    result.add_error(f"{prefix}.subtitle", "must be a string.")
                else:
                    subtitle_str = subtitle
                    if len(subtitle) > LIMIT_SUBTITLE_CHARS:
                        result.add_error(
                            f"{prefix}.subtitle",
                            f"exceeds Apple's {LIMIT_SUBTITLE_CHARS}-character limit (got {len(subtitle)}).",
                        )
                    user_facing_fields.append((f"{prefix}.subtitle", subtitle))

            promo = entry.get("promotionalText")
            if promo is not None:
                if not isinstance(promo, str):
                    result.add_error(f"{prefix}.promotionalText", "must be a string.")
                else:
                    if len(promo) > LIMIT_PROMOTIONAL_TEXT_CHARS:
                        result.add_error(
                            f"{prefix}.promotionalText",
                            f"exceeds Apple's {LIMIT_PROMOTIONAL_TEXT_CHARS}-character limit (got {len(promo)}).",
                        )
                    user_facing_fields.append((f"{prefix}.promotionalText", promo))

            desc = entry.get("description")
            if not isinstance(desc, str) or not desc.strip():
                result.add_error(f"{prefix}.description", "is required and must be a non-empty string.")
            else:
                if len(desc) > LIMIT_DESCRIPTION_CHARS:
                    result.add_error(
                        f"{prefix}.description",
                        f"exceeds Apple's {LIMIT_DESCRIPTION_CHARS}-character limit (got {len(desc)}).",
                    )
                user_facing_fields.append((f"{prefix}.description", desc))

            keywords = entry.get("keywords")
            if keywords is not None:
                if not isinstance(keywords, str):
                    result.add_error(f"{prefix}.keywords", "must be a string.")
                else:
                    _check_keywords(locale, keywords, name_str, subtitle_str, result)
                    user_facing_fields.append((f"{prefix}.keywords", keywords))

            for url_field in ("supportUrl", "marketingUrl", "privacyPolicyUrl"):
                url_val = entry.get(url_field)
                if (
                    url_val is not None
                    and url_val != ""
                    and (not isinstance(url_val, str) or not _is_valid_https_url(url_val))
                ):
                    result.add_error(
                        f"{prefix}.{url_field}",
                        f"must be a valid https:// URL (got {url_val!r}).",
                    )

    # 4. Availability (optional)
    availability = raw.get("availability")
    if availability is not None:
        if not isinstance(availability, dict):
            result.add_error("availability", "must be an object.")
        else:
            avail_new = availability.get("availableInNewTerritories")
            if avail_new is not None and not isinstance(avail_new, bool):
                result.add_error("availability.availableInNewTerritories", "must be a boolean.")
            territories = availability.get("territories")
            if not isinstance(territories, list) or not territories:
                result.add_error("availability.territories", "must be a non-empty array of territory codes.")
            else:
                for idx, code in enumerate(territories):
                    _validate_territory(code, f"availability.territories[{idx}]", result)

    # 5. Review detail (optional)
    review_detail = raw.get("reviewDetail")
    if review_detail is not None:
        if not isinstance(review_detail, dict):
            result.add_error("reviewDetail", "must be an object.")
        else:
            demo_req = review_detail.get("demoAccountRequired", False)
            if not isinstance(demo_req, bool):
                result.add_error("reviewDetail.demoAccountRequired", "must be a boolean.")
            elif demo_req:
                if not review_detail.get("demoAccountName"):
                    result.add_error(
                        "reviewDetail.demoAccountName",
                        "is required when demoAccountRequired is true.",
                    )
                if not review_detail.get("demoAccountPassword"):
                    result.add_error(
                        "reviewDetail.demoAccountPassword",
                        "is required when demoAccountRequired is true.",
                    )

    # 6. App pricing (optional)
    app_pricing = raw.get("appPricing")
    if app_pricing is not None:
        if not isinstance(app_pricing, dict):
            result.add_error("appPricing", "must be an object.")
        else:
            _validate_territory(app_pricing.get("baseTerritory"), "appPricing.baseTerritory", result)
            _validate_non_negative_price(app_pricing.get("basePrice"), "appPricing.basePrice", result)

    # 7. In-app purchases (optional)
    raw_iaps = raw.get("inAppPurchases")
    configured_product_ids: list[str] = []
    if raw_iaps is not None:
        if not isinstance(raw_iaps, list):
            result.add_error("inAppPurchases", "must be an array.")
        else:
            seen_product_ids: set[str] = set()
            for idx, iap in enumerate(raw_iaps):
                prefix = f"inAppPurchases[{idx}]"
                if not isinstance(iap, dict):
                    result.add_error(prefix, "must be an object.")
                    continue

                pid = iap.get("productId")
                if not isinstance(pid, str) or not pid.strip():
                    result.add_error(f"{prefix}.productId", "is required and must be a non-empty string.")
                else:
                    pid_clean = pid.strip()
                    if pid_clean in seen_product_ids:
                        result.add_error(f"{prefix}.productId", f"duplicate productId '{pid_clean}'.")
                    seen_product_ids.add(pid_clean)
                    configured_product_ids.append(pid_clean)

                ref_name = iap.get("referenceName")
                if not isinstance(ref_name, str) or not ref_name.strip():
                    result.add_error(f"{prefix}.referenceName", "is required and must be a non-empty string.")

                iap_type = iap.get("type")
                if iap_type not in SUPPORTED_IAP_TYPES:
                    allowed = ", ".join(sorted(SUPPORTED_IAP_TYPES))
                    result.add_error(
                        f"{prefix}.type",
                        f"must be one of {allowed} (got {iap_type!r}).",
                    )

                iap_pricing = iap.get("pricing")
                if iap_pricing is not None:
                    if not isinstance(iap_pricing, dict):
                        result.add_error(f"{prefix}.pricing", "must be an object.")
                    else:
                        _validate_territory(
                            iap_pricing.get("baseTerritory"),
                            f"{prefix}.pricing.baseTerritory",
                            result,
                        )
                        _validate_non_negative_price(
                            iap_pricing.get("price"),
                            f"{prefix}.pricing.price",
                            result,
                        )

                iap_locs = iap.get("localizations")
                if not isinstance(iap_locs, dict) or not iap_locs:
                    result.add_error(
                        f"{prefix}.localizations",
                        "must be a non-empty object mapping locale codes to {name, description}.",
                    )
                else:
                    for loc, loc_entry in iap_locs.items():
                        loc_prefix = f"{prefix}.localizations.{loc}"
                        if not isinstance(loc_entry, dict):
                            result.add_error(loc_prefix, "must be an object.")
                            continue
                        loc_name = loc_entry.get("name")
                        if not isinstance(loc_name, str) or not loc_name.strip():
                            result.add_error(f"{loc_prefix}.name", "is required.")
                        else:
                            if len(loc_name) > LIMIT_IAP_NAME_CHARS:
                                result.add_error(
                                    f"{loc_prefix}.name",
                                    f"exceeds Apple's {LIMIT_IAP_NAME_CHARS}-character IAP display name limit "
                                    f"(got {len(loc_name)}).",
                                )
                            user_facing_fields.append((f"{loc_prefix}.name", loc_name))

                        loc_desc = loc_entry.get("description")
                        if not isinstance(loc_desc, str) or not loc_desc.strip():
                            result.add_error(f"{loc_prefix}.description", "is required.")
                        else:
                            if len(loc_desc) > LIMIT_IAP_DESCRIPTION_CHARS:
                                result.add_error(
                                    f"{loc_prefix}.description",
                                    f"exceeds Apple's {LIMIT_IAP_DESCRIPTION_CHARS}-character IAP description limit "
                                    f"(got {len(loc_desc)}).",
                                )
                            user_facing_fields.append((f"{loc_prefix}.description", loc_desc))

    # 8. Reserved subscriptionGroups (Phase 2 guard)
    raw_sub_groups = raw.get("subscriptionGroups")
    if raw_sub_groups is not None:
        if not isinstance(raw_sub_groups, list):
            result.add_error("subscriptionGroups", "must be an array (empty in schema v1).")
        elif len(raw_sub_groups) > 0:
            result.add_error(
                "subscriptionGroups",
                "is reserved in schema v1 for Phase 2 auto-renewable subscription support and is not "
                "yet implemented. Remove non-empty entries so nothing is silently ignored.",
            )

    # 9. Validation options: forbiddenPatterns and storekitConfigPath
    validation_block = raw.get("validation")
    if validation_block is not None:
        if not isinstance(validation_block, dict):
            result.add_error("validation", "must be an object.")
        else:
            patterns = validation_block.get("forbiddenPatterns")
            if patterns is not None:
                if not isinstance(patterns, list):
                    result.add_error("validation.forbiddenPatterns", "must be an array of regex strings.")
                else:
                    for idx, pattern in enumerate(patterns):
                        if not isinstance(pattern, str) or not pattern:
                            result.add_error(
                                f"validation.forbiddenPatterns[{idx}]",
                                "must be a non-empty regex string.",
                            )
                            continue
                        try:
                            compiled = re.compile(pattern)
                        except re.error as exc:
                            result.add_error(
                                f"validation.forbiddenPatterns[{idx}]",
                                f"invalid regex '{pattern}': {exc}",
                            )
                            continue
                        for field_path, text in user_facing_fields:
                            match = compiled.search(text)
                            if match:
                                result.add_error(
                                    field_path,
                                    f"matches forbidden pattern /{pattern}/ (matched {match.group(0)!r}).",
                                )

            storekit_rel = validation_block.get("storekitConfigPath")
            if storekit_rel is not None and storekit_rel != "":
                if not isinstance(storekit_rel, str):
                    result.add_error("validation.storekitConfigPath", "must be a file path string.")
                else:
                    sk_path = Path(storekit_rel)
                    if not sk_path.is_absolute() and base_dir is not None:
                        sk_path = base_dir / sk_path
                    if not sk_path.is_file():
                        result.add_error(
                            "validation.storekitConfigPath",
                            f"StoreKit config file not found at '{sk_path}'.",
                        )
                    else:
                        try:
                            storekit_ids = _extract_storekit_product_ids(sk_path)
                        except (OSError, ValueError, json.JSONDecodeError) as exc:
                            result.add_error(
                                "validation.storekitConfigPath",
                                f"failed to parse StoreKit config '{sk_path}': {exc}",
                            )
                        else:
                            meta_ids = set(configured_product_ids)
                            missing_in_storekit = sorted(meta_ids - storekit_ids)
                            missing_in_metadata = sorted(storekit_ids - meta_ids)
                            if missing_in_storekit:
                                result.add_error(
                                    "inAppPurchases",
                                    "productId(s) present in metadata.json but missing from StoreKit config: "
                                    + ", ".join(missing_in_storekit),
                                )
                            if missing_in_metadata:
                                result.add_error(
                                    "validation.storekitConfigPath",
                                    "productID(s) present in StoreKit config but missing from metadata.json: "
                                    + ", ".join(missing_in_metadata),
                                )

    if result.ok:
        result.config = parse_metadata(raw, source_path=source_path)

    return result


def validate_metadata_file(path: str | Path) -> ValidationResult:
    """Load and validate a metadata.json file from disk."""
    file_path = Path(path).resolve()
    result = ValidationResult()
    try:
        raw = load_raw_json(file_path)
    except ValueError as exc:
        result.add_error("$", str(exc))
        return result
    return validate_metadata_dict(raw, base_dir=file_path.parent, source_path=file_path)
