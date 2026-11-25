"""Command-line interface for ios-appstore-sync."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from ios_appstore_sync.asc_client import ASCAuth, ASCClient, ASCError
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
        description="Validate and idempotently sync App Store Connect drafts from declarative JSON.",
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
        help="Sync validated metadata.json to an editable App Store Connect draft.",
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
        help="Enable syncing appAvailabilities v2 when availability is configured.",
    )
    p_sync.add_argument(
        "--sync-pricing",
        action="store_true",
        default=False,
        help="Enable syncing appPriceSchedules when appPricing is configured.",
    )
    p_sync.add_argument(
        "--sync-iap",
        action="store_true",
        default=False,
        help="Enable syncing inAppPurchases v2, localizations, and price schedules.",
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

    try:
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
        )
        report = sync_metadata(client, validation.config, options)
    except ASCError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    markdown = report.format_markdown()
    print(markdown)
    write_step_summary(markdown)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "sync":
        return _cmd_sync(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
