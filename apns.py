"""Token-based (.p8) APNs push — wakes the GoSancho app to fetch /v1/results.

HTTP/2 via httpx (APNs rejects HTTP/1.1); ES256 provider JWT via pyjwt. One
team-scoped key, no per-app cert. The push carries a silent wake signal
(content-available) plus the task id; the app fetches the real text itself."""
from __future__ import annotations
import time
from pathlib import Path

import httpx
import jwt

# Provider JWTs are valid up to 60 min; cache and refresh well before expiry.
_jwt_cache: dict = {"token": None, "iat": 0}


def _provider_jwt(key_path: str, key_id: str, team_id: str) -> str:
    now = int(time.time())
    if _jwt_cache["token"] and now - _jwt_cache["iat"] < 2400:  # reuse < 40 min old
        return _jwt_cache["token"]
    key_pem = Path(key_path).read_text()
    token = jwt.encode({"iss": team_id, "iat": now}, key_pem,
                       algorithm="ES256", headers={"kid": key_id})
    _jwt_cache.update(token=token, iat=now)
    return token


def send_push(device_token: str, *, key_path: str, key_id: str, team_id: str,
              topic: str, sandbox: bool, payload: dict | None = None,
              push_type: str = "background") -> tuple[int, str]:
    """POST a push to one device token. Returns (status, reason). 200 == accepted.
    `background` = silent wake (content-available); `alert` = visible."""
    host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
    headers = {
        "authorization": f"bearer {_provider_jwt(key_path, key_id, team_id)}",
        "apns-topic": topic,
        "apns-push-type": push_type,
        "apns-priority": "5" if push_type == "background" else "10",
    }
    body = payload or {"aps": {"content-available": 1}}
    with httpx.Client(http2=True, timeout=10) as client:
        r = client.post(f"https://{host}/3/device/{device_token}", headers=headers, json=body)
    if r.status_code == 200:
        return 200, ""
    try:
        return r.status_code, r.json().get("reason", "")
    except Exception:
        return r.status_code, r.text[:120]
