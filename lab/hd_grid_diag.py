#!/usr/bin/env python3
"""albacore lab: PSD + ref-comb diagnostic for a cs16 HD capture.

Averages 8192-bin PSD (1 bin = 1 subcarrier), reports analog/sideband power,
then correlates the sideband PSD against the every-19th-bin reference comb to
find the integer-bin CFO (and spectral-inversion evidence).
"""
import sys
from pathlib import Path
import numpy as np

FS = 2976750.0
NFFT = 8192
REF_SC = np.array(sorted([s * (546 - 19 * i) for i in range(11) for s in (+1, -1)]))

path = Path(sys.argv[1])
t_start = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
n_win = 400

off = int(t_start * FS) * 4
raw = np.fromfile(path, dtype=np.int16, count=n_win * NFFT * 2, offset=off)
x = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 32768.0
x = x[:(len(x) // NFFT) * NFFT].reshape(-1, NFFT)
w = np.hanning(NFFT).astype(np.float32)
psd = (np.abs(np.fft.fft(x * w, axis=1)) ** 2).mean(axis=0)
psd = np.fft.fftshift(psd)          # index 4096 = DC, subcarrier k at 4096+k
db = 10 * np.log10(psd + 1e-12)

def band_db(k0, k1):
    return 10 * np.log10(psd[4096 + k0:4096 + k1].mean() + 1e-12)

print(f"{path.name}  ({x.shape[0]} windows averaged)")
print(f"  analog center +/-40kHz : {band_db(-110, 110):6.1f} dB")
print(f"  lower sideband -198..-129k: {band_db(-546, -356):6.1f} dB")
print(f"  upper sideband +129..+198k: {band_db(356, 546):6.1f} dB")
print(f"  noise floor    +250..+350k: {band_db(688, 963):6.1f} dB")

# comb correlation: shift ref comb by delta bins, sum PSD at shifted positions
shifts = np.arange(-40, 41)
score = np.array([psd[4096 + REF_SC + s].sum() for s in shifts])
base = np.median(score)
best = shifts[np.argmax(score)]
print(f"  ref-comb scan: best shift = {best:+d} bins "
      f"({best * FS / NFFT:+.0f} Hz), peak/median = {score.max() / base:.3f}")
top = np.argsort(score)[::-1][:5]
for i in top:
    print(f"    shift {shifts[i]:+3d} ({shifts[i] * FS / NFFT:+6.0f} Hz): {score[i] / base:.3f}")

# per-sideband comb (in case one sideband is dead)
for name, sel in (("LSB", REF_SC[REF_SC < 0]), ("USB", REF_SC[REF_SC > 0])):
    sc = np.array([psd[4096 + sel + s].sum() for s in shifts])
    b = shifts[np.argmax(sc)]
    print(f"  {name} comb: best {b:+d} bins, peak/median {sc.max() / np.median(sc):.3f}")
