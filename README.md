# phoneuse-windows · v0.1

Control an **iPhone** from Claude (or any MCP agent) on a **Windows** PC: the agent sees the phone's screen and taps, swipes and types on it. No Linux, no jailbreak, no extra hardware and no app on the iPhone.

> **Version 0.1: experimental.** Tested on one setup only: Windows 11, the laptop's internal Bluetooth (Realtek), and an iPhone running iOS 26. Expect rough edges.

It's a Windows port of [lxe/phoneuse](https://github.com/lxe/phoneuse), which only works on Linux (BlueZ + Hyprland). It uses two features every iPhone already has:

- **Screen Mirroring (AirPlay)**: the iPhone sends its screen to the PC.
- **A Bluetooth mouse and keyboard + AssistiveTouch**: the PC presents itself to the iPhone as a mouse and keyboard and taps, swipes and types.

## How it works

```
 iPhone ──AirPlay (Wi-Fi)──▶ uxplay-windows ──▶ "AirPlay Video Stream" window
   ▲                                                   │ screenshots (PrintWindow)
   │                                                   ▼
   └──Bluetooth LE (HID)── PhoneHid.exe ◀── TCP 127.0.0.1:8791 ── phoneuse (Python, MCP) ◀── Claude
```

| Folder | What it is |
|---|---|
| `phonehid/` | **PhoneHid** (C#): makes the PC act as a Bluetooth LE mouse, pointer and keyboard for the iPhone, using the WinRT `GattServiceProvider` API (HID over GATT). It takes commands on `127.0.0.1:8791`, with the same protocol as the Linux phoneuse daemon. Based on [SaqibS/typeover](https://github.com/SaqibS/typeover). |
| `phoneuse-win/` | **phoneuse** adapted to Windows: the MCP server and command line that see the screen (OCR) and drive the phone. |
| `captura.ps1` | Helper script: saves a screenshot of the mirror to a PNG. |

The changes to phoneuse for Windows:

- `phoneuse/hidclient.py` talks to PhoneHid over TCP.
- `phoneuse/screen_win.py` (new) captures the mirror window and serves the frames.
- `phoneuse/screen.py` and `phoneuse/cli.py` gained Windows branches: in-process OCR, sounds through `winsound`, and a `status` that doesn't use systemd.

## Requirements

- **Windows 10 (2004+) or 11**, with Bluetooth LE that supports peripheral role. Most internal laptop chips do. Some cheap USB dongles don't.
- **iPhone** with iOS 16 or later, on **the same Wi-Fi network** as the PC.
- [**.NET 10 SDK**](https://dotnet.microsoft.com/download), to build PhoneHid.
- [**Python 3.11+**](https://www.python.org/downloads/windows/). This was tested with 3.13. On Windows, use `py`.
- [**uxplay-windows**](https://github.com/leapbtw/uxplay-windows/releases), the AirPlay receiver.
- [**Claude Code**](https://claude.com/claude-code), or another client that supports MCP.

## Installation

### 1. Clone the repository

```powershell
git clone https://github.com/Azulitoquintero/phoneuse-windows.git
cd phoneuse-windows
```

### 2. Build PhoneHid

```powershell
cd phonehid
dotnet publish PhoneHid.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o salida
cd ..
```

This creates `phonehid\salida\PhoneHid.exe`.

### 3. Install phoneuse (Python)

```powershell
cd phoneuse-win
py -m venv .venv
.\.venv\Scripts\python -m pip install -r ..\requirements.txt
cd ..
```

The first time it runs, RapidOCR downloads its OCR models (a few MB).

### 4. Install the AirPlay receiver

Download the latest release of [uxplay-windows](https://github.com/leapbtw/uxplay-windows/releases) and unzip it, for example into `uxplay\app\`. The first time Windows asks for network permission, allow it. If your Wi-Fi is set to "Public", AirPlay may not find the PC: switch it to "Private" or allow uxplay through the firewall.

### 5. Register the MCP in Claude Code

Replace `C:\path\to\phoneuse-windows` with where you cloned it:

```powershell
claude mcp add --scope user phoneuse -e "PYTHONPATH=C:\path\to\phoneuse-windows\phoneuse-win" -- C:\path\to\phoneuse-windows\phoneuse-win\.venv\Scripts\python.exe -m phoneuse.mcp_server
```

Check it with `claude mcp list`. It should show `phoneuse: ... Connected`.

### 6. Prepare the iPhone (once)

1. Settings → Accessibility → Touch → **AssistiveTouch: on**.
   - Optional: raise the **Tracking Speed**.
2. Start `PhoneHid.exe`. On the iPhone: Settings → **Bluetooth** → tap **your PC's name** (it shows up as a keyboard or mouse) and pair.
3. Settings → General → Keyboard → Hardware Keyboard → set the layout to **English (US)**, because PhoneHid types with the US layout.
4. While you're using it: Settings → Display & Brightness → **Auto-Lock → Never**. If the iPhone locks, the agent can't unlock it.

## Use

Every time:

1. Start `phonehid\salida\PhoneHid.exe`. The iPhone reconnects to it over Bluetooth on its own.
2. Start `uxplay-windows.exe` → right-click its **tray** icon → **Start**.
3. On the iPhone: Control Center → **Screen Mirroring** → choose **uxplay-windows**.
4. Ask Claude for something, for example:
   - "go to the iPhone's home screen"
   - "open Instagram and go to messages"
   - "tell David 'hi, I'm Claude'"

The MCP's main tools:

| Tool | What it does |
|---|---|
| `look` | Takes a screenshot of the iPhone and reads its text with OCR. |
| `tap`, `long_press` | Taps at a point (exact, with the absolute pointer). |
| `tap_on` | Taps a piece of text visible on screen. |
| `swipe`, `scroll`, `scroll_by` | Swipes and scrolling. |
| `type_text`, `press_key` | Typing and keyboard shortcuts, such as `cmd+h` for the home screen. |
| `open_app` | Opens an app through Spotlight search. |
| `navigate` | Home, back, app switcher, Control Center, notifications. |
| `status` | Status of the Bluetooth link and the mirror. |

### Pointer calibration

iPhones move the pointer different distances depending on the model and the AssistiveTouch **Tracking Speed**. Swipes need that table. The automatic calibration (`calibrate`) **doesn't work reliably on Windows yet**: on the uxplay mirror the pointer looks faint and lags. Until that's fixed, you can save a table scaled from the iPhone 17 Pro's. On the test iPhone, the factor was 0.1:

```powershell
cd phoneuse-win
.\.venv\Scripts\python -c "from phoneuse import phones, touch; phones.save_calibration('iPhone', [(u, round(p*0.1,3)) for u,p in touch.REF_PTS])"
```

It's saved in `%USERPROFILE%\.config\phoneuse\phones.json`. If a swipe falls short, lower the factor. If it overshoots, raise it.

## Known limitations (v0.1)

- **Typing only supports characters on the US keyboard**: no ñ, no accented letters, no emoji.
- **`unlock` isn't ported.** Unlock the iPhone yourself.
- Automatic calibration fails on Windows (see above).
- `tap_on` with visual descriptions ("the blue icon") needs the grounding model, which isn't installed. Text taps do work.
- Not ported from the original: `phoneuse up/install`, the browser mirror (`web.py`) and the ESP32 bridge.
- uxplay-windows needs **Start** clicked in its tray icon every time, and the mirror sometimes drops. If that happens, reconnect from Control Center.
- If the iPhone stops seeing the mouse after you change PhoneHid: Bluetooth → your PC (i) → **Forget This Device**, and pair again.

## Troubleshooting

| Problem | Fix |
|---|---|
| `PhoneHid isn't running` | Start `PhoneHid.exe`. Port 8791 must be free. |
| `connected=False` | Turn the iPhone's Bluetooth off and on, or restart PhoneHid. |
| `mirror window not found` | Start uxplay (tray → Start) and turn on Screen Mirroring. |
| Taps work but swipes barely move | Calibrate (see above). |
| The iPhone doesn't see uxplay | Same Wi-Fi network, network set to Private, firewall allowed. |

## Credits and license

- [lxe/phoneuse](https://github.com/lxe/phoneuse): Aleksey Smolenchuk, MIT. The original project, in `phoneuse-win/` (its README and license are kept).
- [SaqibS/typeover](https://github.com/SaqibS/typeover): Saqib Shaikh, MIT. The base for the BLE HID peripheral in PhoneHid.
- [leapbtw/uxplay-windows](https://github.com/leapbtw/uxplay-windows) / [UxPlay](https://github.com/FDH2/UxPlay): GPL-3.0. It's downloaded separately and isn't redistributed here.

This repository is distributed under the **MIT** license (see `LICENSE`).
