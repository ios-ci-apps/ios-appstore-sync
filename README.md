# ios-appstore-sync

A reusable CLI and composite GitHub Action that validates and idempotently syncs App Store Connect drafts from a declarative, monetization-agnostic `metadata.json` file on `ubuntu-latest` — no macOS runner required.

## Features

- **Offline PR validation (`validate`)**: Checks schema v1, Apple character and UTF-8 byte limits (including 100-byte CJK keyword limits), `https://` URLs, configurable forbidden regex patterns, reserved `subscriptionGroups` guardrails, and optional Xcode `.storekit` product ID cross-checks.
- **Idempotent ASC draft sync (`sync`)**: Authenticates with an App Store Connect ES256 API key and syncs `appInfoLocalizations`, `appStoreVersionLocalizations`, `copyright`, `categories`, `appStoreReviewDetails`, optional `appAvailabilities` v2, optional `appPriceSchedules` (free or paid upfront), and optional `inAppPurchases` v2 (`CONSUMABLE`, `NON_CONSUMABLE`, `NON_RENEWING_SUBSCRIPTION`, localizations, and price schedules).
- **Safe by default**:
  - `dry-run` defaults to `true` — reads live ASC state, prints a `CREATE` / `UPDATE` / `UNCHANGED` / `SKIPPED` diff table (and writes to `$GITHUB_STEP_SUMMARY` when set), and issues **zero** mutating HTTP requests.
  - Refuses to mutate `IN_REVIEW`, `READY_FOR_SALE`, or other non-editable versions.
  - Hard safety guardrail: never calls App Store submission endpoints (`reviewSubmissions` or `appStoreVersionSubmissions`).

## Quick start

1. Copy one of the starter configs from [`examples/`](examples/) into your app repository (e.g. `appstore/metadata.json`):
   - [`examples/free-tipjar-metadata.json`](examples/free-tipjar-metadata.json) — free app with optional consumable tip jar IAPs and `.storekit` cross-check
   - [`examples/paid-upfront-metadata.json`](examples/paid-upfront-metadata.json) — paid upfront app with zero IAPs
   - [`examples/pro-unlock-metadata.json`](examples/pro-unlock-metadata.json) — free app with a single non-consumable Pro unlock
2. Copy [`examples/appstore-sync.yml`](examples/appstore-sync.yml) into `.github/workflows/` and pin `ios-ci-apps/ios-appstore-sync` to a full 40-character commit SHA.
3. Configure an **App Store Connect API** key (App Manager role is typically sufficient) in a GitHub Actions **Environment** restricted to your default branch:
   - Secret: `ASC_PRIVATE_KEY` (full contents of the `.p8` file)
   - Variables: `ASC_KEY_ID`, `ASC_ISSUER_ID`

## Action inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `mode` | yes | `validate` | `validate` or `sync` |
| `metadata-path` | yes | | Path to declarative `metadata.json` (`schemaVersion: 1`) |
| `dry-run` | | `true` | Read-only diff mode; issues zero mutating API requests |
| `sync-availability` | | `false` | Sync `appAvailabilities` v2 when `availability` is configured |
| `sync-pricing` | | `false` | Sync `appPriceSchedules` when `appPricing` is configured |
| `sync-iap` | | `false` | Sync `inAppPurchases` v2, localizations, and price schedules |
| `api-key-id`, `api-issuer-id` | for `sync` | | App Store Connect API Key ID and Issuer ID |
| `api-private-key` | one of these two (for `sync`) | | Contents of the `.p8` private key (from a secret) |
| `api-key-path` | | | Path to the `.p8` private key file on disk |

## Local CLI usage

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Offline validation
.venv/bin/ios-appstore-sync validate --metadata examples/free-tipjar-metadata.json

# Dry-run diff against App Store Connect (zero mutations)
export ASC_KEY_ID="..."
export ASC_ISSUER_ID="..."
export ASC_KEY_PATH="/path/to/AuthKey.p8"
.venv/bin/ios-appstore-sync sync --metadata appstore/metadata.json --dry-run true --sync-pricing --sync-iap
```

## Security

- Credentials are never printed, logged, or included in error messages.
- `validate` runs completely offline and unsets `ASC_*` variables before invoking the CLI.
- Never run `mode: sync` on `pull_request` or `pull_request_target` events. See [SECURITY.md](SECURITY.md) for details.

## Tests

```bash
pytest -v
```

## License

MIT — see [LICENSE](LICENSE).
