#!/usr/bin/env python3
"""albacore lab: universal antenna auto-calibration (the STVT method for HD).

For each station on the chosen antenna port, sweep the gain grid
(rfgain_sel x IFGR), score each point by HD sideband margin with a
clipping guard, and pick the knee: the LOWEST gain within 1 dB of the
best margin (headroom beats raw gain — the STVT law). Results land in
hd_ant_cal.json, which hd_radio.open_sdr consults automatically, so
every tool adapts to whatever antenna is plugged in. Env overrides
(HD_IFGR/HD_RFGAIN) always win over the cal.

Usage:
  hd_ant_autotune.py --ant "Antenna C" --mhz 93.3 90.9 88.5 103.5 ...
"""
import argparse, json, sys, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
import hd_radio

hd_radio._ensure_sdr_dll_path()
SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
FS = 2976750.0
NFFT = 8192
CAL = Path(r"Z:\src\gr-radiotuna\lab\hd_ant_cal.json")


def measure(ant, mhz, ifgr, rfgain, secs=2.5):
    sdr = SoapySDR.Device("driver=sdrplay")
    try:
        sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
        sdr.setFrequency(SOAPY_SDR_RX, 0, mhz * 1e6)
        sdr.setAntenna(SOAPY_SDR_RX, 0, ant)
        try:
            sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", ifgr)
        try:
            sdr.writeSetting("rfgain_sel", str(rfgain))
        except Exception:
            pass
        st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
        sdr.activateStream(st)
        buf = np.empty(2 * 262144, np.int16)
        got = []
        t0 = time.time()
        while time.time() - t0 < secs:
            r = sdr.readStream(st, [buf], 262144, timeoutUs=1000000)
            if r.ret > 0:
                got.append(buf[:2 * r.ret].copy())
        sdr.deactivateStream(st)
        sdr.closeStream(st)
    finally:
        sdr = None
    raw = np.concatenate(got)
    peak = int(np.abs(raw).max())
    x = raw.astype(np.float32) / 32768.0
    x = (x[0::2] + 1j * x[1::2]).astype(np.complex64)
    seg = x[: len(x) // NFFT * NFFT].reshape(-1, NFFT)
    w = np.hanning(NFFT).astype(np.float32)
    psd = np.fft.fftshift((np.abs(np.fft.fft(seg * w, axis=1)) ** 2).mean(0))
    db = 10 * np.log10(psd + 1e-12)
    floor = float(np.median(db[4096 + 700:4096 + 1200]))
    sb = float(db[4096 + 357:4096 + 545].mean())
    return sb - floor, peak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ant", required=True)
    ap.add_argument("--mhz", nargs="+", type=float, required=True)
    ap.add_argument("--grid-rf", nargs="+", default=["5", "7"])
    ap.add_argument("--grid-ifgr", nargs="+", type=int, default=[20, 30, 40])
    a = ap.parse_args()
    cal = json.loads(CAL.read_text()) if CAL.exists() else {}
    acal = cal.setdefault(a.ant, {})
    for mhz in a.mhz:
        results = []
        for rf in a.grid_rf:
            for ifgr in a.grid_ifgr:
                try:
                    margin, peak = measure(a.ant, mhz, ifgr, rf)
                    clip = peak > 30000
                    results.append((margin, ifgr, rf, peak, clip))
                    print(f"{mhz:5.1f} rf{rf} ifgr{ifgr}: margin {margin:+5.1f} dB "
                          f"peak {peak}{' CLIP' if clip else ''}")
                except Exception as e:
                    print(f"{mhz:5.1f} rf{rf} ifgr{ifgr}: fail {str(e)[:40]}")
                time.sleep(0.5)
        ok = [r for r in results if not r[4]]
        if not ok:
            print(f"{mhz:5.1f}: all points clip?! skipping")
            continue
        best = max(r[0] for r in ok)
        # knee: lowest total gain within 1 dB of best (gain order: rf asc, ifgr desc)
        cands = [r for r in ok if r[0] >= best - 1.0]
        cands.sort(key=lambda r: (a.grid_rf.index(r[2]), -r[1]))
        m, ifgr, rf, peak, _ = cands[0]
        acal[f"{mhz:.1f}"] = {"ifgr": ifgr, "rfgain": rf,
                              "margin_db": round(m, 1),
                              "utc": datetime.now(timezone.utc).isoformat()}
        print(f"{mhz:5.1f} -> KNEE rf{rf} ifgr{ifgr} (margin {m:+.1f} dB)")
    # per-antenna default = the setting most stations chose
    from collections import Counter
    votes = Counter((v["ifgr"], v["rfgain"]) for k, v in acal.items()
                    if isinstance(v, dict) and "ifgr" in v)
    if votes:
        (difgr, drf), _ = votes.most_common(1)[0]
        acal["_default"] = {"ifgr": difgr, "rfgain": drf}
    CAL.write_text(json.dumps(cal, indent=1))
    print(f"wrote {CAL}")


if __name__ == "__main__":
    main()
