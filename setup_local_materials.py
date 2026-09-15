#!/usr/bin/env python3
"""
setup_local_materials.py — Generate high-quality 9:16 local video materials
for offline video generation in MoneyPrinterTurbo.
"""

import subprocess
import sys
from pathlib import Path

STORAGE_DIR = Path(__file__).parent / "MoneyPrinterTurbo" / "storage" / "local_videos"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = r"C:\Users\Akash Santhnu Sundar\AppData\Local\Programs\Python\Python312\Scripts\ffmpeg.exe"

CLIPS = [
    {
        "name": "quantum_deep_space.mp4",
        "filter": "gradients=s=1080x1920:d=15:r=30:c0=0x050814:c1=0x0d1b2a:speed=0.008",
    },
    {
        "name": "quantum_cyan_pulse.mp4",
        "filter": "gradients=s=1080x1920:d=15:r=30:c0=0x00171f:c1=0x003459:c2=0x007ea7:speed=0.012",
    },
    {
        "name": "quantum_violet_field.mp4",
        "filter": "gradients=s=1080x1920:d=15:r=30:c0=0x120826:c1=0x2b0938:c2=0x4a1259:speed=0.01",
    },
    {
        "name": "quantum_emerald_grid.mp4",
        "filter": "gradients=s=1080x1920:d=15:r=30:c0=0x061a14:c1=0x0b3d2e:c2=0x145c45:speed=0.011",
    },
    {
        "name": "quantum_mandelbrot_core.mp4",
        "filter": "mandelbrot=s=1080x1920:d=15:r=30:rate=0.05",
    },
    {
        "name": "quantum_cellular_matrix.mp4",
        "filter": "life=s=1080x1920:d=15:r=30:rate=15:mold=10:ratio=0.6",
    },
]

def main():
    print(f"[SETUP] Generating {len(CLIPS)} local video materials in {STORAGE_DIR}...")
    for clip in CLIPS:
        out_path = STORAGE_DIR / clip["name"]
        if out_path.exists() and out_path.stat().st_size > 10000:
            print(f"  [OK] Already exists: {clip['name']}")
            continue

        print(f"  Encoding {clip['name']}...")
        cmd = [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", clip["filter"],
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-t", "15",
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"  [WARN] Failed to generate {clip['name']}: {res.stderr[:200]}")
        else:
            print(f"  [OK] Generated {clip['name']} ({out_path.stat().st_size // 1024} KB)")

    print("[SETUP] All local video materials ready!")

if __name__ == "__main__":
    main()
