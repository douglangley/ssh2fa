#!/usr/bin/env python3
"""
PAM SSH 2FA - Native Notification Providers
============================================

Provider-neutral notification interface, per the "Native notification
design" section of AUDIT_REMEDIATION_AND_ADMIN_PLAN.md (Phase 3). This
module deliberately has no dependency on pam_ssh_2fa.py or approval_server.py
-- it's designed to be portable to a future Go/unprivileged daemon without
changing user configuration (same provider names, same config schema).

WHY NATIVE PROVIDERS
---------------------
Apprise supports 80+ services, but this project's actual requirement is
two: Pushover and ntfy. Importing Apprise (and its transitive dependencies)
into a privileged PAM authentication path for that is more surface than
necessary. These native providers use only the Python standard library
(urllib, ssl, json) -- no new dependency. Apprise remains available as a
legacy adapter (AppriseNotifier) for migration; see MODERNIZATION_PLAN.md
and AUDIT_REMEDIATION_AND_ADMIN_PLAN.md for the removal timeline.

INTERFACE
---------
    Notifier.send(Notification) -> DeliveryResult

A Notifier must never put credentials, full URLs, topics, user keys,
tokens, OTPs, or approval links into a DeliveryResult.redacted_detail --
that field is safe to log.

SECURITY NOTES
--------------
- Both providers use ssl.create_default_context(), which verifies the
  server certificate and hostname by default (never disabled here).
- Pushover/ntfy responses are read up to MAX_RESPONSE_BYTES only.
- Redirects are never followed -- a 3xx response is treated as a
  delivery failure rather than silently forwarding an Authorization
  header or token to a different host.
- ntfy access tokens go in the Authorization header, never the URL.
"""

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Protocol
from urllib.parse import urlparse

# =============================================================================
# SHARED TYPES
# =============================================================================

# Read at most this many bytes of any provider's HTTP response body. A
# malicious or misbehaving endpoint returning gigabytes of data must not
# be read into memory in a privileged PAM authentication process.
MAX_RESPONSE_BYTES = 4096


@dataclass
class Notification:
    """
    Provider-neutral notification content.

    Attributes:
        request_id: Non-secret correlation ID for structured logging
            across multiple provider attempts for one authentication
            request. Must NOT be a bearer token/OTP/approval token --
            those are never passed to notifiers.py.
        title: Notification title (already rendered from the user's
            configured template -- notifiers.py does no templating)
        body: Notification body (already rendered)
        click_url: Optional approval link, passed to providers that
            support a clickable action (Pushover's url/url_title, ntfy's
            Click header)
        expires_at: Unix timestamp the notification's underlying OTP/
            approval request expires at, for providers that support
            expressing urgency (currently informational only)
    """

    request_id: str
    title: str
    body: str
    click_url: Optional[str] = None
    expires_at: Optional[float] = None


@dataclass
class DeliveryResult:
    """
    Result of one Notifier.send() call.

    Attributes:
        provider: Short provider name ("pushover", "ntfy", "apprise")
        success: True if the provider accepted the notification
        retryable: True if the failure looks transient (timeout,
            connection error, 5xx, 429) rather than a configuration
            problem (invalid recipient, 4xx). Informational only in
            this Phase 3 scope -- no automatic retry is implemented.
        status_code: HTTP status code, if one was received
        redacted_detail: Human-readable, secret-free description safe
            to log or display (no tokens, keys, topics, URLs, or
            message content)
        elapsed_ms: Wall-clock time the send attempt took
    """

    provider: str
    success: bool
    retryable: bool = False
    status_code: Optional[int] = None
    redacted_detail: str = ""
    elapsed_ms: int = 0


class Notifier(Protocol):
    def send(self, notification: Notification) -> DeliveryResult: ...


def read_secret_file(path: str, logger=None) -> str:
    """
    Read a one-line secret from a file (e.g. a Pushover app token or an
    ntfy access token), rather than embedding it directly in config.ini
    or a per-user config file.

    Args:
        path: Path to the secret file. Empty string means "not configured".
        logger: Optional object with an .error(message, **kwargs) method

    Returns:
        The file's stripped contents, or "" if path is empty, the file
        is missing, or it can't be read. Never raises -- a missing
        secret is a configuration problem the caller should detect from
        the empty return value and fail closed on, not a crash.
    """
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        if logger:
            logger.error(f"Failed to read secret file {path}: {e}")
        return ""


