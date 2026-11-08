"""Windows side of screen.py: the picture comes from uxplay-windows' video window
("AirPlay Video Stream"), captured with PrintWindow so other windows on top
don't matter. A small in-process frames service (shot / status / settle) stands
in for frames.py, which needs GStreamer and Unix sockets."""
import ctypes
import io
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image

from .config import PHONE_ASPECT

WINDOW_TITLE = "AirPlay Video Stream"

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    user32.SetProcessDPIAware()
except Exception:
    pass

user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def window():
    """The mirror's video window handle, or None."""
    h = user32.FindWindowW(None, WINDOW_TITLE)
    return h if h and user32.IsWindowVisible(h) else None


def mirror_live():
    return window() is not None


def window_rect(h=None):
    """((x, y), (w, h)) of the window's client area on screen."""
    h = h or window()
    if not h:
        raise RuntimeError("mirror window not found")
    r = wintypes.RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    p = wintypes.POINT(0, 0)
    user32.ClientToScreen(h, ctypes.byref(p))
    return (p.x, p.y), (r.right - r.left, r.bottom - r.top)


def _capture_window(h):
    if user32.IsIconic(h):  # minimized windows have no picture: restore it without taking focus
        user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
        time.sleep(0.3)
    (_, _), (w, hh) = window_rect(h)
    if w <= 0 or hh <= 0:
        raise RuntimeError("mirror window has no size (minimized?)")
    hdc = user32.GetDC(h)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, hh)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # 1 = client area only, 2 = PW_RENDERFULLCONTENT (GPU-drawn video)
        if not user32.PrintWindow(h, mem, 3):
            raise RuntimeError("PrintWindow failed")
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -hh, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * hh * 4)
        gdi32.GetDIBits(mem, bmp, 0, hh, buf, ctypes.byref(info), 0)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(h, hdc)
    return np.frombuffer(buf, np.uint8).reshape(hh, w, 4)[..., 2::-1]  # BGRA -> RGB


def picture_box(w, h):
    """Where the phone picture sits in a w x h window: aspect-fit, centred."""
    if w / h > PHONE_ASPECT:
        pw = round(h * PHONE_ASPECT)
        return (w - pw) // 2, 0, pw, h
    ph = round(w / PHONE_ASPECT)
    return 0, (h - ph) // 2, w, ph


def grab_rgb_now():
    h = window()
    if not h:
        raise RuntimeError("mirror window not found: start Screen Mirroring to uxplay-windows")
    rgb = _capture_window(h)
    x, y, pw, ph = picture_box(rgb.shape[1], rgb.shape[0])
    return np.ascontiguousarray(rgb[y:y + ph, x:x + pw])


def to_png(rgb):
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "PNG")
    return buf.getvalue()


class Frames:
    """Captures the mirror ~15 times a second and counts picture changes (seq),
    like frames.py: `settle` waits for an action's result, then for stillness."""

    def __init__(self):
        self.seq = 0
        self.last = None
        self.small = None
        self.changed_at = time.time()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            try:
                rgb = grab_rgb_now()
                small = rgb[::8, ::8].mean(axis=2).astype(np.int16)
                with self.cond:
                    if self.small is None or small.shape != self.small.shape or \
                            (np.abs(small - self.small) > 12).mean() > 0.002:
                        self.seq += 1
                        self.changed_at = time.time()
                    self.small, self.last = small, rgb
                    self.cond.notify_all()
            except Exception:
                with self.cond:
                    self.last = None
            time.sleep(0.066)

    def shot(self):
        with self.cond:
            if self.last is None:
                self.cond.wait(1.0)
            if self.last is None:
                raise OSError("no picture from the mirror window")
            return self.last

    def settle(self, since=None, quiet=0.15, change=0.6, timeout=5.0):
        end = time.time() + timeout
        with self.cond:
            if since is not None:  # wait for the action's first change (or give up after `change`)
                until = min(end, time.time() + change)
                while self.seq <= since and time.time() < until:
                    self.cond.wait(until - time.time())
            while time.time() - self.changed_at < quiet and time.time() < end:
                self.cond.wait(quiet)
            still = time.time() - self.changed_at >= quiet
            return f"ok seq={self.seq} still={int(still)} ambient=0 ignored=0.00"


_frames = None
_frames_lock = threading.Lock()


def frames():
    global _frames
    with _frames_lock:
        if _frames is None:
            if not mirror_live():
                raise OSError("mirror window not found")
            _frames = Frames()
        return _frames


def frames_request(cmd, timeout=3.0):
    parts = cmd.split()
    f = frames()
    if parts[0] == "status":
        return f"ok seq={f.seq}", b""
    if parts[0] == "shot":
        png = to_png(f.shot())
        return f"ok {len(png)}", png
    if parts[0] == "settle":
        since = int(parts[1]) if len(parts) > 1 and parts[1] != "None" else None
        quiet = float(parts[2]) if len(parts) > 2 else 0.15
        change = float(parts[3]) if len(parts) > 3 else 0.6
        return f.settle(since, quiet, change, timeout=min(timeout, 6.0) - 0.5), b""
    raise OSError(f"unknown frames request {cmd!r}")
