#!/usr/bin/env python
"""Generate the 2027-08-02 shooting plan: Z6III + 180-600 mm @ 600 mm on a Sky-Watcher SolarQuest.

Design decisions are driven by measurements on the 2026 data in this repo:

  * Hand-pressing the shutter produced inter-frame centre scatter of sigma ~19-29 px at 600 mm
    (p95 up to 60 px), oscillating at ~2 Hz. On a SolarQuest carrying a long, cantilevered
    ~3.1 kg rig that is ringing, not tracking error. Therefore nothing in this plan touches the
    camera body during totality: ladders come from the interval timer, contact bursts from a
    remote release.
  * Two 2026 bursts were soft at every exposure including 1/2500 s, i.e. focus or seeing, not
    shake. Therefore focus is set and taped before C2 and never touched again.
  * The 2026 shoot fired 26 nine-frame ladders in 128 s - 26x redundant - and still only reached
    ~2.8 solar radii, because the longest exposure was 1/10 s. Therefore: fewer ladders, plus a
    dedicated long set for the outer corona.
  * C3 was missed entirely (stopped 20:31:19, resumed 20:31:23 with the photosphere out).
    Therefore contact bursts are fixed single exposures, started well before predicted contact.

SolarQuest specifics:
  * Alt-azimuth with a HelioFind solar sensor. The sensor cannot see the Sun during totality, so
    it must be COVERED before C2; the mount then tracks from its GPS/time solar model. Uncover
    after C3 once the Sun is bright again. Covering the sensor is not covering the objective.
  * Level the tripod carefully - level error shows up directly as azimuth tracking drift.
  * Alt-az means field rotation. At this site/date it is about -1.3 deg/hr, i.e. ~0.09 deg over
    a 250 s totality: 0.8 px at the limb, 2.1 px at 2.8 solar radii. Negligible within one
    bracket, but a rotation term is needed to stack frames minutes apart.

Usage:
    eclipse2027_plan.py --c2 10:43:46 --c3 10:47:56
"""
import argparse
import datetime as dt

ISO = 100
APERTURE = 6.3
FPS = 7.0            # continuous high, electronic shutter, lossless-compressed NEF
NEF_MB = 28          # 2026 frames were 17-19 MB at 6064x4040; allow headroom

SHUTTER_MARKS = [1 / x for x in (8000, 6400, 5000, 4000, 3200, 2500, 2000, 1600, 1250, 1000,
                                 800, 640, 500, 400, 320, 250, 200, 160, 125, 100, 80, 60, 50,
                                 40, 30, 25, 20, 15, 13, 10, 8, 6, 5, 4, 3)] + [0.4, 0.5, 0.6,
                                                                                0.8, 1.0, 1.3, 2.0]


def nice_shutter(t):
    return min(SHUTTER_MARKS, key=lambda m: abs(1 - m / t))


def fmt(t):
    return f"1/{round(1/t):g}" if t < 1 else f"{t:g}s"


