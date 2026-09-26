#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU dashboard for a Turing Smart Screen 3.5" (rev A) on the LLM server.

Why regions instead of one full frame:
  The panel takes RGB565 over 115200 baud (~11520 bytes/s on the wire). A full
  320x480 frame is 307,200 bytes = ~27 s of transfer, so a 5 s full-frame
  refresh is physically impossible. Each widget is therefore rendered as its
  own small bitmap at a fixed position, hashed, and pushed only when its
  content actually changed; the graphs are additionally time-throttled. A
  steady-state 5 s cycle then costs a few KB instead of 300 KB.

Usage:
  gpu_dash.py                 run forever, sample every --interval seconds
  gpu_dash.py --dry-run       compose regions to out/preview.png, no screen
  gpu_dash.py --once          one cycle then exit
  gpu_dash.py --report        wire cost per region, then exit
  gpu_dash.py --demo N        synthesise N history points (layout check)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

# ---------------- config ----------------
# Measured on the panel (2026-09-21): it is a native-landscape UsbMonitor 3.5".
# - orientation commands make bitmaps disappear -> do NOT send them
# - 480x320 bitmaps land with correct aspect ratio
# - the panel rotates received bitmaps 180 degrees -> pre-rotate (FLIP180)
PORT = "/dev/ttyACM0"
DESIGN_W, DESIGN_H = 480, 320
FLIP180 = True
INTERVAL = 5.0
HISTORY_MINUTES = 30
HISTORY_FILE = os.path.join(OUT_DIR, "history.json")
BRIGHTNESS = 100                    # percent
# LANDSCAPE renders the 320x480 design rotated: the panel becomes 480 wide x 320
# tall, so the layout reflows to a wide grid instead of a tall one.
ORIENTATION = "PORTRAIT"            # PORTRAIT | LANDSCAPE

TEMP_SCALE_MIN, TEMP_SCALE_MAX = 40, 90   # graph vertical range (below 40C never happens)
TEMP_WARN, TEMP_CRIT = 58, 70       # CORE/MEM font color: <58 green, <70 yellow, >=70 red
TEMP_LIMIT = 80                     # red threshold line on graphs (gpu-watchdog kills llama at 80 C)
POWER_WARN, POWER_CRIT = 80, 150    # PWR font color: green <=80W, yellow <=150W, red above
TEMP_MARKS = (40, 60, 80)
SPARK_MIN_INTERVAL = 20.0           # s between per-GPU sparkline pushes
TREND_MIN_INTERVAL = 45.0           # s between big-trend pushes
WATT_QUANT = 5                      # W rounding, keeps hashes stable
llama_unit = "llama-server.service"   # 自分の llama.cpp unit 名（プロセス検索の fallback でのみ使用）
# Tapo P110M (smart plug measuring the LLM server's mains power), via plugp100.
# Real values are set on the server (Environment= in gpu-dash.service);
# empty TAPO_USER disables the plug panel ("--").
TAPO_IP = os.environ.get("TAPO_IP", "YOUR_TAPO_IP")
TAPO_USER = os.environ.get("TAPO_USER", "YOUR_TAPO_USER")
TAPO_PASS = os.environ.get("TAPO_PASS", "YOUR_TAPO_PASS")
TAPO_POLL_INTERVAL = 30.0           # s between P110M polls (also the region throttle)
KWH_QUANT = 0.1                     # month-energy rounding, keeps hashes stable
# Plug color thresholds, in WALL power (AC in). The 760 W 80PLUS-Platinum PSU
# guarantees 90/92/89% at 20/50/100% load, so on the wall:
#   50% load = 760*0.50/0.92 = 413 W   -> green up to here
#   70% load = 760*0.70/0.91 = 585 W   -> yellow up to here, red above
PLUG_POWER_WARN = float(os.environ.get("PLUG_POWER_WARN", "415"))
PLUG_POWER_CRIT = float(os.environ.get("PLUG_POWER_CRIT", "590"))
# ---------------------------------------

GREEN = (0, 200, 83)
AMBER = (255, 176, 0)
RED = (255, 82, 82)
CYAN = (0, 180, 216)
BLUE = (80, 140, 250)
WHITE = (245, 247, 250)
GREY = (140, 150, 165)
BG = (12, 14, 20)
PANEL = (24, 28, 38)
HEADER = (28, 34, 50)
TRACK = (18, 21, 29)
OUTLINE = (60, 68, 84)

QUERY = ("index,name,utilization.gpu,utilization.memory,temperature.gpu,"
         "temperature.memory,power.draw,power.limit,memory.used,memory.total,"
         "clocks.sm,clocks.mem,pstate")

# ---- Turing Smart Screen hardware revision A commands (values verified against
# ---- mathoudebine/turing-smart-screen-python library/lcd/lcd_comm_rev_a.py)
CMD_HELLO = 69
CMD_CLEAR = 102
CMD_SCREEN_OFF = 108
CMD_SCREEN_ON = 109
CMD_SET_BRIGHTNESS = 110
CMD_SET_ORIENTATION = 121
CMD_DISPLAY_BITMAP = 197
ORIENT_PORTRAIT = 0
ORIENT_REVERSE_PORTRAIT = 1
ORIENT_LANDSCAPE = 2
ORIENT_REVERSE_LANDSCAPE = 3
ORIENTS = {"PORTRAIT": ORIENT_PORTRAIT, "REVERSE_PORTRAIT": ORIENT_REVERSE_PORTRAIT,
           "LANDSCAPE": ORIENT_LANDSCAPE, "REVERSE_LANDSCAPE": ORIENT_REVERSE_LANDSCAPE}


