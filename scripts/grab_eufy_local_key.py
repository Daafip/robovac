#!/usr/bin/env python3
"""Grab eufy device IDs and local keys for local (Home Assistant / tinytuya) use.

eufy vacuums are Tuya devices. To talk to one directly on the LAN (protocol 3.3
/ 3.4 / 3.5) you need its ``devId`` and ``localKey``. Those secrets live in the
Tuya cloud, tied to your eufy account. This script logs in with your eufy
credentials, walks the eufy -> Tuya auth handshake, and prints every device's
name, ``devId`` and ``localKey`` -- the values Phase B's ``t2266_dump.py`` (and
the robovac integration's config entry) need.

Algorithm ported from the open-source key grabber cited in the plan:
https://github.com/Rjevski/eufy-clean-local-key-grabber (MIT). The client
IDs/secrets below are the eufy Android app's own public constants, not personal
secrets.

Usage
-----
Credentials are read from the environment by default so they never land in your
shell history::

    export EUFY_EMAIL='you@example.com'
    export EUFY_PASSWORD='...'
    uv run python scripts/grab_eufy_local_key.py

Or pass them explicitly (``--password -`` prompts without echoing)::

    uv run python scripts/grab_eufy_local_key.py --email you@example.com --password -

Handy flags::

    --json                 dump raw device records (all fields) as JSON
    --emit-env [MATCH]     print ready-to-source `export ROBOVAC_*` lines for the
                           device whose name/id contains MATCH (default: the first
                           device that looks like a vacuum)

SECURITY: the printed localKey is a device secret. Do not commit it or paste it
anywhere public. It rotates whenever the vacuum is re-paired to Wi-Fi.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import random
import string
import sys
import time
import uuid
from getpass import getpass
from hashlib import md5, sha256
from typing import Any
from urllib.parse import urljoin

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# --------------------------------------------------------------------------- #
# Constants (eufy Android app public values)                                   #
# --------------------------------------------------------------------------- #
EUFY_CLIENT_ID = "eufyhome-app"
EUFY_CLIENT_SECRET = "GQCpr9dSp3uQpsOMgJ4xQ"
EUFY_BASE_URL = "https://home-api.eufylife.com/v1/"

PLATFORM = "sdk_gphone64_arm64"
LANGUAGE = "en"
TIMEZONE = "Europe/London"

TUYA_CLIENT_ID = "yx5v9uc3ef9wg3v9atje"
TUYA_INITIAL_BASE_URL = "https://a1.tuyaeu.com"

APPSECRET = "s8x78u7xwymasd9kqa7a73pjhxqsedaj"
BMP_SECRET = "cepev5pfnhua4dkqkdpmnrdxx378mpjr"
EUFY_HMAC_KEY = f"A_{BMP_SECRET}_{APPSECRET}".encode("utf-8")

# AES-CBC key/IV used to pre-encrypt the Tuya "uid" into a password
TUYA_PASSWORD_KEY = bytes([36, 78, 109, 138, 86, 172, 135, 145, 36, 67, 45, 139, 108, 188, 162, 196])
TUYA_PASSWORD_IV = bytes([119, 36, 86, 242, 167, 102, 76, 243, 57, 44, 53, 151, 233, 62, 87, 71])

DEFAULT_EUFY_HEADERS = {
    "User-Agent": "EufyHome-Android-2.4.0",
    "timezone": TIMEZONE,
    "category": "Home",
    "token": "",
    "uid": "",
    "openudid": PLATFORM,
    "clientType": "2",
    "language": LANGUAGE,
    "country": "US",
    "Accept-Encoding": "gzip",
}

DEFAULT_TUYA_HEADERS = {"User-Agent": "TY-UA=APP/Android/2.4.0/SDK/null"}

SIGNATURE_RELEVANT_PARAMETERS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid", "isH5",
    "h5Token", "os", "clientId", "postData", "time", "requestId", "et", "n4h5",
    "sid", "sp",
}

DEFAULT_TUYA_QUERY_PARAMS = {
    "appVersion": "2.4.0",
    "deviceId": "",
    "platform": PLATFORM,
    "clientId": TUYA_CLIENT_ID,
    "lang": LANGUAGE,
    "osSystem": "12",
    "os": "Android",
    "timeZoneId": TIMEZONE,
    "ttid": "android",
    "et": "0.0.1",
    "sdkVersion": "3.0.8cAnker",
}

TUYA_PASSWORD_CIPHER = Cipher(algorithms.AES(TUYA_PASSWORD_KEY), modes.CBC(TUYA_PASSWORD_IV))


# --------------------------------------------------------------------------- #
# Crypto helpers                                                               #
# --------------------------------------------------------------------------- #
def unpadded_rsa(key_exponent: int, key_n: int, plaintext: bytes) -> bytes:
    """Textbook (no-padding) RSA, as the Tuya login expects."""
    keylength = math.ceil(key_n.bit_length() / 8)
    input_nr = int.from_bytes(plaintext, byteorder="big")
    crypted_nr = pow(input_nr, key_exponent, key_n)
    return crypted_nr.to_bytes(keylength, byteorder="big")


def shuffled_md5(value: str) -> str:
    """Tuya's shuffled MD5 used when signing postData."""
    _hash = md5(value.encode("utf-8")).hexdigest()
    return _hash[8:16] + _hash[0:8] + _hash[24:32] + _hash[16:24]


