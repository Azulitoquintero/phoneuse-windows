# The Bluetooth bridge

The bridge is an ESP32 board (a LilyGO TTGO T-Display) that the iPhone pairs with
as its Bluetooth keyboard and mouse, in place of this PC. phoneuse reaches it over
Wi-Fi, so the board can sit next to the phone on a battery or any USB charger, and
the PC's own Bluetooth radio is out of the path.

```
phoneuse (PC) ── HID daemon ──UDP over Wi-Fi──▶ bridge (ESP32) ──Bluetooth──▶ iPhone
```

## Setting one up

Everything happens from the browser mirror's settings (Settings › Bluetooth bridge)
or `phoneuse bridge setup` in a terminal. No USB cable is needed.

| Step | What happens |
|---|---|
| Find | A new bridge advertises over BLE; the PC finds it with its own Bluetooth. A set-up bridge waits for setup again after you hold its lower button for 3 s. |
| Code | The PC and the bridge exchange X25519 keys. Both show the same 6-digit code; a different code means something else answered. |
| Network | The bridge scans and lists the Wi-Fi networks it sees (2.4 GHz only). |
| Password | Sent to the bridge encrypted (AES-GCM, key from the exchange). The bridge saves the network only once it has joined. |
| Pair | On the iPhone: Settings › Bluetooth › PhoneUse Bridge. |

The PC saves the bridge's id and control key in `~/.config/phoneuse/bridge.json`
before the bridge joins, and finds it again by id if its address changes.

## The control link

UDP port 47700. Every packet is signed (HMAC-SHA256 with the control key from
setup, truncated to 8 bytes), so nobody else on the network can type on the phone.
The bridge answers each report at once: sent, skipped (its Bluetooth queue is busy,
so the sender keeps the motion), or no phone. `bridge/main/link.c` has the packet
format.

While a bridge is in use, the HID daemon sends every report there and drops its own
Bluetooth link to the phone, so the phone never sees two keyboards. The switch
"Send input through bridge" in the settings turns this off.

## Firmware

The firmware lives in `bridge/` (ESP-IDF v6.1). With the board on USB:

```bash
phoneuse bridge flash    # build and flash
phoneuse bridge log 10   # restart it and print 10 s of its log
phoneuse bridge status   # address, phone, round trip
```

The bridge presents the same HID descriptor as the PC daemon (`hid_daemon.py`
REPORT_MAP), byte for byte: keyboard, mouse, absolute pointer, and the consumer
keys (Eject, Siri, Home, Search).

## Measured (2026-11-07)

| | PC Bluetooth | Bridge |
|---|---|---|
| Reports per second | 114 to 215 | 38 |
| Round trip per report | under 1 ms (local socket) | 15 ms median, 29 ms p90 |

The bridge is slower today because each report waits for its answer over Wi-Fi,
and the ESP32 shares one radio between Wi-Fi and Bluetooth (its Wi-Fi signal was
−80 dBm during the test).
