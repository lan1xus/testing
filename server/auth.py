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
import base64
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp


# Endpoints and constants for Epic OAuth
OAUTH_BASE = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth"
DEVICE_AUTHZ_ENDPOINT = f"{OAUTH_BASE}/deviceAuthorization"
TOKEN_ENDPOINT = f"{OAUTH_BASE}/token"
KILL_SESSION_ENDPOINT = f"{OAUTH_BASE}/sessions/kill"
ACCOUNT_BASE = "https://account-public-service-prod.ol.epicgames.com/account/api"


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

    This class is designed for server-side use. It keeps minimal state so the web
    UI can show live auth status.
    """

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

        # Runtime state
        self._status = AuthStatus(authenticated=False, pending=False, error=None, user=None)
        self._pending_device_code: Optional[str] = None
        self._verification_uri_complete: Optional[str] = None
        self._stop_poll = False

        # Tokens during flow
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None

    @property
    def verification_uri_complete(self) -> Optional[str]:
        return self._verification_uri_complete

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
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
        }

    async def logout(self) -> None:
        async with self._lock:
            self._status = AuthStatus(authenticated=False, pending=False, error=None, user=None)
            self._pending_device_code = None
            self._verification_uri_complete = None
            self._access_token = None
            self._refresh_token = None
            self._stop_poll = True

    async def start(self) -> Dict[str, Any]:
        """Start the device code flow.

        Returns a dict containing the verification URL for the user to visit.
        """
        async with self._lock:
            self._stop_poll = False
            self._status = AuthStatus(authenticated=False, pending=True, error=None, user=None, last_event="start")

        session = await self._get_session()

        # Obtain device code using client credentials (token id/secret must be provided)
        # Environment variables:
        #   EPIC_CLIENT_ID, EPIC_CLIENT_SECRET specify which OAuth client to use.
        client_id = os.getenv("EPIC_CLIENT_ID", "")
        client_secret = os.getenv("EPIC_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            async with self._lock:
                self._status.error = "Missing EPIC_CLIENT_ID/EPIC_CLIENT_SECRET"
                self._status.pending = False
            return {"error": self._status.error}

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {"Authorization": f"basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"prompt": "login", "scope": "basic_profile"}
        async with session.post(DEVICE_AUTHZ_ENDPOINT, headers=headers, data=data) as resp:
            if resp.status != 200:
                text = await resp.text()
                async with self._lock:
                    self._status.error = f"Device code request failed: {resp.status} {text}"
                    self._status.pending = False
                return {"error": self._status.error}
            payload = await resp.json()

        device_code = payload.get("device_code")
        verification_uri_complete = payload.get("verification_uri_complete") or payload.get("verification_uri")
        if not device_code or not verification_uri_complete:
            async with self._lock:
                self._status.error = "Device code response incomplete"
                self._status.pending = False
            return {"error": self._status.error}

        async with self._lock:
            self._pending_device_code = device_code
            self._verification_uri_complete = verification_uri_complete
            self._status.last_event = "device_code_acquired"

        # Fire-and-forget poller
        asyncio.create_task(self._poll_for_completion(client_id, client_secret))

        return {"verification_uri_complete": verification_uri_complete}

    async def _poll_for_completion(self, client_id: str, client_secret: str) -> None:
        # Poll token endpoint with device_code to exchange for an access token
        session = await self._get_session()
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {"Authorization": f"basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}

        while True:
            await asyncio.sleep(2.0)
            async with self._lock:
                if self._stop_poll:
                    return
                device_code = self._pending_device_code
            if not device_code:
                return
            data = {"grant_type": "device_code", "device_code": device_code}
            async with session.post(TOKEN_ENDPOINT, headers=headers, data=data) as resp:
                # 200 -> success, 400 with specific error -> authorization_pending
                if resp.status == 200:
                    token_payload = await resp.json()
                else:
                    try:
                        token_payload = await resp.json()
                    except Exception:
                        token_payload = {"error": f"http_{resp.status}"}

            error = token_payload.get("error")
            if error in ("authorization_pending", "slow_down"):
                continue
            if error:
                async with self._lock:
                    self._status.error = f"Device flow failed: {error}"
                    self._status.pending = False
                    self._pending_device_code = None
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
                return

            async with self._lock:
                self._access_token = access_token
                self._refresh_token = refresh_token
                self._status.user = EpicUser(account_id=account_id, display_name=display_name or account_id)
                self._status.last_event = "device_flow_completed"

            # Create DeviceAuth record for the account
            try:
                dev_auth = await self._create_device_auth(access_token, account_id)
                await self._persist_device_auth(dev_auth)
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
            return

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

    async def _persist_device_auth(self, dev_auth: DeviceAuth) -> None:
        # Persist to device_auths.json keyed by account_id
        path = self._storage_path
        try:
            if path.exists():
                content = json.loads(path.read_text("utf-8"))
            else:
                content = {}
        except Exception:
            content = {}
        # Use account_id as key; optionally include email later if available
        content[dev_auth.account_id] = {
            "account_id": dev_auth.account_id,
            "device_id": dev_auth.device_id,
            "secret": dev_auth.secret,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(content, indent=2), encoding="utf-8")
        tmp.replace(path)


__all__ = ["AuthManager", "EpicUser", "DeviceAuth", "AuthStatus"]
