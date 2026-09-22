#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate assets/preview_latest.png with fully synthetic data.

No nvidia-smi, no /proc scan, no real model paths: the preview shows
placeholder values only, so publishing it leaks nothing about the live server.
Run on the Linux server (needs DejaVu fonts), from the repo root:

  ./venv/bin/python tools/gen_preview.py
"""
import datetime
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gpu_dash as G

gpus = [{"index": "0", "name": "Tesla V100-PCIE-32GB", "pstate": "P0",
         "gpu_util": 46.0, "mem_util": 87.0, "temp": 55.0, "mem_temp": 51.0,
         "power": 136.0, "power_limit": 250.0, "mem_used": 27996.0,
         "mem_total": 32768.0, "clk_sm": 1380.0, "clk_mem": 877.0},
        {"index": "1", "name": "Tesla V100-PCIE-32GB", "pstate": "P0",
         "gpu_util": 48.0, "mem_util": 95.0, "temp": 53.0, "mem_temp": 53.0,
         "power": 123.0, "power_limit": 250.0, "mem_used": 31252.0,
         "mem_total": 32768.0, "clk_sm": 1380.0, "clk_mem": 877.0}]

history = []
base = datetime.datetime.now().timestamp()
for i in range(360):
    ts = base - (360 - 1 - i) * G.INTERVAL
    tt = 45 + 8 * abs(math.sin(i / 30.0)) + random.uniform(-1.0, 1.0)
    history.append({"ts": datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
                    "gpus": [{"temp": round(tt, 1), "gpu_util": 40.0 + 15 * abs(math.sin(i / 25.0))},
                             {"temp": round(tt - 3, 1), "gpu_util": 42.0 + 15 * abs(math.sin(i / 25.0))}]})

now = datetime.datetime.now().replace(microsecond=0)
regions = G.build_regions(gpus, history, True, now)
img = G.composite(regions, {"gpus": gpus, "history": history, "llama": True,
                            "model": "SomeModel-Q4_K_M", "comfyui": False, "now": now})

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = os.path.join(repo_root, "assets", "preview_latest.png")
img.save(out)
print("saved", out, img.size)