def _is_non_global_host(host: str) -> bool:
    """
    Decide whether a URL host is confined to a trusted (non-public)
    network, without a DNS lookup (this module never performs network
    resolution at config-validation time). Mirrors
    ApprovalManager._is_non_global_host() in pam_ssh_2fa.py -- duplicated
    rather than imported so this module has no dependency on
    pam_ssh_2fa.py (see the module docstring: it should be portable to a
    future daemon on its own).

    Args:
        host: The hostname or IP literal from a parsed URL

    Returns:
        True if the host is not routable on the public internet
    """
    import ipaddress

    if not host:
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not addr.is_global


def _post_json(
    url: str,
    data: bytes,
    headers: dict,
    connect_timeout: float,
    read_timeout: float,
) -> "urllib.response.addinfourl":
    """
    POST with TLS certificate/hostname verification, no redirect
    following, and a bounded read. Raises urllib.error.HTTPError/
    URLError/socket.timeout on failure -- callers catch these.

    Args:
        url: Destination URL (https:// verified via default SSL context;
            http:// allowed only if the caller already validated it's a
            confined/approved deployment)
        data: Request body bytes
        headers: Request headers (Content-Type, Authorization, etc.)
        connect_timeout: Seconds to allow for connection establishment
        read_timeout: Seconds to allow for reading the response

    Returns:
        The urlopen response object, already fully read up to
        MAX_RESPONSE_BYTES (caller reads .read() again safely -- see
        _read_bounded()).
    """
    context = ssl.create_default_context()  # verifies cert + hostname
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        # A 3xx response could otherwise be followed to a different
        # host while still carrying our Authorization header. Treat
        # every redirect as a failure instead (see module docstring).
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _NoRedirect()
    )
    # urllib's single `timeout` covers the whole blocking call (connect
    # + read together); the stdlib doesn't expose them separately
    # without raw socket handling. connect_timeout/read_timeout are
    # still accepted and summed, so the two config knobs from
    # AUDIT_REMEDIATION_AND_ADMIN_PLAN.md's suggested schema both have
    # an effect, but this is a documented simplification, not a precise
    # split.
    return opener.open(request, timeout=connect_timeout + read_timeout)


def _read_bounded(response) -> bytes:
    """Read at most MAX_RESPONSE_BYTES from an HTTP response."""
    return response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]


# =============================================================================
# PUSHOVER
# =============================================================================

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
# Pushover application tokens and user/group keys are documented as
# 30-character case-sensitive alphanumeric strings.
PUSHOVER_KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
PUSHOVER_TITLE_MAX = 250
PUSHOVER_MESSAGE_MAX = 1024
PUSHOVER_URL_MAX = 512
PUSHOVER_URL_TITLE_MAX = 100