def cmd_bytes(cmd: int, x: int, y: int, ex: int, ey: int) -> bytes:
    """6-byte frame header every rev-A command starts with."""
    return bytes([
        (x >> 2),
        ((x & 3) << 6) | (y >> 4),
        ((y & 15) << 4) | (ex >> 6),
        ((ex & 63) << 2) | (ey >> 8),
        ey & 255,
        cmd,
    ])


class Turing:
    """Minimal rev-A serial driver."""

    def __init__(self, port: str, width: int = DESIGN_W, height: int = DESIGN_H,
                 flip180: bool = FLIP180):
        import serial
        self.serial = serial.Serial(port, 115200, timeout=5, rtscts=True)
        self.width, self.height = width, height
        self.flip180 = flip180
        self.wire_bytes = 0

    def _flush(self):
        try:
            self.serial.reset_input_buffer()
        except Exception:
            pass

    def _write(self, data: bytes):
        self.serial.write(data)
        self.wire_bytes += len(data)

    def hello(self):
        """Ask the panel which model it is. Official Turing 3.5\" rev A stays
        silent; UsbMonitor variants answer 0x01/0x02/0x03 repeated 6 times."""
        self._write(cmd_bytes(CMD_HELLO, 0, 0, 0, 0) * 6)
        time.sleep(0.25)
        try:
            resp = self.serial.read(6)
        except Exception:
            resp = b""
        self._flush()
        known = {1: (320, 480, "UsbMonitor 3.5\""), 2: (480, 800, "UsbMonitor 5\""),
                 3: (600, 1024, "UsbMonitor 7\"")}
        code = resp[0] if resp else 0
        if code in known:
            w, h, name = known[code]
            print(f"HELLO {list(resp)} -> {name} {w}x{h}", file=sys.stderr)
            self.width, self.height = w, h
        else:
            print(f"HELLO {list(resp) or 'no answer'} -> assume Turing 3.5\" "
                  f"{self.width}x{self.height}", file=sys.stderr)

    def init(self, orientation="NONE"):
        # Panel fact (probe 2026-09-21): sending SET_ORIENTATION makes later
        # bitmaps vanish. Only SCREEN_ON + brightness + CLEAR are needed.
        self.hello()
        self._write(cmd_bytes(CMD_SCREEN_ON, 0, 0, 0, 0))
        self.set_brightness(BRIGHTNESS)
        self._write(cmd_bytes(CMD_CLEAR, 0, 0, 0, 0))
        if orientation != "NONE":
            self.set_orientation(ORIENTS[orientation])

    def set_orientation(self, value: int):
        """Reference library sends a 16-byte frame: the 6-byte header, then
        orientation+100, then the panel width and height as 16-bit big-endian.
        Omitting the trailing 10 bytes is accepted by some panels but the
        layout the controller builds after a Clear() depends on them."""
        w, h = (self.width, self.height) if value in (ORIENT_PORTRAIT, ORIENT_REVERSE_PORTRAIT) \
            else (self.height, self.width)
        frame = cmd_bytes(CMD_SET_ORIENTATION, 0, 0, 0, 0) + bytes([
            value + 100,
            (w >> 8) & 0xFF, w & 0xFF,
            (h >> 8) & 0xFF, h & 0xFF,
        ]) + bytes(4)
        self._write(frame)
        self.orientation = value

    def set_brightness(self, percent: int):
        level = int(255 - (max(0, min(100, percent)) / 100.0) * 255)   # 0 = brightest
        self._write(cmd_bytes(CMD_SET_BRIGHTNESS, 0, 0, 0, 0) + bytes([level]))

    def display_image(self, img: Image.Image, x: int, y: int):
        w, h = img.size
        if not (0 <= x and 0 <= y and x + w <= self.width and y + h <= self.height):
            raise ValueError(f"region {w}x{h} at ({x},{y}) outside "
                             f"{self.width}x{self.height}")
        if self.flip180:
            # panel displays everything rotated 180deg; compensate
            img = img.rotate(180)
            x = self.width - x - w
            y = self.height - y - h
        rgb = img.convert("RGB").load()
        out = bytearray(w * h * 2)
        o = 0
        for yy in range(h):
            for xx in range(w):
                r, g, b = rgb[xx, yy][:3]
                v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                out[o] = v & 0xFF
                out[o + 1] = v >> 8
                o += 2
        self._write(cmd_bytes(CMD_DISPLAY_BITMAP, x, y, x + w - 1, y + h - 1))
        chunk = self.width * 8          # same chunk size as the reference library
        for i in range(0, len(out), chunk):
            part = bytes(out[i:i + chunk])
            self.serial.write(part)
            self.wire_bytes += len(part)

    def close(self):
        try:
            self.serial.close()
        except Exception:
            pass


