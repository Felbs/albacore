#!/usr/bin/env python3
"""albacore lab: synthetic NRSC-5 grid to validate hd_ref_lock in vitro.

Builds a 4x-rate hybrid-like signal: DBPSK reference subcarriers + random
QPSK data subcarriers, 8640-sample symbols with raised-sine tapered CP
(overlap-added like the real TX), known timing offset + CFO + noise.
Then runs the same extraction/metric as hd_ref_lock and reports whether
the known truth is recovered.
"""
import numpy as np
import hd_ref_lock as L

rng = np.random.default_rng(7)
FS, SYM, NFFT, CPX = L.FS, L.SYM, L.NFFT, L.CPX
REF_SC = L.REF_SC

N_SYMS = 40
TRUE_T0 = 3000       # samples of dead air before first symbol
TRUE_CFO = 75.0      # Hz
SNR_DB = 20.0

# data subcarriers: all of +/-356..546 except refs
all_sc = np.concatenate([np.arange(-546, -355), np.arange(356, 547)])
data_sc = np.array([k for k in all_sc if k not in set(REF_SC.tolist())])

# DBPSK ref bits: start at +1, random differential bits per symbol
ref_bits = rng.integers(0, 2, size=(N_SYMS, len(REF_SC)))
ref_sym = np.cumprod(np.where(ref_bits, -1.0, 1.0), axis=0)  # BPSK levels

# taper shape (4x scaled from acquire.c): sin ramp CPX, flat, cos ramp CPX
shape = np.ones(SYM + CPX, np.float32)
shape[:CPX] = np.sin(np.pi / 2 * np.arange(CPX) / CPX)
shape[SYM:] = np.cos(np.pi / 2 * np.arange(CPX) / CPX)

n_total = TRUE_T0 + (N_SYMS + 2) * SYM
x = np.zeros(n_total + SYM, np.complex64)
t_sym = np.arange(SYM + CPX)
for s in range(N_SYMS):
    spec = np.zeros(NFFT, np.complex64)
    spec[REF_SC % NFFT] = ref_sym[s]
    spec[data_sc % NFFT] = (rng.choice([1, -1], len(data_sc))
                            + 1j * rng.choice([1, -1], len(data_sc))) / np.sqrt(2)
    # time waveform: subcarrier tones over SYM+CPX samples (cyclic extension)
    wav = np.fft.ifft(spec) * NFFT  # one NFFT period
    wav = np.concatenate([wav, wav, wav])[:SYM + CPX]  # extend cyclically
    start = TRUE_T0 + s * SYM
    x[start:start + SYM + CPX] += (shape * wav).astype(np.complex64)

# CFO + noise
n = np.arange(len(x), dtype=np.float64)
x = x * np.exp(2j * np.pi * TRUE_CFO * n / FS)
sig_p = np.mean(np.abs(x[TRUE_T0:TRUE_T0 + 10 * SYM]) ** 2)
noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) / np.sqrt(2)
x = (x + noise * np.sqrt(sig_p / 10 ** (SNR_DB / 10))).astype(np.complex64)

print(f"synthetic: t0={TRUE_T0} (mod {SYM}: {TRUE_T0 % SYM}), cfo={TRUE_CFO} Hz, "
      f"snr={SNR_DB} dB, {N_SYMS} symbols")

W = L.ref_matrix()

# direct check at truth: extract at known t0 with known cfo removed
shifted = (x * np.exp(-2j * np.pi * TRUE_CFO * n / FS)).astype(np.complex64)
for label, t0 in [("t0=truth(sym start)", TRUE_T0),
                  ("t0=truth+CPX", TRUE_T0 + CPX),
                  ("t0=truth+CPX/2", TRUE_T0 + CPX // 2)]:
    R = L.extract_refs(shifted, t0, 32, W)
    m, d = L.dbpsk_metric(R)
    print(f"  {label:22s}: metric={m:+.3f}")

# full coarse search
best, grid, t0s, fracs, dks = L.coarse_search(x[:TRUE_T0 + 36 * SYM + NFFT], 32)
m0, t0, frac, dk = best
print(f"coarse: metric={m0:.3f} t0={t0} (truth mod SYM {TRUE_T0 % SYM}) "
      f"frac={frac:+.0f} dk={dk:+d} -> cfo={frac + dk * L.SC_SPACING:+.0f} Hz (truth {TRUE_CFO})")
