"""Screen sampling for ambient sync, standard library only (ctypes + GDI).

Windows' own scaler (StretchBlt in HALFTONE mode) shrinks the whole screen to a tiny grid, so every grid cell is
already the average of a patch of screen. Lights then average the few cells in their zone.

Protected video (Netflix, Disney+, ...) is blanked by Windows for any capture, so it reads as black.
"""
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:                                   # real pixel sizes on scaled (125%/150%) displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    user32.SetProcessDPIAware()

SRCCOPY, HALFTONE, DIB_RGB_COLORS = 0x00CC0020, 4, 0
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

# 64-bit handles: declare types so ctypes doesn't truncate them to C ints
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.StretchBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def monitors():
    """[(x, y, w, h), ...] for each monitor, primary first."""
    out = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, hdc, rect, data):
        r = rect.contents
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    out.sort(key=lambda m: (m[0] != 0 or m[1] != 0, m[0], m[1]))     # the primary monitor sits at 0,0
    return out


COLORONCOLOR = 3


class Sampler:
    """average=True: Windows averages each cell (smooth, ~10ms CPU per grab at 1440p).
    average=False: each cell is one sampled pixel (~4ms at 64x36); average neighbouring cells yourself."""

    def __init__(self, monitor=0, cols=32, rows=18, average=True):
        mons = monitors()
        self.x, self.y, self.w, self.h = mons[min(monitor, len(mons) - 1)]
        self.cols, self.rows = cols, rows
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        self.bmp = gdi32.CreateCompatibleBitmap(self.screen, cols, rows)
        gdi32.SelectObject(self.mem, self.bmp)
        gdi32.SetStretchBltMode(self.mem, HALFTONE if average else COLORONCOLOR)
        gdi32.SetBrushOrgEx(self.mem, 0, 0, None)
        self.info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), cols, -rows, 1, 32, 0, 0, 0, 0, 0, 0)
        self.buf = (ctypes.c_ubyte * (cols * rows * 4))()

    def grab(self):
        """Grid of (r, g, b) tuples, rows top to bottom."""
        gdi32.StretchBlt(self.mem, 0, 0, self.cols, self.rows, self.screen, self.x, self.y, self.w, self.h, SRCCOPY)
        gdi32.GetDIBits(self.mem, self.bmp, 0, self.rows, self.buf, ctypes.byref(self.info), DIB_RGB_COLORS)
        b, c = self.buf, self.cols
        return [[(b[(y * c + x) * 4 + 2], b[(y * c + x) * 4 + 1], b[(y * c + x) * 4]) for x in range(c)]
                for y in range(self.rows)]

    def grab_raw(self):
        """The grid as raw BGRX bytes (4 per cell, rows top to bottom): cheap to send over the network."""
        gdi32.StretchBlt(self.mem, 0, 0, self.cols, self.rows, self.screen, self.x, self.y, self.w, self.h, SRCCOPY)
        gdi32.GetDIBits(self.mem, self.bmp, 0, self.rows, self.buf, ctypes.byref(self.info), DIB_RGB_COLORS)
        return bytes(self.buf)

    def close(self):
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.mem)
        user32.ReleaseDC(None, self.screen)


if __name__ == "__main__":             # quick benchmark: py screen.py
    import time
    print("monitors:", monitors())
    s = Sampler()
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < 3:
        g = s.grab()
        n += 1
    dt = (time.perf_counter() - t0) / n
    flat = [p for row in g for p in row]
    avg = tuple(sum(p[i] for p in flat) // len(flat) for i in range(3))
    print(f"{n / 3:.0f} grabs/s ({dt * 1000:.1f} ms each) of {s.w}x{s.h} -> {s.cols}x{s.rows}; screen average {avg}")
