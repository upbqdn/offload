#!/usr/bin/env python
"""Straight development of a single raw frame - the honest baseline.

No merge, no registration, no radial flattening, no sky model, no local contrast. Just camera
white balance, one global exposure multiplier, an sRGB transfer curve, and an optional crop.
Anything visible here is in one frame as shot.
"""
import argparse
import os

import numpy as np
import rawpy
from PIL import Image


def srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('--out', required=True)
    ap.add_argument('--gain', type=float, default=0.0,
                    help='global exposure multiplier; 0 = auto (99.9th percentile to 0.95)')
    ap.add_argument('--cy', type=float, help='crop centre y, full-res px')
    ap.add_argument('--cx', type=float)
    ap.add_argument('--half-width', type=float, default=0.0, help='crop half-width, px')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    for p in a.files:
        with rawpy.imread(p) as raw:
            rgb = raw.postprocess(use_camera_wb=True, gamma=(1, 1), no_auto_bright=True,
                                  output_bps=16, user_flip=0,
                                  output_color=rawpy.ColorSpace.sRGB,
                                  demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                                  highlight_mode=rawpy.HighlightMode.Clip)
        x = rgb.astype(np.float32) / 65535.0
        if a.half_width and a.cy and a.cx:
            hw = int(a.half_width)
            y0, y1 = max(0, int(a.cy - hw)), min(x.shape[0], int(a.cy + hw))
            x0, x1 = max(0, int(a.cx - hw)), min(x.shape[1], int(a.cx + hw))
            x = x[y0:y1, x0:x1]
        gain = a.gain or (0.95 / max(float(np.percentile(x, 99.9)), 1e-6))
        img = srgb(x * gain)
        name = os.path.basename(p).rsplit('.', 1)[0]
        Image.fromarray((img * 255).astype(np.uint8)).save(
            f'{a.out}/{name}_straight.jpg', quality=95, subsampling=0)
        print(f"{name}: gain={gain:.3f} p99.9={np.percentile(x,99.9):.5f} "
              f"clipped={(x.max(2)>=0.999).mean()*100:.3f}% -> {a.out}/{name}_straight.jpg")


if __name__ == '__main__':
    main()