# ---------------- sampling ----------------
def sample_gpus():
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as exc:
        print(f"[warn] nvidia-smi failed: {exc}", file=sys.stderr)
        return []
    gpus = []
    for line in out.splitlines():
        f = [c.strip() for c in line.split(",")]
        if len(f) < 13:
            continue

        def num(v):
            try:
                return float(v)
            except ValueError:
                return None

        gpus.append({
            "index": f[0], "name": f[1],
            "gpu_util": num(f[2]), "mem_util": num(f[3]),
            "temp": num(f[4]), "mem_temp": num(f[5]),
            "power": num(f[6]), "power_limit": num(f[7]),
            "mem_used": num(f[8]), "mem_total": num(f[9]),
            "clk_sm": num(f[10]), "clk_mem": num(f[11]),
            "pstate": f[12],
        })
    return gpus


def proc_cmdlines():
    """(pid, argv) for every process readable to us, straight from /proc."""
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:                     # no /proc (dry-run preview on Windows)
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                raw = fh.read()
        except Exception:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if argv:
            out.append((entry, argv))
    return out


def model_label_from_path(path):
    """/models/SomeModel-GGUF/Q4_K_M/xxx.gguf
    -> 'SomeModel-Q4_K_M' (repo name minus -GGUF, plus quant dir)."""
    parts = [x for x in path.split("/") if x]
    for i, part in enumerate(parts):
        if part.endswith("-GGUF"):
            base = part[:-len("-GGUF")]
            between = parts[i + 1:-1]          # directories between repo dir and file
            if between:
                return f"{base}-{between[-1]}"
            return base
    return parts[-1].rsplit(".", 1)[0]         # fallback: bare filename


def llama_process():
    """Return (True, model_label) when a llama-server process is alive, else
    (False/None, None). The model label is extracted from its --model argument."""
    for pid, argv in proc_cmdlines():
        if not any("llama-server" in a for a in argv[:2]):
            continue
        model = None
        for i, a in enumerate(argv):
            if a in ("-m", "--model") and i + 1 < len(argv):
                model = argv[i + 1]
                break
            if a.startswith("--model="):
                model = a.split("=", 1)[1]
                break
        return True, (model_label_from_path(model) if model else None)
    return None, None


def comfyui_running():
    for pid, argv in proc_cmdlines():
        joined = " ".join(argv)
        if "ComfyUI/main.py" in joined:
            return True
    return False


