#!/usr/bin/env python3
"""albacore lab: full-market stress test — every HD station, every subchannel.

One capture per station, then replay-decode EVERY advertised program from
the same capture through the albacore build (ALBACORE=1), scoring each
subchannel with the blind audio meter (LISTEN%). Output: the market map.

Run with radioconda python.
"""
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\albacore\lab")
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_field_survey as fs
import hd_radio
import importlib.util
_s = importlib.util.spec_from_file_location("meter", r"Z:\src\albacore\lab\hd_audio_meter.py")
meter = importlib.util.module_from_spec(_s)
_s.loader.exec_module(meter)

EXE = r"Z:\src\albacore\build\src\nrsc5.exe"
TMP = Path(r"C:\Users\emane\.claude\jobs\3948c3da\tmp")
FS_CAP = hd_radio.FS_CAP


def decode_prog(cu8, prog, alb=True):
    env = dict(os.environ)
    env["PATH"] = r"C:\msys64\mingw64\bin;" + env["PATH"]
    for k in ("ALBACORE", "ALBACORE_PART_WEIGHT", "ALBACORE_ROBUST_TRACK", "ALBACORE_ERASE"):
        env.pop(k, None)
    if alb:
        env["ALBACORE"] = "1"
    wav = TMP / f"stress_p{prog}.wav"
    if wav.exists():
        wav.unlink()
    r = subprocess.run([EXE, "-r", str(cu8), "-o", str(wav), str(prog)],
                       capture_output=True, text=True, timeout=300, env=env)
    log = r.stderr + r.stdout
    progs = {int(m[0]): m[1].strip() for m in
             re.findall(r"Audio program (\d+): (.*?),", log)}
    name = (re.findall(r"Station name: (.*)", log) or [""])[0].strip()
    listen, secs = 0.0, 0
    if wav.exists() and wav.stat().st_size > 44:
        x, fsr = meter.load_wav(wav)
        rows = meter.per_second_metrics(x, fsr)
        if rows:
            listen = meter.judge(rows)
            secs = len(rows)
    return {"listen": listen, "secs": secs, "progs": progs, "name": name}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mhz", nargs="+", type=float,
                    default=[88.5, 89.3, 90.9, 91.9, 93.3, 93.9, 99.7, 103.5, 107.5])
    ap.add_argument("--secs", type=float, default=30.0)
    a = ap.parse_args()
    results = []
    print(f"{'MHz':>6} {'name':10s} {'prog':>4} {'type':16s} {'secs':>5} {'LISTEN%':>7}")
    for mhz in a.mhz:
        cu8 = None
        for attempt in range(3):
            try:
                out, secs, wall = fs.capture(mhz, a.secs, "stress")
                raw = np.fromfile(out, dtype=np.int16)
                cu8 = TMP / "stress.cu8"
                hd_radio.cs16_to_cu8(hd_radio.decimate2_cs16(raw)).tofile(cu8)
                break
            except Exception as e:
                print(f"{mhz:6.1f} capture retry ({str(e)[:40]})")
                time.sleep(8)
        if cu8 is None:
            print(f"{mhz:6.1f} CAPTURE FAILED")
            continue
        first = decode_prog(cu8, 0)
        progs = first["progs"] or ({0: "?"} if first["secs"] else {})
        if not progs:
            print(f"{mhz:6.1f} {'(no HD sync)':10s}    -")
            results.append({"mhz": mhz, "name": first["name"], "programs": {}})
            continue
        st = {"mhz": mhz, "name": first["name"], "programs": {}}
        for p in sorted(progs):
            r = first if p == 0 else decode_prog(cu8, p)
            st["programs"][p] = {"type": progs[p], "listen": r["listen"], "secs": r["secs"]}
            print(f"{mhz:6.1f} {first['name'][:10]:10s} HD{p+1:d}  {progs[p][:16]:16s} "
                  f"{r['secs']:5d} {r['listen']:6.0f}%")
        results.append(st)
    stamp = datetime.now(timezone.utc).strftime("%m%d_%H%MZ")
    outp = Path(r"Z:\src\albacore\lab\out") / f"stress_{stamp}.json"
    outp.parent.mkdir(exist_ok=True)
    outp.write_text(json.dumps(results, indent=1))
    print(f"\nwrote {outp}")


if __name__ == "__main__":
    main()
