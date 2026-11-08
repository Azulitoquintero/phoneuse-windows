"""phoneuse: see and operate an iPhone from a PC (AirPlay mirror + Bluetooth HID).

Windows port: input goes through PhoneHid.exe (BLE HID, 127.0.0.1:8791) and the
picture comes from the uxplay-windows window. See WINDOWS.md.
"""
import os
import tempfile

if os.name == "nt":
    # The code keeps its runtime files in $XDG_RUNTIME_DIR (default /tmp).
    os.environ.setdefault("XDG_RUNTIME_DIR", tempfile.gettempdir())
