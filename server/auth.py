# Copyright (c) 2025
#
# This file implements an Epic Games Device Authentication helper for a Fortnite bot
# web interface. The implementation conceptually mirrors commonly available
# device-auth generators used by the Fortnite community.
#
# License Notice (Commons Clause + Apache 2.0 Modified)
#
# The logic contained in this module is an independent reimplementation informed by
# documentation and publicly available examples of Epic's OAuth device flow.
# To respect ecosystem licensing norms, this module is distributed under the
# Apache License 2.0 with the Commons Clause restriction as commonly used by
# similar device-auth generation tools. See the repository NOTICE file for more
# details.
#
# You may obtain a copy of the Apache 2.0 License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Commons Clause Restriction Notice
# "Commons Clause" License Condition v1.0
#
# The Software is provided to you by the Licensor under the License, as defined
# below, subject to the following condition.
#
# Without limiting other conditions in the License, the grant of rights under the
# License will not include, and the License does not grant to you, the right to
# Sell the Software.
#
# For purposes of the foregoing, “Sell” means practicing any or all of the rights
# granted to you under the License to provide to third parties, for a fee or other
# consideration (including without limitation fees for hosting or consulting/
# support services related to the Software), a product or service whose value
# derives, entirely or substantially, from the functionality of the Software. Any
# license notice or attribution required by the License must also include this
# Commons Clause License Condition notice.
#
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an “AS IS” BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied.

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp


# Endpoints and constants for Epic OAuth
OAUTH_BASE = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth"
DEVICE_AUTHZ_ENDPOINT = f"{OAUTH_BASE}/deviceAuthorization"
TOKEN_ENDPOINT = f"{OAUTH_BASE}/token"
ACCOUNT_BASE = "https://account-public-service-prod.ol.epicgames.com/account/api"

# Default Basic tokens (base64(client_id:client_secret)) for Epic OAuth clients.
# These values are widely circulated in the Fortnite device-auth community.
# If you prefer, you can override them with environment variables below.
#
# Environment overrides:
#   EPIC_SWITCH_BASIC   = base64(client_id:client_secret) for the Switch client
#   EPIC_ANDROID_BASIC  = base64(client_id:client_secret) for the Android client
#
# Fallback order: SWITCH -> ANDROID
# If the first attempt fails, the flow will automatically try the next.
#
# Note: We intentionally do not embed redirect/callback URLs. The device-code
# flow is callback-less and safe for localhost usage.
DEFAULT_SWITCH_BASIC_B64: str | None = None  # set to None to prefer ANDROID default unless provided via env
# Known public default for the Fortnite Android Game Client
DEFAULT_ANDROID_BASIC_B64: str | None = (
    "ZWM2ODRiOGM2ODdmNDc5ZmFkZWEzY2IyYWQ4M2Y1YzY6OWE0OWYxMjQ1NWVhNGI4YTk2ZjRjZTZmYmFiYzIzZjk="
)


@dataclass
class EpicUser:
    account_id: str
    display_name: str
    email: Optional[str] = None


@dataclass
class DeviceAuth:
    device_id: str
    account_id: str
    secret: str


@dataclass
class AuthStatus:
    authenticated: bool
    pending: bool
    error: Optional[str]
    user: Optional[EpicUser]
    last_event: Optional[str] = None


