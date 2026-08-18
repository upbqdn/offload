#!/usr/bin/env python
"""Register + HDR-merge one bracket ladder of totality frames into a linear radiance map.

Radiometry: every frame is developed with the *same* white balance (taken from the reference
frame) and linear gamma, so frame values differ only by exposure time. Radiance is
value / exposure_time; per-pixel weights fall off towards raw saturation and towards the
noise floor, and are proportional to exposure time (photon-noise-optimal).

Registration: phase correlation on an annulus around the lunar limb (0.95-1.45 R), the only
feature present at every exposure level.

Outputs (in --out):
  hdr_linear.npy   float32 HxWx3 linear radiance, sky pedestal removed
  hdr_log.tif      16-bit log-encoded TIFF (viewable, keeps the whole range)
  render_*.jpg     display renders: plain log stretch and radial-gradient stretch
  merge.json       per-frame shifts, weights and the geometry used
"""
import argparse
import csv
import json
import os
import subprocess
from fractions import Fraction

import numpy as np
import rawpy
import tifffile
from scipy.ndimage import gaussian_filter, map_coordinates, shift as ndshift

SAT_HI, SAT_SOFT = 0.98, 0.75      # weight rolloff between SAT_SOFT and SAT_HI of full scale
NOISE_LO, NOISE_SOFT = 0.0005, 0.004


def exposures(files):
    """{filename: (exposure_seconds, iso)} via one exiftool call."""
    out = subprocess.run(['exiftool', '-q', '-T', '-p', '$FileName,$ExposureTime,$ISO', *files],
                         capture_output=True, text=True, check=True).stdout
    m = {}
    for row in csv.reader(out.strip().splitlines()):
        name, exp, iso = row
        m[name] = (float(Fraction(exp)) if '/' in exp else float(exp), float(iso))
    return m


def develop(path, wb, half=True):
    """Linear RGB (float32, 0..1 of full scale) + raw saturation mask, same grid as the RGB."""
    with rawpy.imread(path) as raw:
        sat_full = raw.raw_image_visible >= raw.white_level - 4
        rgb = raw.postprocess(user_wb=list(wb), gamma=(1, 1), no_auto_bright=True,
                              output_bps=16, half_size=half, user_flip=0,
                              output_color=rawpy.ColorSpace.sRGB,
                              demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                              highlight_mode=rawpy.HighlightMode.Clip)
    a = rgb.astype(np.float32) / 65535.0
    if half:
        h, w = a.shape[:2]
        s = sat_full[:2 * h, :2 * w]
        sat = (s[0::2, 0::2] | s[0::2, 1::2] | s[1::2, 0::2] | s[1::2, 1::2])
    else:
        sat = sat_full[:a.shape[0], :a.shape[1]]
    return a, sat


def fit_center(g, cy, cx, r0, n_az=180, span=0.12, iters=4):
    """Fit the lunar disk's centre and radius from the limb.

    Per azimuth the limb is the radius of steepest outward brightness rise (parabolically
    refined). Those radii satisfy r(theta) = R + dy*sin(theta) + dx*cos(theta) for a disk
    offset by (dy, dx) from the assumed centre, so an IRLS solve gives centre and radius at
    once. Weighting by edge amplitude and Cauchy reweighting keeps prominences and coronal
    streamers from pulling the fit.

    Chosen over phase correlation because a near-circularly-symmetric limb annulus gives
    phase correlation an ambiguous peak: it produced spurious 20-30 px shifts.
    """
    gs = gaussian_filter(np.clip(g, 0, None), 1.0)
    R = r0
    res = np.zeros(n_az)
    for _ in range(iters):
        th = np.linspace(0, 2 * np.pi, n_az, endpoint=False)
        r = np.arange(R * (1 - span), R * (1 + span), 0.2)
        yy = cy + np.outer(np.sin(th), r)
        xx = cx + np.outer(np.cos(th), r)
        p = map_coordinates(gs, [yy.ravel(), xx.ravel()], order=1,
                            mode='nearest').reshape(n_az, -1)
        dp = np.gradient(p, r, axis=1)
        j = np.argmax(dp, axis=1)
        idx = np.arange(n_az)
        amp = dp[idx, j]
        a = dp[idx, np.clip(j - 1, 0, len(r) - 1)]
        c = dp[idx, np.clip(j + 1, 0, len(r) - 1)]
        den = a - 2 * amp + c
        off = np.where(den != 0, 0.5 * (a - c) / np.where(den != 0, den, 1), 0.0)
        rr = r[j] + np.clip(off, -1, 1) * 0.2

        med = np.median(amp[amp > 0]) if np.any(amp > 0) else 1.0
        base = np.clip(amp / max(med, 1e-9), 0, 4)
        base[amp <= 0] = 0
        A = np.stack([np.ones(n_az), np.sin(th), np.cos(th)], 1)
        w = base
        sol = np.array([R, 0.0, 0.0])
        for _ in range(4):
            sol, *_ = np.linalg.lstsq(A * w[:, None], rr * w, rcond=None)
            res = A @ sol - rr
            s = 1.4826 * np.median(np.abs(res - np.median(res))) or 1.0
            w = base / (1 + (res / (2.5 * s)) ** 2)
        R, dy, dx = sol
        cy += dy
        cx += dx
    return float(cy), float(cx), float(R), float(np.sqrt(np.mean(res ** 2)))


