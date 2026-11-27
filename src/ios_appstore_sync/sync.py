"""Idempotent App Store Connect draft sync engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from ios_appstore_sync.asc_client import ASCClient, ASCError
from ios_appstore_sync.schema import (
    AppPricingConfig,
    AvailabilityConfig,
    InAppPurchaseConfig,
    MetadataConfig,
    ReviewDetailConfig,
)

DiffAction = Literal["CREATE", "UPDATE", "UNCHANGED", "SKIPPED"]

LIVE_CHANGE_TAG = "[LIVE — app-level, immediate]"

EDITABLE_VERSION_STATES = frozenset(
    {
        "PREPARE_FOR_SUBMISSION",
        "DEVELOPER_REJECTED",
        "REJECTED",
        "METADATA_REJECTED",
        "INVALID_BINARY",
    }
)

EDITABLE_APP_INFO_STATES = EDITABLE_VERSION_STATES

LIVE_VERSION_STATES = frozenset(
    {
        "READY_FOR_SALE",
        "READY_FOR_DISTRIBUTION",
        "PENDING_DEVELOPER_RELEASE",
        "PROCESSING_FOR_APP_STORE",
        "PENDING_APPLE_RELEASE",
    }
)


class NonEditableVersionError(ASCError):
    """Raised when the target App Store version is in a non-editable state."""


class NonEditableAppInfoError(ASCError):
    """Raised when no editable appInfo resource exists for the app."""


class LiveAppChangeError(ASCError):
    """Raised when an immediate app-level mutation is attempted on a live app without confirmation."""


@dataclass(frozen=True)
class DiffEntry:
    action: DiffAction
    resource: str
    target: str
    details: str = ""


@dataclass(frozen=True)
class SyncOptions:
    dry_run: bool = True
    sync_availability: bool = False
    sync_pricing: bool = False
    sync_iap: bool = False
    sync_screenshots: bool = False
    allow_live_changes: bool = False
    screenshots_dir: Path | None = None
    screenshot_display_type: str | None = None


@dataclass
class SyncReport:
    bundle_id: str
    version: str
    dry_run: bool
    entries: list[DiffEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, action: DiffAction, resource: str, target: str, details: str = "") -> None:
        self.entries.append(DiffEntry(action=action, resource=resource, target=target, details=details))

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def counts(self) -> dict[str, int]:
        result = {"CREATE": 0, "UPDATE": 0, "UNCHANGED": 0, "SKIPPED": 0}
        for entry in self.entries:
            result[entry.action] = result.get(entry.action, 0) + 1
        return result

    def format_markdown(self, *, error: str | None = None) -> str:
        mode = "DRY RUN (no changes written)" if self.dry_run else "LIVE SYNC"
        if error is not None:
            mode = f"{mode} — FAILED MID-SYNC"
        counts = self.counts()
        summary_line = (
            f"- **Summary:** {counts['CREATE']} create, {counts['UPDATE']} update, "
            f"{counts['UNCHANGED']} unchanged, {counts['SKIPPED']} skipped"
        )
        lines = [
            f"## App Store Connect Sync — {mode}",
            "",
            f"- **Bundle ID:** `{self.bundle_id}`",
            f"- **Version:** `{self.version}`",
            summary_line,
            "",
            "| Action | Resource | Target | Details |",
            "|---|---|---|---|",
        ]
        has_live_changes = False
        for entry in self.entries:
            if LIVE_CHANGE_TAG in entry.details:
                has_live_changes = True
            details = entry.details.replace("|", "\\|").replace("\n", " ")
            target = entry.target.replace("|", "\\|")
            lines.append(f"| `{entry.action}` | `{entry.resource}` | `{target}` | {details} |")
        if self.dry_run and has_live_changes:
            lines.extend(
                [
                    "",
                    f"> **Note:** Rows tagged `{LIVE_CHANGE_TAG}` modify app-level resources that take "
                    "effect immediately on the live App Store (not gated by a draft version). "
                    "When the app has a live version, `--allow-live-changes` is required for non-dry-run sync.",
                ]
            )
        if self.warnings:
            lines.extend(["", "### Warnings", ""])
            for warning in self.warnings:
                lines.append(f"- {warning}")
        if error is not None:
            lines.extend(["", "### Error", "", error])
        return "\n".join(lines) + "\n"


def write_step_summary(markdown: str) -> None:
    """Append markdown report to $GITHUB_STEP_SUMMARY when running in GitHub Actions."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(markdown)


def _norm_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _version_state(version: dict[str, Any]) -> str:
    attrs = version.get("attributes") or {}
    return str(attrs.get("appVersionState") or attrs.get("appStoreState") or "")


def _app_info_state(info: dict[str, Any]) -> str:
    attrs = info.get("attributes") or {}
    return str(attrs.get("state") or attrs.get("appStoreState") or "")


