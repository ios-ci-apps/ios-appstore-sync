"""App Store Connect screenshot and IAP review screenshot upload helpers."""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

from ios_appstore_sync.asc_client import ASCClient, ASCError

if TYPE_CHECKING:
    from ios_appstore_sync.schema import MetadataConfig
    from ios_appstore_sync.sync import SyncReport


MAX_SCREENSHOTS_PER_SET = 10
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_TIMEOUT = 120.0

# Allowed pixel dimensions (portrait or landscape) per Apple display type
ALLOWED_DISPLAY_DIMENSIONS: dict[str, set[tuple[int, int]]] = {
    "APP_IPHONE_69": {
        (1320, 2868),
        (2868, 1320),
        (1290, 2796),
        (2796, 1290),
    },
    "APP_IPHONE_67": {
        (1290, 2796),
        (2796, 1290),
        (1320, 2868),
        (2868, 1320),
    },
    "APP_IPHONE_65": {
        (1284, 2778),
        (2778, 1284),
        (1242, 2688),
        (2688, 1242),
    },
    "APP_IPAD_PRO_3GEN_129": {
        (2048, 2732),
        (2732, 2048),
    },
}


class ScreenshotValidationError(ASCError):
    """Raised when a local PNG violates Apple App Store screenshot requirements."""


@dataclass(frozen=True)
class LocalScreenshot:
    path: Path
    file_name: str
    file_size: int
    md5: str
    width: int
    height: int
    mode: str


def _ensure_within_base(path: Path, base_dir: Path, *, label: str) -> Path:
    """Resolve path and verify it stays strictly within base_dir (blocks '..' and escaping symlinks)."""
    base_resolved = base_dir.resolve()
    target_resolved = path.resolve()
    if not target_resolved.is_relative_to(base_resolved):
        raise ScreenshotValidationError(
            f"{label} path '{path}' resolves to '{target_resolved}', which escapes "
            f"allowed base directory '{base_resolved}'."
        )
    return target_resolved


def _unknown_display_type_warning(display_type: str) -> str:
    known = ", ".join(sorted(ALLOWED_DISPLAY_DIMENSIONS))
    return (
        f"Unknown screenshotDisplayType '{display_type}'; skipping pixel dimension validation "
        f"(known types: {known})."
    )


def inspect_png(
    path: str | Path,
    *,
    display_type: str | None = None,
    require_opaque_rgb: bool = True,
    warnings_out: list[str] | None = None,
    warn_unknown_display_type: bool = True,
) -> LocalScreenshot:
    """Validate a local PNG screenshot and compute its MD5 checksum and size."""
    file_path = Path(path)
    if not file_path.is_file():
        raise ScreenshotValidationError(f"Screenshot file does not exist: '{file_path}'")

    raw_bytes = file_path.read_bytes()
    try:
        with Image.open(file_path) as img:
            width, height = img.size
            mode = img.mode
            img_format = img.format
    except (OSError, UnidentifiedImageError) as exc:
        raise ScreenshotValidationError(f"Invalid image file '{file_path}': {exc}") from exc

    if img_format != "PNG":
        raise ScreenshotValidationError(
            f"Screenshot '{file_path.name}' must be a PNG image (got {img_format!r})."
        )

    if require_opaque_rgb and mode != "RGB":
        raise ScreenshotValidationError(
            f"Screenshot '{file_path.name}' must be opaque RGB with no alpha channel (got mode={mode!r})."
        )

    if display_type is not None:
        allowed = ALLOWED_DISPLAY_DIMENSIONS.get(display_type)
        if allowed is None:
            warning_msg = _unknown_display_type_warning(display_type)
            if warnings_out is not None and warning_msg not in warnings_out:
                warnings_out.append(warning_msg)
            if warn_unknown_display_type:
                print(f"WARN: {warning_msg}", file=sys.stderr)
        elif (width, height) not in allowed:
            allowed_str = ", ".join(f"{w}x{h}" for w, h in sorted(allowed))
            raise ScreenshotValidationError(
                f"Screenshot '{file_path.name}' has dimensions {width}x{height}, which is invalid "
                f"for {display_type} (expected one of: {allowed_str})."
            )

    md5_hex = hashlib.md5(raw_bytes).hexdigest()
    return LocalScreenshot(
        path=file_path,
        file_name=file_path.name,
        file_size=len(raw_bytes),
        md5=md5_hex,
        width=width,
        height=height,
        mode=mode,
    )