def smoothstep(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ref', required=True)
    ap.add_argument('--cy', type=float, required=True, help='disk centre y in the working grid')
    ap.add_argument('--cx', type=float, required=True)
    ap.add_argument('--r', type=float, required=True, help='disk radius in the working grid')
    ap.add_argument('--out', required=True)
    ap.add_argument('--full', action='store_true', help='work at full resolution')
    ap.add_argument('--centers', help='JSON {basename: [cy, cx]} of half-res disk centres; '
                                      'used instead of fitting on the working grid')
    ap.add_argument('files', nargs='+')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    half = not a.full
    sc = 1.0 if half else 2.0
    cy, cx, r0 = a.cy * sc, a.cx * sc, a.r * sc

    with rawpy.imread(a.ref) as raw:
        wb = np.asarray(raw.camera_whitebalance, dtype=np.float64)
    wb = wb / wb.max()                      # keep gains <=1 so WB adds no clipping
    exp = exposures([a.ref] + a.files)

    centers = json.load(open(a.centers)) if a.centers else None

    def geometry(path, rgb, cy0, cx0, r0_):
        """Disk centre for a frame: supplied (validated at half res, scaled) or fitted."""
        name = os.path.basename(path)
        if centers is not None:
            if name not in centers:
                raise SystemExit(f'{name}: no centre in {a.centers}')
            gy, gx = centers[name]
            return gy * sc, gx * sc, r0_, 0.0
        return fit_center(rgb[..., 1], cy0, cx0, r0_)

    ref_rgb, _ = develop(a.ref, wb, half)
    tcy, tcx, tR, tres = geometry(a.ref, ref_rgb, cy, cx, r0)
    print(f"reference {os.path.basename(a.ref)}: centre=({tcy:.2f},{tcx:.2f}) R={tR:.2f} "
          f"resid={tres:.2f} source={'supplied' if centers else 'fit'}", flush=True)

    yy, xx = np.mgrid[0:ref_rgb.shape[0], 0:ref_rgb.shape[1]]
    rad = np.hypot(yy - tcy, xx - tcx)
    sky = rad > 4.5 * tR

    num = np.zeros(ref_rgb.shape, np.float64)
    den = np.zeros(ref_rgb.shape, np.float64)
    recs = []
    for p in a.files:
        name = os.path.basename(p)
        e, iso = exp[name]
        rgb, sat = develop(p, wb, half)
        fy, fx, fR, fres = geometry(p, rgb, tcy, tcx, tR)
        dy, dx = tcy - fy, tcx - fx          # shift that brings this frame onto the reference
        pedestal = np.median(rgb[sky], axis=0) if sky.any() else np.zeros(3)
        rgb = rgb - pedestal                 # per-channel sky/veiling floor
        satf = ndshift(sat.astype(np.float32), (dy, dx), order=1, mode='nearest')
        rgb = ndshift(rgb, (dy, dx, 0), order=1, mode='nearest')
        v = rgb.max(2, keepdims=True)
        w = (smoothstep((SAT_HI - v) / (SAT_HI - SAT_SOFT))
             * smoothstep((v - NOISE_LO) / (NOISE_SOFT - NOISE_LO)))
        w = w * (1.0 - satf[..., None]) * (e * 100.0 / iso)
        num += w * (rgb / (e * iso / 100.0))
        den += w
        recs.append(dict(file=name, exp=e, iso=iso, dy=float(dy), dx=float(dx),
                         fit_R=fR, fit_resid=fres, pedestal=[float(x) for x in pedestal],
                         wsum=float(w.sum()), clip_frac=float(sat.mean())))
        print(f"{name} exp={e:.5g}s dy={dy:+7.2f} dx={dx:+7.2f} R={fR:6.2f} "
              f"resid={fres:4.2f} clip={sat.mean()*100:5.2f}%", flush=True)

    cover = float((den.sum(2) > 0).mean())
    hdr = np.where(den > 0, num / np.maximum(den, 1e-12), 0).astype(np.float32)
    np.save(f'{a.out}/hdr_linear.npy', hdr)
    json.dump(dict(frames=recs, cy=tcy, cx=tcx, r=tR, half=half, coverage=cover,
                   shape=list(hdr.shape)), open(f'{a.out}/merge.json', 'w'), indent=1)

    # Linear float TIFF as the interchange copy; all display rendering lives in
    # eclipse_render.py so there is exactly one render path.
    tifffile.imwrite(f'{a.out}/hdr_linear.tif', hdr, photometric='rgb')
    pos = hdr[hdr > 0]
    lo = float(np.percentile(pos, 1)) if pos.size else 1e-6
    hi = float(hdr.max())
    print(f"\nmerged {len(a.files)} frames  coverage={cover*100:.2f}%  "
          f"dynamic range {hi/lo:.3g}x  -> {a.out}")


if __name__ == '__main__':
    main()
