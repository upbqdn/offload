#!/usr/bin/env python
"""Render a merged linear corona radiance map (hdr_linear.npy + merge.json) for display.

The corona falls ~3 decades between the limb and 3 solar radii, so no single tone curve shows
it all. Two presets:

  structure  full radial flattening plus multi-scale local contrast. Shows streamer filaments
             and polar plumes; the classic scientific corona look.
  natural    gentle flattening, keeping some of the real brightness falloff, so it reads like a
             photograph rather than a map.

Pipeline:
  1. sky: robust 2D cubic fit outside --bg-r, subtracted (near-sunset sky is a steep gradient,
     and removing it additively also removes its colour cast).
  2. grey balance: one global channel gain measured on the mid-corona annulus. A uniform
     multiply, so it cannot invert any local hue - prominences stay warm relative to the rest.
  3. radial flattening: divide all channels by one shared luminance radial profile, floored at
     --taper and tapered off past --alpha-out so the sky is never divided by itself.
  4. asinh stretch, then multi-scale unsharp on luminance only (no colour fringing).
  5. neutral dark occulting disk with a soft inward ramp; fade and crop chosen so the fade ring
     lands OUTSIDE the crop, otherwise the frame shows a synthetic vignette donut.
"""
import argparse
import json

import numpy as np
import tifffile
from PIL import Image
from scipy.ndimage import gaussian_filter

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

PRESETS = {
    # alpha, msc sigmas, msc weights, asinh a, crop, fade
    'structure': dict(alpha=0.85, msc=(3, 10, 32, 96), msc_w=(0.5, 0.8, 0.6, 0.35),
                      asinh=10.0, crop=2.35, fade=(3.0, 4.2)),
    'natural': dict(alpha=0.55, msc=(4, 16, 48), msc_w=(0.35, 0.45, 0.3),
                    asinh=5.0, crop=2.35, fade=(3.0, 4.2)),
    # No radial flattening at all: the corona keeps its real ~500:1 falloff and an aggressive
    # asinh does the compression. Looks like a photograph of totality, not a coronal map.
    'photo': dict(alpha=0.0, msc=(4, 16), msc_w=(0.30, 0.20),
                  asinh=140.0, crop=2.0, fade=(2.5, 3.4)),
}


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def fit_background(img, rad, r_min, order=3, iters=3):
    """Robust 2D polynomial fit of the sky outside r_min, per channel."""
    h, w = rad.shape
    yy, xx = np.mgrid[0:h, 0:w]
    ny = ((yy - h / 2) / (h / 2)).astype(np.float32)
    nx = ((xx - w / 2) / (w / 2)).astype(np.float32)
    terms = [np.ones_like(nx)]
    for total in range(1, order + 1):
        for py in range(total + 1):
            terms.append(nx ** (total - py) * ny ** py)
    basis = np.stack(terms, -1)
    sel = rad > r_min
    B = basis[sel]
    model = np.zeros_like(img)
    for ch in range(img.shape[2]):
        y = img[sel, ch].astype(np.float64)
        wgt = np.ones_like(y)
        for _ in range(iters):
            coef, *_ = np.linalg.lstsq(B * wgt[:, None], y * wgt, rcond=None)
            res = B @ coef - y
            s = 1.4826 * np.median(np.abs(res - np.median(res))) or 1.0
            wgt = 1.0 / (1.0 + (res / (2.5 * s)) ** 2)
        model[..., ch] = basis @ coef
    return model


def radial_profile(plane, rad, nbins, rmax):
    idx = np.clip((rad / rmax * nbins).astype(np.int32), 0, nbins - 1)
    acc = np.zeros(nbins)
    cnt = np.zeros(nbins, np.int64)
    np.add.at(acc, idx.ravel(), plane.ravel())
    np.add.at(cnt, idx.ravel(), 1)
    good = cnt > 0
    prof = np.interp(np.arange(nbins), np.nonzero(good)[0], (acc / np.maximum(cnt, 1))[good])
    return gaussian_filter(prof, 2.0)


