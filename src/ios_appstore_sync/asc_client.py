"""App Store Connect REST API client with ES256 JWT auth, retry, and safety guardrails."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import jwt

DEFAULT_BASE_URL = "https://api.appstoreconnect.apple.com"
_FORBIDDEN_SUBMISSION_SEGMENTS = (
    "reviewsubmissions",
    "appstoreversionsubmissions",
)
_SERVER_ERROR_RETRY_CODES = frozenset({500, 502, 503, 504})
_IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class ASCError(RuntimeError):
    """Base error for App Store Connect client failures."""


class ASCTransportError(ASCError):
    """Raised on network or timeout transport errors communicating with ASC."""


class ASCSafetyError(ASCError):
    """Raised when a forbidden endpoint or disallowed redirect/pagination host is attempted."""


class ASCReadOnlyError(ASCError):
    """Raised when a mutating request is attempted in dry-run/read-only mode."""


class ASCAPIError(ASCError):
    """Structured error raised on non-2xx App Store Connect API responses."""

    def __init__(self, status: int, method: str, url: str, detail: str) -> None:
        super().__init__(f"ASC API {method} {url} failed with HTTP {status}: {detail}")
        self.status = status
        self.method = method
        self.url = url
        self.detail = detail


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


Transport = Callable[[str, str, dict[str, str], bytes | None, float], HTTPResponse]


def urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> HTTPResponse:
    """Default stdlib HTTP transport using urllib.request."""
    req = Request(url=url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return HTTPResponse(status=resp.status, headers=resp_headers, body=resp.read())
    except HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        err_body = exc.read() if exc.fp is not None else b""
        return HTTPResponse(status=exc.code, headers=resp_headers, body=err_body)
    except TimeoutError as exc:
        raise ASCTransportError(f"Request timed out during {method} {url}: {exc}") from exc
    except URLError as exc:
        raise ASCTransportError(f"Network error during {method} {url}: {exc.reason}") from exc


def format_asc_error_body(body: bytes) -> str:
    """Format Apple's JSON API error list into a human-readable string."""
    if not body:
        return "empty response body"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body.decode("utf-8", errors="replace")[:500]

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        parts: list[str] = []
        for item in errors:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "")
            title = item.get("title", "")
            detail = item.get("detail", "")
            summary = " - ".join(p for p in (code, title, detail) if p)
            if summary:
                parts.append(summary)
        if parts:
            return "; ".join(parts)
    return str(payload)[:500]