# --------------------------------------------------------------------------- #
# eufy Home cloud session                                                      #
# --------------------------------------------------------------------------- #
class EufyHomeSession:
    """Logs into the eufy Home cloud to read the account's user info."""

    def __init__(self, email: str, password: str) -> None:
        self.session = requests.session()
        self.session.headers = DEFAULT_EUFY_HEADERS.copy()
        self.base_url = EUFY_BASE_URL
        self.email = email
        self.password = password
        self.user_info: dict[str, Any] = {}

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path)

    def login(self) -> None:
        resp = self.session.post(
            self.url("user/email/login"),
            json={
                "client_Secret": EUFY_CLIENT_SECRET,
                "client_id": EUFY_CLIENT_ID,
                "email": self.email,
                "password": self.password,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"eufy login failed: {data.get('message', data)}")

        self.user_info = data["user_info"]
        self.session.headers["uid"] = self.user_info["id"]
        self.session.headers["token"] = data["access_token"]
        # request_host is the account's regional host; it no longer carries the
        # "/v1/" path prefix, so build subsequent URLs from it explicitly.
        self.base_url = self.user_info["request_host"].rstrip("/") + "/v1/"

    def get_user_info(self) -> dict[str, Any]:
        """Return the account's user info (from the login response -- no extra call)."""
        if not self.user_info:
            self.login()
        return self.user_info


# --------------------------------------------------------------------------- #
# Tuya cloud session (holds the local keys)                                    #
# --------------------------------------------------------------------------- #
class TuyaAPISession:
    """Signed Tuya mobile-API session that exposes devId + localKey."""

    def __init__(self, username: str, country_code: str) -> None:
        self.session = requests.session()
        self.session.headers = DEFAULT_TUYA_HEADERS.copy()
        self.default_query_params = DEFAULT_TUYA_QUERY_PARAMS.copy()
        self.default_query_params["deviceId"] = self.generate_new_device_id()
        self.username = username
        self.country_code = country_code
        self.session_id: str | None = None
        self.base_url = TUYA_INITIAL_BASE_URL

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path)

    @staticmethod
    def generate_new_device_id() -> str:
        expected_length = 44
        base64_characters = string.ascii_letters + string.digits
        prefix = "8534c8ec0ed0"  # brand/model-derived part, faked to a common device
        return prefix + "".join(
            random.choice(base64_characters) for _ in range(expected_length - len(prefix))
        )

    @staticmethod
    def encode_post_data(data: dict[str, Any] | None) -> str:
        return json.dumps(data, separators=(",", ":")) if data else ""

    @staticmethod
    def get_signature(query_params: dict[str, str], encoded_post_data: str) -> str:
        params = query_params.copy()
        if encoded_post_data:
            params["postData"] = encoded_post_data
        sorted_pairs = sorted(params.items())
        filtered = filter(lambda p: p[0] and p[0] in SIGNATURE_RELEVANT_PARAMETERS, sorted_pairs)
        mapped = map(
            lambda p: p[0] + "=" + (shuffled_md5(p[1]) if p[0] == "postData" else p[1]),
            filtered,
        )
        message = "||".join(mapped)
        return hmac.HMAC(key=EUFY_HMAC_KEY, msg=message.encode("utf-8"), digestmod=sha256).hexdigest()

    def _request(
        self,
        action: str,
        version: str = "1.0",
        data: dict[str, Any] | None = None,
        query_params: dict[str, str] | None = None,
        requires_session: bool = True,
    ) -> Any:
        if not self.session_id and requires_session:
            self.acquire_session()

        extra = {
            "time": str(int(time.time())),
            "requestId": str(uuid.uuid4()),
            "a": action,
            "v": version,
            **(query_params or {}),
        }
        params = {**self.default_query_params, **extra}
        encoded_post_data = self.encode_post_data(data)

        resp = self.session.post(
            self.url("/api.json"),
            params={**params, "sign": self.get_signature(params, encoded_post_data)},
            data={"postData": encoded_post_data} if encoded_post_data else None,
        )
        resp.raise_for_status()
        body = resp.json()
        if "result" not in body:
            raise RuntimeError(f"Tuya API error for '{action}': {body}")
        return body["result"]

    def determine_password(self, username: str) -> str:
        padded_size = 16 * math.ceil(len(username) / 16)
        password_uid = username.zfill(padded_size)
        encryptor = TUYA_PASSWORD_CIPHER.encryptor()
        encrypted = encryptor.update(password_uid.encode("utf8")) + encryptor.finalize()
        return md5(encrypted.hex().upper().encode("utf-8")).hexdigest()

    def request_token(self) -> dict[str, Any]:
        return self._request(
            action="tuya.m.user.uid.token.create",
            data={"uid": self.username, "countryCode": self.country_code},
            requires_session=False,
        )

    def acquire_session(self) -> None:
        token_response = self.request_token()
        encrypted_password = unpadded_rsa(
            key_exponent=int(token_response["exponent"]),
            key_n=int(token_response["publicKey"]),
            plaintext=self.determine_password(self.username).encode("utf-8"),
        )
        session_response = self._request(
            action="tuya.m.user.uid.password.login.reg",
            data={
                "uid": self.username,
                "createGroup": True,
                "ifencrypt": 1,
                "passwd": encrypted_password.hex(),
                "countryCode": self.country_code,
                "options": '{"group": 1}',
                "token": token_response["token"],
            },
            requires_session=False,
        )
        self.session_id = self.default_query_params["sid"] = session_response["sid"]
        self.base_url = session_response["domain"]["mobileApiUrl"]

    def list_homes(self) -> list[dict[str, Any]]:
        return self._request(action="tuya.m.location.list", version="2.1")

    def list_devices(self, home_id: str) -> list[dict[str, Any]]:
        own = self._request(
            action="tuya.m.my.group.device.list",
            version="1.0",
            query_params={"gid": home_id},
        )
        shared = self._request(action="tuya.m.my.shared.device.list", version="1.0")
        return own + shared


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def collect_devices(email: str, password: str) -> list[dict[str, Any]]:
    eufy = EufyHomeSession(email=email, password=password)
    user_info = eufy.get_user_info()
    tuya = TuyaAPISession(
        username=f"eh-{user_info['id']}",
        country_code=user_info["phone_code"],
    )
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for home in tuya.list_homes():
        for device in tuya.list_devices(home["groupId"]):
            dev_id = device.get("devId")
            if dev_id and dev_id not in seen:
                seen.add(dev_id)
                devices.append(device)
    return devices