def multiscale_contrast(lum, sigmas, weights, mask):
    """Sum of unsharp masks at several scales - what actually reveals streamer filaments."""
    out = lum.copy()
    for s, w in zip(sigmas, weights):
        out = out + w * mask * (lum - gaussian_filter(lum, s))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--preset', choices=sorted(PRESETS), default='structure')
    ap.add_argument('--taper', type=float, default=3.0, help='profile floor radius, in R')
    ap.add_argument('--alpha-in', type=float, default=1.9)
    ap.add_argument('--alpha-out', type=float, default=2.8)
    ap.add_argument('--bg-r', type=float, default=3.0, help='sky fit starts here, in R')
    ap.add_argument('--black', type=float, default=0.5,
                    help='black point as a percentile of the sky region')
    ap.add_argument('--white', type=float, default=0.90,
                    help='white point: corona p99.9 maps here, soft knee above')
    ap.add_argument('--no-neutralize', dest='neutralize', action='store_false')
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    P = PRESETS[a.preset]

    meta = json.load(open(f'{a.dir}/merge.json'))
    hdr = np.load(f'{a.dir}/hdr_linear.npy').astype(np.float32)
    cy, cx, R = meta['cy'], meta['cx'], meta['r']
    h, w = hdr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    rad = np.hypot(yy - cy, xx - cx).astype(np.float32)
    rmax = float(rad.max())

    hdr -= fit_background(hdr, rad, a.bg_r * R)
    sky = rad > a.bg_r * R
    print(f"sky: median {np.median(hdr[sky]):.3g}  std {np.std(hdr[sky]):.3g}")

    if a.neutralize:
        wb_ann = (rad > 1.3 * R) & (rad < 2.2 * R)
        med = np.median(hdr[wb_ann], axis=0)
        gains = (float(med[1]) / np.maximum(med, 1e-12)).astype(np.float32)
        hdr *= gains
        print(f"grey balance: R={gains[0]:.3f} G={gains[1]:.3f} B={gains[2]:.3f}")

    alpha_map = P['alpha'] * (1.0 - smoothstep((rad / R - a.alpha_in) /
                                               (a.alpha_out - a.alpha_in)))
    j = int(a.taper * R / rmax * 800)
    prof = radial_profile(hdr @ LUMA, rad, 800, rmax)
    prof = np.maximum(prof, max(prof[min(j, 799)], 1e-12))
    m = np.interp(rad / rmax * 800, np.arange(800), prof)
    flat = hdr / (m ** alpha_map)[..., None]

    # Normalise with headroom: k at the 99.97th percentile of the whole corona, so the bright
    # inner corona lands just under the clip point instead of blowing to white (the previous
    # 99.5th percentile clipped the limb, which is what made the render look washed out).
    ann = (rad > 1.0 * R) & (rad < 2.5 * R)
    k = float(np.percentile(flat[ann], 99.97))
    x = np.clip(flat / max(k, 1e-12), 0, None)
    black = float(np.percentile((x @ LUMA)[sky], a.black * 100))
    x = np.clip(x - black, 0, None)
    img = np.arcsinh(x * P['asinh']) / np.arcsinh(P['asinh'])

    # multi-scale contrast on luminance; suppressed inside the disk and out in the sky
    lum = img @ LUMA
    mask = (smoothstep((rad / R - 0.99) / 0.04) *
            (1.0 - smoothstep((rad / R - 2.4) / 0.5))).astype(np.float32)
    lum2 = multiscale_contrast(lum, P['msc'], P['msc_w'], mask)
    # rescale to leave highlight headroom, then a soft knee instead of a hard clip
    hi = float(np.percentile(lum2[ann], 99.9))
    lum2 = lum2 * (a.white / max(hi, 1e-9))
    knee = a.white
    over = lum2 > knee
    lum2[over] = knee + (1.0 - knee) * np.tanh((lum2[over] - knee) / (1.0 - knee))
    chroma = img - lum[..., None]
    img = np.clip(lum2[..., None] + chroma, 0, 1)

    # occulting disk: neutral and dark, soft ramp so prominence bases (r ~ 0.99 R) survive
    disk = (0.06 + 0.94 * smoothstep((rad / R - 0.955) / 0.045)).astype(np.float32)
    desat = smoothstep((rad / R - 0.955) / 0.045)[..., None]
    y = (img @ LUMA)[..., None]
    img = (y + (img - y) * desat) * disk[..., None]

    fade = 1.0 - smoothstep((rad / R - P['fade'][0]) / (P['fade'][1] - P['fade'][0]))
    img = np.clip(img * fade[..., None], 0, 1)

    half = int(P['crop'] * R)
    y0, y1 = max(0, int(cy - half)), min(h, int(cy + half))
    x0, x1 = max(0, int(cx - half)), min(w, int(cx + half))
    img = img[y0:y1, x0:x1]

    tag = a.tag or a.preset
    Image.fromarray((img * 255).astype(np.uint8)).save(
        f'{a.dir}/corona_{tag}.jpg', quality=95, subsampling=0)
    tifffile.imwrite(f'{a.dir}/corona_{tag}.tif', (img * 65535).astype(np.uint16),
                     photometric='rgb')
    rc = rad[y0:y1, x0:x1]
    print(f"preset={a.preset} k={k:.4g} black={black:.4g} crop={img.shape[1]}x{img.shape[0]}"
          f" -> {a.dir}/corona_{tag}.jpg")
    for lab, lo, hi in (('limb 1.02-1.1R', 1.02, 1.1), ('mid 1.5-2R', 1.5, 2.0),
                        ('corner >2.8R', 2.8, 99.0)):
        s = (rc > lo * R) & (rc < hi * R)
        if s.any():
            print(f"  {lab:>15}: median {np.median(img[s]):.3f}  p99 {np.percentile(img[s], 99):.3f}")


if __name__ == '__main__':
    main()
