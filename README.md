# ios-appstore-sync

A reusable CLI and composite GitHub Action that validates metadata, composites brand-framed App Store screenshots, and idempotently syncs App Store Connect drafts from declarative JSON on `ubuntu-latest` — no macOS runner required.

## Features

- **Offline PR validation (`validate`)**: Checks schema v1, Apple character and UTF-8 byte limits (including 100-byte CJK keyword limits), `https://` URLs, configurable forbidden regex patterns, reserved `subscriptionGroups` guardrails, and optional Xcode `.storekit` product ID cross-checks.
- **Brand-framed screenshot compositor (`frame`)**: Uses Pillow on Linux to composite raw simulator PNGs with declarative `screenshots.json` themes (background/gradient, accent, bezel, corner radius, typography with automatic Noto CJK TC/SC/JP/KR fallback). Outputs exact `1320x2868` opaque RGB PNGs (alpha channel stripped), generates `contact-sheet.html` and `contact-sheet.png` for human sign-off, and optionally exports resized `web-screenshots/` PNGs.
- **Idempotent ASC draft sync (`sync`)**: Authenticates with an App Store Connect ES256 API key and syncs:
  - `appInfoLocalizations`, `appStoreVersionLocalizations`, `copyright`, `categories`, and `appStoreReviewDetails`
  - Optional `appAvailabilities` v2 (`--sync-availability`)
  - Optional `appPriceSchedules` for free or paid-upfront apps (`--sync-pricing`)
  - Optional `inAppPurchases` v2 (`CONSUMABLE`, `NON_CONSUMABLE`, `NON_RENEWING_SUBSCRIPTION`), localizations, and price schedules (`--sync-iap`)
  - Optional `appScreenshotSets` / `appScreenshots` (reserve → chunked upload → MD5 commit) and `inAppPurchaseAppStoreReviewScreenshots` (`--sync-screenshots`)
- **Safe by default**:
  - `dry-run` defaults to `true` — reads live ASC state, prints a `CREATE` / `UPDATE` / `UNCHANGED` / `SKIPPED` diff table (and writes to `$GITHUB_STEP_SUMMARY` when set), and issues **zero** mutating HTTP requests.
  - Refuses to mutate `IN_REVIEW`, `READY_FOR_SALE`, or other non-editable versions.
  - Hard safety guardrail: never calls App Store submission endpoints (`reviewSubmissions` or `appStoreVersionSubmissions`).

## Quick start

1. Copy starter configs from [`examples/`](examples/) into your app repository (e.g. `appstore/`):
   - [`examples/free-tipjar-metadata.json`](examples/free-tipjar-metadata.json) — free app with optional consumable tip jar IAPs and `.storekit` cross-check
   - [`examples/paid-upfront-metadata.json`](examples/paid-upfront-metadata.json) — paid upfront app with zero IAPs
   - [`examples/pro-unlock-metadata.json`](examples/pro-unlock-metadata.json) — free app with a single non-consumable Pro unlock
   - [`examples/screenshots.json`](examples/screenshots.json) — brand-framed 6.9" iPhone screenshot theme for `en-US` and `zh-Hant`
2. Copy [`examples/appstore-sync.yml`](examples/appstore-sync.yml) into `.github/workflows/` and pin `ios-ci-apps/ios-appstore-sync` to a full 40-character commit SHA.
3. Configure an **App Store Connect API** key (App Manager role is typically sufficient) in a GitHub Actions **Environment** restricted to your default branch:
   - Secret: `ASC_PRIVATE_KEY` (full contents of the `.p8` file)
   - Variables: `ASC_KEY_ID`, `ASC_ISSUER_ID`

## Action inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `mode` | yes | `validate` | `validate`, `frame`, or `sync` |
| `metadata-path` | for `validate`/`sync` | `""` | Path to declarative `metadata.json` (`schemaVersion: 1`) |
| `screenshots-config-path` | for `frame` | `""` | Path to declarative `screenshots.json` theme config |
| `raw-screenshots-dir` | for `frame` | `""` | Directory with raw simulator PNGs (flat or per-locale subdirs) |
| `output-dir` | for `frame` | `""` | Output directory for framed PNGs, contact sheets, and `web-screenshots/`; also passed to `sync` as `--screenshots-dir` |
| `dry-run` | | `true` | Read-only diff mode; issues zero mutating API requests |
| `sync-availability` | | `false` | Sync `appAvailabilities` v2 when `availability` is configured |
| `sync-pricing` | | `false` | Sync `appPriceSchedules` when `appPricing` is configured |
| `sync-iap` | | `false` | Sync `inAppPurchases` v2, localizations, and price schedules |
| `sync-screenshots` | | `false` | Upload framed app screenshots and IAP review screenshots |
| `screenshot-display-type` | | `""` | Override ASC display type (`APP_IPHONE_69`, `APP_IPHONE_67`, etc.) |
| `api-key-id`, `api-issuer-id` | for `sync` | `""` | App Store Connect API Key ID and Issuer ID |
| `api-private-key` | one of these two (for `sync`) | `""` | Contents of the `.p8` private key (from a secret) |
| `api-key-path` | | `""` | Path to the `.p8` private key file on disk |

## Local CLI usage

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Offline validation
.venv/bin/ios-appstore-sync validate --metadata examples/free-tipjar-metadata.json

# Frame screenshots and generate contact sheet (HTML + PNG)
.venv/bin/ios-appstore-sync frame \
  --config examples/screenshots.json \
  --raw-dir ./raw-screenshots \
  --output-dir ./framed-screenshots

# Dry-run diff against App Store Connect (zero mutations)
export ASC_KEY_ID="..."
export ASC_ISSUER_ID="..."
export ASC_KEY_PATH="/path/to/AuthKey.p8"
.venv/bin/ios-appstore-sync sync \
  --metadata appstore/metadata.json \
  --screenshots-dir ./framed-screenshots \
  --dry-run true \
  --sync-pricing --sync-iap --sync-screenshots
```

## Security

- Credentials are never printed, logged, or included in error messages.
- `validate` and `frame` run completely offline and unset `ASC_*` variables before invoking the CLI.
- Never run `mode: sync` on `pull_request` or `pull_request_target` events. See [SECURITY.md](SECURITY.md) for details.

## Tests

```bash
pytest -v
```

## License

MIT — see [LICENSE](LICENSE).
