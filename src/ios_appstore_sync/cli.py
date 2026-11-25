"""Command-line interface for ios-appstore-sync."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ios_appstore_sync.asc_client import ASCAuth, ASCClient, ASCError
from ios_appstore_sync.framer import FramingError, frame_screenshots
from ios_appstore_sync.sync import SyncOptions, sync_metadata, write_step_summary
from ios_appstore_sync.validator import validate_metadata_file


def _parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected boolean ('true' or 'false'), got {value!r}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ios-appstore-sync",
        description="Validate, frame screenshots, and idempotently sync App Store Connect drafts and app settings.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    p_val = subparsers.add_parser(
        "validate",
        help="Validate metadata.json offline against schema v1 and Apple App Store limits.",
    )
    p_val.add_argument(
        "--metadata",
        required=True,
        help="Path to declarative metadata.json file.",
    )

    # sync
    p_sync = subparsers.add_parser(
        "sync",
        help=(
            "Sync validated metadata.json (and optional screenshots) to an editable ASC version draft "
            "and optional immediate app-level settings."
        ),
    )
    p_sync.add_argument(
        "--metadata",
        required=True,
        help="Path to declarative metadata.json file.",
    )
    p_sync.add_argument(
        "--dry-run",
        type=_parse_bool_flag,
        default=True,
        help="When true (default), perform reads only and issue zero mutating requests.",
    )
    p_sync.add_argument(
        "--sync-availability",
        action="store_true",
        default=False,
        help="Enable syncing appAvailabilities v2 (app-level, immediate effect) when availability is configured.",
    )
    p_sync.add_argument(
        "--sync-pricing",
        action="store_true",
        default=False,
        help="Enable syncing appPriceSchedules (app-level, immediate effect) when appPricing is configured.",
    )
    p_sync.add_argument(
        "--sync-iap",
        action="store_true",
        default=False,
        help="Enable syncing inAppPurchases v2, localizations, and price schedules (IAP pricing is immediate).",
    )
    p_sync.add_argument(
        "--sync-screenshots",
        action="store_true",
        default=False,
        help="Enable syncing app screenshots and IAP review screenshots.",
    )
    p_sync.add_argument(
        "--allow-live-changes",
        action="store_true",
        default=False,
        help=(
            "Confirm immediate app-level changes (app availability, app pricing, IAP pricing) "
            "when the app already has a live/released version."
        ),
    )
    p_sync.add_argument(
        "--screenshots-dir",
        default=None,
        help="Directory containing per-locale framed PNGs (overrides metadata.screenshots.directory).",
    )
    p_sync.add_argument(
        "--screenshot-display-type",
        default=None,
        help="Override ASC screenshotDisplayType (e.g. APP_IPHONE_69 or APP_IPHONE_67).",
    )
    p_sync.add_argument(
        "--api-key-id",
        default=None,
        help="App Store Connect API Key ID (defaults to ASC_KEY_ID env var).",
    )
    p_sync.add_argument(
        "--api-issuer-id",
        default=None,
        help="App Store Connect Issuer ID (defaults to ASC_ISSUER_ID env var).",
    )
    p_sync.add_argument(
        "--api-key-path",
        default=None,
        help="Path to .p8 private key file (defaults to ASC_KEY_PATH or ASC_PRIVATE_KEY env var).",
    )

    # frame
    p_frame = subparsers.add_parser(
        "frame",
        help="Composite raw simulator PNGs into opaque RGB brand-framed App Store screenshots.",
    )
    p_frame.add_argument(
        "--config",
        required=True,
        help="Path to declarative screenshots.json theme config.",
    )
    p_frame.add_argument(
        "--raw-dir",
        required=True,
        help="Directory containing raw simulator PNGs (flat or per-locale subdirectories).",
    )
    p_frame.add_argument(
        "--output-dir",
        required=True,
        help="Directory where framed PNGs, contact sheets, and optional web-screenshots/ are written.",
    )

    return parser


def _cmd_validate(args: argparse.Namespace) -> int:
    result = validate_metadata_file(args.metadata)
    print(result.format_report())
    return 0 if result.ok else 1


def _cmd_sync(args: argparse.Namespace) -> int:
    validation = validate_metadata_file(args.metadata)
    if not validation.ok or validation.config is None:
        print(validation.format_report(), file=sys.stderr)
        return 1
    if validation.warnings:
        for warning in validation.warnings:
            print(warning.format(), file=sys.stderr)

    auth = ASCAuth.from_env(
        key_id=args.api_key_id,
        issuer_id=args.api_issuer_id,
        key_path=args.api_key_path,
    )
    client = ASCClient(auth=auth, read_only=args.dry_run)
    options = SyncOptions(
        dry_run=args.dry_run,
        sync_availability=args.sync_availability,
        sync_pricing=args.sync_pricing,
        sync_iap=args.sync_iap,
        sync_screenshots=args.sync_screenshots,
        allow_live_changes=args.allow_live_changes,
        screenshots_dir=Path(args.screenshots_dir) if args.screenshots_dir else None,
        screenshot_display_type=args.screenshot_display_type,
    )
    report = sync_metadata(client, validation.config, options)

    markdown = report.format_markdown()
    print(markdown)
    write_step_summary(markdown)
    return 0


def _cmd_frame(args: argparse.Namespace) -> int:
    result = frame_screenshots(
        config_path=args.config,
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
    )

    print(f"Framed {len(result.screenshots)} screenshot(s):")
    for shot in result.screenshots:
        print(f"  - {shot.locale}/{shot.output_path.name}: {shot.width}x{shot.height} ({shot.mode})")
    print(f"Contact sheet PNG:  {result.contact_sheet_png}")
    print(f"Contact sheet HTML: {result.contact_sheet_html}")
    if result.web_export_dir is not None:
        print(f"Web screenshots:    {result.web_export_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "sync":
            return _cmd_sync(args)
        if args.command == "frame":
            return _cmd_frame(args)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON: {exc}", file=sys.stderr)
        return 1
    except (ASCError, FramingError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
