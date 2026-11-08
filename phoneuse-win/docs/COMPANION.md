# The companion app

The companion app is an optional iOS app. phoneuse works without it, but with it the phone shows a Live Activity
while an agent uses the phone, the owner can approve unlocks and take over from the phone, and the owner can
change phoneuse's settings (the bridge, keep-awake, the keyboard, the mirror's audio) without the PC.

The app talks to the phoneuse PC over the local network, through the companion API that this file describes. The
PC starts, updates and ends the Live Activity with Apple push notifications (APNs), so the app doesn't need to run.

## Finding and pairing

The PC's companion API listens on TCP port 8778 on the local network and advertises itself over Bonjour as
`_phoneuse._tcp`, with TXT records `id` (the PC's id, 16 hex characters) and `name` (the PC's name).

Pairing gives the app a 32-byte key that it shares with the PC. `phoneuse app pair` makes a pairing link:

```
phoneuse://pair?id=<pc id>&name=<pc name, URL-encoded>&host=<LAN IP>&port=8778&key=<key, base64url without padding>
```

phoneuse can open the link on the phone by itself (it types the link into Safari's address bar), and the browser
mirror's settings show the same link as a QR code. The app saves the key in the Keychain, and the PC saves it in
`~/.config/phoneuse/companion.json` (mode 600). A pairing link works once, for 10 minutes. The app keeps the
host it was given and falls back to Bonjour (by `id`) when the PC's address changes.

## Signing requests

Every request carries three headers:

| Header | Value |
|---|---|
| `X-PU-Time` | Unix time in seconds |
| `X-PU-Nonce` | 16 random bytes, hex |
| `X-PU-Sig` | hex HMAC-SHA256 with the pairing key over `METHOD\nPATH\nTIME\nNONCE\nSHA256HEX(BODY)` |

`PATH` includes the query string, if any, and `BODY` is the exact request body (empty for GET). The PC refuses a
request whose time is more than 60 s off or whose nonce it has seen in the last 120 s, with status 401.

Secrets (the passcode and Wi-Fi passwords) travel sealed: `{"sealed": "<base64 of nonce(12) | ciphertext | tag(16)>"}`,
AES-256-GCM with the key HKDF-SHA256(pairing key, salt empty, info `"phoneuse seal"`, 32 bytes) and the field name
(`"passcode"` or `"password"`) as associated data.

Every response is JSON. Errors are `{"error": "<short sentence for the owner>"}` with status 4xx or 5xx.

## Endpoints

| Method and path | Body | What the PC does |
|---|---|---|
| `GET /v1/state` | | Returns the state below |
| `POST /v1/agent/takeover` | `{}` | Stops the agent: its next action fails with "the user took over" |
| `POST /v1/agent/resume` | `{}` | Lets the agent continue |
| `POST /v1/unlock/answer` | `{"id", "approve": bool}` | Answers a pending unlock request |
| `POST /v1/session/ask` | `{"on": bool}` | Ask the owner before each agent session, or not (off by default) |
| `POST /v1/session/answer` | `{"id", "approve": bool}` | Answers a pending session request |
| `POST /v1/passcode` | `{"sealed"}` (field `passcode`) | Saves the passcode that unlocks use |
| `DELETE /v1/passcode` | | Forgets the passcode |
| `POST /v1/awake` | `{"on": bool}` | Keep the screen awake or not |
| `POST /v1/keyboard` | `{}` | Shows or hides the on-screen keyboard (the Eject key) |
| `POST /v1/cursor` | `{"on": bool}` | Turns the pointer (AssistiveTouch) on or off through Siri; takes up to 15 s |
| `POST /v1/bridge/use` | `{"on": bool}` | Sends input through the bridge, or through the PC's own Bluetooth |
| `GET /v1/bridge/networks` | | `{"networks": [{"ssid", "rssi", "secure"}]}` that the bridge sees |
| `POST /v1/bridge/network` | `{"ssid", "sealed"}` (field `password`) | Moves the bridge to that Wi-Fi network |
| `POST /v1/mirror/audio` | `{"on": bool}` | Turns the mirror's audio on or off (see Audio) |
| `POST /v1/push` | see Push | Registers the app's push tokens |

`GET /v1/state` returns:

```json
{
  "pc": {"id": "…", "name": "lxe-desktop", "version": "9ccd370"},
  "mirroring": true,
  "phone": "LXEPhone17Pro",
  "bluetooth": {"connected": true, "via": "bridge"},
  "agent": {"state": "idle | driving | paused", "action": "Opening Gmail", "since": 1794153854},
  "awake": true,
  "audio": {"on": true, "needs_remirror": false},
  "session": {"ask": false, "pending": {"id": "…", "since": 1794153854, "action": "Opening Gmail"}},
  "unlock": {"passcode_saved": true, "pending": {"id": "…", "since": 1794153854}},
  "bridge": {"saved": true, "id": "…", "ip": "192.168.0.51", "ssid": "Giraffe IoT", "use": true,
             "online": true, "rtt_ms": 15}
}
```

`session.pending`, `unlock.pending` and `bridge` are `null` when there is no pending request or no bridge.

## Asking before an agent session

When `session.ask` is on, an agent's first action waits until the owner allows the session, for up to 2 minutes.
The owner can answer in the app's notification, in the app, in a desktop notification or in the browser mirror. If
the owner declines or doesn't answer, the agent's action fails with a message that tells it to stop. Once allowed,
the session lasts until the agent hands the phone back (`phoneuse done`), the owner takes over, or the agent sends
no action for 5 minutes; after that, the next action asks again.

## Audio

UxPlay plays the phone's audio on the PC. The PC can't change that while the phone mirrors, so
`POST /v1/mirror/audio` restarts UxPlay with audio on or off (UxPlay's `-as 0`), which ends the mirroring. The
answer has `"needs_remirror": true`, and the app then asks the owner to start Screen Mirroring to PhoneUse again
from Control Center. iOS doesn't let apps start Screen Mirroring.

## Push

The app sends its tokens whenever iOS gives it new ones:

```json
{"bundle_id": "…", "env": "sandbox | production",
 "device": "<APNs device token, hex>",
 "push_to_start": "<Live Activity push-to-start token, hex>",
 "activity": {"id": "<Activity id>", "token": "<that activity's push token, hex>"}}
```

Each field is optional, so the app can send one token at a time. The PC keeps the latest of each.

The PC sends APNs pushes with a token-based key (`~/.config/phoneuse/apns.json`: `key_id`, `team_id`, and the
`.p8` key's path):

- **Start** (topic `<bundle id>.push-type.liveactivity`, push type `liveactivity`), when an agent session starts, to
  the push-to-start token: `event: "start"`, `attributes-type: "AgentActivityAttributes"`,
  `attributes: {"pcName": "…"}`, `content-state` as below, and an `alert` ("An agent is using your iPhone").
- **Update**, to the activity's token, when the agent's action or state changes, at most once per second:
  `event: "update"`, `content-state`.
- **End**, when the session ends (`phoneuse done`, or 20 s without an action): `event: "end"`, the last
  `content-state`, and `dismissal-date` 10 s later.
- **Session request** (topic `<bundle id>`, push type `alert`), to the device token: an alert with category
  `SESSION`, `interruption-level: "time-sensitive"` and `"session_id"` in the payload. The app's notification
  actions **Allow** and **Deny** call `POST /v1/session/answer`.
- **Unlock request** (topic `<bundle id>`, push type `alert`), to the device token: an alert with category
  `UNLOCK` and `"unlock_id"` in the payload. The app's notification actions **Unlock** (with
  `.authenticationRequired`) and **Deny** call `POST /v1/unlock/answer`.

`content-state` is `{"state": "idle | asking | driving | paused", "phase": "idle | asking | acting | reading | thinking | paused", "action": "Opening Gmail", "steps": 12, "requestId": "…", "startedAt": <unix seconds>}`.

The Live Activity stays on the phone the whole time the phone mirrors to the PC, and ends when mirroring stops.
`idle` means the owner is in control. `asking` means an agent waits for the owner's answer to a session request:
`requestId` is the request's id, and the activity shows Allow and Deny buttons that call `POST /v1/session/answer`;
the update that enters `asking` carries an alert, so the Dynamic Island expands. `driving` means the agent uses
the phone, and `paused` that the owner took over. The PC starts the activity by push only when the app hasn't sent
a token for a live one, and at most once every 10 minutes per device; the app also starts it itself (the same
attributes and content) when it opens while the PC mirrors and no activity is running.

`phase` says what the agent is doing at the moment: `acting` while phoneuse sends a tap, a swipe or keys to the
phone, `reading` while the agent looks at the screen, and `thinking` between actions, while the agent's model
works out its next step. The phase changes every few seconds, and the PC sends at most one update per second.

## The app

The Live Activity shows the agent's current action, the number of steps and the time since the session started,
with a **Take over** button (a `LiveActivityIntent` that calls `POST /v1/agent/takeover`) and, while paused, a
**Resume** button. The Dynamic Island's compact view shows an icon and the step count.

The app's own screen shows the PC's state and has these settings, each changing the PC at once:

- the agent session: take over or resume, and whether to ask before an agent starts;
- unlocking: whether a passcode is saved, save or replace it, forget it, and a pending request to approve or deny;
- keep the screen awake;
- the on-screen keyboard: show or hide;
- the pointer: on or off;
- the bridge: its status, sending input through it or not, and moving it to another Wi-Fi network;
- the mirror's audio: on or off.