def _execute_upload_operations(
    client: ASCClient,
    data: bytes,
    upload_operations: list[dict[str, Any]],
) -> None:
    for op in upload_operations:
        method = str(op.get("method", "PUT"))
        url = str(op["url"])
        offset = int(op.get("offset", 0))
        length = int(op.get("length", len(data) - offset))
        headers: dict[str, str] = {}
        for header_obj in op.get("requestHeaders") or []:
            if isinstance(header_obj, dict) and header_obj.get("name"):
                headers[str(header_obj["name"])] = str(header_obj.get("value", ""))
        chunk = data[offset : offset + length]
        client.upload_chunk(method=method, url=url, headers=headers, data=chunk)


def _wait_for_asset_delivery(
    client: ASCClient,
    endpoint: str,
    *,
    file_label: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Poll an ASC screenshot resource until assetDeliveryState reaches COMPLETE."""
    deadline = monotonic_fn() + poll_timeout
    last_state = "UNKNOWN"
    while True:
        resp = client.get(endpoint) or {}
        attrs = (resp.get("data") or {}).get("attributes") or {}
        delivery = attrs.get("assetDeliveryState") or {}
        state = str(delivery.get("state") or "")
        if state:
            last_state = state

        if state == "COMPLETE":
            return

        if state == "FAILED":
            errors = delivery.get("errors") or []
            err_msgs: list[str] = []
            for err in errors:
                if isinstance(err, dict):
                    code = err.get("code", "")
                    desc = err.get("description") or err.get("detail") or ""
                    msg = " - ".join(p for p in (code, desc) if p)
                    if msg:
                        err_msgs.append(msg)
                elif err:
                    err_msgs.append(str(err))
            detail = "; ".join(err_msgs) if err_msgs else "no error details returned by ASC"
            raise ASCError(
                f"ASC assetDeliveryState FAILED for '{file_label}' ({endpoint}): {detail}"
            )

        if monotonic_fn() >= deadline:
            raise ASCError(
                f"Timed out after {poll_timeout:.1f}s waiting for assetDeliveryState=COMPLETE "
                f"for '{file_label}' ({endpoint}); last state was {last_state!r}."
            )
        sleep_fn(poll_interval)


def sync_app_screenshots(
    *,
    client: ASCClient,
    version_loc_ids: dict[str, str],
    screenshots_dir: Path,
    display_type: str,
    dry_run: bool,
    report: SyncReport,
    base_dir: Path | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Idempotently sync framed screenshots per locale to ASC appScreenshotSets."""
    if base_dir is not None:
        shots_root = _ensure_within_base(screenshots_dir, base_dir, label="screenshots.directory")
    else:
        shots_root = screenshots_dir.resolve()

    if not shots_root.is_dir():
        raise ScreenshotValidationError(
            f"Screenshots directory does not exist: '{screenshots_dir}'"
        )

    if display_type not in ALLOWED_DISPLAY_DIMENSIONS:
        warning_msg = _unknown_display_type_warning(display_type)
        report.add_warning(warning_msg)
        print(f"WARN: {warning_msg}", file=sys.stderr)

    locale_dirs = sorted(
        p for p in shots_root.iterdir() if p.is_dir() and p.name != "web-screenshots"
    )
    for locale_dir in locale_dirs:
        locale = locale_dir.name
        safe_locale_dir = _ensure_within_base(
            locale_dir, shots_root, label=f"screenshot locale '{locale}'"
        )
        png_paths = sorted(p for p in safe_locale_dir.glob("*.png") if p.is_file())
        if not png_paths:
            continue

        safe_png_paths = [
            _ensure_within_base(p, shots_root, label=f"screenshot '{locale}/{p.name}'")
            for p in png_paths
        ]
        if len(safe_png_paths) > MAX_SCREENSHOTS_PER_SET:
            raise ScreenshotValidationError(
                f"Locale '{locale}' has {len(safe_png_paths)} screenshots, exceeding Apple's "
                f"maximum of {MAX_SCREENSHOTS_PER_SET} screenshots per display set."
            )

        local_shots = [
            inspect_png(
                p,
                display_type=display_type,
                require_opaque_rgb=True,
                warn_unknown_display_type=False,
            )
            for p in safe_png_paths
        ]

        loc_id = version_loc_ids.get(locale)
        if loc_id is None:
            if dry_run:
                for shot in local_shots:
                    report.add(
                        "CREATE",
                        "appScreenshot",
                        f"{locale}/{shot.file_name}",
                        f"{display_type} md5={shot.md5[:8]} (pending localization)",
                    )
                continue
            report.add(
                "SKIPPED",
                "appScreenshotSet",
                locale,
                "locale not present in appStoreVersionLocalizations",
            )
            continue

        sets = client.get_all(f"/v1/appStoreVersionLocalizations/{loc_id}/appScreenshotSets")
        target_set = next(
            (
                s
                for s in sets
                if (s.get("attributes") or {}).get("screenshotDisplayType") == display_type
            ),
            None,
        )

        existing_shots: list[dict[str, Any]] = []
        set_id: str | None = None
        if target_set is not None:
            set_id = str(target_set["id"])
            existing_shots = client.get_all(f"/v1/appScreenshotSets/{set_id}/appScreenshots")

        existing_md5s = [
            str((s.get("attributes") or {}).get("sourceFileChecksum", "")).lower()
            for s in existing_shots
        ]
        desired_md5s = [s.md5.lower() for s in local_shots]

        if existing_md5s == desired_md5s:
            for shot in local_shots:
                report.add(
                    "UNCHANGED",
                    "appScreenshot",
                    f"{locale}/{shot.file_name}",
                    f"{display_type} md5={shot.md5[:8]}",
                )
            continue

        if dry_run:
            action = "CREATE" if not existing_shots else "UPDATE"
            for shot in local_shots:
                report.add(
                    action,
                    "appScreenshot",
                    f"{locale}/{shot.file_name}",
                    f"{display_type} {shot.width}x{shot.height} md5={shot.md5[:8]}",
                )
            continue

        # Create screenshot set if missing
        if set_id is None:
            created_set = client.post(
                "/v1/appScreenshotSets",
                {
                    "data": {
                        "type": "appScreenshotSets",
                        "attributes": {"screenshotDisplayType": display_type},
                        "relationships": {
                            "appStoreVersionLocalization": {
                                "data": {
                                    "type": "appStoreVersionLocalizations",
                                    "id": loc_id,
                                }
                            }
                        },
                    }
                },
            )
            set_id = str((created_set.get("data") or {})["id"])
            report.add("CREATE", "appScreenshotSet", f"{locale}:{display_type}")

        # Safe upload-before-delete: only pre-delete the minimum excess needed to stay within
        # ASC's 10-screenshot-per-set cap before uploading new screenshots.
        existing_ids = [str(s["id"]) for s in existing_shots]
        excess = max(0, len(existing_ids) + len(local_shots) - MAX_SCREENSHOTS_PER_SET)
        if excess > 0:
            pre_deleted_ids = existing_ids[len(existing_ids) - excess :]
            remaining_existing_ids = existing_ids[: len(existing_ids) - excess]
            for old_id in pre_deleted_ids:
                client.delete(f"/v1/appScreenshots/{old_id}")
        else:
            pre_deleted_ids = []
            remaining_existing_ids = list(existing_ids)

        newly_uploaded_ids: list[str] = []
        action = "CREATE" if not existing_shots else "UPDATE"
        for shot in local_shots:
            file_label = f"{locale}/{shot.file_name}"
            reserved_id: str | None = None
            try:
                data = shot.path.read_bytes()
                reserved = client.post(
                    "/v1/appScreenshots",
                    {
                        "data": {
                            "type": "appScreenshots",
                            "attributes": {
                                "fileName": shot.file_name,
                                "fileSize": shot.file_size,
                            },
                            "relationships": {
                                "appScreenshotSet": {
                                    "data": {"type": "appScreenshotSets", "id": set_id}
                                }
                            },
                        }
                    },
                )
                shot_data = reserved.get("data") or {}
                reserved_id = str(shot_data["id"])
                upload_ops = (shot_data.get("attributes") or {}).get("uploadOperations") or []
                _execute_upload_operations(client, data, upload_ops)
                client.patch(
                    f"/v1/appScreenshots/{reserved_id}",
                    {
                        "data": {
                            "type": "appScreenshots",
                            "id": reserved_id,
                            "attributes": {
                                "uploaded": True,
                                "sourceFileChecksum": shot.md5,
                            },
                        }
                    },
                )
                _wait_for_asset_delivery(
                    client,
                    f"/v1/appScreenshots/{reserved_id}",
                    file_label=file_label,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                )
            except Exception as exc:
                if reserved_id is not None and reserved_id not in newly_uploaded_ids:
                    newly_uploaded_ids.append(reserved_id)
                pre_del_note = f", pre_deleted_ids={pre_deleted_ids}" if pre_deleted_ids else ""
                raise ASCError(
                    f"Screenshot upload/processing failed mid-sync: {exc}. "
                    f"Recovery state: screenshot_set_id={set_id!r}, "
                    f"existing_ids={remaining_existing_ids}, "
                    f"newly_uploaded_ids={newly_uploaded_ids}, "
                    f"failed_file={file_label!r}{pre_del_note}."
                ) from exc

            newly_uploaded_ids.append(reserved_id)
            report.add(
                action,
                "appScreenshot",
                file_label,
                f"{display_type} {shot.width}x{shot.height} md5={shot.md5[:8]}",
            )

        # Put new screenshots first in exact desired order, then delete remaining old ones.
        ordered_ids = newly_uploaded_ids + remaining_existing_ids
        if len(ordered_ids) > 1:
            client.patch(
                f"/v1/appScreenshotSets/{set_id}/relationships/appScreenshots",
                {
                    "data": [
                        {"type": "appScreenshots", "id": sid} for sid in ordered_ids
                    ]
                },
            )

        for old_id in remaining_existing_ids:
            client.delete(f"/v1/appScreenshots/{old_id}")


def sync_iap_review_screenshots(
    *,
    client: ASCClient,
    iap_ids: dict[str, str],
    config: MetadataConfig,
    dry_run: bool,
    report: SyncReport,
    base_dir: Path | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Idempotently upload reviewScreenshot PNGs for configured in-app purchases."""
    resolved_base = (
        base_dir
        if base_dir is not None
        else (config.source_path.parent if config.source_path is not None else Path.cwd())
    ).resolve()

    for iap in config.in_app_purchases:
        if not iap.review_screenshot:
            continue

        raw_shot_path = Path(iap.review_screenshot)
        shot_path = raw_shot_path if raw_shot_path.is_absolute() else (resolved_base / raw_shot_path)
        safe_shot_path = _ensure_within_base(
            shot_path,
            resolved_base,
            label=f"IAP '{iap.product_id}' reviewScreenshot",
        )
        local_shot = inspect_png(safe_shot_path, display_type=None, require_opaque_rgb=True)

        iap_id = iap_ids.get(iap.product_id)
        if iap_id is None:
            if dry_run:
                report.add(
                    "CREATE",
                    "inAppPurchaseReviewScreenshot",
                    iap.product_id,
                    f"{local_shot.file_name} md5={local_shot.md5[:8]} (pending IAP creation)",
                )
            else:
                report.add(
                    "SKIPPED",
                    "inAppPurchaseReviewScreenshot",
                    iap.product_id,
                    "IAP ID unavailable (enable --sync-iap)",
                )
            continue

        existing_resp = client.get(
            f"/v2/inAppPurchases/{iap_id}/appStoreReviewScreenshot",
            allow_404=True,
        )
        existing_data = (existing_resp or {}).get("data")
        existing_md5 = (
            str((existing_data.get("attributes") or {}).get("sourceFileChecksum", "")).lower()
            if isinstance(existing_data, dict)
            else ""
        )

        if existing_md5 == local_shot.md5.lower():
            report.add(
                "UNCHANGED",
                "inAppPurchaseReviewScreenshot",
                iap.product_id,
                f"{local_shot.file_name} md5={local_shot.md5[:8]}",
            )
            continue

        action = "CREATE" if not existing_data else "UPDATE"
        if dry_run:
            report.add(
                action,
                "inAppPurchaseReviewScreenshot",
                iap.product_id,
                f"{local_shot.file_name} {local_shot.width}x{local_shot.height} md5={local_shot.md5[:8]}",
            )
            continue

        if isinstance(existing_data, dict) and existing_data.get("id"):
            client.delete(f"/v1/inAppPurchaseAppStoreReviewScreenshots/{existing_data['id']}")

        data = local_shot.path.read_bytes()
        reserved = client.post(
            "/v1/inAppPurchaseAppStoreReviewScreenshots",
            {
                "data": {
                    "type": "inAppPurchaseAppStoreReviewScreenshots",
                    "attributes": {
                        "fileName": local_shot.file_name,
                        "fileSize": local_shot.file_size,
                    },
                    "relationships": {
                        "inAppPurchaseV2": {
                            "data": {"type": "inAppPurchases", "id": iap_id}
                        }
                    },
                }
            },
        )
        shot_data = reserved.get("data") or {}
        shot_id = str(shot_data["id"])
        upload_ops = (shot_data.get("attributes") or {}).get("uploadOperations") or []
        _execute_upload_operations(client, data, upload_ops)
        client.patch(
            f"/v1/inAppPurchaseAppStoreReviewScreenshots/{shot_id}",
            {
                "data": {
                    "type": "inAppPurchaseAppStoreReviewScreenshots",
                    "id": shot_id,
                    "attributes": {
                        "uploaded": True,
                        "sourceFileChecksum": local_shot.md5,
                    },
                }
            },
        )
        _wait_for_asset_delivery(
            client,
            f"/v1/inAppPurchaseAppStoreReviewScreenshots/{shot_id}",
            file_label=f"{iap.product_id}/{local_shot.file_name}",
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        report.add(
            action,
            "inAppPurchaseReviewScreenshot",
            iap.product_id,
            f"{local_shot.file_name} {local_shot.width}x{local_shot.height} md5={local_shot.md5[:8]}",
        )
