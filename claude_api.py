"""
Claude API client for fetching usage data.

Authentication: Uses sessionKey cookie from claude.ai browser session.

Two HTTP backends with automatic fallback:
  1. curl_cffi  — Chrome TLS fingerprint impersonation (fast, may fail on some machines)
  2. PowerShell — .NET SChannel, Windows native TLS stack (like macOS URLSession)

API endpoints used:
  - GET /api/organizations            → list user's organizations
  - GET /api/organizations/{id}/usage → usage data (5h, 7d limits)
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone

log = logging.getLogger(__name__)

API_BASE = "https://claude.ai/api"


class ClaudeAPIError(Exception):
    """Raised when an API call fails (auth error, network issue, etc.)."""
    pass


# ---------------------------------------------------------------------------
# HTTP backend: PowerShell (.NET SChannel — Windows native TLS)
#
# This is the Windows equivalent of macOS URLSession.
# Uses .NET's HttpClient via PowerShell, which presents a native Windows
# TLS fingerprint that Cloudflare is unlikely to block.
# No external dependencies — PowerShell is built into Windows 10/11.
# ---------------------------------------------------------------------------

def _powershell_request(url: str, session_key: str, timeout: int = 30) -> dict:
    """
    Make an HTTP GET request using PowerShell Invoke-RestMethod.

    Returns:
        Parsed JSON response as a Python dict/list.

    Raises:
        ClaudeAPIError on HTTP errors, Cloudflare blocks, or timeouts.
    """
    # PowerShell script that:
    #   1. Creates a WebSession with the sessionKey cookie
    #   2. Sends GET request with browser-like headers
    #   3. Outputs the response as JSON
    ps_script = f'''
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$cookie = New-Object System.Net.Cookie('sessionKey', '{session_key}', '/', 'claude.ai')
$session.Cookies.Add($cookie)
$session.UserAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'

$headers = @{{
    'Accept'          = 'application/json'
    'Accept-Language'  = 'en-US,en;q=0.9'
    'Referer'         = 'https://claude.ai/'
    'Origin'          = 'https://claude.ai'
    'Sec-Ch-Ua'       = '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'
    'Sec-Fetch-Dest'  = 'empty'
    'Sec-Fetch-Mode'  = 'cors'
    'Sec-Fetch-Site'  = 'same-origin'
}}

$resp = Invoke-WebRequest -Uri '{url}' -WebSession $session -Headers $headers -UseBasicParsing -TimeoutSec {timeout}

if ($resp.Headers['Content-Type'] -notlike '*application/json*') {{
    Write-Error 'CLOUDFLARE_BLOCKED'
    exit 1
}}

Write-Output $resp.Content
'''

    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
            capture_output=True, text=True,
            timeout=timeout + 10,  # Extra buffer for PowerShell startup
            creationflags=0x08000000,  # CREATE_NO_WINDOW — hide console flash
        )
    except subprocess.TimeoutExpired:
        raise ClaudeAPIError(f"Request timed out: {url}")
    except FileNotFoundError:
        raise ClaudeAPIError("PowerShell not found on this system")

    stderr = result.stderr.strip()
    stdout = result.stdout.strip()

    if result.returncode != 0 or not stdout:
        if 'CLOUDFLARE_BLOCKED' in stderr:
            raise ClaudeAPIError("Cloudflare blocked the request (PowerShell backend)")
        if '(401)' in stderr or '(403)' in stderr:
            if 'session' in stderr.lower() or 'invalid' in stderr.lower():
                raise ClaudeAPIError("Invalid sessionKey - please update your key")
            raise ClaudeAPIError(f"Authentication error: {stderr[:150]}")
        if '(429)' in stderr:
            raise ClaudeAPIError("Rate limited — try again later")
        raise ClaudeAPIError(f"HTTP request failed: {stderr[:200]}")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Check if the response is an HTML page (Cloudflare challenge)
        if '<html' in stdout.lower() or 'just a moment' in stdout.lower():
            raise ClaudeAPIError("Cloudflare blocked the request (got HTML instead of JSON)")
        raise ClaudeAPIError(f"Invalid JSON response: {stdout[:100]}")


# ---------------------------------------------------------------------------
# HTTP backend: curl_cffi (Chrome TLS fingerprint impersonation)
# ---------------------------------------------------------------------------

def _curl_cffi_request(url: str, session_key: str, timeout: int = 30) -> dict:
    """
    Make an HTTP GET request using curl_cffi with Chrome impersonation.

    Falls back through available Chrome fingerprints.
    May be blocked by Cloudflare on some machines/networks.
    """
    from curl_cffi import requests

    _FINGERPRINTS = ("chrome136", "chrome131", "chrome124", "chrome120", "chrome")

    session = None
    fp_used = "unknown"
    for fp in _FINGERPRINTS:
        try:
            session = requests.Session(impersonate=fp)
            fp_used = fp
            break
        except Exception:
            continue

    if session is None:
        raise ClaudeAPIError("curl_cffi: no supported Chrome fingerprint available")

    session.headers.update({
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
        "Sec-Ch-Ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    })
    session.cookies.set("sessionKey", session_key, domain="claude.ai")

    # Warmup: visit homepage to get Cloudflare clearance cookies
    try:
        session.get("https://claude.ai/", timeout=10)
    except Exception:
        pass

    resp = session.get(url, timeout=timeout)

    if resp.status_code in (401, 403):
        try:
            data = resp.json()
            msg = data.get("error", {}).get("message", "")
        except Exception:
            msg = ""
        if "invalid" in msg.lower() or "session" in msg.lower():
            raise ClaudeAPIError("Invalid sessionKey - please update your key")
        raise ClaudeAPIError(f"HTTP {resp.status_code}: {msg or resp.text[:100]}")

    if resp.status_code != 200:
        raise ClaudeAPIError(f"HTTP {resp.status_code}")

    ct = resp.headers.get("content-type", "")
    if "application/json" not in ct:
        raise ClaudeAPIError(f"Cloudflare blocked (curl_cffi, fingerprint={fp_used})")

    return resp.json()


# ---------------------------------------------------------------------------
# Main API client — tries curl_cffi first, falls back to PowerShell
# ---------------------------------------------------------------------------

class ClaudeAPI:
    """
    Client for the claude.ai web API.

    Tries curl_cffi (fast) first. If Cloudflare blocks it,
    automatically falls back to PowerShell (Windows native TLS).
    The working backend is remembered for subsequent requests.
    """

    def __init__(self, session_key: str):
        self.session_key = session_key
        self._backend = None  # "curl_cffi" or "powershell", auto-detected

    def _request(self, url: str) -> dict:
        """Make a GET request, auto-selecting the best backend."""

        # If we already know which backend works, use it directly
        if self._backend == "powershell":
            return _powershell_request(url, self.session_key)
        if self._backend == "curl_cffi":
            return _curl_cffi_request(url, self.session_key)

        # First call: try curl_cffi, fall back to PowerShell
        try:
            result = _curl_cffi_request(url, self.session_key)
            self._backend = "curl_cffi"
            log.info("Using curl_cffi backend (Cloudflare bypass OK)")
            return result
        except ClaudeAPIError as e:
            if "invalid" in str(e).lower() or "sessionkey" in str(e).lower():
                raise  # Auth error, don't retry with another backend
            log.warning(f"curl_cffi failed: {e}, trying PowerShell...")

        try:
            result = _powershell_request(url, self.session_key)
            self._backend = "powershell"
            log.info("Using PowerShell backend (Windows native TLS)")
            return result
        except ClaudeAPIError:
            raise
        except Exception as e:
            raise ClaudeAPIError(f"All backends failed. Last error: {e}")

    def get_organizations(self) -> list[dict]:
        """Fetch the list of organizations for the authenticated user."""
        return self._request(f"{API_BASE}/organizations")

    def get_usage(self, org_id: str) -> dict:
        """Fetch raw usage data for a specific organization."""
        return self._request(f"{API_BASE}/organizations/{org_id}/usage")

    def fetch_all(self, org_id: str) -> dict:
        """Fetch and parse usage into a structured result."""
        raw = self.get_usage(org_id)
        return _parse_usage(raw)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_utilization(value) -> float:
    """Parse utilization value (0.0-1.0) from API, handling int/float/string."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _parse_reset_time(value) -> datetime | None:
    """Parse reset timestamp from API (ISO 8601 string or Unix timestamp)."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_usage(raw: dict) -> dict:
    """Parse raw API response into structured usage data with percentages."""
    result = {}
    for key, label in [
        ("five_hour", "5h Session"),
        ("seven_day", "7d Weekly"),
    ]:
        period = raw.get(key, {})
        if period is None:
            period = {}
        utilization = _parse_utilization(period.get("utilization"))
        reset_time = _parse_reset_time(period.get("resets_at"))
        result[key] = {
            "label": label,
            "percentage": round(utilization * 100, 1) if utilization <= 1.0 else round(utilization, 1),
            "reset_time": reset_time,
        }
    return result
