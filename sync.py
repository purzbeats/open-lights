"""Screen sync: lights take their color from what's on the monitor.

Local mode (server on the PC): a thread grabs a 64x36 grid of screen pixels ~30 times a second, only while
some light is syncing. Remote mode (server on the Pi): the PC's screen_agent.py pushes the same grids over the
network, and only while this side says it wants them.
Each light reads its zone: the cells it sits next to, averaged with vivid colors weighted over greys,
letterbox bars ignored. Lights near the screen follow quickly; whole-room "ambient" lights drift slowly.
"""
import colorsys
import math
import threading
import time


GRID = (64, 36)
HZ = 30

# Picture regions as fractions (x0, y0, x1, y1). Segmented lights are sliced along the region's long side.
ZONES = {
    "all": (0.0, 0.0, 1.0, 1.0),
    "center": (0.25, 0.2, 0.75, 0.8),
    "left": (0.0, 0.0, 0.22, 1.0),
    "right": (0.78, 0.0, 1.0, 1.0),
    "top": (0.0, 0.0, 1.0, 0.25),
    "bottom": (0.0, 0.75, 1.0, 1.0),
    "left-half": (0.0, 0.0, 0.5, 1.0),       # lights further out to the side: broader area, gentler response
    "right-half": (0.5, 0.0, 1.0, 1.0),
}
AMBIENT = {"all", "left-half", "right-half"}  # zones that react slowly (lights further from the screen)

# Each light reads small patches at fixed points rather than whole strips, so taskbars, window borders and
# whatever sits at the very edge of the screen don't leak in. Edge points are inset this far from the edge.
INSET_PX = 100
PATCH = 2                     # patch size in grid cells (a cell is ~40 px on a 2560-wide screen -> ~80 px)
BAND = 0.30                   # multi-segment top/bottom lights read a band this tall (fraction of the picture)
BROAD = 3                     # long multi-segment lights take this many broad readings and blend between them
ZONE_NAMES = {"all": "Whole screen (ambient)", "center": "Center", "left": "Left edge", "right": "Right edge",
              "top": "Top edge", "bottom": "Bottom edge", "sides": "Wrap around (left up, right down)",
              "left-half": "Left half (further left in the room)", "right-half": "Right half (further right in the room)"}
TAU_NEAR, TAU_AMBIENT, TAU_CUT = 0.12, 0.9, 0.05     # smoothing time constants (seconds)
BLACK = 0.06                                          # screen values below this become true black


HUE_BINS = 18                 # 20 degrees each
VIVID_SAT, VIVID_VAL = 0.3, 0.12
NEUTRAL_SHARE = 0.10          # below this share of vivid pixels the scene counts as neutral: use the plain average


def _pick(cells):
    """The color a light should show for a patch of screen.

    Plain averaging mixes distinct colors toward grey (red + green + blue = grey; white UI pulls everything grey).
    Instead: group the vivid pixels by hue, take the strongest hue family, and use the average of just those
    pixels. A small vivid area in a dark/neutral patch gives a dimmer version of its color; a genuinely neutral
    patch (greys, whites, skin-toned beige) falls back to the weighted average."""
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(HUE_BINS)]     # r, g, b, weight per hue bin
    vivid_w = total_w = 0.0
    for r, g, b in cells:
        mx, mn = max(r, g, b), min(r, g, b)
        v = mx / 255
        total_w += 0.05 + v
        if mx == 0:
            continue
        sat = (mx - mn) / mx
        if sat < VIVID_SAT or v < VIVID_VAL:
            continue
        d = mx - mn
        h = ((g - b) / d) % 6 if mx == r else ((b - r) / d + 2 if mx == g else (r - g) / d + 4)
        w = sat * sat * v
        bn = bins[int(h * HUE_BINS / 6) % HUE_BINS]
        bn[0] += r * w
        bn[1] += g * w
        bn[2] += b * w
        bn[3] += w
        vivid_w += 0.05 + v
    share = vivid_w / total_w if total_w else 0.0
    if share < NEUTRAL_SHARE:
        return _avg(cells)
    # strongest hue family = best bin together with its two neighbours (hues straddling a bin edge)
    best = max(range(HUE_BINS), key=lambda i: bins[i - 1][3] + bins[i][3] + bins[(i + 1) % HUE_BINS][3])
    fam = [bins[best - 1], bins[best], bins[(best + 1) % HUE_BINS]]
    w = sum(f[3] for f in fam)
    color = tuple(sum(f[j] for f in fam) / w for j in range(3))
    return tuple(c * (0.45 + 0.55 * min(1.0, share * 2.5)) for c in color)     # small vivid areas glow dimmer


