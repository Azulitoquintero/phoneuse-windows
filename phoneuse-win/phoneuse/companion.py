"""The companion app's API (docs/COMPANION.md), served by the browser mirror's
process on the local network (port 8778), so the app can show phoneuse's state,
change its settings, and get a Live Activity while an agent uses the phone.

Only paired apps can use it: every request is signed with the key that pairing
gave the app (HMAC-SHA256, with a time and a nonce, so a recorded request can't be
replayed), and secrets travel sealed with AES-GCM. The page's own routes on
localhost:8777 stay on localhost.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.parse

from aiohttp import web

from . import apns, bridge, phones, unlock
from .config import RUNTIME_DIR, config_path

PORT = 8778
STORE = os.path.join(os.path.dirname(config_path()), "companion.json")
START_RETRY = 600  # seconds between Live Activity start pushes to a device that sent no activity token back
AUDIO_OFF = os.path.join(os.path.dirname(config_path()), "audio-off")  # exists while the mirror's audio is off
UXPLAY_PID = os.path.join(RUNTIME_DIR, "phoneuse-uxplay.pid")  # written by `phoneuse up`
PAIR_TTL = 600
SKEW = 60


def log(*a):
    print(time.strftime("%H:%M:%S"), "companion:", *a, flush=True)


# ---------- the paired apps ----------

def load():
    try:
        with open(STORE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if "pc_id" not in d:
        d["pc_id"] = secrets.token_hex(8)
        save(d)
    d.setdefault("devices", {})
    return d


def save(d):
    os.makedirs(os.path.dirname(STORE), mode=0o700, exist_ok=True)
    tmp = STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, STORE)


def lan_ip():
    """The address that the phone reaches this PC at: the source of the default route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent
        return s.getsockname()[0]
    finally:
        s.close()


def pc_name():
    return socket.gethostname()


def new_pairing():
    """A pairing link that works once, for 10 minutes."""
    d = load()
    key = secrets.token_bytes(32)
    d["pending"] = {"key": key.hex(), "expires": time.time() + PAIR_TTL}
    save(d)
    q = urllib.parse.urlencode({"id": d["pc_id"], "name": pc_name(), "host": lan_ip(), "port": PORT,
                                "key": base64.urlsafe_b64encode(key).rstrip(b"=").decode()})
    return f"phoneuse://pair?{q}"


def _seal_key(key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"phoneuse seal").derive(key)


def unseal(key, sealed, field):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(sealed)
    return AESGCM(_seal_key(key)).decrypt(raw[:12], raw[12:], field.encode()).decode()


def seal(key, text, field):  # for tests and the mock: the app does this side
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(_seal_key(key)).encrypt(nonce, text.encode(), field.encode())).decode()


def signature(key, method, path, t, nonce, body):
    msg = f"{method}\n{path}\n{t}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# ---------- the server ----------

