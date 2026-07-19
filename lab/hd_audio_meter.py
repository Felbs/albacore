#!/usr/bin/env python3
"""albacore lab: blind audio-quality meter for decoded HD Radio audio.

The TV law, ported: decoded WAVEFORMS are a measurement layer that
decoder counters (MER/BER/frame CRCs) cannot see — codec artifacts,
dropouts, and mutes happen after the counters. This meter scores a
decoded WAV per second, no reference needed:

  - silence   : RMS below floor (decoder muted / no frames)
  - clicks    : sample-delta outliers (discontinuities, splices)
  - bwidth    : upper spectral rolloff point (HDC failure collapses
                bandwidth before it mutes)
  - flatness  : spectral flatness bursts (watery/metallic artifacts)
  - LISTEN%   : fraction of seconds that pass all gates — the audio
                GLASS% for the Knob of Time.

Usage: python hd_audio_meter.py FILE.wav [--csv out.csv] [--per-second]
"""
import argparse, csv, sys, wave
from pathlib import Path
import numpy as np


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        fs = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    raw = raw[:len(raw) // 2 * 2]          # growing files: partial sample
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch == 2:
        x = x[:len(x) // 2 * 2].reshape(-1, 2).mean(axis=1)
    return x, fs


def per_second_metrics(x, fs):
    n_sec = len(x) // fs
    out = []
    for s in range(n_sec):
        seg = x[s * fs:(s + 1) * fs]
        rms = float(np.sqrt((seg ** 2).mean()))
        # clicks: calibrated on known-good music — max sample delta stays
        # below ~7x RMS (p95 6.8); real splices/discontinuities exceed 8x
        d = np.abs(np.diff(seg))
        clicks = int((d > 8 * (rms + 1e-9)).sum())
        # spectrum of the second
        w = np.hanning(len(seg)).astype(np.float32)
        spec = np.abs(np.fft.rfft(seg * w)) ** 2
        freqs = np.fft.rfftfreq(len(seg), 1 / fs)
        p = spec / (spec.sum() + 1e-12)
        # bandwidth: frequency below which 99% of energy lives
        cum = np.cumsum(p)
        bw = float(freqs[min(int(np.searchsorted(cum, 0.99)), len(freqs) - 1)])
        # spectral flatness (geometric/arithmetic mean) over 0.3-8 kHz
        band = spec[(freqs > 300) & (freqs < 8000)] + 1e-12
        flat = float(np.exp(np.log(band).mean()) / band.mean())
        out.append({"sec": s, "rms": rms, "clicks": clicks,
                    "bw_hz": bw, "flatness": flat})
    return out


def judge(rows):
    """Gates -> per-second ok flag + LISTEN%."""
    for r in rows:
        r["silent"] = r["rms"] < 1e-3
        r["clicky"] = r["clicks"] > 5
        r["collapsed"] = (not r["silent"]) and r["bw_hz"] < 3000
        r["ok"] = not (r["silent"] or r["clicky"] or r["collapsed"])
    n = len(rows)
    listen = 100.0 * sum(r["ok"] for r in rows) / max(n, 1)
    return listen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--csv")
    ap.add_argument("--per-second", action="store_true")
    a = ap.parse_args()
    x, fs = load_wav(a.wav)
    rows = per_second_metrics(x, fs)
    if not rows:
        print(f"{Path(a.wav).name}: <1s of audio")
        return
    listen = judge(rows)
    sil = sum(r["silent"] for r in rows)
    clk = sum(r["clicky"] for r in rows)
    col = sum(r["collapsed"] for r in rows)
    med_bw = np.median([r["bw_hz"] for r in rows if not r["silent"]] or [0])
    print(f"{Path(a.wav).name}: {len(rows)}s  LISTEN {listen:.0f}%  "
          f"(silent {sil}s, clicky {clk}s, bw-collapsed {col}s, "
          f"median bw {med_bw/1000:.1f} kHz)")
    if a.per_second:
        for r in rows:
            mark = "" if r["ok"] else (" SILENT" if r["silent"] else
                                       " CLICKY" if r["clicky"] else " COLLAPSED")
        print("  " + "".join("." if r["ok"] else "X" for r in rows))
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            wcsv = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wcsv.writeheader()
            wcsv.writerows(rows)
        print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()
