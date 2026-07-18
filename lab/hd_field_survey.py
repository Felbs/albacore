#!/usr/bin/env python3
"""albacore lab: live FM HD field survey for knob-pair validation targets.

For each station: capture cs16 @ 4x native, quick nrsc5 replay (sync/MER/
BER/audio), plus the lab per-ref MER dial (sideband asymmetry + per-ref
spread = selectivity fingerprint). Ranks the field for stock-vs-knobs A/B.

Run with radioconda python (needs SoapySDR).
"""
import argparse, json, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_radio
from hd_radio import FS_CAP

CORPUS = Path(r"Z:\src\gr-radiotuna\lab\hd_cliff")
TMP = Path(r"C:\Users\emane\.claude\jobs\3948c3da\tmp")
NRSC5 = r"C:\Tools\nrsc5\nrsc5.exe"


def capture(mhz, secs, tag):
    sdr, st, RX = hd_radio.open_sdr(mhz, ifgr=40, rfgain="5")
    buf = np.empty(2 * 262144, np.int16)
    n_want = int(secs * FS_CAP)
    stamp = datetime.now(timezone.utc).strftime("%m%d_%H%M%SZ")
    out = CORPUS / f"hdfield_{mhz:.1f}_{stamp}_{tag}.cs16"
    got = 0
    t0 = time.time()
    with open(out, "wb") as f:
        while got < n_want and time.time() - t0 < secs * 2 + 10:
            r = sdr.readStream(st, [buf], 262144, timeoutUs=1000000)
            if r.ret > 0:
                n = min(r.ret, n_want - got)
                buf[:2 * n].tofile(f)
                got += n
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    wall = time.time() - t0
    meta = {"freq_hz": mhz * 1e6, "fs_hz": FS_CAP, "format": "cs16",
            "secs": got / FS_CAP, "wall_s": wall, "ifgr": 40, "rfgain": 5,
            "utc": datetime.now(timezone.utc).isoformat(), "tag": tag}
    Path(str(out) + ".json").write_text(json.dumps(meta, indent=1))
    return out, got / FS_CAP, wall


def nrsc5_replay(cs16, secs):
    raw = np.fromfile(cs16, dtype=np.int16, count=int(secs * FS_CAP) * 2)
    cu8 = hd_radio.cs16_to_cu8(hd_radio.decimate2_cs16(raw))
    f = TMP / "survey.cu8"
    cu8.tofile(f)
    wav = TMP / "survey.wav"
    if wav.exists():
        wav.unlink()
    r = subprocess.run([NRSC5, "-r", str(f), "-o", str(wav), "0"],
                       capture_output=True, text=True, timeout=300)
    log = r.stderr + r.stdout
    mers = [(float(a), float(b)) for a, b in
            re.findall(r"MER: ([-\d.]+) dB \(lower\), ([-\d.]+) dB \(upper\)", log)]
    bers = [float(m) for m in re.findall(r"avg: ([\d.]+)", log)]
    name = (re.findall(r"Station name: (.*)", log) or ["?"])[0].strip()
    audio_s = (wav.stat().st_size - 44) / (44100 * 4) if wav.exists() and wav.stat().st_size > 44 else 0.0
    f.unlink()
    lo = np.median([a for a, b in mers]) if mers else float("nan")
    hi = np.median([b for a, b in mers]) if mers else float("nan")
    return {"name": name, "sync": "Synchronized" in log,
            "mer_lo": lo, "mer_hi": hi,
            "ber": bers[-1] if bers else float("nan"), "audio_s": audio_s}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mhz", nargs="+", type=float, required=True)
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--tag", default="fieldtest")
    a = ap.parse_args()
    print(f"{'MHz':>6} {'name':10s} {'cap s':>6} {'sync':>5} {'MER lo/hi':>12} "
          f"{'BER':>9} {'audio':>6}")
    for mhz in a.mhz:
        try:
            out, secs, wall = capture(mhz, a.secs, a.tag)
        except Exception as e:
            print(f"{mhz:6.1f} CAPTURE FAIL: {str(e)[:60]}")
            continue
        res = nrsc5_replay(out, secs)
        print(f"{mhz:6.1f} {res['name'][:10]:10s} {secs:6.1f} {str(res['sync']):>5} "
              f"{res['mer_lo']:5.1f}/{res['mer_hi']:5.1f} {res['ber']:9.5f} "
              f"{res['audio_s']:6.1f}  -> {out.name}")


if __name__ == "__main__":
    main()
