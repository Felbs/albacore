#!/usr/bin/env python3
"""albacore lab: overnight HD specimen cube (the TV overnight-cube, ported).

Every SLOT minutes, capture SECS seconds at each rotation station and log.
Politely skips a slot if the SDR is busy (the scheduled simulcast/longwave
jobs win — their windows matter more than any single cube slot). Specimens
land in the hd_cliff corpus tagged 'cube'. Run detached with radioconda
python via Start-Process; stops at --until (UTC ISO).
"""
import argparse, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, r"Z:\src\albacore\lab")
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_field_survey as fs

LOG = Path(r"Z:\SDR_Agent_v2\hd_cube_log.txt")


def log(m):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%SZ}  {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", required=True, help="UTC ISO stop time")
    ap.add_argument("--slot-min", type=float, default=20)
    ap.add_argument("--secs", type=float, default=20)
    ap.add_argument("--mhz", nargs="+", type=float,
                    default=[91.9, 93.9, 90.9, 93.3])
    a = ap.parse_args()
    until = datetime.fromisoformat(a.until.replace("Z", "+00:00"))
    log(f"cube armed: {a.mhz} every {a.slot_min}min until {until:%H:%M}Z")
    while datetime.now(timezone.utc) < until:
        slot_t0 = time.time()
        for mhz in a.mhz:
            try:
                out, secs, wall = fs.capture(mhz, a.secs, "cube")
                res = fs.nrsc5_replay(out, secs)
                log(f"{mhz:5.1f} {res['name'][:10]:10s} sync={res['sync']} "
                    f"mer {res['mer_lo']:.1f}/{res['mer_hi']:.1f} "
                    f"ber {res['ber']:.4f} audio {res['audio_s']:.0f}s "
                    f"-> {out.name}")
            except Exception as e:
                log(f"{mhz:5.1f} skip ({str(e)[:50]})")
                break  # SDR busy: give the whole slot away
        wait = a.slot_min * 60 - (time.time() - slot_t0)
        if wait > 0:
            time.sleep(wait)
    log("cube done")


if __name__ == "__main__":
    main()