def _avg(cells):
    """Average of (r, g, b) cells, vivid and bright cells weighted more than grey/dark ones."""
    tr = tg = tb = tw = 0.0
    for r, g, b in cells:
        mx, mn = max(r, g, b), min(r, g, b)
        sat = (mx - mn) / mx if mx else 0.0
        w = 0.08 + (mx / 255) * (0.35 + sat ** 1.5)
        tr += r * w
        tg += g * w
        tb += b * w
        tw += w
    return (tr / tw, tg / tw, tb / tw) if tw else (0.0, 0.0, 0.0)


def _grade(rgb):
    """Gentle saturation boost, and crushed blacks: near-black scenes go fully dark, darks fall off steeply."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    s = min(1.0, s * 1.35)
    v = max(0.0, (v - BLACK) / (1 - BLACK)) ** 1.2
    return tuple(c * 255 for c in colorsys.hsv_to_rgb(h, s, v))


class ScreenSync:
    def __init__(self, monitor=0, remote=False):
        self.monitor = monitor
        self.remote = remote          # True: frames arrive via feed() from the PC's screen agent
        self.prev_mean = None
        self.last_frame = 0.0
        self.lock = threading.Lock()
        self.grid = None              # rows of (r, g, b)
        self.active = (0, 0, GRID[0], GRID[1])   # picture area inside letterbox bars: x0, y0, x1, y1 (cells)
        self.last_cut = 0.0
        self.last_used = 0.0
        self.thread = None
        self.smooth = {}              # (dev, segment) -> [r, g, b, last_time]
        self.frame = 0                # increments with every new grid
        self.cell = (40.0, 40.0)      # screen pixels per grid cell (x, y); set from the real screen size
        self.targets = {}             # (zone, k, n) -> (frame, graded color): computed once per frame

    # ---------- sampling thread ----------

    def touch(self):
        """Called every engine tick while a light syncs; starts sampling if needed."""
        self.last_used = time.time()
        if self.remote:
            return                    # the PC agent sees wanted() and starts sending
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def wanted(self):
        """Is any light syncing right now? (The PC agent polls this.)"""
        return time.time() - self.last_used < 2

    def feed(self, g, screen_w=0, screen_h=0):
        """A frame from the PC agent (rows of (r, g, b)), plus the real screen size it was sampled from."""
        self.last_frame = time.time()
        if screen_w and screen_h:
            self.cell = (screen_w / len(g[0]), screen_h / len(g))
        self.process(g)

    def loop(self):
        from screen import Sampler    # Windows-only, so imported only when sampling locally
        sampler = Sampler(self.monitor, *GRID, average=False)
        self.cell = (sampler.w / GRID[0], sampler.h / GRID[1])
        print(f"screen sync: sampling {sampler.w}x{sampler.h} at {HZ}/s", flush=True)
        try:
            while self.wanted():                              # stop when nobody has asked for 2s
                t0 = time.perf_counter()
                self.process(sampler.grab())
                time.sleep(max(0.0, 1 / HZ - (time.perf_counter() - t0)))
        finally:
            sampler.close()
            print("screen sync: sampling stopped", flush=True)

    def process(self, g):
        """Find the picture area (skip letterbox bars), spot scene cuts, store the grid for colors()."""
        if True:
                rows, cols = len(g), len(g[0])
                luma = [[(r * 3 + gg * 6 + b) // 10 for r, gg, b in row] for row in g]
                # letterbox / pillarbox: trim near-black edge rows and columns (at most a quarter each side)
                y0, y1, x0, x1 = 0, rows, 0, cols
                while y0 < rows // 4 and max(luma[y0]) < 14:
                    y0 += 1
                while y1 > rows - rows // 4 and max(luma[y1 - 1]) < 14:
                    y1 -= 1
                while x0 < cols // 4 and max(luma[y][x0] for y in range(y0, y1)) < 14:
                    x0 += 1
                while x1 > cols - cols // 4 and max(luma[y][x1 - 1] for y in range(y0, y1)) < 14:
                    x1 -= 1
                if y1 - y0 < 4 or x1 - x0 < 4:                # (near) black screen: use everything
                    y0, y1, x0, x1 = 0, rows, 0, cols
                mean = _avg([p for row in g[y0:y1] for p in row[x0:x1:2]])
                if self.prev_mean and sum(abs(a - b) for a, b in zip(mean, self.prev_mean)) > 90:
                    self.last_cut = time.time()               # scene cut: let lights snap
                self.prev_mean = mean
                with self.lock:
                    self.grid, self.active = g, (x0, y0, x1, y1)
                    self.frame += 1

    # ---------- per-light colors ----------

    def _point(self, zone, k, n):
        """Where segment k of n in a zone samples, in grid cells (x, y)."""
        ax0, ay0, ax1, ay1 = self.active
        w, h = ax1 - ax0, ay1 - ay0
        ix = min(INSET_PX / self.cell[0], w / 4)          # 100 px in from the edge, in cells
        iy = min(INSET_PX / self.cell[1], h / 4)
        along = (k + 0.5) / n                               # spread segments evenly along the line
        if zone == "sides":                                  # first half up the left side, second half down the right
            half = max(1, (n + 1) // 2)
            if k < half:
                return ax0 + ix, ay1 - iy - (h - 2 * iy) * (k + 0.5) / half
            m = max(1, n - half)
            return ax1 - ix, ay0 + iy + (h - 2 * iy) * (k - half + 0.5) / m
        if zone == "top":
            return ax0 + ix + (w - 2 * ix) * along, ay0 + iy
        if zone == "bottom":
            return ax0 + ix + (w - 2 * ix) * along, ay1 - iy
        down = ay0 + iy + (h - 2 * iy) * along if n > 1 else ay0 + h / 2
        if zone == "left":
            return ax0 + ix, down
        if zone == "right":
            return ax1 - ix, down
        if zone == "left-half":
            return ax0 + w * 0.25, down
        if zone == "right-half":
            return ax0 + w * 0.75, down
        # center / all: middle of the picture (several segments spread across it)
        return (ax0 + ix + (w - 2 * ix) * along if n > 1 else ax0 + w / 2), ay0 + h / 2

    def _region(self, zone, k, n):
        """Cells a segment reads: a band slice for multi-segment top/bottom lights, else a patch at its point."""
        if zone in ("top", "bottom") and n > 1:
            ax0, ay0, ax1, ay1 = self.active
            w, h = ax1 - ax0, ay1 - ay0
            ix = min(INSET_PX / self.cell[0], w / 4)
            iy = min(INSET_PX / self.cell[1], h / 4)
            xs = ax0 + ix + (w - 2 * ix) * k / n
            xe = ax0 + ix + (w - 2 * ix) * (k + 1) / n
            ys, ye = (ay1 - iy - h * BAND, ay1 - iy) if zone == "bottom" else (ay0 + iy, ay0 + iy + h * BAND)
            x0, y0 = int(xs), int(ys)
            return x0, y0, max(x0 + 1, int(math.ceil(xe))), max(y0 + 1, int(math.ceil(ye)))
        px, py = self._point(zone, k, n)
        rows, cols = (len(self.grid), len(self.grid[0])) if self.grid else (GRID[1], GRID[0])
        x0 = max(0, min(cols - PATCH, int(round(px - PATCH / 2))))
        y0 = max(0, min(rows - PATCH, int(round(py - PATCH / 2))))
        return x0, y0, x0 + PATCH, y0 + PATCH

    def colors(self, dev_id, n, zone):
        with self.lock:
            g, frame = self.grid, self.frame
        if g is None:
            return [(0, 0, 0)] * n
        now = time.time()
        tau = TAU_CUT if now - self.last_cut < 0.25 else (TAU_AMBIENT if zone in AMBIENT else TAU_NEAR)
        # long strips (more segments than BROAD) read BROAD wide areas and blend smoothly between them:
        # a calm gradient across the strip instead of a rainbow of separate readings
        m = BROAD if n > BROAD else n
        reads = []
        for k in range(m):
            cached = self.targets.get((zone, k, m))
            if cached and cached[0] == frame:
                reads.append(cached[1])
            else:
                x0, y0, x1, y1 = self._region(zone, k, m)
                t = _grade(_pick([p for row in g[y0:y1] for p in row[x0:x1]]))
                self.targets[(zone, k, m)] = (frame, t)
                reads.append(t)
        if m == n:
            targets = reads
        else:
            targets = []
            for k in range(n):
                pos = k * (m - 1) / (n - 1)                    # 0 .. m-1 along the strip
                i = min(int(pos), m - 2)
                f = pos - i
                targets.append(tuple(reads[i][j] * (1 - f) + reads[i + 1][j] * f for j in range(3)))
        out = []
        for k, target in enumerate(targets):
            s = self.smooth.get((dev_id, k))
            if s is None:
                s = self.smooth[(dev_id, k)] = [*target, now]
            a = 1 - math.exp(-(now - s[3]) / tau)
            for j in range(3):
                s[j] += (target[j] - s[j]) * a
            s[3] = now
            out.append(tuple(int(round(c)) for c in s[:3]))
        return out
