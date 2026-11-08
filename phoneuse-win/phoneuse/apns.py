"""Apple push notifications for the companion app (docs/COMPANION.md): Live Activity
start, update and end, and the unlock request.

The key comes from ~/.config/phoneuse/apns.json: {"key_id", "team_id", "key_file"}
(a .p8 token key with the Apple Push Notifications service). Pushes go over HTTP/2
to api.push.apple.com, or api.sandbox.push.apple.com for development builds.
"""
import base64
import json
import os
import threading
import time

from .config import config_path

APNS_FILE = os.path.join(os.path.dirname(config_path()), "apns.json")
HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}

_lock = threading.Lock()
_jwt = (None, 0.0)  # Apple accepts a token for an hour; a new one every 50 minutes
_client = None


def configured():
    return os.path.exists(APNS_FILE)


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _token():
    global _jwt
    if _jwt[0] and time.time() - _jwt[1] < 3000:
        return _jwt[0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    with open(APNS_FILE) as f:
        cfg = json.load(f)
    with open(os.path.expanduser(cfg["key_file"]), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), None)
    now = int(time.time())
    head = _b64(json.dumps({"alg": "ES256", "kid": cfg["key_id"]}).encode())
    body = _b64(json.dumps({"iss": cfg["team_id"], "iat": now}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    _jwt = (token, now)
    return token


def send(device_token, payload, topic, push_type, env="production", priority=10):
    """Send one push. Returns (status, reason); status 0 when it never left."""
    global _client
    import httpx
    with _lock:
        try:
            if _client is None:
                _client = httpx.Client(http2=True, timeout=10)
            headers = {"authorization": f"bearer {_token()}", "apns-topic": topic, "apns-push-type": push_type,
                       "apns-priority": str(priority)}
            r = _client.post(f"{HOSTS.get(env, HOSTS['production'])}/3/device/{device_token}",
                             content=json.dumps(payload), headers=headers)
            return r.status_code, (r.json().get("reason", "") if r.content else "")
        except Exception as e:
            _client = None
            return 0, f"{type(e).__name__}: {e}"
