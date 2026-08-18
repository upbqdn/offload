#!/usr/bin/env python
"""Build a partial-phase sequence montage from single frames.

Each panel is a manually selected straight development (camera WB, one global gain, sRGB
curve) cropped on the sun. No HDR or local processing. Panels are auto-gained individually
because the sun's brightness drops by orders of magnitude as it sets into haze.

The groups JSON must contain exactly one curated file per panel. Automated cross-exposure
sharpness ranking is deliberately absent: underexposed noise and saturation make scores
non-comparable between bracket levels.
"""
import argparse
import json
import os

import numpy as np
import rawpy
from PIL import Image, ImageDraw
from scipy.ndimage import center_of_mass, gaussian_filter


def srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)




def develop(path):
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(use_camera_wb=True, gamma=(1, 1), no_auto_bright=True,
                              output_bps=16, user_flip=0, output_color=rawpy.ColorSpace.sRGB,
                              demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                              highlight_mode=rawpy.HighlightMode.Clip)
    return rgb.astype(np.float32) / 65535.0


def sun_centre(img):
    """Centroid of the bright limb/crescent."""
    g = gaussian_filter(img[..., 1], 8)
    thr = max(0.25 * float(g.max()), 1e-4)
    m = g > thr
    if m.sum() < 50:
        return img.shape[0] / 2, img.shape[1] / 2
    cy, cx = center_of_mass(m)
    return float(cy), float(cx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--groups', required=True,
                    help='JSON [{"label": str, "files": [paths]}] in time order')
    ap.add_argument('--out', required=True)
    ap.add_argument('--panel-dir',
                    help='also save each curated panel at native crop resolution')
    ap.add_argument('--half-width', type=int, default=520)
    ap.add_argument('--cols', type=int, default=5)
    ap.add_argument('--tile', type=int, default=380)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    if a.panel_dir:
        os.makedirs(a.panel_dir, exist_ok=True)

    groups = json.load(open(a.groups))
    panels = []
    for grp in groups:
        if len(grp['files']) != 1:
            raise SystemExit(f"{grp['label']}: expected one curated file, got {len(grp['files'])}")
        chosen = grp['files'][0]
        img = develop(chosen)
        cy, cx = sun_centre(img)
        hw = a.half_width
        y0, y1 = max(0, int(cy - hw)), min(img.shape[0], int(cy + hw))
        x0, x1 = max(0, int(cx - hw)), min(img.shape[1], int(cx + hw))
        crop = img[y0:y1, x0:x1]
        gain = 0.92 / max(float(np.percentile(crop, 99.95)), 1e-6)
        panel = (srgb(crop * gain) * 255).astype(np.uint8)
        native = Image.fromarray(panel)
        if a.panel_dir:
            label = grp['label'].replace(':', '')
            frame = os.path.basename(chosen).split('.')[0]
            native.save(f"{a.panel_dir}/{label}_{frame}.jpg", quality=95, subsampling=0)
        im = native.resize((a.tile, a.tile))
        panels.append((grp['label'], os.path.basename(chosen), im))
        print(f"{grp['label']:>10}  {os.path.basename(chosen):<16} "
              f"gain={gain:7.2f} centre=({cy:.0f},{cx:.0f})")

    cols = a.cols
    rowsn = (len(panels) + cols - 1) // cols
    lab_h = 20
    sheet = Image.new('RGB', (cols * a.tile, rowsn * (a.tile + lab_h)), 'black')
    d = ImageDraw.Draw(sheet)
    for k, (lab, fn, im) in enumerate(panels):
        x, y = (k % cols) * a.tile, (k // cols) * (a.tile + lab_h)
        sheet.paste(im, (x, y + lab_h))
        d.text((x + 4, y + 4), lab, fill='yellow')
    sheet.save(a.out, quality=95, subsampling=0)
    print(f"-> {a.out}  {sheet.size[0]}x{sheet.size[1]}")


if __name__ == '__main__':
    main()