class Companion:
    def __init__(self, mirror, unlocks):
        self.mirror, self.unlocks = mirror, unlocks
        self.nonces = {}  # nonce -> when seen
        self.audio_restarted = 0.0  # when UxPlay was last restarted for an audio change
        self.sent = None  # the Live Activity's last content-state sent: (state, action, steps)
        self.sent_at = 0.0
        self.activity_started = 0.0
        self.start_tried = {}  # device id -> when we last pushed a Live Activity start to it
        self._mirror_since = 0.0

    # ---- auth ----
    @web.middleware
    async def auth(self, req, handler):
        body = await req.read()
        t, nonce, sig = (req.headers.get(h, "") for h in ("X-PU-Time", "X-PU-Nonce", "X-PU-Sig"))
        try:
            if abs(time.time() - int(t)) > SKEW:
                raise ValueError("clock")
        except ValueError:
            return web.json_response({"error": "This phone's clock and the PC's differ; check both."}, status=401)
        now = time.time()
        self.nonces = {n: s for n, s in self.nonces.items() if now - s < 2 * SKEW}
        if not nonce or nonce in self.nonces:
            return web.json_response({"error": "Request was sent twice."}, status=401)
        d = load()
        keys = [(k, bytes.fromhex(dev["key"])) for k, dev in d["devices"].items()]
        pending = d.get("pending")
        if pending and pending["expires"] > now:
            keys.append((None, bytes.fromhex(pending["key"])))
        for dev_id, key in keys:
            if hmac.compare_digest(signature(key, req.method, req.path_qs, t, nonce, body), sig):
                break
        else:
            return web.json_response({"error": "This app isn't paired with the PC. Run `phoneuse app pair`."},
                                     status=401)
        self.nonces[nonce] = now
        if dev_id is None:  # first request with a pairing key: the pairing is used up
            dev_id = hashlib.sha256(key).hexdigest()[:12]
            d["devices"][dev_id] = {"key": key.hex(), "added": time.strftime("%Y-%m-%d %H:%M")}
            d.pop("pending", None)
            save(d)
            log("paired an app:", dev_id)
        req["device"], req["key"] = dev_id, key
        req["json"] = json.loads(body) if body else {}
        return await handler(req)

    # ---- state ----
    def daemon(self, cmd):
        try:
            return self.mirror.get_hid().cmd(cmd)
        except Exception as e:
            self.mirror.hid = None
            return f"err {e}"

    async def run(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

    async def state(self, req):
        m = self.mirror
        st = await self.run(self.daemon, "status")
        b = bridge.saved()
        bridge_state = None
        if b:
            def ask():
                try:
                    c = bridge.resolve(b)
                    return bridge.Link(c).status() if c else {}
                except Exception:
                    return {}
            s = await self.run(ask)
            bridge_state = {"saved": True, "id": b.get("id"), "ip": b.get("ip"), "ssid": s.get("ssid") or b.get("ssid"),
                            "use": b.get("use", True), "online": bool(s.get("online", bool(s))),
                            "rtt_ms": s.get("rtt_ms")}
        p = self.unlocks.pending
        d = load()
        return web.json_response({
            "pc": {"id": d["pc_id"], "name": pc_name(), "version": _version()},
            "mirroring": m.live(),
            "phone": phones.current(),
            "bluetooth": {"connected": "connected=True" in st, "via": "bridge" if "bridge=" in st else "pc"},
            "agent": {"state": m.agent(), "action": m.agent_action, "since": int(m.agent_started) or None,
                      "steps": m.agent_steps},
            "awake": m.awake,
            "audio": {"on": not os.path.exists(AUDIO_OFF), "needs_remirror": self.needs_remirror()},
            "session": {"ask": os.path.exists(self._ask_file()),
                        "pending": ({"id": sp["id"], "since": int(sp["since"]), "action": sp["action"]}
                                    if (sp := self.mirror.sessions.pending) else None)},
            "unlock": {"passcode_saved": await self.run(unlock.saved),
                       "pending": {"id": p["id"], "since": int(p.get("since", 0))} if p else None},
            "bridge": bridge_state,
        })

    @staticmethod
    def _ask_file():
        from .web import ASK_FILE
        return ASK_FILE

    def needs_remirror(self):
        return bool(self.audio_restarted) and not self.mirror.live()

    # ---- actions ----
    async def takeover(self, req):
        self.mirror.take_over(True)
        return web.json_response({"agent": self.mirror.agent()})

    async def resume(self, req):
        self.mirror.take_over(False)
        return web.json_response({"agent": self.mirror.agent()})

    async def unlock_answer(self, req):
        p, b = self.unlocks.pending, req["json"]
        if not p or b.get("id") != p["id"]:
            return web.json_response({"error": "That unlock request is over."}, status=409)
        p["approved"] = bool(b.get("approve"))
        p["event"].set()
        return web.json_response({"ok": True})

    async def session_answer(self, req):
        b = req["json"]
        if not self.mirror.sessions.answer_id(b.get("id"), b.get("approve")):
            return web.json_response({"error": "That request is over."}, status=409)
        return web.json_response({"ok": True})

    async def ask_setting(self, req):
        from .web import set_ask_before_session, ask_before_session
        set_ask_before_session(bool(req["json"].get("on")))
        return web.json_response({"on": ask_before_session()})

    async def passcode(self, req):
        try:
            if req.method == "DELETE":
                await self.run(unlock.forget)
            else:
                try:
                    code = unseal(req["key"], req["json"]["sealed"], "passcode")
                except Exception:
                    raise ValueError("Couldn't read the passcode.")
                await self.run(unlock.save, code)
        except (KeyError, ValueError) as e:
            return web.json_response({"error": str(e) or "Couldn't read the passcode."}, status=400)
        return web.json_response({"passcode_saved": await self.run(unlock.saved)})

    async def awake(self, req):
        self.mirror.set_awake(bool(req["json"].get("on")))
        return web.json_response({"awake": self.mirror.awake})

    async def keyboard(self, req):
        r = await self.run(self.daemon, "eject")
        return web.json_response({"ok": r.startswith("ok")} if r.startswith("ok") else {"error": r}, status=200)

    async def cursor(self, req):
        on = "on" if req["json"].get("on") else "off"
        r = await self.run(lambda: subprocess.run([os.path.join(_root(), "bin", "phoneuse"), "cursor", on],
                                                  capture_output=True, text=True, timeout=40))
        out = (r.stdout or r.stderr).strip().splitlines()
        return web.json_response({"result": out[-1] if out else ""}, status=200 if r.returncode == 0 else 502)

    async def bridge_use(self, req):
        from .bridge_web import use_bridge
        cfg = bridge.saved()
        if not cfg:
            return web.json_response({"error": "No bridge is set up."}, status=409)
        cfg["use"] = bool(req["json"].get("on"))
        bridge.save(cfg)
        await self.run(use_bridge, cfg)
        return web.json_response({"use": cfg["use"]})

    async def bridge_networks(self, req):
        cfg = bridge.saved()
        if not cfg:
            return web.json_response({"error": "No bridge is set up."}, status=409)
        try:
            nets = await self.run(lambda: bridge.Link(bridge.resolve(cfg) or cfg).networks())
        except Exception as e:
            return web.json_response({"error": f"The bridge didn't answer: {e}"}, status=502)
        # The bridge's short keys (s, r, l) become the doc's names.
        nets = [{"ssid": n.get("s", n.get("ssid")), "rssi": n.get("r", n.get("rssi")),
                 "secure": bool(n.get("l", n.get("secure", True)))} for n in nets]
        return web.json_response({"networks": nets})

    async def bridge_network(self, req):
        from .bridge_web import BridgeRoutes
        b = req["json"]
        try:
            password = unseal(req["key"], b["sealed"], "password") if b.get("sealed") else ""
        except Exception:
            return web.json_response({"error": "Couldn't read the password."}, status=400)
        r = await BridgeRoutes(lambda _r: True).change_network(b.get("ssid", ""), password)
        out = json.loads(r.body)
        return web.json_response(out, status=502 if "error" in out else 200)

    async def audio(self, req):
        on = bool(req["json"].get("on"))
        if on == (not os.path.exists(AUDIO_OFF)):
            return web.json_response({"on": on, "needs_remirror": self.needs_remirror()})
        if on:
            os.remove(AUDIO_OFF)
        else:
            open(AUDIO_OFF, "w").close()
        try:
            with open(UXPLAY_PID) as f:
                os.kill(int(f.read()), 15)  # `phoneuse up` starts it again with the new setting
        except (OSError, ValueError):
            return web.json_response({"error": "phoneuse up isn't running UxPlay; the setting applies when it starts."},
                                     status=409)
        self.audio_restarted = time.time()
        log("mirror audio", "on" if on else "off", "- UxPlay restarted; the phone has to mirror again")
        return web.json_response({"on": on, "needs_remirror": True})

    async def push(self, req):
        b, d = req["json"], load()
        dev = d["devices"][req["device"]]
        if b.get("env") and b["env"] != dev.get("env"):
            dev.pop("token_env", None)  # another build: every token's environment is the app's again
        for k in ("bundle_id", "env", "device", "push_to_start", "activity"):
            if b.get(k) and b[k] != dev.get(k):
                dev[k] = b[k]
                dev.get("token_env", {}).pop(k, None)  # a new token: its environment is the app's
        save(d)
        return web.json_response({"ok": True})

    # ---- pushes ----
    def _devices(self):
        return [dev for dev in load()["devices"].values() if dev.get("bundle_id")]

    def _send(self, dev, field, token, payload, topic, push_type, priority=10):
        """Send to one of a device's tokens. A token from another build can belong to the
        other APNs environment (a TestFlight token on a development build): on
        BadDeviceToken, try the other one and remember it for that token."""
        envs = dev.get("token_env", {})
        env = envs.get(field) or dev.get("env", "production")
        r = apns.send(token, payload, topic, push_type, env, priority)
        if r[0] == 400 and "BadDeviceToken" in r[1]:
            other = "production" if env == "sandbox" else "sandbox"
            r2 = apns.send(token, payload, topic, push_type, other, priority)
            if r2[0] == 200:
                d = load()
                for v in d["devices"].values():
                    if v.get(field) == token or (v.get(field) or {}).get("token") == token:
                        v.setdefault("token_env", {})[field] = other
                save(d)
                return r2
        return r

    def push_request_over(self, kind, rid):
        """Tell the app a session or unlock request is over, so it removes its notification."""
        if not apns.configured():
            return
        for dev in self._devices():
            if dev.get("device"):
                r = self._send(dev, "device", dev["device"],
                               {"aps": {"content-available": 1}, "request_over": {"kind": kind, "id": rid}},
                               dev["bundle_id"], "background", priority=5)
                log(f"{kind} request over push:", r)

    def push_session(self, rid, action):
        if not apns.configured():
            return
        for dev in self._devices():
            if dev.get("device"):
                r = self._send(dev, "device", dev["device"], {"aps": {"alert": {"title": "Let an agent use your phone?",
                                                                 "body": f"{action or 'An agent'} wants to start."},
                                                       "category": "SESSION", "sound": "default",
                                                       "interruption-level": "time-sensitive"},
                                              "session_id": rid},
                              dev["bundle_id"], "alert")
                log("session request push:", r)

    def push_unlock(self, rid):
        if not apns.configured():
            return
        for dev in self._devices():
            if dev.get("device"):
                r = self._send(dev, "device", dev["device"], {"aps": {"alert": {"title": "Unlock the iPhone?",
                                                                 "body": "An agent asks to enter your saved passcode."},
                                                       "category": "UNLOCK", "sound": "default"},
                                              "unlock_id": rid},
                              dev["bundle_id"], "alert")
                log("unlock push:", r)

    async def activities(self):
        """Keep one Live Activity on the phone the whole time the phone mirrors to this PC:
        "idle" (the owner is in control), "asking" (an agent waits for the owner's
        Allow, with Allow and Deny buttons), "driving" (with the agent's phase: acting,
        reading or thinking) or "paused" (the owner took over). It ends when mirroring
        stops. An activity the app or iOS ended is started again by push."""
        while True:
            await asyncio.sleep(0.5)
            if not apns.configured():
                continue
            want = self._activity_state()
            urgent = bool(want and self.sent and want[0] != self.sent[0])  # a new state goes out at once
            if want == self.sent and not self._needs_start(want):
                continue
            if not urgent and want and self.sent and time.time() - self.sent_at < 1.0:
                continue
            await self.run(self._send_activity, want)

    def _activity_state(self):
        """(state, phase, action, steps, request id), or None while the phone doesn't mirror."""
        m = self.mirror
        if not m.live():
            return None
        p = m.sessions.pending if m.sessions else None
        if p:
            return ("asking", "asking", p["action"] or "An agent", 0, p["id"])
        a = m.agent()
        if a == "driving":
            return ("driving", m.agent_phase, m.agent_action or "Working", m.agent_steps, "")
        if a == "paused":
            return ("paused", "paused", m.agent_action, m.agent_steps, "")
        return ("idle", "idle", "", 0, "")

    def _needs_start(self, want):
        """A device without a live activity, which we haven't asked to start lately."""
        if not want:
            return False
        now = time.time()
        return any(not (dev.get("activity") or {}).get("token") and now - self.start_tried.get(i, 0) > START_RETRY
                   for i, dev in self._devices_by_id().items() if dev.get("push_to_start"))

    def _devices_by_id(self):
        return {i: dev for i, dev in load()["devices"].items() if dev.get("bundle_id")}

    def _send_activity(self, want):
        m, before = self.mirror, self.sent
        self.sent, self.sent_at = want, time.time()

        def content(st):
            return {"state": st[0], "phase": st[1], "action": st[2], "steps": st[3], "requestId": st[4],
                    "startedAt": int(m.agent_started if st[0] in ("driving", "paused") else self.mirror_since())}

        alert = None
        if want and want[0] == "asking" and (not before or before[0] != "asking"):
            alert = {"title": "An agent wants control of your iPhone", "body": f"First step: {want[2]}",
                     "sound": "default"}
        for i, dev in self._devices_by_id().items():
            topic = dev["bundle_id"] + ".push-type.liveactivity"
            aps = {"timestamp": int(time.time())}
            act = dev.get("activity") or {}
            if want and not act.get("token"):
                # one start, then wait for the app to send the new activity's token: starting
                # again sooner could leave two activities on the phone
                if not dev.get("push_to_start") or time.time() - self.start_tried.get(i, 0) < START_RETRY:
                    continue
                self.start_tried[i] = time.time()
                aps.update({"event": "start", "content-state": content(want),
                            "attributes-type": "AgentActivityAttributes", "attributes": {"pcName": pc_name()},
                            "alert": alert or {"title": "PhoneUse", "body": "Mirroring to " + pc_name()}})
                r = self._send(dev, "push_to_start", dev["push_to_start"], {"aps": aps}, topic, "liveactivity")
            elif act.get("token"):
                if want:
                    aps.update({"event": "update", "content-state": content(want)})
                    if alert:
                        aps["alert"] = alert
                else:
                    aps.update({"event": "end", "content-state": content(before or ("idle", "idle", "", 0, "")),
                                "dismissal-date": int(time.time())})
                r = self._send(dev, "activity", act["token"], {"aps": aps}, topic, "liveactivity",
                               priority=10 if (alert or not want or (before and want[0] != before[0])) else 5)
                if r[0] == 410 or (r[0] == 400 and "BadDeviceToken" in r[1]):  # that activity is gone
                    d = load()
                    for v in d["devices"].values():
                        if (v.get("activity") or {}).get("token") == act["token"]:
                            v.pop("activity", None)
                    save(d)
            else:
                continue
            log(f"live activity {aps.get('event')} ({want[0] if want else 'off'}):", r)

    def mirror_since(self):
        if self.mirror.live():
            if not self._mirror_since:
                self._mirror_since = time.time()
        else:
            self._mirror_since = 0
        return self._mirror_since or time.time()

    # ---- Bonjour ----
    async def advertise(self):
        """Keep `_phoneuse._tcp` published through Avahi."""
        while True:
            try:
                p = await asyncio.create_subprocess_exec(
                    "avahi-publish-service", f"PhoneUse on {pc_name()}", "_phoneuse._tcp", str(PORT),
                    f"id={load()['pc_id']}", f"name={pc_name()}",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p.wait()
            except OSError as e:
                log("can't publish over Bonjour:", e)
                return
            await asyncio.sleep(5)

    def app(self):
        a = web.Application(middlewares=[self.auth], client_max_size=64 * 1024)
        r = a.router
        r.add_get("/v1/state", self.state)
        r.add_post("/v1/agent/takeover", self.takeover)
        r.add_post("/v1/agent/resume", self.resume)
        r.add_post("/v1/unlock/answer", self.unlock_answer)
        r.add_post("/v1/session/answer", self.session_answer)
        r.add_post("/v1/session/ask", self.ask_setting)
        r.add_route("POST", "/v1/passcode", self.passcode)
        r.add_route("DELETE", "/v1/passcode", self.passcode)
        r.add_post("/v1/awake", self.awake)
        r.add_post("/v1/keyboard", self.keyboard)
        r.add_post("/v1/cursor", self.cursor)
        r.add_post("/v1/bridge/use", self.bridge_use)
        r.add_get("/v1/bridge/networks", self.bridge_networks)
        r.add_post("/v1/bridge/network", self.bridge_network)
        r.add_post("/v1/mirror/audio", self.audio)
        r.add_post("/v1/push", self.push)
        return a

    async def start(self):
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log(f"companion API on port {PORT}")
        asyncio.ensure_future(self.advertise())
        asyncio.ensure_future(self.activities())


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _version():
    try:
        return subprocess.run(["git", "-C", _root(), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
