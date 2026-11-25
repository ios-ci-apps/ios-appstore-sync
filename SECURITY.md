# Security policy

Please report security problems privately through **Report a vulnerability** under this repository's *Security* tab, not in a public issue.

Worth reporting:
- any way for the CLI or composite action to print, log, or leak the App Store Connect private key (`ASC_PRIVATE_KEY` / `.p8` contents) or signed ES256 JWT tokens;
- any way for `dry-run` mode to issue a mutating HTTP request (`POST`, `PATCH`, `PUT`, or `DELETE`);
- any code path capable of calling App Store submission endpoints (`reviewSubmissions` or `appStoreVersionSubmissions`);
- any way for untrusted inputs to execute arbitrary shell commands or write outside the configured output directory;
- anything in this repository's own CI that could run on a self-hosted runner.

## Operational guidance

- Keep your App Store Connect API key in a GitHub Actions **Environment** secret restricted to your default branch (or a `chmod 600` file on a trusted runner), never committed to a repository.
- Use the least-privileged App Store Connect API key role that covers the endpoints you enable (typically **App Manager** for metadata, pricing, IAPs, and screenshots). Revoke the key immediately in App Store Connect if it leaks.
- Pin this action to a full 40-character commit SHA in calling workflows, not a mutable tag or branch.
- Never pass App Store Connect credentials into jobs triggered by `pull_request` or `pull_request_target`. Run `mode: validate` offline on pull requests, and restrict `mode: sync` to reviewed pushes or manually approved `workflow_dispatch` runs on your default branch.