def looks_like_vacuum(device: dict[str, Any]) -> bool:
    name = str(device.get("name", "")).lower()
    return any(k in name for k in ("vac", "clean", "robo", "x8", "x9", "x10", "omni"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grab eufy device IDs and local keys from the Tuya cloud."
    )
    parser.add_argument("--email", default=os.environ.get("EUFY_EMAIL"),
                        help="eufy account email (or env EUFY_EMAIL)")
    parser.add_argument("--password", default=os.environ.get("EUFY_PASSWORD"),
                        help="eufy account password (or env EUFY_PASSWORD; '-' to prompt)")
    parser.add_argument("--json", action="store_true",
                        help="print full raw device records as JSON")
    parser.add_argument("--emit-env", nargs="?", const="", metavar="MATCH",
                        help="print `export ROBOVAC_*` lines for the device whose "
                             "name/devId contains MATCH (default: first vacuum-like device)")
    args = parser.parse_args()

    email = args.email
    password = args.password
    if password == "-" or (email and not password):
        password = getpass("eufy password: ")
    if not email or not password:
        parser.error("email and password required (via --email/--password or "
                     "EUFY_EMAIL/EUFY_PASSWORD env vars)")

    try:
        devices = collect_devices(email, password)
    except requests.HTTPError as exc:
        print(f"HTTP error talking to eufy/Tuya: {exc}", file=sys.stderr)
        return 1
    except (KeyError, RuntimeError) as exc:
        print(f"Failed to retrieve devices: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("No devices found on this account.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(devices, indent=2, sort_keys=True))
        return 0

    if args.emit_env is not None:
        match = args.emit_env.lower()
        chosen = None
        for device in devices:
            hay = f"{device.get('name', '')} {device.get('devId', '')}".lower()
            if (match and match in hay) or (not match and looks_like_vacuum(device)):
                chosen = device
                break
        chosen = chosen or devices[0]
        print(f"# {chosen.get('name', '?')}")
        print(f"export ROBOVAC_DEVID='{chosen['devId']}'")
        print(f"export ROBOVAC_KEY='{chosen['localKey']}'")
        return 0

    print(f"Found {len(devices)} device(s):\n")
    for device in devices:
        marker = "  <-- vacuum?" if looks_like_vacuum(device) else ""
        print(f"  name:     {device.get('name', '?')}{marker}")
        print(f"  devId:    {device['devId']}")
        print(f"  localKey: {device['localKey']}")
        if device.get("productId"):
            print(f"  product:  {device['productId']}")
        print()
    print("SECURITY: localKey is a device secret -- do not commit or share it. "
          "It rotates on Wi-Fi re-pairing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
