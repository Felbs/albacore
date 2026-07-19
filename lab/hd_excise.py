#!/usr/bin/env python3
"""albacore lab: narrowband interferer excision — the sync rescuer.

THE FINDING (7/19 selective cliff-walk): a CW interferer inside a
digital sideband at roughly the sideband's own power level kills
NRSC-5 ACQUISITION outright — the decoder never syncs, so no FEC
sophistication (CSI weighting, erasures, MMSE) ever gets a vote.
Controls proved it: the same tone outside the sidebands is harmless,
and in-band tones 10 dB weaker are harmless. Sync is the cliff.

THE LEVER: detect persistent spectral lines inside the sidebands and
subtract them before the decoder. A 120 Hz-wide excision (mix the line
to DC, extract with a one-pole, subtract) resurrected a specimen from
sync=False to full decode (MER events + 2.2 MB audio) in-vitro. The
partition costs of the notch are exactly what the certified
ALBACORE=1 partition weighting already handles.

Usage:
  hd_excise.py IN.cs16 OUT.cs16 [--fs 2976750] [--max-lines 3]
               [--thresh-db 15]

Prints what it found and removed. Chain into any replay/listen path.
Next steps: live A/B leg in the day lab; then port into nrsc5's input
stage (input.c) behind ALBACORE_EXCISE.
"""
import argparse
from pathlib import Path

import numpy as np
from scipy.signal import lfilter


def detect_lines(x, fs, max_lines, thresh_db):
    """Persistent lines inside the digital sidebands (125-205 kHz)."""
    N = 1 << 18
    nseg = min(20, len(x) // N)
    seg = x[: nseg * N].reshape(-1, N)
    psd = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(0))
    fax = np.fft.fftshift(np.fft.fftfreq(N, 1 / fs))
    sb = (np.abs(fax) > 125e3) & (np.abs(fax) < 205e3)
    med = float(np.median(psd[sb]))
    lines = []
    p = psd.copy()
    for _ in range(max_lines):
        i = int(np.argmax(np.where(sb, p, 0)))
        snr = 10 * np.log10(p[i] / med + 1e-12)
        if snr < thresh_db:
            break
        lines.append((float(fax[i]), float(snr)))
        w = int(500 / (fs / N))          # blank +-500 Hz around the find
        p[max(0, i - w):i + w] = med
    return lines


def excise(x, fs, f_line, bw_hz=120.0):
    n = np.arange(len(x), dtype=np.float64)
    mix = np.exp(-2j * np.pi * f_line * n / fs)
    a1 = float(np.exp(-2 * np.pi * bw_hz / fs))
    line = lfilter([1 - a1], [1, -a1], x * mix)
    return x - line * np.conj(mix)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--fs", type=float, default=2_976_750.0)
    ap.add_argument("--max-lines", type=int, default=3)
    ap.add_argument("--thresh-db", type=float, default=15.0)
    a = ap.parse_args()

    raw = np.fromfile(a.infile, dtype=np.int16)
    x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    lines = detect_lines(x, a.fs, a.max_lines, a.thresh_db)
    if not lines:
        print("no persistent sideband lines above threshold — "
              "writing input unchanged")
    for f_line, snr in lines:
        print(f"excising line at {f_line:+9.1f} Hz ({snr:.1f} dB above "
              f"sideband median)")
        x = excise(x, a.fs, f_line)
    out = np.empty(2 * len(x), np.int16)
    out[0::2] = np.clip(x.real, -32767, 32767).astype(np.int16)
    out[1::2] = np.clip(x.imag, -32767, 32767).astype(np.int16)
    out.tofile(a.outfile)
    print(f"wrote {a.outfile} ({len(lines)} line(s) removed)")


if __name__ == "__main__":
    main()