def ladder(base, n=9, step=1.0):
    half = (n - 1) // 2
    lo = [base / 2 ** (step * k) for k in range(1, half + 1)]
    hi = [base * 2 ** (step * k) for k in range(1, half + 1)]
    return sorted(nice_shutter(x) for x in [base] + lo + hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--c2', required=True, help='second contact, local HH:MM:SS')
    ap.add_argument('--c3', required=True, help='third contact, local HH:MM:SS')
    ap.add_argument('--partial-end', default='12:01:00')
    a = ap.parse_args()

    parse = lambda s: dt.datetime.strptime(s, '%H:%M:%S')
    c2, c3 = parse(a.c2), parse(a.c3)
    tot = (c3 - c2).total_seconds()

    print(f"=== 2027-08-02   Z6III + 180-600 @ 600 mm   f/{APERTURE}  ISO {ISO}  SolarQuest ===")
    print(f"C2 {c2:%H:%M:%S}    C3 {c3:%H:%M:%S}    totality {tot:.0f} s\n")

    A = ladder(1 / 250)
    B = ladder(1 / 8, n=5)
    C = nice_shutter(1 / 2000)
    print("--- exposure sets: save as menu banks / presets before the day ---")
    print(f"  A  corona, 9 frames @ 1 EV, base 1/250 : {', '.join(fmt(x) for x in A)}")
    print(f"  B  outer corona, 5 frames @ 1 EV, base 1/8 : {', '.join(fmt(x) for x in B)}")
    print(f"  C  contacts, fixed, continuous high    : {fmt(C)}")
    print("  P  partials, through filter             : from your own filter test, +/-1 EV\n")

    mid0 = c2 + dt.timedelta(seconds=20)
    mid1 = c3 - dt.timedelta(seconds=20)
    mid = (mid1 - mid0).total_seconds()
    nA = max(int(mid * 0.6 // 20), 1)
    nB = max(int(mid * 0.4 // 15), 1)
    switch = mid0 + dt.timedelta(seconds=mid * 0.6)

    steps = [
        (c2 - dt.timedelta(minutes=75), "Level the tripod properly (bubble, all three legs). "
                                        "Legs short and wide, no centre column. Camera bag hung "
                                        "low. Solar filter ON before you point at the Sun."),
        (c2 - dt.timedelta(minutes=70), "Power SolarQuest, let HelioFind acquire and centre. "
                                        "Confirm it is tracking. Set P: one frame every 5 min."),
        (c2 - dt.timedelta(minutes=20), "Focus: magnified live view on the lunar limb, manual, "
                                        "then TAPE the focus ring. AF off. VR off."),
        (c2 - dt.timedelta(minutes=8), "Final settings check: M, ISO 100, f/6.3, WB Daylight, "
                                       "lossless NEF + Basic JPEG, review off, long-exp NR off, "
                                       "electronic shutter, continuous high."),
        (c2 - dt.timedelta(minutes=3), "COVER THE HELIOFIND SENSOR. Mount now tracks on its GPS "
                                       "solar model. Do not cover the lens."),
        (c2 - dt.timedelta(seconds=40), "Load set C. Keep the objective solar filter ON. "
                                        "Hands on the remote release only."),
        (c2 + dt.timedelta(seconds=1), "Only after totality is visually/operationally confirmed "
                                       "(no photosphere): eyes away, remove OBJECTIVE filter, "
                                       "then hold release for chromosphere/prominences."),
        (c2 + dt.timedelta(seconds=9), "Release. Start interval timer with set A."),
        (mid0, f"Set A x{nA}, one ladder every 20 s = {nA*9} frames."),
        (switch, f"Switch to set B x{nB}, every 15 s = {nB*5} frames (outer corona)."),
        (mid1, "Stop interval timer. Load set C. Objective filter remains OFF during totality."),
        (c3 - dt.timedelta(seconds=8), "Hold release. At the FIRST returning bead: stop, eyes "
                                       "away, and reinstall the OBJECTIVE filter immediately."),
        (c3, "OBJECTIVE FILTER ON by this time. Do not wait for the predicted time if a bead "
             "appears earlier."),
        (c3 + dt.timedelta(seconds=45), "Uncover HelioFind once the Sun is bright again. "
                                        f"Resume set P every 5 min until {a.partial_end}."),
    ]
    print("--- timeline ---")
    for when, what in steps:
        print(f"  {when:%H:%M:%S}  C2{(when-c2).total_seconds():+6.0f}s  {what}")

    # Safe contact coverage: 8 s immediately after confirmed C2 and 8 s immediately before C3.
    # The objective filter stays on while any photosphere is visible. At C3 the first returning
    # bead is the operational cue to stop and reinstall it; predicted times are not trusted over
    # what is visibly happening.
    burst_s = 8
    fC = int(2 * burst_s * FPS)
    frames = nA * 9 + nB * 5 + fC
    hands_on = 2 * burst_s + 20
    print(f"\n--- budget ---")
    print(f"  totality frames   : {frames}   (A {nA*9}, B {nB*5}, C ~{fC})")
    print(f"  card, totality    : ~{frames*NEF_MB/1024:.1f} GB")
    print(f"  card, whole day   : ~{(frames+150)*NEF_MB/1024:.1f} GB")
    print(f"  hands-on          : ~{hands_on} s of {tot:.0f} s")
    print(f"  hands-free        : ~{tot-hands_on:.0f} s with the interval timer running "
          f"<- look up, that is what the automation bought you")


if __name__ == '__main__':
    main()