def llama_running():
    """Process scan first (manual launches count); systemd unit only as fallback."""
    running, label = llama_process()
    if running:
        return True, label
    try:
        state = subprocess.run(["systemctl", "is-active", llama_unit],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        if state == "active":
            return True, None
        if state in ("inactive", "failed"):
            return False, None
    except Exception:
        pass
    return None, None


# ---- Tapo P110M (TPAP) ----
_tapo = {"t": 0.0, "power_w": None, "month_kwh": None}


def sample_tapo(force=False):
    """Poll the P110M every TAPO_POLL_INTERVAL s; cache the last good values.
    Returns the cache dict. Never raises: on any failure the old values stay
    and the panel keeps rendering (possibly "--")."""
    now = time.time()
    if not force and (now - _tapo["t"]) < TAPO_POLL_INTERVAL:
        return _tapo
    _tapo["t"] = now
    if not TAPO_USER:
        return _tapo
    try:
        import asyncio
        import aiohttp
        from plugp100 import TapoDiscovery, connect_discovered_device
        from plugp100.common.credentials import AuthCredential

        async def _read():
            cred = AuthCredential(TAPO_USER, TAPO_PASS)
            async with aiohttp.ClientSession() as session:
                disc = TapoDiscovery(TAPO_IP, 20802, 10)
                devices = await disc.scan(timeout=10)
                if not devices:
                    raise RuntimeError("P110M not discovered")
                dev = devices[0]
                tapo_dev = await connect_discovered_device(dev, cred, session)
                # device-level API works for componentless TPAP firmwares
                e = await tapo_dev.client.get_energy_usage()
                if not e.is_success():
                    raise RuntimeError(str(e.error()))
                return e.get()
        info = asyncio.run(_read())
        _tapo["power_w"] = (info.current_power or 0) / 1000.0      # mW -> W
        _tapo["month_kwh"] = (info.month_energy or 0) / 1000.0     # mWh -> kWh
    except Exception as exc:
        print(f"tapo poll failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return _tapo


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(history, fh, separators=(",", ":"))
    os.replace(tmp, HISTORY_FILE)


# ---------------- drawing helpers ----------------
SCALE = 1.0
_fonts = {}


def S(v):
    return int(round(v * SCALE))


def font(kind, size):
    size = max(8, size)
    key = (kind, size)
    if key not in _fonts:
        path = {
            "mono": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "mono_bold": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "sans_bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        }[kind]
        if not os.path.exists(path):    # preview off the server (Windows): use Consolas/Arial
            path = {"mono": "consola.ttf", "mono_bold": "consolab.ttf",
                    "sans": "arial.ttf", "sans_bold": "arialbd.ttf"}[kind]
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def tw(d, s, f):
    b = d.textbbox((0, 0), s, font=f)
    return b[2] - b[0]


def text_fit(d, xy, s, kind, size, max_w, fill):
    """Draw s with its LEFT edge at xy, shrinking the font until it fits max_w."""
    size = max(8, size)
    while size > 9 and tw(d, s, font(kind, size)) > max_w:
        size -= 1
    d.text((xy[0], xy[1]), s, font=font(kind, size), fill=fill)
    return size


def text_fit_r(d, right_x, y, s, kind, size, max_w, fill):
    """Draw s with its RIGHT edge at right_x, shrinking until it fits max_w.
    (right_x - measured width is the left edge - this is what the clipped
    MEM/PWR values were missing.)"""
    size = max(8, size)
    while size > 9 and tw(d, s, font(kind, size)) > max_w:
        size -= 1
    d.text((right_x - tw(d, s, font(kind, size)), y), s, font=font(kind, size), fill=fill)
    return size


def temp_color(t):
    if t is None:
        return GREY
    if t >= TEMP_CRIT:
        return RED
    if t >= TEMP_WARN:
        return AMBER
    return GREEN


def power_color(p_w):
    if p_w is None:
        return GREY
    if p_w > POWER_CRIT:
        return RED
    if p_w > POWER_WARN:
        return AMBER
    return GREEN


def plug_power_color(p_w):
    """Watts from the P110M are WALL power -> thresholds in PLUG_POWER_WARN/CRIT."""
    if p_w is None:
        return GREY
    if p_w > PLUG_POWER_CRIT:
        return RED
    if p_w > PLUG_POWER_WARN:
        return AMBER
    return GREEN


def gpu(gpus, k):
    return gpus[k] if k < len(gpus) else None


def draw_graph(d, w, h, series, colors, title, time_labels=None):
    """Multi-series line graph inside a w x h region.

    Y ticks sit on the LEFT of the plot area so the trace (which ends at "now",
    i.e. the right edge) can never cross the tick labels.
    """
    f = font("mono", S(11))
    d.text((S(4), S(2)), title, font=f, fill=WHITE)
    px1 = w - S(7)
    label_room = S(16) if time_labels else 0
    py0, py1 = S(18), h - S(7) - label_room

    # thin the marks until adjacent gridlines have room for their labels
    marks = list(TEMP_MARKS)
    step_val = marks[1] - marks[0]
    bb0 = d.textbbox((0, 0), str(marks[0]), font=f)
    label_h = bb0[3] - bb0[1]
    plot_h = py1 - py0
    scale_span = TEMP_SCALE_MAX - TEMP_SCALE_MIN
    while len(marks) > 1 and plot_h * step_val / scale_span < label_h + 3:
        step_val *= 2
        marks = marks[::2] if len(marks) > 2 else marks[-1:]
    tick_w = max(tw(d, str(v), f) for v in marks) if marks else 0
    px0 = S(4) + tick_w + S(8)
    for val in marks:
        yy = py1 - int(plot_h * (val - TEMP_SCALE_MIN) / scale_span)
        d.line([(px0 - S(5), yy), (px1, yy)],
               fill=(RED if val >= TEMP_LIMIT else OUTLINE))
        bb = d.textbbox((0, 0), str(val), font=f)
        d.text((S(4), yy - (bb[3] - bb[1]) // 2 - bb[1]), str(val), font=f, fill=GREY)
    n = max((len(s) for s in series), default=0)
    if n >= 2:
        for s, c in zip(series, colors):
            pts = []
            for i, v in enumerate(s):
                if v is None:
                    continue
                px = px0 + i * (px1 - px0) / (n - 1)
                ratio = min(1.0, max(0.0, (v - TEMP_SCALE_MIN) / scale_span))
                py = py1 - ratio * (py1 - py0)
                pts.append((px, py))
            if len(pts) >= 2:
                d.line(pts, fill=c, width=max(1, S(2)))
    if time_labels:
        ft = font("mono", S(10))
        for lab, frac in time_labels:
            xx = px0 + (px1 - px0) * frac
            xx = max(S(2), min(xx, w - S(2) - tw(d, lab, ft)))
            d.text((xx, py1 + S(3)), lab, font=ft, fill=GREY)


def draw_bar(d, w, h, value, vmax, color, label, val_text):
    flab = font("mono", S(13))
    fval = font("mono_bold", S(14))
    d.text((S(2), S(1)), label, font=flab, fill=GREY)
    d.text((w - S(2) - tw(d, val_text, fval), S(0)), val_text, font=fval, fill=color)
    by = S(15)
    d.rectangle([S(2), by, w - S(2), h - S(3)], fill=TRACK, outline=OUTLINE)
    if value is not None and vmax:
        frac = max(0.0, min(1.0, value / vmax))
        fw = int((w - S(4)) * round(frac * 50) / 50)   # 2% steps -> fewer re-pushes
        if fw > 0:
            d.rectangle([S(3), by + 1, S(3) + fw, h - S(3)], fill=color)


# ---------------- regions ----------------
class Region:
    __slots__ = ("name", "x", "y", "w", "h", "min_interval", "bg", "border",
                 "draw_fn", "last_hash", "last_sent", "image")

    def __init__(self, name, x, y, w, h, bg=BG, border=False,
                 min_interval=0.0, draw_fn=None):
        self.name, self.x, self.y, self.w, self.h = name, x, y, w, h
        self.bg, self.border, self.min_interval, self.draw_fn = bg, border, min_interval, draw_fn
        self.last_hash, self.last_sent, self.image = None, 0.0, None

    @property
    def bytes(self):
        return self.w * self.h * 2

    def build(self, ctx):
        img = Image.new("RGB", (self.w, self.h), self.bg)
        d = ImageDraw.Draw(img)
        if self.border:
            d.rectangle([0, 0, self.w - 1, self.h - 1], fill=self.bg, outline=OUTLINE)
        if self.draw_fn:
            self.draw_fn(d, self.w, self.h, ctx)
        self.image = img
        return img


def build_regions(gpus, history, llama, now):
    """Ordered region list for one cycle (native 480x320 landscape layout).
    Static frames come first so dynamic widgets paint on top of them.
    All positions are checked by the asserts at the end."""
    W = int(round(DESIGN_W * SCALE))
    H = int(round(DESIGN_H * SCALE))
    pad, gap = 0, 0                    # no dead space: panels touch each other
    pw = (W - 2 * pad - gap) // 2

    cy = pad                        # cards start at the very top (no header)
    ch = S(160)                     # cards height
    ty = cy + ch                    # trend panel touches the cards
    th = S(84)
    sy = ty + th                    # status panel touches the trend
    sh = H - sy - pad

    R = []

    def add(*a, **kw):
        R.append(Region(*a, **kw))

    # ---- two GPU cards side by side (no header bar) ----
    for k in range(2):
        x0 = pad + k * (pw + gap)

        def frame(d, w, h, c, k=k):
            # "GPU 0 (Tesla V100-PCIE-32GB)" - left edge; shrinks until it fits
            # the width reserved before the pstate widget (w - S(46)).
            g = gpu(c["gpus"], k)
            label = f"GPU {k} ({g['name']})" if (g and g.get("name")) else f"GPU {k}"
            text_fit(d, (S(5), S(4)), label, "sans_bold", S(13), w - S(46), BLUE)

        add(f"card{k}", x0, cy, pw, ch, bg=PANEL, border=True, draw_fn=frame)

        def pstate(d, w, h, c, k=k):
            g = gpu(c["gpus"], k)
            s = (g.get("pstate") or "--") if g else "--"
            text_fit_r(d, w - S(8), S(4), s, "mono", S(11), S(34), GREY)

        add(f"pstate{k}", x0 + pw - S(42), cy + S(2), S(40), S(14), bg=PANEL,
            min_interval=15.0, draw_fn=pstate)

        ix, iw = x0 + S(2), pw - 2 * S(2)   # card interior minus 2px each side
        col_gap = S(1)
        col_w = (iw - 2 * col_gap) // 3     # three equal columns

        def big_readout(d, w, h, c, k=k, label="", unit="", kind="temp"):
            g = gpu(c["gpus"], k)
            if kind == "temp":
                v = g["temp"] if g else None
                col = temp_color(v)
            elif kind == "memt":
                v = g["mem_temp"] if g else None
                col = temp_color(v)
            else:
                v = g["power"] if g else None
                col = power_color(v)
            if v is None:
                big = "--"
            elif kind == "power":
                big = str(int(round(v / WATT_QUANT) * WATT_QUANT))
            else:
                big = str(int(v))
            text_fit(d, (S(2), S(1)), label, "mono", S(10), w - S(4), GREY)
            txt = big + unit
            # font size is fixed from a worst-case string (pre-reserved width),
            # so 3-digit 135W renders the same size as 2-digit 50W - no jitter
            reserve = "255W" if kind == "power" else "100C"
            size = S(28)
            while size > S(14) and tw(d, reserve, font("sans_bold", size)) > w - S(4):
                size -= 1
            d.text((S(2), S(13)), txt, font=font("sans_bold", size), fill=col)

        add(f"temp{k}", ix, cy + S(18), col_w, S(46), bg=PANEL,
            draw_fn=lambda d, w, h, c, k=k: big_readout(d, w, h, c, k, "CORE", "℃", "temp"))
        add(f"memt{k}", ix + col_w + col_gap, cy + S(18), col_w, S(46), bg=PANEL,
            draw_fn=lambda d, w, h, c, k=k: big_readout(d, w, h, c, k, "MEM", "℃", "memt"))
        add(f"pwr{k}", ix + 2 * (col_w + col_gap), cy + S(18), iw - 2 * (col_w + col_gap), S(46),
            bg=PANEL, draw_fn=lambda d, w, h, c, k=k: big_readout(d, w, h, c, k, "PWR", "W", "power"))

        def spark(d, w, h, c, k=k):
            draw_graph(d, w, h,
                       [[hst["gpus"][k]["temp"] for hst in c["history"]
                         if len(hst["gpus"]) > k]], [GREEN], "TEMP")

        add(f"spark{k}", x0 + S(2), cy + S(66), pw - S(4), S(44), bg=PANEL,
            min_interval=SPARK_MIN_INTERVAL, draw_fn=spark)

        def util(d, w, h, c, k=k):
            g = gpu(c["gpus"], k)
            u = g["gpu_util"] if g else None
            draw_bar(d, w, h, u, 100, CYAN, "UTIL",
                     f"{int(u)}%" if u is not None else "--")

        add(f"util{k}", x0 + S(2), cy + S(112), pw - S(4), S(22), bg=PANEL, draw_fn=util)

        def vram(d, w, h, c, k=k):
            g = gpu(c["gpus"], k)
            mu = g["mem_used"] if g else None
            mt = (g["mem_total"] if g else None) or 32768
            draw_bar(d, w, h, mu, mt, BLUE, "VRAM",
                     f"{int(mu)}/{int(mt)}" if mu is not None else "--")

        add(f"vram{k}", x0 + S(2), cy + S(136), pw - S(4), S(22), bg=PANEL, draw_fn=vram)

    # ---- trend panel (half width) + plug panel (half width) ----
    def trend(d, w, h, c):
        series = [[hst["gpus"][k]["temp"] for hst in c["history"] if len(hst["gpus"]) > k]
                  for k in range(2)]
        # half width: only the end labels fit without collision
        draw_graph(d, w, h, series, [GREEN, AMBER], "CORE TEMP TREND",
                   time_labels=[("-30m", 0.0), ("now", 1.0)])

    hw = (W - 2 * pad) // 2
    add("trend", pad, ty, hw, th, bg=PANEL, border=True,
        min_interval=TREND_MIN_INTERVAL, draw_fn=trend)
    add("plug_bg", pad + hw + pad, ty, hw, th, bg=PANEL, border=True)

    def plug_power(d, w, h, c):
        # big instant-watts readout (left half of the plug panel). Font size is
        # fixed from the worst case "255W", so digits never jitter between frames.
        t = c["tapo"]
        pw = t["power_w"]
        big = "--" if pw is None else str(int(round(pw / WATT_QUANT) * WATT_QUANT))
        text_fit(d, (S(2), S(1)), "PLUG W", "mono", S(10), w - S(4), GREY)
        txt = big + "W"
        size = S(26)
        while size > S(14) and tw(d, "255W", font("sans_bold", size)) > w - S(4):
            size -= 1
        d.text((S(2), S(14)), txt, font=font("sans_bold", size),
               fill=plug_power_color(pw))

    def plug_month(d, w, h, c):
        # big month-energy readout (right half), same style/size as the watts,
        # always BLUE (cumulative value, no alarm meaning).
        t = c["tapo"]
        kwh = t["month_kwh"]
        text_fit(d, (S(2), S(1)), "MONTH", "mono", S(10), w - S(4), GREY)
        txt = "  --" if kwh is None else \
            f"{round(kwh / KWH_QUANT) * KWH_QUANT:.1f}"
        size = S(26)
        while size > S(14) and tw(d, "999.9", font("sans_bold", size)) > w - S(4):
            size -= 1
        d.text((S(2), S(14)), txt, font=font("sans_bold", size), fill=BLUE)
        d.text((S(2), S(40)), "kWh", font=font("mono", S(11)), fill=BLUE)

    # 2px inset inside the plug panel frame (opaque regions repaint borders)
    add("plug_pwr", pad + hw + pad + S(2), ty + S(2), hw // 2 - S(2), S(56),
        bg=PANEL, min_interval=TAPO_POLL_INTERVAL, draw_fn=plug_power)
    add("plug_month", pad + hw + pad + hw // 2, ty + S(2), hw // 2 - S(2), S(56),
        bg=PANEL, min_interval=TAPO_POLL_INTERVAL, draw_fn=plug_month)

    # ---- status panel (full width, 2 rows) ----
    add("status_bg", pad, sy, W - 2 * pad, sh, bg=PANEL, border=True)

    def totals(d, w, h, c):
        pw_ = sum(g["power"] for g in c["gpus"] if g.get("power") is not None)
        utils = [g["gpu_util"] for g in c["gpus"] if g.get("gpu_util") is not None]
        ua = sum(utils) / len(utils) if utils else None
        left = f"TOTAL {int(round(pw_ / WATT_QUANT) * WATT_QUANT):4d} W"
        mid = f"AVG UTIL {int(ua) if ua is not None else '--'}%"
        text_fit(d, (S(5), S(3)), left, "mono_bold", S(12), S(78), WHITE)
        text_fit(d, (S(88), S(3)), mid, "mono_bold", S(12), S(92), CYAN)

    add("totals", pad + S(2), sy + S(2), S(184), S(19), bg=PANEL, draw_fn=totals)

    def status_dot(d, w, h, c, key, name):
        state = c.get(key)
        if state is True:
            col, txt = GREEN, f"{name} ACTIVE"
        elif state is False:
            col, txt = RED, f"{name} OFF"
        else:
            col, txt = GREY, f"{name} ?"
        d.ellipse([S(3), S(6), S(11), S(14)], fill=col)
        text_fit(d, (S(15), S(3)), txt, "mono", S(11), w - S(18), col)

    def svc(d, w, h, c):
        status_dot(d, w, h, c, "llama", "llama")

    def comfy(d, w, h, c):
        status_dot(d, w, h, c, "comfyui", "ComfyUI")

    add("svc", pad + S(188), sy + S(2), S(100), S(19), bg=PANEL, min_interval=15.0, draw_fn=svc)
    add("comfy", pad + S(292), sy + S(2), S(96), S(19), bg=PANEL, min_interval=15.0, draw_fn=comfy)

    def pts(d, w, h, c):
        text_fit_r(d, w - S(2), S(3), f"{len(c['history'])} pts", "mono", S(11), w - S(4), GREY)

    add("pts", pad + S(392), sy + S(2), W - 2 * pad - S(394), S(19), bg=PANEL,
        min_interval=30.0, draw_fn=pts)

    def clocks(d, w, h, c):
        txt = " ".join(f"G{g['index']} {int(g['clk_sm'])}/{int(g['clk_mem'] or 0)}MHz"
                       for g in c["gpus"] if g.get("clk_sm") is not None)
        text_fit(d, (S(5), S(2)), txt or "--", "mono", S(11), w - S(10), GREY)

    add("clocks", pad + S(2), sy + S(23), S(226), S(15), bg=PANEL, min_interval=15.0, draw_fn=clocks)

    def stamp(d, w, h, c):
        text_fit(d, (S(4), S(2)), c["now"].strftime("%m-%d %H:%M:%S"), "mono", S(11), w - S(8), CYAN)

    add("stamp", pad + S(234), sy + S(23), S(120), S(15), bg=PANEL, min_interval=8.0, draw_fn=stamp)

    def refresh(d, w, h, c):
        text_fit_r(d, w - S(2), S(2), f"push {INTERVAL:.0f}s", "mono", S(11), w - S(4), GREY)

    add("refresh", pad + S(358), sy + S(23), W - 2 * pad - S(360), S(15), bg=PANEL,
        min_interval=60.0, draw_fn=refresh)

    def model(d, w, h, c):
        label = c.get("model") or "no model"
        text_fit(d, (S(5), S(2)), label, "mono_bold", S(17), w - S(10), WHITE)

    add("model", pad + S(2), sy + S(41), W - 2 * pad - 2 * S(2), S(24), bg=PANEL,
        min_interval=20.0, draw_fn=model)

    # ---- geometry verification ----
    for r in R:
        assert 0 <= r.x and 0 <= r.y and r.x + r.w <= W and r.y + r.h <= H, \
            f"region {r.name} {r.w}x{r.h} @({r.x},{r.y}) outside {W}x{H}"
    for k in range(2):
        cx, cw = pad + k * (pw + gap), pw
        kids = [r for r in R if r.name.endswith(str(k))
                and r.name.startswith(("pstate", "temp", "memt", "pwr", "spark", "util", "vram"))]
        for i, a1 in enumerate(kids):
            for b1 in kids[i + 1:]:
                assert (a1.x + a1.w <= b1.x or b1.x + b1.w <= a1.x
                        or a1.y + a1.h <= b1.y or b1.y + b1.h <= a1.y), \
                    f"card{k} widgets {a1.name} and {b1.name} overlap"
        for r in kids:
            assert cx <= r.x and r.x + r.w <= cx + cw + 1, f"{r.name} outside card {k}"
            assert cy <= r.y and r.y + r.h <= cy + ch + 1, f"{r.name} outside card {k}"
    for r in R:
        if r.name == "trend":
            assert r.y >= cy + ch and r.y + r.h <= sy and r.w <= hw
    assert pad + 2 * hw <= W - pad + 1, "trend+plug halves exceed canvas width"
    for r in R:
        if r.name.startswith("plug"):
            assert r.x >= pad + hw, f"{r.name} crosses into the trend half"
            assert r.y >= ty and r.y + r.h <= sy + 1, f"{r.name} outside plug panel"
            if r.name != "plug_bg":
                assert r.x + r.w <= pad + 2 * hw - 1, f"{r.name} eats the plug frame"
    status = [r for r in R if r.y > sy and r.name != "trend"]
    for i, a1 in enumerate(status):
        for b1 in status[i + 1:]:
            assert (a1.x + a1.w <= b1.x or b1.x + b1.w <= a1.x
                    or a1.y + a1.h <= b1.y or b1.y + b1.h <= a1.y), \
                f"status widgets {a1.name} and {b1.name} overlap"
    for r in status:
            assert pad <= r.x and r.x + r.w <= W - pad + 1 and r.y + r.h <= sy + sh + 1, \
                f"{r.name} outside status panel"
    return R


# ---------------- push / composite ----------------
def push(regions, ctx, screen, tnow):
    """Send only regions whose rendered content changed, honouring min_interval."""
    sent, throttled = [], 0
    for r in regions:
        img = r.build(ctx)
        h = hashlib.md5(img.tobytes()).hexdigest()
        if h == r.last_hash:
            continue                      # unchanged: nothing on the wire
        if r.min_interval and (tnow - r.last_sent) < r.min_interval:
            throttled += r.bytes
            continue
        if screen is not None:
            screen.display_image(img, r.x, r.y)
        r.last_hash = h
        r.last_sent = tnow
        sent.append((r.name, r.bytes))
    return sent, throttled


def composite(regions, ctx):
    canvas = Image.new("RGB", (int(round(DESIGN_W * SCALE)), int(round(DESIGN_H * SCALE))), BG)
    for r in regions:
        canvas.paste(r.build(ctx), (r.x, r.y))
    return canvas


# ---------------- main ----------------
def main():
    global INTERVAL, SPARK_MIN_INTERVAL, TREND_MIN_INTERVAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="compose PNG only")
    ap.add_argument("--report", action="store_true", help="wire cost per region, then exit")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--interval", type=float, default=INTERVAL)
    ap.add_argument("--spark-interval", type=float, default=SPARK_MIN_INTERVAL)
    ap.add_argument("--trend-interval", type=float, default=TREND_MIN_INTERVAL)
    ap.add_argument("--demo", type=int, default=0, help="synthesise N history points")
    ap.add_argument("--fake-load", type=float, default=0.0,
                    help="preview: multiply measured power by this factor")
    args = ap.parse_args()

    INTERVAL = args.interval
    SPARK_MIN_INTERVAL = args.spark_interval
    TREND_MIN_INTERVAL = args.trend_interval

    os.makedirs(OUT_DIR, exist_ok=True)
    screen = None
    if not args.dry_run:
        try:
            screen = Turing(args.port)
            screen.init()
        except Exception as exc:
            print(f"cannot open screen {args.port}: {exc}", file=sys.stderr)
            print("check: /dev/ttyACM0 permissions (udev rule), no other program "
                  "holding the port", file=sys.stderr)
            return 2
        SCALE = min(screen.width / DESIGN_W, screen.height / DESIGN_H)
        print(f"screen ready {args.port} {screen.width}x{screen.height} "
              f"scale={SCALE:.2f}", file=sys.stderr)

    max_points = max(2, int(HISTORY_MINUTES * 60 / INTERVAL))
    history = load_history()

    if args.demo:
        import math
        import random
        history = []
        base = datetime.now().timestamp()
        for i in range(args.demo):
            ts = base - (args.demo - 1 - i) * INTERVAL
            tt = 42 + 18 * abs(math.sin(i / 9.0)) + random.uniform(-1.5, 1.5)
            u = 40 + 55 * abs(math.sin(i / 7.0 + 1))
            history.append({"ts": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
                            "gpus": [{"name": "Tesla V100-PCIE-32GB",
                                      "temp": round(tt, 1), "gpu_util": round(u, 1),
                                      "mem_temp": round(tt + 2, 1), "power": round(90 + u, 1),
                                      "mem_used": 28000 + i * 10},
                                     {"name": "Tesla V100-PCIE-32GB",
                                      "temp": round(tt + 3, 1), "gpu_util": round(u * 0.9, 1),
                                      "mem_temp": round(tt + 5, 1), "power": round(95 + u, 1),
                                      "mem_used": 31000 + i * 5}]})

    if args.report:
        ls, ml = llama_running()
        ctx = {"gpus": sample_gpus(), "history": history, "llama": ls, "model": ml,
               "comfyui": comfyui_running(), "now": datetime.now(),
               "tapo": sample_tapo(force=True)}
        regions = build_regions(ctx["gpus"], ctx["history"], ctx["llama"], ctx["now"])
        total = 0
        print(f"{'region':<11} {'x':>4} {'y':>4} {'w':>4} {'h':>4} {'bytes':>7} "
              f"{'wire_s':>7} {'throttle':>9}")
        for r in regions:
            total += r.bytes
            print(f"{r.name:<11} {r.x:>4} {r.y:>4} {r.w:>4} {r.h:>4} {r.bytes:>7} "
                  f"{r.bytes/11520:>7.2f} {r.min_interval:>9.0f}")
        print(f"total if everything pushed: {total} B = {total/11520:.1f} s wire "
              f"(full-frame worst case {DESIGN_W*DESIGN_H*2} B = "
              f"{DESIGN_W*DESIGN_H*2/11520:.1f} s)")
        composite(regions, ctx).save(os.path.join(OUT_DIR, "preview.png"))
        return 0

    while True:
        t0 = time.time()
        now = datetime.now()
        gpus = sample_gpus()
        if args.fake_load:
            for g in gpus:
                if g["power"] is not None:
                    g["power"] *= args.fake_load
        if gpus and not args.demo:
            history.append({"ts": now.isoformat(timespec="seconds"),
                            "gpus": [{"temp": g["temp"], "gpu_util": g["gpu_util"],
                                      "mem_temp": g["mem_temp"], "power": g["power"],
                                      "mem_used": g["mem_used"]} for g in gpus]})
            history = history[-max_points:]
            save_history(history)

        llama_state, model_label = llama_running()
        tapo = sample_tapo()
        if args.demo and tapo["power_w"] is None:
            # demo layout preview without touching the plug
            tapo = dict(tapo, power_w=146.0, month_kwh=28.4)
        ctx = {"gpus": gpus, "history": history, "llama": llama_state,
               "model": model_label, "comfyui": comfyui_running(), "now": now,
               "tapo": tapo}
        regions = build_regions(gpus, history, ctx["llama"], now)
        try:
            sent, throttled = push(regions, ctx, screen, t0)
        except Exception as exc:
            print(f"screen write failed: {exc}", file=sys.stderr)
            if screen:
                try:
                    screen.close()
                except Exception:
                    pass
            screen = None
            if not args.once:
                time.sleep(10)
                try:
                    screen = Turing(args.port)
                    screen.init()
                    SCALE = min(screen.width / DESIGN_W, screen.height / DESIGN_H)
                    print("screen reopened", file=sys.stderr)
                except Exception as exc2:
                    print(f"reopen failed: {exc2}", file=sys.stderr)
            sent, throttled = [], 0

        if args.dry_run:
            composite(regions, ctx).save(os.path.join(OUT_DIR, "preview.png"))

        print(f"{now.strftime('%H:%M:%S')} gpus={len(gpus)} "
              f"temps={[g['temp'] for g in gpus]} util={[g['gpu_util'] for g in gpus]} "
              f"hist={len(history)} pushed={sum(b for _, b in sent)}B "
              f"({','.join(n for n, _ in sent)}) throttled={throttled}B "
              f"wire_total={screen.wire_bytes if screen else 0}B "
              f"cycle={time.time() - t0:.2f}s", file=sys.stderr)

        if args.once:
            break
        time.sleep(max(0.0, INTERVAL - (time.time() - t0)))

    if screen is not None:
        screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