def _changed_fields(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key, target_val in desired.items():
        if target_val is None:
            continue
        cur_val = current.get(key)
        if isinstance(target_val, bool):
            if bool(cur_val) != target_val:
                diff[key] = target_val
        else:
            if _norm_str(cur_val) != _norm_str(target_val):
                diff[key] = target_val
    return diff


def _prices_equal(a: Any, b: Any) -> bool:
    try:
        return Decimal(str(a).strip()) == Decimal(str(b).strip())
    except (InvalidOperation, ValueError):
        return str(a).strip() == str(b).strip()


class _LiveAppGuard:
    """Lazily checks whether the app has a live/released version before immediate app-level mutations."""

    def __init__(self, client: ASCClient, app_id: str, options: SyncOptions) -> None:
        self._client = client
        self._app_id = app_id
        self._options = options
        self._live_versions: list[tuple[str, str]] | None = None

    def _get_live_versions(self) -> list[tuple[str, str]]:
        if self._live_versions is None:
            versions = self._client.get_all(f"/v1/apps/{self._app_id}/appStoreVersions")
            found: list[tuple[str, str]] = []
            for ver in versions:
                state = _version_state(ver)
                if state in LIVE_VERSION_STATES:
                    attrs = ver.get("attributes") or {}
                    ver_str = str(attrs.get("versionString") or ver.get("id") or "?")
                    found.append((ver_str, state))
            self._live_versions = found
        return self._live_versions

    def ensure_allowed(self, resource: str, target: str) -> None:
        if self._options.dry_run or self._options.allow_live_changes:
            return
        live = self._get_live_versions()
        if live:
            live_desc = ", ".join(f"{v} ({s})" for v, s in live)
            raise LiveAppChangeError(
                f"Refused immediate app-level mutation on {resource} ({target}) because app "
                f"'{self._app_id}' has live/released version(s): {live_desc}. "
                "App availability, app pricing, and IAP price schedules take effect immediately "
                "on the live App Store (not gated by a draft version). "
                "Re-run with --allow-live-changes (or allow_live_changes=True) to confirm."
            )


def _resolve_app(client: ASCClient, bundle_id: str) -> dict[str, Any]:
    apps = client.get_all("/v1/apps", params={"filter[bundleId]": bundle_id})
    for app in apps:
        attrs = app.get("attributes") or {}
        if attrs.get("bundleId") == bundle_id:
            return app
    raise ASCError(f"App with bundleId '{bundle_id}' was not found in App Store Connect.")


def _resolve_or_create_version(
    client: ASCClient,
    app_id: str,
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
) -> tuple[str | None, dict[str, Any]]:
    versions = client.get_all(
        f"/v1/apps/{app_id}/appStoreVersions",
        params={"filter[platform]": "IOS", "filter[versionString]": config.version},
    )
    matching = [
        v for v in versions if (v.get("attributes") or {}).get("versionString") == config.version
    ]

    if matching:
        ver = matching[0]
        attrs = ver.get("attributes") or {}
        state = _version_state(ver)
        if state not in EDITABLE_VERSION_STATES:
            raise NonEditableVersionError(
                f"iOS App Store version '{config.version}' is in non-editable state '{state}'. "
                f"Refusing to mutate versions outside {sorted(EDITABLE_VERSION_STATES)}."
            )
        report.add("UNCHANGED", "appStoreVersion", config.version, f"state={state}")
        return str(ver["id"]), attrs

    if options.dry_run:
        report.add(
            "CREATE",
            "appStoreVersion",
            config.version,
            "would create IOS PREPARE_FOR_SUBMISSION version",
        )
        return None, {}

    attributes: dict[str, Any] = {
        "platform": "IOS",
        "versionString": config.version,
    }
    if config.copyright is not None:
        attributes["copyright"] = config.copyright

    created = client.post(
        "/v1/appStoreVersions",
        {
            "data": {
                "type": "appStoreVersions",
                "attributes": attributes,
                "relationships": {
                    "app": {"data": {"type": "apps", "id": app_id}},
                },
            }
        },
    )
    ver_data = created.get("data") or {}
    report.add("CREATE", "appStoreVersion", config.version, "created IOS version")
    return str(ver_data["id"]), ver_data.get("attributes") or attributes


def _sync_copyright(
    client: ASCClient,
    version_id: str | None,
    version_attrs: dict[str, Any],
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    if config.copyright is None:
        return
    if version_id is None:
        report.add("CREATE", "appStoreVersion.copyright", config.version, f"copyright={config.copyright!r}")
        return

    current = _norm_str(version_attrs.get("copyright"))
    if current == config.copyright:
        report.add("UNCHANGED", "appStoreVersion.copyright", config.version)
        return

    if not options.dry_run:
        client.patch(
            f"/v1/appStoreVersions/{version_id}",
            {
                "data": {
                    "type": "appStoreVersions",
                    "id": version_id,
                    "attributes": {"copyright": config.copyright},
                }
            },
        )
    report.add("UPDATE", "appStoreVersion.copyright", config.version, f"{current!r} -> {config.copyright!r}")


def _resolve_editable_app_info(client: ASCClient, app_id: str) -> dict[str, Any]:
    infos = client.get_all(f"/v1/apps/{app_id}/appInfos")
    if not infos:
        raise NonEditableAppInfoError(f"No appInfos resource found for app '{app_id}'.")

    for info in infos:
        if _app_info_state(info) in EDITABLE_APP_INFO_STATES:
            return info

    found_states = [_app_info_state(info) or "<unknown>" for info in infos]
    raise NonEditableAppInfoError(
        f"No editable appInfo found for app '{app_id}' (found states: {', '.join(found_states)}). "
        f"Refusing to mutate appInfo outside {sorted(EDITABLE_APP_INFO_STATES)}."
    )


def _sync_categories(
    client: ASCClient,
    app_info: dict[str, Any],
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    if config.categories is None:
        return

    app_info_id = str(app_info["id"])
    detail = client.get(
        f"/v1/appInfos/{app_info_id}",
        params={"include": "primaryCategory,secondaryCategory"},
    ) or {}
    rels = (detail.get("data") or {}).get("relationships") or app_info.get("relationships") or {}
    cur_primary = ((rels.get("primaryCategory") or {}).get("data") or {}).get("id")
    cur_secondary = ((rels.get("secondaryCategory") or {}).get("data") or {}).get("id")

    desired_primary = config.categories.primary
    desired_secondary = config.categories.secondary

    rel_updates: dict[str, Any] = {}
    changes: list[str] = []

    if cur_primary != desired_primary:
        rel_updates["primaryCategory"] = {"data": {"type": "appCategories", "id": desired_primary}}
        changes.append(f"primary: {cur_primary} -> {desired_primary}")

    if desired_secondary is not None and cur_secondary != desired_secondary:
        rel_updates["secondaryCategory"] = {"data": {"type": "appCategories", "id": desired_secondary}}
        changes.append(f"secondary: {cur_secondary} -> {desired_secondary}")

    if not rel_updates:
        report.add("UNCHANGED", "appInfo.categories", app_info_id)
        return

    if not options.dry_run:
        client.patch(
            f"/v1/appInfos/{app_info_id}",
            {
                "data": {
                    "type": "appInfos",
                    "id": app_info_id,
                    "relationships": rel_updates,
                }
            },
        )
    report.add("UPDATE", "appInfo.categories", app_info_id, ", ".join(changes))


def _sync_app_info_localizations(
    client: ASCClient,
    app_info_id: str,
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    existing = client.get_all(f"/v1/appInfos/{app_info_id}/appInfoLocalizations")
    by_locale = {
        str((item.get("attributes") or {}).get("locale")): item
        for item in existing
        if (item.get("attributes") or {}).get("locale")
    }

    for locale, loc in config.localizations.items():
        desired: dict[str, Any] = {"name": loc.name}
        if loc.subtitle is not None:
            desired["subtitle"] = loc.subtitle
        if loc.privacy_policy_url is not None:
            desired["privacyPolicyUrl"] = loc.privacy_policy_url

        if locale not in by_locale:
            if not options.dry_run:
                client.post(
                    "/v1/appInfoLocalizations",
                    {
                        "data": {
                            "type": "appInfoLocalizations",
                            "attributes": {"locale": locale, **desired},
                            "relationships": {
                                "appInfo": {"data": {"type": "appInfos", "id": app_info_id}},
                            },
                        }
                    },
                )
            report.add(
                "CREATE",
                "appInfoLocalization",
                locale,
                f"fields={','.join(sorted(desired))}",
            )
            continue

        cur_item = by_locale[locale]
        cur_id = str(cur_item["id"])
        diff = _changed_fields(cur_item.get("attributes") or {}, desired)
        if not diff:
            report.add("UNCHANGED", "appInfoLocalization", locale)
            continue

        if not options.dry_run:
            client.patch(
                f"/v1/appInfoLocalizations/{cur_id}",
                {
                    "data": {
                        "type": "appInfoLocalizations",
                        "id": cur_id,
                        "attributes": diff,
                    }
                },
            )
        report.add(
            "UPDATE",
            "appInfoLocalization",
            locale,
            f"fields={','.join(sorted(diff))}",
        )


def _sync_version_localizations(
    client: ASCClient,
    version_id: str | None,
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
) -> dict[str, str]:
    """Sync appStoreVersionLocalizations and return locale -> localization_id map."""
    by_locale: dict[str, dict[str, Any]] = {}
    if version_id is not None:
        existing = client.get_all(f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations")
        by_locale = {
            str((item.get("attributes") or {}).get("locale")): item
            for item in existing
            if (item.get("attributes") or {}).get("locale")
        }

    loc_ids: dict[str, str] = {}
    for locale, loc in config.localizations.items():
        desired: dict[str, Any] = {"description": loc.description}
        if loc.promotional_text is not None:
            desired["promotionalText"] = loc.promotional_text
        if loc.keywords is not None:
            desired["keywords"] = loc.keywords
        if loc.support_url is not None:
            desired["supportUrl"] = loc.support_url
        if loc.marketing_url is not None:
            desired["marketingUrl"] = loc.marketing_url

        if locale not in by_locale:
            if not options.dry_run and version_id is not None:
                created = client.post(
                    "/v1/appStoreVersionLocalizations",
                    {
                        "data": {
                            "type": "appStoreVersionLocalizations",
                            "attributes": {"locale": locale, **desired},
                            "relationships": {
                                "appStoreVersion": {
                                    "data": {"type": "appStoreVersions", "id": version_id}
                                },
                            },
                        }
                    },
                )
                created_id = (created.get("data") or {}).get("id")
                if created_id:
                    loc_ids[locale] = str(created_id)
            report.add(
                "CREATE",
                "appStoreVersionLocalization",
                locale,
                f"fields={','.join(sorted(desired))}",
            )
            continue

        cur_item = by_locale[locale]
        cur_id = str(cur_item["id"])
        loc_ids[locale] = cur_id
        diff = _changed_fields(cur_item.get("attributes") or {}, desired)
        if not diff:
            report.add("UNCHANGED", "appStoreVersionLocalization", locale)
            continue

        if not options.dry_run:
            client.patch(
                f"/v1/appStoreVersionLocalizations/{cur_id}",
                {
                    "data": {
                        "type": "appStoreVersionLocalizations",
                        "id": cur_id,
                        "attributes": diff,
                    }
                },
            )
        report.add(
            "UPDATE",
            "appStoreVersionLocalization",
            locale,
            f"fields={','.join(sorted(diff))}",
        )
    return loc_ids


def _sync_review_detail(
    client: ASCClient,
    version_id: str | None,
    review_detail: ReviewDetailConfig | None,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    if review_detail is None:
        return

    desired: dict[str, Any] = {
        "demoAccountRequired": review_detail.demo_account_required,
    }
    optional_map = {
        "contactFirstName": review_detail.contact_first_name,
        "contactLastName": review_detail.contact_last_name,
        "contactPhone": review_detail.contact_phone,
        "contactEmail": review_detail.contact_email,
        "demoAccountName": review_detail.demo_account_name,
        "demoAccountPassword": review_detail.demo_account_password,
        "notes": review_detail.notes,
    }
    for key, val in optional_map.items():
        if val is not None:
            desired[key] = val

    existing_resp = (
        client.get(f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail", allow_404=True)
        if version_id is not None
        else None
    )
    existing_data = (existing_resp or {}).get("data")

    if not existing_data:
        if not options.dry_run and version_id is not None:
            client.post(
                "/v1/appStoreReviewDetails",
                {
                    "data": {
                        "type": "appStoreReviewDetails",
                        "attributes": desired,
                        "relationships": {
                            "appStoreVersion": {
                                "data": {"type": "appStoreVersions", "id": version_id}
                            },
                        },
                    }
                },
            )
        report.add("CREATE", "appStoreReviewDetail", "reviewDetail", f"fields={','.join(sorted(desired))}")
        return

    detail_id = str(existing_data["id"])
    diff = _changed_fields(existing_data.get("attributes") or {}, desired)
    if not diff:
        report.add("UNCHANGED", "appStoreReviewDetail", detail_id)
        return

    if not options.dry_run:
        client.patch(
            f"/v1/appStoreReviewDetails/{detail_id}",
            {
                "data": {
                    "type": "appStoreReviewDetails",
                    "id": detail_id,
                    "attributes": diff,
                }
            },
        )
    report.add("UPDATE", "appStoreReviewDetail", detail_id, f"fields={','.join(sorted(diff))}")


def _format_territories_summary(territories: set[str]) -> str:
    sorted_codes = sorted(territories)
    if len(sorted_codes) <= 10:
        return ",".join(sorted_codes)
    return f"{len(sorted_codes)} territories"


def _sync_availability(
    client: ASCClient,
    app_id: str,
    availability: AvailabilityConfig | None,
    options: SyncOptions,
    report: SyncReport,
    live_guard: _LiveAppGuard,
) -> None:
    if availability is None:
        return
    if not options.sync_availability:
        report.add("SKIPPED", "appAvailability", app_id, "--sync-availability not enabled")
        return

    current = client.get(
        f"/v1/apps/{app_id}/appAvailabilityV2",
        allow_404=True,
    )
    has_existing = bool(current and isinstance(current.get("data"), dict))
    cur_new: bool | None = None
    cur_territories: set[str] = set()
    terr_to_record: dict[str, tuple[str, bool]] = {}
    if has_existing and current is not None:
        avail_data = current["data"]
        avail_id = avail_data.get("id")
        cur_new = (avail_data.get("attributes") or {}).get("availableInNewTerritories")
        if avail_id:
            included_records: list[dict[str, Any]] = []
            terr_items = client.get_all(
                f"/v2/appAvailabilities/{avail_id}/territoryAvailabilities",
                params={"limit": "200", "include": "territory"},
                included_out=included_records,
            )
            included_territories = {
                str(inc["id"]): inc
                for inc in included_records
                if inc.get("type") == "territories" and inc.get("id")
            }
            for item in terr_items:
                attrs = item.get("attributes") or {}
                is_available = bool(attrs.get("available"))
                ta_id = str(item.get("id") or "")
                rel_terr = ((item.get("relationships") or {}).get("territory") or {}).get("data") or {}
                tid = rel_terr.get("id")
                code: str | None = None
                if tid:
                    tid_str = str(tid)
                    inc_rec = included_territories.get(tid_str)
                    code = str(inc_rec["id"]) if inc_rec and inc_rec.get("id") else tid_str
                elif ta_id and ta_id in included_territories:
                    code = ta_id
                if code:
                    if ta_id:
                        terr_to_record[code] = (ta_id, is_available)
                    if is_available:
                        cur_territories.add(code)

    desired_territories = set(availability.territories)
    terr_summary = _format_territories_summary(desired_territories)

    if has_existing:
        to_enable = sorted(desired_territories - cur_territories)
        to_disable = sorted(cur_territories - desired_territories)
        new_terr_changed = (
            cur_new is not None and cur_new != availability.available_in_new_territories
        )

        if not to_enable and not to_disable and not new_terr_changed:
            report.add(
                "UNCHANGED",
                "appAvailability",
                app_id,
                f"territories={terr_summary}",
            )
            return

        if new_terr_changed:
            report.add_warning(
                f"appAvailability ({app_id}): App Store Connect API v2 has no PATCH endpoint "
                f"for availableInNewTerritories ({cur_new} -> {availability.available_in_new_territories}); "
                "update that toggle manually in App Store Connect → Pricing and Availability."
            )

        if not to_enable and not to_disable:
            report.add(
                "UNCHANGED",
                "appAvailability",
                app_id,
                f"territories={terr_summary} (availableInNewTerritories unchanged via API)",
            )
            return

        missing_codes = sorted(code for code in to_enable + to_disable if code not in terr_to_record)
        if missing_codes:
            raise ASCError(
                f"Unknown territory code(s) not found in appAvailabilityV2 for app '{app_id}': "
                f"{', '.join(missing_codes)}."
            )

        if not options.dry_run:
            live_guard.ensure_allowed("appAvailability", app_id)
            for code in to_enable:
                ta_id, _ = terr_to_record[code]
                client.patch(
                    f"/v1/territoryAvailabilities/{ta_id}",
                    {
                        "data": {
                            "type": "territoryAvailabilities",
                            "id": ta_id,
                            "attributes": {"available": True},
                        }
                    },
                )
            for code in to_disable:
                ta_id, _ = terr_to_record[code]
                client.patch(
                    f"/v1/territoryAvailabilities/{ta_id}",
                    {
                        "data": {
                            "type": "territoryAvailabilities",
                            "id": ta_id,
                            "attributes": {"available": False},
                        }
                    },
                )

        delta_parts: list[str] = []
        if to_enable:
            delta_parts.append(f"enable={_format_territories_summary(set(to_enable))}")
        if to_disable:
            delta_parts.append(f"disable={_format_territories_summary(set(to_disable))}")
        delta_parts.append(f"territories={terr_summary}")
        report.add(
            "UPDATE",
            "appAvailability",
            app_id,
            f"{LIVE_CHANGE_TAG} {', '.join(delta_parts)}",
        )
        return

    # Brand-new app with no appAvailabilityV2 yet: POST /v2/appAvailabilities requires
    # every App Store territory to be included with available=True/False.
    all_terrs = client.get_all("/v1/territories", params={"limit": "200"})
    all_codes = sorted({str(t["id"]) for t in all_terrs if isinstance(t, dict) and t.get("id")})
    if all_codes:
        unknown = sorted(desired_territories - set(all_codes))
        if unknown:
            raise ASCError(
                f"Unknown territory code(s) not returned by /v1/territories: {', '.join(unknown)}."
            )
    else:
        all_codes = sorted(desired_territories)

    if not options.dry_run:
        live_guard.ensure_allowed("appAvailability", app_id)
        inline_refs = []
        included_items = []
        for idx, territory in enumerate(all_codes):
            temp_id = f"${{territory-{idx}}}"
            inline_refs.append({"type": "territoryAvailabilities", "id": temp_id})
            included_items.append(
                {
                    "type": "territoryAvailabilities",
                    "id": temp_id,
                    "attributes": {"available": territory in desired_territories},
                    "relationships": {
                        "territory": {"data": {"type": "territories", "id": territory}},
                    },
                }
            )
        client.post(
            "/v2/appAvailabilities",
            {
                "data": {
                    "type": "appAvailabilities",
                    "attributes": {
                        "availableInNewTerritories": availability.available_in_new_territories,
                    },
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": app_id}},
                        "territoryAvailabilities": {"data": inline_refs},
                    },
                },
                "included": included_items,
            },
        )
    report.add(
        "CREATE",
        "appAvailability",
        app_id,
        f"{LIVE_CHANGE_TAG} newTerritories={availability.available_in_new_territories}, "
        f"territories={terr_summary}",
    )


def _find_price_point(
    points: list[dict[str, Any]],
    target_price: str,
    territory: str,
    resource_label: str,
) -> dict[str, Any]:
    for point in points:
        attrs = point.get("attributes") or {}
        if _prices_equal(attrs.get("customerPrice", ""), target_price):
            return point
    raise ASCError(
        f"No matching {resource_label} price point found for territory '{territory}' "
        f"and price '{target_price}'."
    )


def _sync_app_pricing(
    client: ASCClient,
    app_id: str,
    pricing: AppPricingConfig | None,
    options: SyncOptions,
    report: SyncReport,
    live_guard: _LiveAppGuard,
) -> None:
    if pricing is None:
        return
    if not options.sync_pricing:
        report.add("SKIPPED", "appPriceSchedule", app_id, "--sync-pricing not enabled")
        return

    points = client.get_all(
        f"/v1/apps/{app_id}/appPricePoints",
        params={"filter[territory]": pricing.base_territory, "limit": 200},
    )
    target_point = _find_price_point(points, pricing.base_price, pricing.base_territory, "app")
    target_point_id = str(target_point["id"])

    current_sched = client.get(
        f"/v1/apps/{app_id}/appPriceSchedule",
        params={"include": "baseTerritory,manualPrices"},
        allow_404=True,
    )
    cur_base_territory: str | None = None
    cur_point_ids: set[str] = set()
    if current_sched and isinstance(current_sched.get("data"), dict):
        rels = current_sched["data"].get("relationships") or {}
        cur_base_territory = ((rels.get("baseTerritory") or {}).get("data") or {}).get("id")
        for inc in current_sched.get("included") or []:
            if inc.get("type") == "appPrices":
                pp = ((inc.get("relationships") or {}).get("appPricePoint") or {}).get("data") or {}
                if pp.get("id"):
                    cur_point_ids.add(str(pp["id"]))

    if cur_base_territory == pricing.base_territory and target_point_id in cur_point_ids:
        report.add(
            "UNCHANGED",
            "appPriceSchedule",
            app_id,
            f"{pricing.base_territory} {pricing.base_price}",
        )
        return

    action: DiffAction = "CREATE" if current_sched is None else "UPDATE"
    if not options.dry_run:
        live_guard.ensure_allowed("appPriceSchedule", app_id)
        inline_id = "${price-0}"
        client.post(
            "/v1/appPriceSchedules",
            {
                "data": {
                    "type": "appPriceSchedules",
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": app_id}},
                        "baseTerritory": {"data": {"type": "territories", "id": pricing.base_territory}},
                        "manualPrices": {"data": [{"type": "appPrices", "id": inline_id}]},
                    },
                },
                "included": [
                    {
                        "type": "appPrices",
                        "id": inline_id,
                        "attributes": {"startDate": None},
                        "relationships": {
                            "appPricePoint": {
                                "data": {"type": "appPricePoints", "id": target_point_id}
                            },
                        },
                    }
                ],
            },
        )
    report.add(
        action,
        "appPriceSchedule",
        app_id,
        f"{LIVE_CHANGE_TAG} {pricing.base_territory} {pricing.base_price} (pricePoint={target_point_id})",
    )


def _sync_iap_localizations(
    client: ASCClient,
    iap_id: str | None,
    iap: InAppPurchaseConfig,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    by_locale: dict[str, dict[str, Any]] = {}
    if iap_id is not None:
        existing = client.get_all(f"/v2/inAppPurchases/{iap_id}/inAppPurchaseLocalizations")
        by_locale = {
            str((item.get("attributes") or {}).get("locale")): item
            for item in existing
            if (item.get("attributes") or {}).get("locale")
        }

    for locale, loc in iap.localizations.items():
        target = f"{iap.product_id}:{locale}"
        desired = {"name": loc.name, "description": loc.description}
        if locale not in by_locale:
            if not options.dry_run and iap_id is not None:
                client.post(
                    "/v1/inAppPurchaseLocalizations",
                    {
                        "data": {
                            "type": "inAppPurchaseLocalizations",
                            "attributes": {"locale": locale, **desired},
                            "relationships": {
                                "inAppPurchaseV2": {
                                    "data": {"type": "inAppPurchases", "id": iap_id}
                                },
                            },
                        }
                    },
                )
            report.add("CREATE", "inAppPurchaseLocalization", target, f"name={loc.name!r}")
            continue

        cur = by_locale[locale]
        cur_id = str(cur["id"])
        diff = _changed_fields(cur.get("attributes") or {}, desired)
        if not diff:
            report.add("UNCHANGED", "inAppPurchaseLocalization", target)
            continue

        if not options.dry_run:
            client.patch(
                f"/v1/inAppPurchaseLocalizations/{cur_id}",
                {
                    "data": {
                        "type": "inAppPurchaseLocalizations",
                        "id": cur_id,
                        "attributes": diff,
                    }
                },
            )
        report.add("UPDATE", "inAppPurchaseLocalization", target, f"fields={','.join(sorted(diff))}")


def _sync_iap_pricing(
    client: ASCClient,
    iap_id: str | None,
    iap: InAppPurchaseConfig,
    options: SyncOptions,
    report: SyncReport,
    live_guard: _LiveAppGuard,
) -> None:
    if iap.pricing is None:
        return

    if iap_id is None:
        report.add(
            "CREATE",
            "inAppPurchasePriceSchedule",
            iap.product_id,
            f"{LIVE_CHANGE_TAG} {iap.pricing.base_territory} {iap.pricing.price} (pending IAP creation)",
        )
        return

    points = client.get_all(
        f"/v2/inAppPurchases/{iap_id}/pricePoints",
        params={"filter[territory]": iap.pricing.base_territory, "limit": 200},
    )
    target_point = _find_price_point(
        points, iap.pricing.price, iap.pricing.base_territory, f"IAP '{iap.product_id}'"
    )
    target_point_id = str(target_point["id"])

    current_sched = client.get(
        f"/v1/inAppPurchasePriceSchedules/{iap_id}",
        params={"include": "baseTerritory,manualPrices"},
        allow_404=True,
    )
    cur_base: str | None = None
    cur_points: set[str] = set()
    if current_sched and isinstance(current_sched.get("data"), dict):
        rels = current_sched["data"].get("relationships") or {}
        cur_base = ((rels.get("baseTerritory") or {}).get("data") or {}).get("id")
        for inc in current_sched.get("included") or []:
            if inc.get("type") == "inAppPurchasePrices":
                pp = (
                    (inc.get("relationships") or {}).get("inAppPurchasePricePoint") or {}
                ).get("data") or {}
                if pp.get("id"):
                    cur_points.add(str(pp["id"]))

    if cur_base == iap.pricing.base_territory and target_point_id in cur_points:
        report.add(
            "UNCHANGED",
            "inAppPurchasePriceSchedule",
            iap.product_id,
            f"{iap.pricing.base_territory} {iap.pricing.price}",
        )
        return

    action: DiffAction = "CREATE" if current_sched is None else "UPDATE"
    if not options.dry_run:
        live_guard.ensure_allowed("inAppPurchasePriceSchedule", iap.product_id)
        inline_id = "${iap-price-0}"
        client.post(
            "/v1/inAppPurchasePriceSchedules",
            {
                "data": {
                    "type": "inAppPurchasePriceSchedules",
                    "relationships": {
                        "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                        "baseTerritory": {
                            "data": {"type": "territories", "id": iap.pricing.base_territory}
                        },
                        "manualPrices": {
                            "data": [{"type": "inAppPurchasePrices", "id": inline_id}]
                        },
                    },
                },
                "included": [
                    {
                        "type": "inAppPurchasePrices",
                        "id": inline_id,
                        "attributes": {"startDate": None},
                        "relationships": {
                            "inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}},
                            "inAppPurchasePricePoint": {
                                "data": {
                                    "type": "inAppPurchasePricePoints",
                                    "id": target_point_id,
                                }
                            },
                        },
                    }
                ],
            },
        )
    report.add(
        action,
        "inAppPurchasePriceSchedule",
        iap.product_id,
        f"{LIVE_CHANGE_TAG} {iap.pricing.base_territory} {iap.pricing.price} (pricePoint={target_point_id})",
    )


def _sync_in_app_purchases(
    client: ASCClient,
    app_id: str,
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
    live_guard: _LiveAppGuard,
) -> dict[str, str]:
    """Sync inAppPurchases v2 and return productId -> iap_id map."""
    if not config.in_app_purchases:
        return {}
    if not options.sync_iap:
        report.add(
            "SKIPPED",
            "inAppPurchases",
            f"{len(config.in_app_purchases)} item(s)",
            "--sync-iap not enabled",
        )
        return {}

    existing_iaps = client.get_all(f"/v1/apps/{app_id}/inAppPurchasesV2")
    by_product_id = {
        str((item.get("attributes") or {}).get("productId")): item
        for item in existing_iaps
        if (item.get("attributes") or {}).get("productId")
    }

    iap_ids: dict[str, str] = {}
    for iap in config.in_app_purchases:
        if iap.product_id not in by_product_id:
            iap_id: str | None = None
            if not options.dry_run:
                if iap.pricing is not None:
                    live_guard.ensure_allowed("inAppPurchasePriceSchedule", iap.product_id)
                attrs: dict[str, Any] = {
                    "name": iap.reference_name,
                    "productId": iap.product_id,
                    "inAppPurchaseType": iap.type,
                }
                if iap.review_note is not None:
                    attrs["reviewNote"] = iap.review_note
                created = client.post(
                    "/v2/inAppPurchases",
                    {
                        "data": {
                            "type": "inAppPurchases",
                            "attributes": attrs,
                            "relationships": {
                                "app": {"data": {"type": "apps", "id": app_id}},
                            },
                        }
                    },
                )
                created_id = (created.get("data") or {}).get("id")
                if created_id:
                    iap_id = str(created_id)
                    iap_ids[iap.product_id] = iap_id
            report.add(
                "CREATE",
                "inAppPurchase",
                iap.product_id,
                f"type={iap.type}, referenceName={iap.reference_name!r}",
            )
            _sync_iap_localizations(client, iap_id, iap, options, report)
            _sync_iap_pricing(client, iap_id, iap, options, report, live_guard)
            continue

        cur = by_product_id[iap.product_id]
        cur_attrs = cur.get("attributes") or {}
        existing_type = cur_attrs.get("inAppPurchaseType")
        if existing_type and str(existing_type) != iap.type:
            raise ASCError(
                f"In-app purchase '{iap.product_id}' already exists in App Store Connect with "
                f"inAppPurchaseType='{existing_type}', which conflicts with configured "
                f"type='{iap.type}'. Apple does not permit changing an IAP's type after creation."
            )

        iap_id = str(cur["id"])
        iap_ids[iap.product_id] = iap_id
        desired_attrs: dict[str, Any] = {"name": iap.reference_name}
        if iap.review_note is not None:
            desired_attrs["reviewNote"] = iap.review_note
        diff = _changed_fields(cur_attrs, desired_attrs)
        if not diff:
            report.add("UNCHANGED", "inAppPurchase", iap.product_id)
        else:
            if not options.dry_run:
                client.patch(
                    f"/v2/inAppPurchases/{iap_id}",
                    {
                        "data": {
                            "type": "inAppPurchases",
                            "id": iap_id,
                            "attributes": diff,
                        }
                    },
                )
            report.add("UPDATE", "inAppPurchase", iap.product_id, f"fields={','.join(sorted(diff))}")

        _sync_iap_localizations(client, iap_id, iap, options, report)
        _sync_iap_pricing(client, iap_id, iap, options, report, live_guard)

    return iap_ids


def _preflight_live_guard(
    live_guard: _LiveAppGuard,
    config: MetadataConfig,
    options: SyncOptions,
) -> None:
    """Fail fast before any draft writes when live-affecting sync options target a live app."""
    if options.dry_run or options.allow_live_changes:
        return
    requested: list[str] = []
    if options.sync_availability and config.availability is not None:
        requested.append("availability")
    if options.sync_pricing and config.app_pricing is not None:
        requested.append("appPricing")
    if options.sync_iap and any(iap.pricing is not None for iap in config.in_app_purchases):
        requested.append("inAppPurchasePricing")
    if requested:
        live_guard.ensure_allowed("live-app-settings", ", ".join(requested))


def sync_metadata(
    client: ASCClient,
    config: MetadataConfig,
    options: SyncOptions | None = None,
    *,
    allow_live_changes: bool | None = None,
) -> SyncReport:
    """Idempotently sync validated MetadataConfig against App Store Connect."""
    opts = options or SyncOptions()
    if allow_live_changes is not None:
        opts = replace(opts, allow_live_changes=allow_live_changes)

    report = SyncReport(
        bundle_id=config.bundle_id,
        version=config.version,
        dry_run=opts.dry_run,
    )

    try:
        app = _resolve_app(client, config.bundle_id)
        app_id = str(app["id"])
        live_guard = _LiveAppGuard(client, app_id, opts)
        _preflight_live_guard(live_guard, config, opts)

        version_id, version_attrs = _resolve_or_create_version(client, app_id, config, opts, report)
        _sync_copyright(client, version_id, version_attrs, config, opts, report)

        app_info = _resolve_editable_app_info(client, app_id)
        app_info_id = str(app_info["id"])
        _sync_categories(client, app_info, config, opts, report)
        _sync_app_info_localizations(client, app_info_id, config, opts, report)

        version_loc_ids = _sync_version_localizations(client, version_id, config, opts, report)
        _sync_review_detail(client, version_id, config.review_detail, opts, report)
        _sync_availability(client, app_id, config.availability, opts, report, live_guard)
        _sync_app_pricing(client, app_id, config.app_pricing, opts, report, live_guard)
        iap_ids = _sync_in_app_purchases(client, app_id, config, opts, report, live_guard)

        _sync_screenshots_phase(client, version_loc_ids, iap_ids, config, opts, report)
    except Exception as exc:
        exc.partial_report = report  # type: ignore[attr-defined]
        raise

    return report


def _sync_screenshots_phase(
    client: ASCClient,
    version_loc_ids: dict[str, str],
    iap_ids: dict[str, str],
    config: MetadataConfig,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    has_iap_review_shots = any(iap.review_screenshot for iap in config.in_app_purchases)
    configured_dir = options.screenshots_dir
    shots_base_dir: Path | None = None
    if configured_dir is None and config.screenshots and config.screenshots.directory:
        metadata_base = config.source_path.parent if config.source_path is not None else Path.cwd()
        rel = Path(config.screenshots.directory)
        configured_dir = rel if rel.is_absolute() else (metadata_base / rel)
        shots_base_dir = metadata_base

    if not options.sync_screenshots:
        if configured_dir is not None or has_iap_review_shots:
            report.add("SKIPPED", "screenshots", config.version, "--sync-screenshots not enabled")
        return

    from ios_appstore_sync.screenshots_sync import (
        sync_app_screenshots,
        sync_iap_review_screenshots,
    )

    if configured_dir is not None:
        display_type = (
            options.screenshot_display_type
            or (config.screenshots.display_type if config.screenshots else None)
            or "APP_IPHONE_69"
        )
        sync_app_screenshots(
            client=client,
            version_loc_ids=version_loc_ids,
            screenshots_dir=configured_dir,
            display_type=display_type,
            dry_run=options.dry_run,
            report=report,
            base_dir=shots_base_dir,
        )

    if has_iap_review_shots:
        sync_iap_review_screenshots(
            client=client,
            iap_ids=iap_ids,
            config=config,
            dry_run=options.dry_run,
            report=report,
        )
