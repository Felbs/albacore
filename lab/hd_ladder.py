#!/usr/bin/env python3
"""albacore lab: AWGN degradation ladder — find stock nrsc5's exact cliff.

Adds calibrated complex AWGN to a cs16 capture (levels = dB raise of the
measured in-band noise floor), replays each rung through stock nrsc5, and
tabulates avg BER + decoded-audio duration. Every future albacore
improvement is a shift of this ladder.

Usage: python hd_ladder.py CAPTURE.cs16 [--secs 30] [--rungs 0,2,4,6,8,10]
"""
import argparse, re, subprocess, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_radio

FS = 2976750.0
NFFT = 8192
TMP = Path(r"C:\Users\emane\.claude\jobs\3948c3da\tmp")
import os
NRSC5 = os.environ.get("NRSC5_EXE", r"C:\Tools\nrsc5\nrsc5.exe")


def noise_floor(x):
    """Mean PSD in +250..+350 kHz (bins 688..963), linear per-bin power."""
    xs = x[:200 * NFFT].reshape(-1, NFFT)
    w = np.hanning(NFFT).astype(np.float32)
    psd = (np.abs(np.fft.fft(xs * w, axis=1)) ** 2).mean(0)
    psd = np.fft.fftshift(psd)
    # per-sample variance of a white process that would produce this
    # per-bin level: E|X_k|^2 = sigma^2 * sum(w^2)
    return psd[4096 + 688:4096 + 963].mean() / (w ** 2).sum()


def run_rung(x, sigma2, tag, rng, good_decim=False):
    if sigma2 > 0:
        nz = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
        y = x + (nz * np.sqrt(sigma2 / 2)).astype(np.complex64)
    else:
        y = x
    if good_decim:
        # proper halfband instead of hd_radio's 2-tap average: the 2-tap
        # folds most out-of-band noise into the signal band on decimation
        from scipy.signal import resample_poly
        h = None
        yd = resample_poly(y, 1, 2, window=("kaiser", 9.0))
        yi = np.clip(np.round(yd.real), -32768, 32767).astype(np.int16)
        yq = np.clip(np.round(yd.imag), -32768, 32767).astype(np.int16)
        inter = np.empty(2 * len(yd), np.int16)
        inter[0::2] = yi; inter[1::2] = yq
        cu8 = hd_radio.cs16_to_cu8(inter)
    else:
        yi = np.clip(np.round(y.real), -32768, 32767).astype(np.int16)
        yq = np.clip(np.round(y.imag), -32768, 32767).astype(np.int16)
        inter = np.empty(2 * len(y), np.int16)
        inter[0::2] = yi; inter[1::2] = yq
        cu8 = hd_radio.cs16_to_cu8(hd_radio.decimate2_cs16(inter))
    f = TMP / f"ladder_{tag}.cu8"
    cu8.tofile(f)
    wav = TMP / f"ladder_{tag}.wav"
    if wav.exists():
        wav.unlink()
    r = subprocess.run([NRSC5, "-r", str(f), "-o", str(wav), "0"],
                       capture_output=True, text=True, timeout=300)
    log = r.stderr + r.stdout
    bers = [float(m) for m in re.findall(r"BER: [\d.]+, avg: ([\d.]+)", log)]
    mers = re.findall(r"MER: ([-\d.]+) dB \(lower\), ([-\d.]+) dB \(upper\)", log)
    sync = "Synchronized" in log
    audio_s = 0.0
    if wav.exists() and wav.stat().st_size > 44:
        audio_s = (wav.stat().st_size - 44) / (44100.0 * 2 * 2)
    lb = np.median([float(a) for a, b in mers]) if mers else float("nan")
    ub = np.median([float(b) for a, b in mers]) if mers else float("nan")
    f.unlink()
    return {"sync": sync, "ber": bers[-1] if bers else float("nan"),
            "mer_lb": lb, "mer_ub": ub, "audio_s": audio_s}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--t-start", type=float, default=5.0)
    ap.add_argument("--rungs", default="0,2,4,6,8,10,12")
    ap.add_argument("--good-decim", action="store_true")
    a = ap.parse_args()

    raw = np.fromfile(a.capture, dtype=np.int16,
                      count=int(a.secs * FS) * 2, offset=int(a.t_start * FS) * 4)
    x = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32))
    nf = noise_floor(x)
    rng = np.random.default_rng(42)
    name = Path(a.capture).stem
    print(f"{name}: {a.secs:.0f}s, noise floor {10*np.log10(nf):.1f} dB/bin-equiv")
    print(f"{'rung(dB)':>8} {'sync':>5} {'BER':>9} {'MER lb/ub':>12} {'audio s':>8}")
    for r_db in [float(v) for v in a.rungs.split(",")]:
        sigma2 = nf * (10 ** (r_db / 10) - 1)  # per-sample added variance
        res = run_rung(x, sigma2, f"{name[:12]}_{r_db:g}", rng, a.good_decim)
        print(f"{r_db:8g} {str(res['sync']):>5} {res['ber']:9.5f} "
              f"{res['mer_lb']:5.1f}/{res['mer_ub']:5.1f} {res['audio_s']:8.1f}")


if __name__ == "__main__":
    main()