class AuthManager:
    """Manages the Epic Device Auth flow and persisted device auth records.

    - Obtains device codes using SWITCH/ANDROID OAuth clients (Basic auth),
      falling back automatically.
    - Polls token endpoint until the user approves the device code.
    - Creates and persists a DeviceAuth record (AdvancedAuth-compatible shape).

    Unit-friendly: you can inject a session factory and a sleep function to
    ease testing.
    """

    def __init__(
        self,
        storage_path: Path,
        *,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_factory = session_factory
        self._sleep = sleep or asyncio.sleep

        # Runtime state
        self._status = AuthStatus(authenticated=False, pending=False, error=None, user=None)
        self._pending_device_code: Optional[str] = None
        self._verification_uri_complete: Optional[str] = None
        self._stop_poll = False
        self._used_platform: Optional[str] = None  # 'SWITCH' | 'ANDROID'

        # Tokens during flow
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None

    @property
    def verification_uri_complete(self) -> Optional[str]:
        return self._verification_uri_complete

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._session_factory is not None:
                self._session = self._session_factory()  # type: ignore[assignment]
            else:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def status(self) -> Dict[str, Any]:
        s = self._status
        return {
            "authenticated": s.authenticated,
            "pending": s.pending,
            "error": s.error,
            "user": asdict(s.user) if s.user else None,
            "verification_uri_complete": self._verification_uri_complete if s.pending else None,
            "last_event": s.last_event,
        }

    async def logout(self) -> None:
        async with self._lock:
            self._status = AuthStatus(authenticated=False, pending=False, error=None, user=None, last_event="logout")
            self._pending_device_code = None
            self._verification_uri_complete = None
            self._access_token = None
            self._refresh_token = None
            self._stop_poll = True
            self._used_platform = None

    def _resolve_basic_candidates(self) -> List[Tuple[str, str]]:
        """Return a list of (PLATFORM, basic_b64) candidates to try in order."""
        out: List[Tuple[str, str]] = []
        env_switch = (os.getenv("EPIC_SWITCH_BASIC", "") or "").strip()
        env_android = (os.getenv("EPIC_ANDROID_BASIC", "") or "").strip()

        if env_switch:
            b64 = env_switch.split()[-1] if " " in env_switch else env_switch
            out.append(("SWITCH", b64))
        elif DEFAULT_SWITCH_BASIC_B64:
            out.append(("SWITCH", DEFAULT_SWITCH_BASIC_B64))

        if env_android:
            b64 = env_android.split()[-1] if " " in env_android else env_android
            out.append(("ANDROID", b64))
        elif DEFAULT_ANDROID_BASIC_B64:
            out.append(("ANDROID", DEFAULT_ANDROID_BASIC_B64))

        return out

    async def _request_device_code(self, session: aiohttp.ClientSession, basic_b64: str) -> Dict[str, Any]:
        headers = {"Authorization": f"basic {basic_b64}", "Content-Type": "application/x-www-form-urlencoded"}
        # Keep scope minimal and avoid redirect URIs; device flow is callback-less and localhost-safe
        data = {"prompt": "login", "scope": "basic_profile"}
        async with session.post(DEVICE_AUTHZ_ENDPOINT, headers=headers, data=data) as resp:
            payload: Dict[str, Any]
            try:
                payload = await resp.json()
            except Exception:
                payload = {"error": f"http_{resp.status}", "raw": await resp.text()}
        return payload

    async def start(self) -> Dict[str, Any]:
        """Start the device code flow.

        Returns a dict containing the verification URL for the user to visit.
        """
        async with self._lock:
            self._stop_poll = False
            self._status = AuthStatus(authenticated=False, pending=True, error=None, user=None, last_event="start")

        session = await self._get_session()

        # Try candidates in order (SWITCH -> ANDROID). On error, fall back.
        candidates = self._resolve_basic_candidates()
        if not candidates:
            async with self._lock:
                self._status.error = (
                    "Missing Basic credentials. Set EPIC_SWITCH_BASIC/EPIC_ANDROID_BASIC or use built-in defaults."
                )
                self._status.pending = False
                self._status.last_event = "start_failed"
            return {"error": self._status.error}

        device_code: Optional[str] = None
        verification_uri_complete: Optional[str] = None
        used_basic: Optional[str] = None
        used_platform: Optional[str] = None

        for platform, basic_b64 in candidates:
            payload = await self._request_device_code(session, basic_b64)
            device_code = payload.get("device_code")
            verification_uri_complete = payload.get("verification_uri_complete") or payload.get("verification_uri")
            err = payload.get("error")
            if device_code and verification_uri_complete and not err:
                used_basic = basic_b64
                used_platform = platform
                break
            # If authorization server says invalid_client, fall through and try next
            # Record the last error in status for visibility
            async with self._lock:
                self._status.error = payload.get("error_description") or payload.get("error") or "device_code_error"
                self._status.last_event = f"device_code_error_{platform.lower()}"
        
        if not device_code or not verification_uri_complete or not used_basic or not used_platform:
            async with self._lock:
                self._status.pending = False
                if not self._status.error:
                    self._status.error = "Device code response incomplete"
            return {"error": self._status.error}

        async with self._lock:
            self._pending_device_code = device_code
            self._verification_uri_complete = verification_uri_complete
            self._status.error = None
            self._status.last_event = "device_code_acquired"
            self._used_platform = used_platform

        # Fire-and-forget poller
        asyncio.create_task(self._poll_for_completion(used_basic))

        return {"verification_uri_complete": verification_uri_complete}

    async def _poll_for_completion(self, basic_b64: str) -> None:
        # Poll token endpoint with device_code to exchange for an access token
        session = await self._get_session()
        headers = {"Authorization": f"basic {basic_b64}", "Content-Type": "application/x-www-form-urlencoded"}

        while True:
            await self._sleep(2.0)
            async with self._lock:
                if self._stop_poll:
                    return
                device_code = self._pending_device_code
            if not device_code:
                return
            data = {"grant_type": "device_code", "device_code": device_code}
            async with session.post(TOKEN_ENDPOINT, headers=headers, data=data) as resp:
                try:
                    token_payload = await resp.json()
                except Exception:
                    token_payload = {"error": f"http_{resp.status}"}

            error = token_payload.get("error")
            if error in ("authorization_pending", "slow_down"):
                async with self._lock:
                    self._status.last_event = "authorization_pending"
                continue
            if error:
                async with self._lock:
                    self._status.error = f"Device flow failed: {error}"
                    self._status.pending = False
                    self._pending_device_code = None
                    self._status.last_event = "device_flow_failed"
                return

            # Success: capture access/refresh tokens
            access_token = token_payload.get("access_token")
            refresh_token = token_payload.get("refresh_token")
            account_id = token_payload.get("account_id")
            display_name = token_payload.get("display_name") or token_payload.get("displayName")
            if not access_token or not account_id:
                async with self._lock:
                    self._status.error = "Token payload missing fields"
                    self._status.pending = False
                    self._pending_device_code = None
                    self._status.last_event = "token_payload_invalid"
                return

            # Fetch user details (email/display name) if possible
            email: Optional[str] = None
            try:
                details = await self._fetch_user_details(access_token, account_id)
                email = details.get("email")
                display_name = display_name or details.get("displayName") or details.get("display_name")
            except Exception:
                pass

            async with self._lock:
                self._access_token = access_token
                self._refresh_token = refresh_token
                self._status.user = EpicUser(account_id=account_id, display_name=(display_name or account_id), email=email)
                self._status.last_event = "device_flow_completed"

            # Create DeviceAuth record for the account
            try:
                dev_auth = await self._create_device_auth(access_token, account_id)
                await self._persist_device_auth(dev_auth, display_name=(display_name or account_id), email=email)
                async with self._lock:
                    self._status.authenticated = True
                    self._status.pending = False
                    self._pending_device_code = None
                    self._status.last_event = "device_auth_created"
            except Exception as e:
                async with self._lock:
                    self._status.error = f"Failed to create/persist device auth: {e}"
                    self._status.pending = False
                    self._pending_device_code = None
                    self._status.last_event = "device_auth_failed"
            return

    async def _fetch_user_details(self, access_token: str, account_id: str) -> Dict[str, Any]:
        session = await self._get_session()
        headers = {"Authorization": f"bearer {access_token}"}
        url = f"{ACCOUNT_BASE}/public/account/{account_id}"
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return {}
            return await resp.json()

    async def _create_device_auth(self, access_token: str, account_id: str) -> DeviceAuth:
        session = await self._get_session()
        headers = {"Authorization": f"bearer {access_token}", "Content-Type": "application/json"}
        url = f"{ACCOUNT_BASE}/public/account/{account_id}/deviceAuth"
        async with session.post(url, headers=headers, json={}) as resp:
            if resp.status != 200 and resp.status != 201:
                text = await resp.text()
                raise RuntimeError(f"Device auth create failed: {resp.status} {text}")
            data = await resp.json()
        device_id = data.get("deviceId") or data.get("device_id")
        secret = data.get("secret")
        if not device_id or not secret:
            raise RuntimeError("Device auth response missing fields")
        return DeviceAuth(device_id=device_id, account_id=account_id, secret=secret)

    async def _persist_device_auth(
        self,
        dev_auth: DeviceAuth,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        """Persist to device_auths.json keyed by email (if available) or account_id.

        The stored payload is AdvancedAuth-compatible (camelCase keys).
        """
        path = self._storage_path
        try:
            if path.exists():
                content = json.loads(path.read_text("utf-8"))
            else:
                content = {}
        except Exception:
            content = {}

        key = (email or dev_auth.account_id).lower() if email else dev_auth.account_id
        record = {
            "accountId": dev_auth.account_id,
            "deviceId": dev_auth.device_id,
            "secret": dev_auth.secret,
            "displayName": display_name,
            "email": email,
            "platform": self._used_platform,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        content[key] = record

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(content, indent=2), encoding="utf-8")
        tmp.replace(path)


__all__ = ["AuthManager", "EpicUser", "DeviceAuth", "AuthStatus"]
