"""App Store Connect screenshot and IAP review screenshot upload helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

from ios_appstore_sync.asc_client import ASCClient, ASCError

if TYPE_CHECKING:
    from ios_appstore_sync.schema import MetadataConfig
    from ios_appstore_sync.sync import SyncReport


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


def inspect_png(
    path: str | Path,
    *,
    display_type: str | None = None,
    require_opaque_rgb: bool = True,
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
        if allowed is not None and (width, height) not in allowed:
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


def sync_app_screenshots(
    *,
    client: ASCClient,
    version_loc_ids: dict[str, str],
    screenshots_dir: Path,
    display_type: str,
    dry_run: bool,
    report: SyncReport,
) -> None:
    """Idempotently sync framed screenshots per locale to ASC appScreenshotSets."""
    if not screenshots_dir.is_dir():
        raise ScreenshotValidationError(
            f"Screenshots directory does not exist: '{screenshots_dir}'"
        )

    locale_dirs = sorted(
        p for p in screenshots_dir.iterdir() if p.is_dir() and p.name != "web-screenshots"
    )
    for locale_dir in locale_dirs:
        locale = locale_dir.name
        png_paths = sorted(p for p in locale_dir.glob("*.png") if p.is_file())
        if not png_paths:
            continue

        local_shots = [
            inspect_png(p, display_type=display_type, require_opaque_rgb=True)
            for p in png_paths
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

        # Delete stale screenshots before uploading updated set
        for old_shot in existing_shots:
            client.delete(f"/v1/appScreenshots/{old_shot['id']}")

        uploaded_ids: list[str] = []
        action = "CREATE" if not existing_shots else "UPDATE"
        for shot in local_shots:
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
            shot_id = str(shot_data["id"])
            upload_ops = (shot_data.get("attributes") or {}).get("uploadOperations") or []
            _execute_upload_operations(client, data, upload_ops)
            client.patch(
                f"/v1/appScreenshots/{shot_id}",
                {
                    "data": {
                        "type": "appScreenshots",
                        "id": shot_id,
                        "attributes": {
                            "uploaded": True,
                            "sourceFileChecksum": shot.md5,
                        },
                    }
                },
            )
            uploaded_ids.append(shot_id)
            report.add(
                action,
                "appScreenshot",
                f"{locale}/{shot.file_name}",
                f"{display_type} {shot.width}x{shot.height} md5={shot.md5[:8]}",
            )

        if len(uploaded_ids) > 1:
            client.patch(
                f"/v1/appScreenshotSets/{set_id}/relationships/appScreenshots",
                {
                    "data": [
                        {"type": "appScreenshots", "id": sid} for sid in uploaded_ids
                    ]
                },
            )


def sync_iap_review_screenshots(
    *,
    client: ASCClient,
    iap_ids: dict[str, str],
    config: MetadataConfig,
    dry_run: bool,
    report: SyncReport,
) -> None:
    """Idempotently upload reviewScreenshot PNGs for configured in-app purchases."""
    base_dir = config.source_path.parent if config.source_path is not None else Path.cwd()

    for iap in config.in_app_purchases:
        if not iap.review_screenshot:
            continue

        shot_path = Path(iap.review_screenshot)
        if not shot_path.is_absolute():
            shot_path = base_dir / shot_path
        local_shot = inspect_png(shot_path, display_type=None, require_opaque_rgb=True)

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
        report.add(
            action,
            "inAppPurchaseReviewScreenshot",
            iap.product_id,
            f"{local_shot.file_name} {local_shot.width}x{local_shot.height} md5={local_shot.md5[:8]}",
        )
