#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Orientation probe for the Turing/UsbMonitor 3.5" screen.

Sends a 4-color quadrant test pattern at the requested orientation so a human
can read off how the panel maps coordinates: which letter lands in which
physical corner, and whether letters are upright, rotated or mirrored.

Run on the LLM server (gpu-dash must be stopped first, the serial port is
exclusive):

  sudo systemctl stop gpu-dash
  sudo ~/gpu_dash/venv/bin/python ~/gpu_dash/orient_probe.py --orientation LANDSCAPE
  sudo systemctl start gpu-dash      # restore afterwards
"""
import argparse
import sys
import time

import serial
from PIL import Image, ImageFont, ImageDraw

CMD_HELLO = 69
CMD_CLEAR = 102
CMD_SCREEN_ON = 109
CMD_SET_BRIGHTNESS = 110
CMD_SET_ORIENTATION = 121
CMD_DISPLAY_BITMAP = 197

ORIENTS = {"NONE": None, "PORTRAIT": 0, "REVERSE_PORTRAIT": 1,
           "LANDSCAPE": 2, "REVERSE_LANDSCAPE": 3}
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

RED = (255, 40, 40)
GREEN = (40, 220, 80)
BLUE = (40, 120, 255)
YELLOW = (255, 210, 40)
BLACK = (10, 10, 10)
WHITE = (255, 255, 255)


def hdr(cmd, x, y, ex, ey):
    return bytes([
        (x >> 2),
        ((x & 3) << 6) | (y >> 4),
        ((y & 15) << 4) | (ex >> 6),
        ((ex & 63) << 2) | (ey >> 8),
        ey & 255,
        cmd,
    ])


def quadrant_canvas(cw, ch):
    """A, B, C, D in canvas quadrants + a white strip on the canvas top edge."""
    img = Image.new("RGB", (cw, ch), BLACK)
    d = ImageDraw.Draw(img)
    m = 6                      # margin
    qw, qh = (cw - 3 * m) // 2, (ch - 3 * m) // 2
    quads = [
        ("A", m, m, RED),
        ("B", 2 * m + qw, m, GREEN),
        ("C", m, 2 * m + qh, BLUE),
        ("D", 2 * m + qw, 2 * m + qh, YELLOW),
    ]
    f_big = ImageFont.truetype(FONT_PATH, min(qh - 20, 130))
    f_small = ImageFont.truetype(FONT_PATH, 18)
    for name, x, y, color in quads:
        d.rectangle([x, y, x + qw, y + qh], fill=color)
        d.text((x + qw // 2, y + qh // 2), name, font=f_big, fill=BLACK, anchor="mm")
    d.rectangle([0, 0, cw - 1, 8], fill=WHITE)          # canvas top edge marker
    d.text((12, 12), "canvas top", font=f_small, fill=WHITE)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--orientation", default="LANDSCAPE", choices=sorted(ORIENTS))
    ap.add_argument("--flip180", action="store_true",
                    help="rotate the bitmap 180 degrees before sending")
    ap.add_argument("--canvas", default="auto", choices=("auto", "320x480", "480x320"),
                    help="bitmap size to send; auto follows --orientation")
    ap.add_argument("--orient-format", default="new", choices=("new", "legacy"),
                    help="new: 16-byte frame with width/height (reference lib). "
                         "legacy: 6-byte header + 1 byte only")
    ap.add_argument("--brightness", type=int, default=100)
    args = ap.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=5, rtscts=True)

    def w(b):
        ser.write(b)

    w(hdr(CMD_HELLO, 0, 0, 0, 0) * 6)
    time.sleep(0.25)
    try:
        resp = ser.read(6)
    except Exception:
        resp = b""
    print(f"HELLO response: {list(resp) or 'no answer'}")
    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    w(hdr(CMD_SCREEN_ON, 0, 0, 0, 0))
    level = int(255 - (max(0, min(100, args.brightness)) / 100.0) * 255)
    w(hdr(CMD_SET_BRIGHTNESS, 0, 0, 0, 0) + bytes([level]))

    v = ORIENTS[args.orientation]
    cw, ch = (480, 320) if v in (2, 3) else (320, 480)
    if args.canvas != "auto":
        cw, ch = (int(x) for x in args.canvas.split("x"))

    def orientation_frame(value, width, height):
        # reference library sends: header, orientation+100, width, height (16-bit BE)
        return hdr(CMD_SET_ORIENTATION, 0, 0, 0, 0) + bytes([
            value + 100, (width >> 8) & 0xFF, width & 0xFF,
            (height >> 8) & 0xFF, height & 0xFF,
        ]) + bytes(5)

    def orientation_frame_legacy(value):
        return hdr(CMD_SET_ORIENTATION, 0, 0, 0, 0) + bytes([value])

    def orientation_any(value):
        if value is None:
            return
        if args.orient_format == "new":
            w(orientation_frame(value, 480 if value in (2, 3) else 320,
                                320 if value in (2, 3) else 480))
        else:
            w(orientation_frame_legacy(value))

    # NONE sends no orientation command at all: baseline test of the serial path
    if v is not None:
        orientation_any(0)
        time.sleep(0.15)
    w(hdr(CMD_CLEAR, 0, 0, 0, 0))
    time.sleep(0.4)
    if v is not None and v != 0:
        orientation_any(v)
    time.sleep(0.3)
    print(f"orientation={args.orientation} (value {v}, format {args.orient_format}), "
          f"canvas {cw}x{ch}")

    img = quadrant_canvas(cw, ch)
    if args.flip180:
        img = img.rotate(180)
        print("bitmap rotated 180deg before send")
    rgb = img.load()
    out = bytearray(cw * ch * 2)
    o = 0
    for y in range(ch):
        for x in range(cw):
            r, g, b = rgb[x, y]
            val = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out[o] = val & 0xFF
            out[o + 1] = val >> 8
            o += 2
    w(hdr(CMD_DISPLAY_BITMAP, 0, 0, cw - 1, ch - 1))
    chunk = cw * 8
    for i in range(0, len(out), chunk):
        ser.write(bytes(out[i:i + chunk]))

    print("pattern sent. now read the panel:")
    print("  if orientation is correct you see, physically:")
    print("    red A | green B")
    print("    blue C | yellow D     with letters UPRIGHT")
    print("  report: color at each physical corner + upright/rotated/mirrored")
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