class ASCAuth:
    """ES256 JWT token generator for App Store Connect API keys.

    Private key contents and signed JWT strings are never exposed in repr/str.
    """

    def __init__(
        self,
        key_id: str,
        issuer_id: str,
        private_key_pem: str,
        *,
        ttl_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not key_id.strip():
            raise ASCError("ASC_KEY_ID is required.")
        if not issuer_id.strip():
            raise ASCError("ASC_ISSUER_ID is required.")
        if not private_key_pem.strip():
            raise ASCError("ASC_PRIVATE_KEY or ASC_KEY_PATH is required.")
        if ttl_seconds <= 0 or ttl_seconds > 1200:
            raise ValueError("ttl_seconds must be between 1 and 1200 per Apple ASC spec.")

        self._key_id = key_id.strip()
        self._issuer_id = issuer_id.strip()
        self._private_key_pem = private_key_pem
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cached_token: str | None = None
        self._cached_exp: int = 0

    def __repr__(self) -> str:
        return f"ASCAuth(key_id={self._key_id!r}, issuer_id='***')"

    @classmethod
    def from_env(
        cls,
        *,
        key_id: str | None = None,
        issuer_id: str | None = None,
        private_key: str | None = None,
        key_path: str | None = None,
    ) -> ASCAuth:
        resolved_key_id = key_id or os.environ.get("ASC_KEY_ID", "")
        resolved_issuer_id = issuer_id or os.environ.get("ASC_ISSUER_ID", "")
        resolved_private_key = private_key or os.environ.get("ASC_PRIVATE_KEY", "")
        resolved_key_path = key_path or os.environ.get("ASC_KEY_PATH", "")

        if not resolved_private_key and resolved_key_path:
            path = Path(resolved_key_path)
            if not path.is_file():
                raise ASCError(f"ASC_KEY_PATH file does not exist: '{path}'")
            resolved_private_key = path.read_text(encoding="utf-8")

        return cls(
            key_id=resolved_key_id,
            issuer_id=resolved_issuer_id,
            private_key_pem=resolved_private_key,
        )

    def get_token(self) -> str:
        now = int(self._clock())
        if self._cached_token is not None and now < (self._cached_exp - 60):
            return self._cached_token

        exp = now + self._ttl_seconds
        payload = {
            "iss": self._issuer_id,
            "iat": now,
            "exp": exp,
            "aud": "appstoreconnect-v1",
        }
        headers = {
            "alg": "ES256",
            "kid": self._key_id,
            "typ": "JWT",
        }
        token = jwt.encode(
            payload,
            self._private_key_pem,
            algorithm="ES256",
            headers=headers,
        )
        if isinstance(token, bytes):
            token = token.decode("ascii")
        self._cached_token = token
        self._cached_exp = exp
        return token


class ASCClient:
    """Minimal App Store Connect REST API client with retry and safety guardrails."""

    def __init__(
        self,
        auth: ASCAuth,
        *,
        base_url: str = DEFAULT_BASE_URL,
        read_only: bool = False,
        timeout: float = 30.0,
        max_retries: int = 4,
        base_backoff: float = 1.0,
        transport: Transport = urllib_transport,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._allowed_api_host = (urlparse(self._base_url).hostname or "").lower()
        self.read_only = read_only
        self._timeout = timeout
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._transport = transport
        self._sleep = sleep_fn

    def _build_url(self, path_or_url: str, params: dict[str, Any] | None = None) -> str:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = urljoin(f"{self._base_url}/", path_or_url.lstrip("/"))
        if params:
            query = urlencode([(k, v) for k, v in params.items() if v is not None], doseq=True)
            if query:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{query}"
        return url

    def _validate_pagination_url(self, next_url: str) -> None:
        parsed = urlparse(next_url)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or not hostname
            or hostname != self._allowed_api_host
        ):
            raise ASCSafetyError(
                f"Refused to follow pagination links.next URL with disallowed scheme or host: "
                f"{next_url!r} (allowed: https://{self._allowed_api_host})."
            )

    @staticmethod
    def _should_retry_status(method: str, status: int) -> bool:
        if status == 429:
            return True
        return status in _SERVER_ERROR_RETRY_CODES and method in _IDEMPOTENT_METHODS

    def _enforce_guardrails(self, method: str, url: str) -> None:
        method_upper = method.upper()
        parsed_path = urlparse(url).path.lower()
        for forbidden in _FORBIDDEN_SUBMISSION_SEGMENTS:
            if forbidden in parsed_path:
                raise ASCSafetyError(
                    f"Refused request to forbidden submission endpoint: {method_upper} {parsed_path}"
                )
        if self.read_only and method_upper in _MUTATING_METHODS:
            raise ASCReadOnlyError(
                f"Refused mutating request {method_upper} {parsed_path} while dry-run/read-only mode is active."
            )

    def _retry_delay(self, attempt: int, headers: dict[str, str]) -> float:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                delay = float(retry_after)
                if delay >= 0:
                    return min(delay, 60.0)
            except ValueError:
                pass
        return min(self._base_backoff * (2**attempt), 60.0)

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        method_upper = method.upper()
        url = self._build_url(path_or_url, params=params)
        self._enforce_guardrails(method_upper, url)

        body_bytes: bytes | None = None
        if json_body is not None:
            body_bytes = json.dumps(json_body, separators=(",", ":")).encode("utf-8")

        for attempt in range(self._max_retries + 1):
            headers = {
                "Authorization": f"Bearer {self._auth.get_token()}",
                "Accept": "application/json",
            }
            if body_bytes is not None:
                headers["Content-Type"] = "application/json"

            try:
                resp = self._transport(method_upper, url, headers, body_bytes, self._timeout)
            except (ASCTransportError, URLError, TimeoutError) as exc:
                if method_upper in _IDEMPOTENT_METHODS and attempt < self._max_retries:
                    self._sleep(self._retry_delay(attempt, {}))
                    continue
                if isinstance(exc, ASCError):
                    raise
                raise ASCTransportError(f"Transport error during {method_upper} {url}: {exc}") from exc

            if resp.status == 404 and (allow_404 or (method_upper == "DELETE" and attempt > 0)):
                return None

            if 200 <= resp.status < 300:
                if resp.status == 204 or not resp.body:
                    return {}
                return resp.json()

            if self._should_retry_status(method_upper, resp.status) and attempt < self._max_retries:
                self._sleep(self._retry_delay(attempt, resp.headers))
                continue

            raise ASCAPIError(
                status=resp.status,
                method=method_upper,
                url=url,
                detail=format_asc_error_body(resp.body),
            )

        raise ASCError(f"Exhausted retries for {method_upper} {url}")

    def get(
        self,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        return self.request("GET", path_or_url, params=params, allow_404=allow_404)

    def post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        res = self.request("POST", path, json_body=json_body)
        return res or {}

    def patch(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        res = self.request("PATCH", path, json_body=json_body)
        return res or {}

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def get_all(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        included_out: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate through an App Store Connect collection endpoint."""
        items: list[dict[str, Any]] = []
        next_url: str | None = path
        next_params = params

        while next_url:
            page = self.get(next_url, params=next_params) or {}
            data = page.get("data")
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.append(data)
            if included_out is not None:
                included = page.get("included")
                if isinstance(included, list):
                    included_out.extend(included)
            links = page.get("links") or {}
            raw_next = links.get("next") if isinstance(links, dict) else None
            if raw_next:
                next_str = str(raw_next)
                self._validate_pagination_url(next_str)
                next_url = next_str
            else:
                next_url = None
            next_params = None

        return items

    def upload_chunk(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes,
    ) -> None:
        """Upload a binary chunk to an Apple presigned uploadOperation URL.

        Presigned upload URLs do not receive the ASC Bearer JWT header.
        """
        method_upper = method.upper()
        self._enforce_guardrails(method_upper, url)

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._transport(method_upper, url, dict(headers), data, self._timeout)
            except (ASCTransportError, URLError, TimeoutError) as exc:
                if method_upper in _IDEMPOTENT_METHODS and attempt < self._max_retries:
                    self._sleep(self._retry_delay(attempt, {}))
                    continue
                if isinstance(exc, ASCError):
                    raise
                raise ASCTransportError(f"Transport error during {method_upper} {url}: {exc}") from exc

            if 200 <= resp.status < 300:
                return
            if self._should_retry_status(method_upper, resp.status) and attempt < self._max_retries:
                self._sleep(self._retry_delay(attempt, resp.headers))
                continue
            raise ASCAPIError(
                status=resp.status,
                method=method_upper,
                url=url,
                detail=format_asc_error_body(resp.body),
            )