class PushoverNotifier:
    """
    Native Pushover provider. Sends directly to Pushover's fixed API
    endpoint -- no third-party library required.

    Attributes:
        app_token: Pushover application API token (30 alphanumeric chars)
        user_key: Recipient user or group key (30 alphanumeric chars)
        connect_timeout: Seconds allowed for connection establishment
        read_timeout: Seconds allowed for reading the response
        logger: Optional logger

    Usage:
        notifier = PushoverNotifier(app_token=token, user_key=key)
        result = notifier.send(Notification(
            request_id="abc123", title="SSH Login",
            body="Your code is 123456", click_url=None,
        ))
    """

    name = "pushover"

    def __init__(
        self,
        app_token: str,
        user_key: str,
        connect_timeout: float = 3,
        read_timeout: float = 4,
        logger=None,
    ):
        """
        Args:
            app_token: Pushover application API token
            user_key: Recipient user/group key
            connect_timeout: Seconds allowed for connection establishment
            read_timeout: Seconds allowed for reading the response
            logger: Optional logger

        Raises:
            ValueError: If app_token or user_key isn't a well-formed
                30-character alphanumeric Pushover key. Validated before
                any network use, per AUDIT_REMEDIATION_AND_ADMIN_PLAN.md.
        """
        if not PUSHOVER_KEY_RE.match(app_token or ""):
            raise ValueError("Pushover app_token is not a valid 30-character key")
        if not PUSHOVER_KEY_RE.match(user_key or ""):
            raise ValueError("Pushover user_key is not a valid 30-character key")
        self.app_token = app_token
        self.user_key = user_key
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger

    def send(self, notification: Notification) -> DeliveryResult:
        start = time.monotonic()

        title = notification.title[:PUSHOVER_TITLE_MAX]
        message = notification.body[:PUSHOVER_MESSAGE_MAX]

        form = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message,
            "priority": "0",  # normal priority -- see module docstring
        }
        if notification.click_url:
            form["url"] = notification.click_url[:PUSHOVER_URL_MAX]
            form["url_title"] = "Open approval link"[:PUSHOVER_URL_TITLE_MAX]

        data = "&".join(
            f"{k}={urllib.parse.quote(str(v))}" for k, v in form.items()
        ).encode("ascii")

        elapsed = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731

        try:
            response = _post_json(
                PUSHOVER_API_URL,
                data,
                {"Content-Type": "application/x-www-form-urlencoded"},
                self.connect_timeout,
                self.read_timeout,
            )
        except urllib.error.HTTPError as e:
            body = _read_bounded(e)
            status = self._parse_status(body)
            return DeliveryResult(
                provider=self.name,
                success=False,
                retryable=e.code >= 500 or e.code == 429,
                status_code=e.code,
                redacted_detail=f"HTTP {e.code}, pushover status={status}",
                elapsed_ms=elapsed(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return DeliveryResult(
                provider=self.name,
                success=False,
                retryable=True,
                redacted_detail=f"connection error: {type(e).__name__}",
                elapsed_ms=elapsed(),
            )

        with response:
            body = _read_bounded(response)
        status_code = response.status
        pushover_status = self._parse_status(body)
        success = status_code == 200 and pushover_status == 1

        return DeliveryResult(
            provider=self.name,
            success=success,
            retryable=not success,
            status_code=status_code,
            redacted_detail=f"HTTP {status_code}, pushover status={pushover_status}",
            elapsed_ms=elapsed(),
        )

    @staticmethod
    def _parse_status(body: bytes) -> Optional[int]:
        """Extract Pushover's JSON "status" field without trusting the
        rest of the payload (which could contain an "errors" array with
        arbitrary-length strings)."""
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        status = parsed.get("status")
        return status if isinstance(status, int) else None


# =============================================================================
# NTFY
# =============================================================================


class NtfyNotifier:
    """
    Native ntfy provider. POSTs directly to a self-hosted or ntfy.sh
    publish URL -- no third-party library required.

    Attributes:
        publish_url: Full https://host[:port]/topic publish URL
        access_token: Optional bearer token (sent in the Authorization
            header, never the URL)
        connect_timeout: Seconds allowed for connection establishment
        read_timeout: Seconds allowed for reading the response
        logger: Optional logger

    Usage:
        notifier = NtfyNotifier(publish_url="https://ntfy.example.com/ssh-alice")
        result = notifier.send(Notification(
            request_id="abc123", title="SSH Login", body="Code: 123456",
        ))
    """

    name = "ntfy"

    def __init__(
        self,
        publish_url: str,
        access_token: str = "",
        connect_timeout: float = 3,
        read_timeout: float = 4,
        allow_insecure_http: bool = False,
        logger=None,
    ):
        """
        Args:
            publish_url: Full https://host[:port]/topic publish URL
            access_token: Optional bearer token
            connect_timeout: Seconds allowed for connection establishment
            read_timeout: Seconds allowed for reading the response
            allow_insecure_http: Permit a plain http:// publish_url even
                to a public host. Off by default -- see
                _is_non_global_host.
            logger: Optional logger

        Raises:
            ValueError: If publish_url has an unsupported scheme, no
                host, or is an insecure http:// URL to a public host
                without allow_insecure_http
        """
        parsed = urlparse(publish_url or "")
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"ntfy publish_url must use http:// or https:// (got {parsed.scheme!r})")
        if not parsed.hostname:
            raise ValueError("ntfy publish_url has no host")
        if not parsed.path or parsed.path == "/":
            raise ValueError("ntfy publish_url must include a topic path")
        if parsed.scheme == "http" and not (
            allow_insecure_http or _is_non_global_host(parsed.hostname)
        ):
            raise ValueError(
                "ntfy publish_url uses http:// for a public host; use "
                "https:// or set allow_insecure_http for a confined "
                "deployment (not recommended)"
            )

        self.publish_url = publish_url
        self.access_token = access_token
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger

        if parsed.hostname == "ntfy.sh" and not access_token:
            if logger:
                logger.warning(
                    "Publishing to an unauthenticated ntfy.sh topic -- the "
                    "topic name is effectively a shared password; anyone "
                    "who learns it can read (and send) messages on it. "
                    "Prefer a reserved/protected topic or a self-hosted "
                    "instance with an access token for production."
                )

    def send(self, notification: Notification) -> DeliveryResult:
        start = time.monotonic()
        elapsed = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731

        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": _ascii_header_value(notification.title),
            "Priority": "default",
        }
        if notification.click_url:
            headers["Click"] = notification.click_url
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        data = notification.body.encode("utf-8")

        try:
            response = _post_json(
                self.publish_url,
                data,
                headers,
                self.connect_timeout,
                self.read_timeout,
            )
        except urllib.error.HTTPError as e:
            _read_bounded(e)
            return DeliveryResult(
                provider=self.name,
                success=False,
                retryable=e.code >= 500 or e.code == 429,
                status_code=e.code,
                redacted_detail=f"HTTP {e.code}",
                elapsed_ms=elapsed(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return DeliveryResult(
                provider=self.name,
                success=False,
                retryable=True,
                redacted_detail=f"connection error: {type(e).__name__}",
                elapsed_ms=elapsed(),
            )

        with response:
            _read_bounded(response)
        status_code = response.status
        success = 200 <= status_code < 300

        return DeliveryResult(
            provider=self.name,
            success=success,
            retryable=not success,
            status_code=status_code,
            redacted_detail=f"HTTP {status_code}",
            elapsed_ms=elapsed(),
        )


def _ascii_header_value(value: str) -> str:
    """
    ntfy's Title/Click headers must be valid HTTP header values (no
    control characters, ASCII-safe). Non-ASCII/control characters are
    replaced rather than rejected, so a hostname or username with an
    unusual character degrades gracefully instead of crashing the
    privileged auth path.
    """
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in value)


# =============================================================================
# APPRISE (LEGACY ADAPTER)
# =============================================================================


class AppriseNotifier:
    """
    Legacy adapter: sends an already-rendered title/body via Apprise.

    Retained only for migration -- see AUDIT_REMEDIATION_AND_ADMIN_PLAN.md
    Phase 3 ("Apprise compatibility"). New per-user configs should prefer
    PushoverNotifier/NtfyNotifier; this exists so an existing
    apprise_urls-based config keeps working, and so "apprise" can be
    listed alongside native providers during a gradual migration.

    Unlike pam_ssh_2fa.NotificationSender, this class does no templating
    -- it sends notification.title/notification.body exactly as given,
    matching every other Notifier in this module.
    """

    name = "apprise"

    def __init__(self, apprise_urls: List[str], logger=None):
        self.apprise_urls = apprise_urls
        self.logger = logger

    def send(self, notification: Notification) -> DeliveryResult:
        start = time.monotonic()
        elapsed = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731

        try:
            import apprise
        except ImportError:
            return DeliveryResult(
                provider=self.name,
                success=False,
                redacted_detail="apprise not installed",
                elapsed_ms=elapsed(),
            )

        if not self.apprise_urls:
            return DeliveryResult(
                provider=self.name,
                success=False,
                redacted_detail="no apprise_urls configured",
                elapsed_ms=elapsed(),
            )

        apobj = apprise.Apprise()
        for url in self.apprise_urls:
            apobj.add(url)

        try:
            ok = apobj.notify(
                title=notification.title,
                body=notification.body,
                notify_type=apprise.NotifyType.INFO,
            )
        except Exception as e:  # Apprise can raise provider-specific exceptions
            return DeliveryResult(
                provider=self.name,
                success=False,
                retryable=True,
                redacted_detail=f"send exception: {type(e).__name__}",
                elapsed_ms=elapsed(),
            )

        return DeliveryResult(
            provider=self.name,
            success=bool(ok),
            retryable=not ok,
            redacted_detail="apprise notify() returned "
            + ("success" if ok else "failure"),
            elapsed_ms=elapsed(),
        )
