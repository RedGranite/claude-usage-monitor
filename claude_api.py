"""
Claude API client for fetching usage data.

Supports two authentication modes:

  1. Session Key mode (claude.ai API):
     - Uses sessionKey cookie from browser + curl_cffi to bypass Cloudflare.
     - Endpoints: /api/organizations, /api/organizations/{id}/usage
     - May be blocked by Cloudflare on some machines.

  2. OAuth Token mode (api.anthropic.com — NO Cloudflare):
     - Uses OAuth access_token from Claude Code CLI.
     - Sends a minimal Messages API request and reads rate-limit headers.
     - Headers: anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}
     - Completely bypasses Cloudflare since it hits api.anthropic.com directly.
"""

from __future__ import annotations

import urllib.request
import json
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Session Key mode (claude.ai) — uses curl_cffi for Cloudflare bypass
# ---------------------------------------------------------------------------

API_BASE = "https://claude.ai/api"


class ClaudeAPIError(Exception):
    """Raised when an API call fails (auth error, network issue, etc.)."""
    pass


class ClaudeAPI:
    """
    Client for claude.ai web API using sessionKey cookie.
    Requires curl_cffi for TLS fingerprint impersonation.
    """

    _FINGERPRINTS = ("chrome136", "chrome131", "chrome124", "chrome120", "chrome")

    def __init__(self, session_key: str):
        from curl_cffi import requests

        self.session_key = session_key
        self._fp_used = "unknown"

        for fp in self._FINGERPRINTS:
            try:
                self.session = requests.Session(impersonate=fp)
                self._fp_used = fp
                break
            except Exception:
                continue

        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
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
        self.session.cookies.set("sessionKey", session_key, domain="claude.ai")

    def _check_response(self, resp, action: str):
        if resp.status_code in (401, 403):
            try:
                data = resp.json()
                msg = data.get("error", {}).get("message", "")
            except Exception:
                msg = ""
            if "invalid" in msg.lower() or "session" in msg.lower():
                raise ClaudeAPIError("Invalid sessionKey - please update your key")
            raise ClaudeAPIError(f"{action}: HTTP {resp.status_code} - {msg or resp.text[:100]}")
        if resp.status_code != 200:
            raise ClaudeAPIError(f"{action}: HTTP {resp.status_code}")
        ct = resp.headers.get("content-type", "")
        if "application/json" not in ct:
            raise ClaudeAPIError(
                f"{action}: Cloudflare blocked the request "
                f"(fingerprint={self._fp_used}). "
                "Try switching to OAuth Token mode in settings."
            )

    def _warmup(self):
        try:
            self.session.get("https://claude.ai/", timeout=10)
        except Exception:
            pass

    def get_organizations(self) -> list[dict]:
        self._warmup()
        resp = self.session.get(f"{API_BASE}/organizations", timeout=30)
        self._check_response(resp, "Fetch organizations")
        return resp.json()

    def get_usage(self, org_id: str) -> dict:
        resp = self.session.get(f"{API_BASE}/organizations/{org_id}/usage", timeout=30)
        self._check_response(resp, "Fetch usage")
        return resp.json()

    def fetch_all(self, org_id: str) -> dict:
        raw = self.get_usage(org_id)
        return _parse_usage(raw)


# ---------------------------------------------------------------------------
# OAuth Token mode (api.anthropic.com) — NO Cloudflare
# ---------------------------------------------------------------------------

MESSAGES_API = "https://api.anthropic.com/v1/messages"


class OAuthAPI:
    """
    Client that uses an OAuth access_token (from Claude Code CLI) to
    fetch usage data via the Messages API rate-limit headers.

    This completely bypasses Cloudflare since it hits api.anthropic.com.
    It sends a minimal 1-token request to claude-haiku-4-5 and reads
    the rate-limit headers from the response.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token

    def fetch_all(self, org_id: str = "") -> dict:
        """
        Send a minimal Messages API request and parse rate-limit headers.
        org_id is ignored (kept for interface compatibility).
        """
        body = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()

        req = urllib.request.Request(MESSAGES_API, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.access_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "claude-code/2.1.5")
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("anthropic-beta", "oauth-2025-04-20")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                headers = resp.headers
        except urllib.error.HTTPError as e:
            # 4xx/5xx still have headers we can read
            if e.code == 401:
                raise ClaudeAPIError("Invalid OAuth token - please update your token")
            headers = e.headers
            if headers is None:
                raise ClaudeAPIError(f"Messages API: HTTP {e.code}")
        except Exception as e:
            raise ClaudeAPIError(f"Messages API request failed: {e}")

        # Parse rate-limit headers into the same format as session key mode
        result = {}
        for key, label, h_prefix in [
            ("five_hour",  "5h Session", "anthropic-ratelimit-unified-5h"),
            ("seven_day",  "7d Weekly",  "anthropic-ratelimit-unified-7d"),
        ]:
            util_str = headers.get(f"{h_prefix}-utilization", "")
            reset_str = headers.get(f"{h_prefix}-reset", "")

            utilization = 0.0
            if util_str:
                try:
                    utilization = float(util_str)
                except (ValueError, TypeError):
                    pass

            reset_time = None
            if reset_str:
                try:
                    reset_time = datetime.fromtimestamp(float(reset_str), tz=timezone.utc)
                except (ValueError, TypeError):
                    try:
                        reset_time = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

            result[key] = {
                "label": label,
                "percentage": round(utilization * 100, 1) if utilization <= 1.0 else round(utilization, 1),
                "reset_time": reset_time,
            }

        # Check if we got any data at all
        if all(v["percentage"] == 0.0 and v["reset_time"] is None for v in result.values()):
            raise ClaudeAPIError("No rate-limit headers received. Token may be invalid.")

        return result


# ---------------------------------------------------------------------------
# Shared parsing helpers (for session key mode)
# ---------------------------------------------------------------------------

def _parse_utilization(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _parse_reset_time(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_usage(raw: dict) -> dict:
    result = {}
    for key, label in [
        ("five_hour", "5h Session"),
        ("seven_day", "7d Weekly"),
        ("seven_day_opus", "7d Opus"),
        ("seven_day_sonnet", "7d Sonnet"),
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
