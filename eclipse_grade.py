#!/usr/bin/env python
"""Grade totality frames: fit the lunar disk, then measure how badly each frame is smeared.

The shutter was pressed by hand at 600 mm, so every frame in a burst sits at a different
position (that is registration's problem) and each *long* frame is additionally smeared by the
motion that happened while its shutter was open (that is this script's problem).

Smear is measured on the lunar limb, a step edge whose true profile is identical in every
frame of a given exposure level. Two numbers per azimuth:

  width  radial distance between the 8% and 25% crossings of the limb rise. Low crossings are
         used because the inner corona clips in the long exposures, so the top of the step is
         unusable there.
  slope  steepest normalised radial gradient at the limb.

Anisotropy of the width around the limb gives the smear: a linear motion of length L at angle
phi widens the edge by L*|cos(theta - phi)|, so w_hi/w_lo percentiles over azimuth yield the
length and direction. Values are comparable *within* one exposure level, which is how the
selection step uses them.

One JSON object per file on stdout.
"""
import argparse
import json
import sys

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates

from eclipse_merge import develop, fit_center

N_AZ = 240
CROSS_LO, CROSS_HI = 0.08, 0.25


def limb_metrics(g, cy, cx, R):
    th = np.linspace(0, 2 * np.pi, N_AZ, endpoint=False)
    r = np.arange(0.80 * R, 1.25 * R, 0.2)
    yy = cy + np.outer(np.sin(th), r)
    xx = cx + np.outer(np.cos(th), r)
    gs = gaussian_filter(np.clip(g, 0, None), 0.8)
    p = map_coordinates(gs, [yy.ravel(), xx.ravel()], order=1, mode='nearest').reshape(N_AZ, -1)

    n_in = max(3, int(0.06 * R / 0.2))
    lo = np.median(p[:, :n_in], axis=1)
    amp = np.percentile(p, 99, axis=1) - lo
    widths = np.full(N_AZ, np.nan)
    slopes = np.full(N_AZ, np.nan)
    for i in range(N_AZ):
        if amp[i] <= 0:
            continue
        f = (p[i] - lo[i]) / amp[i]
        ja, jb = np.argmax(f > CROSS_LO), np.argmax(f > CROSS_HI)
        if ja == 0 or jb == 0 or jb <= ja:
            continue
        ra = np.interp(CROSS_LO, [f[ja - 1], f[ja]], [r[ja - 1], r[ja]])
        rb = np.interp(CROSS_HI, [f[jb - 1], f[jb]], [r[jb - 1], r[jb]])
        widths[i] = rb - ra
        d = np.gradient(f, r)
        slopes[i] = np.nanmax(d[(r > 0.95 * R) & (r < 1.10 * R)])

    ok = np.isfinite(widths)
    if ok.sum() < 40:
        return dict(n_az=int(ok.sum()))
    wv = widths[ok]
    w_lo, w_hi = float(np.percentile(wv, 10)), float(np.percentile(wv, 90))
    i_hi = int(np.nanargmax(np.where(ok, widths, -np.inf)))
    return dict(n_az=int(ok.sum()), width=float(np.median(wv)),
                slope=float(np.nanmedian(slopes[ok])), w_lo=w_lo, w_hi=w_hi,
                smear_px=float(np.sqrt(max(w_hi ** 2 - w_lo ** 2, 0.0))),
                smear_deg=float(np.degrees(th[i_hi]) % 180))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cy', type=float, required=True)
    ap.add_argument('--cx', type=float, required=True)
    ap.add_argument('--r', type=float, required=True)
    ap.add_argument('files', nargs='+')
    a = ap.parse_args()

    import rawpy
    with rawpy.imread(a.files[0]) as raw:
        wb = np.asarray(raw.camera_whitebalance, dtype=np.float64)
    wb = wb / wb.max()

    for p in a.files:
        name = p.split('/')[-1]
        try:
            rgb, sat = develop(p, wb, half=True)
            g = rgb[..., 1]
            fy, fx, R, res = fit_center(g, a.cy, a.cx, a.r)
            rec = dict(file=name, cy=fy, cx=fx, fit_R=R, fit_resid=res,
                       clip_frac=float(sat.mean()))
            rec.update(limb_metrics(g, fy, fx, R))
            print(json.dumps(rec), flush=True)
        except Exception as exc:
            print(json.dumps(dict(file=name, error=repr(exc))), flush=True)
            print(f'{name}: {exc!r}', file=sys.stderr)


if __name__ == '__main__':
    main()
