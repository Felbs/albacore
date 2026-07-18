#!/usr/bin/env python3
"""albacore lab: lock the NRSC-5 FM OFDM grid via reference-subcarrier DBPSK.

The CP is tapered+overlap-added, so blind CP autocorrelation is weak (proven
7/18). The real hook is the 22 reference subcarriers (every 19th, +/-356..546)
carrying DBPSK with a known 32-bit block pattern (sync.c:171). This script:

  1. loads a slice of a cs16 capture (4x native rate: fs=2976750, sym=8640,
     useful=8192, 1 FFT bin per subcarrier),
  2. coarse-searches (symbol timing t0) x (fractional CFO) scoring DBPSK-ness
     of the ref bins: d = R[s]*conj(R[s-1]) should be +/-real once the
     deterministic per-ref stride rotation exp(2j*pi*k*448/8192) is removed,
  3. refines the lock, decodes differential ref bits, cyclically correlates
     against the known training word for block alignment,
  4. emits per-ref MER (the first per-subcarrier dial) + |H(f,t)| waterfall.

Usage:
  python hd_ref_lock.py CAPTURE.cs16 [--t-start 5] [--syms 32] [--long 512]
                        [--out-prefix name]
"""
import argparse, json, sys
from pathlib import Path
import numpy as np

FS = 2976750.0          # capture rate = 4x nrsc5 native 744187.5
SYM = 8640              # total symbol @4x (2160 native)
NFFT = 8192             # useful symbol @4x (2048 native)
CPX = SYM - NFFT        # 448 @4x
BLKSZ = 32
SC_SPACING = FS / NFFT  # 363.373 Hz, == native 744187.5/2048

# reference subcarriers, MP1: LB_START..+10 partitions, both sidebands
REF_SC = np.array(sorted([s * (546 - 19 * i) for i in range(11) for s in (+1, -1)]))

# sign pattern per block, sync.c:171 (-1 = don't care; rsid bits 10,11 vary
# per ref so treat them as don't care for the pooled correlation)
NEEDLE = np.array([0, 1, 0, 0, 0, 1, 1, -1, 1, 0, -1, -1, -1, 0, 0, -1,
                   -1, -1, -1, -1, 0, 1, 0, -1, -1, -1, -1, -1, -1, -1, -1, 0],
                  dtype=np.int8)


def load_cs16(path, t_start, n_samps):
    off = int(t_start * FS) * 4  # 2 int16 per sample
    raw = np.fromfile(path, dtype=np.int16, count=n_samps * 2, offset=off)
    if raw.size < n_samps * 2:
        raise SystemExit(f"capture too short: wanted {n_samps} samples at t={t_start}s")
    x = raw.astype(np.float32).view(np.complex64) if False else \
        (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32))
    return x.astype(np.complex64) / 32768.0


def ref_matrix():
    """W: (NFFT, n_refs) so windows @ W = ref-bin DFT values."""
    n = np.arange(NFFT)[:, None]
    return np.exp(-2j * np.pi * REF_SC[None, :] * n / NFFT).astype(np.complex64)


def extract_refs(seg, t0, n_syms, W):
    """R[s, j]: ref-bin values for n_syms symbols starting at sample t0."""
    idx = t0 + np.arange(n_syms)[:, None] * SYM + np.arange(NFFT)[None, :]
    return seg[idx] @ W


def derotate(d):
    """Remove the common rotation on diff products from residual CFO.

    Each OFDM symbol restarts phase at its own start (cyclic block), so
    d = R[s]*conj(R[s-1]) is inherently +/-real; the ONLY systematic
    rotation is the common e^{j*2pi*df*T_sym} from residual CFO. Estimate
    it blindly from angle(sum(d^2)) (sign-invariant) and remove.
    Returns (d_derotated, theta)."""
    theta = 0.5 * np.angle((d.astype(np.complex128) ** 2).sum())
    return d * np.exp(-1j * theta).astype(np.complex64), float(theta)


def dbpsk_metric(R):
    """DBPSK-ness in [~0 noise, ->1 locked] of ref-bin matrix R[s, j]."""
    d, _ = derotate(R[1:] * np.conj(R[:-1]))
    a = np.abs(d).sum()
    return float((np.abs(d.real).sum() - np.abs(d.imag).sum()) / (a + 1e-12)), d


def coarse_search(seg, n_syms, t0_step=24, frac_step=45.0, dk_span=8,
                  t0_chunk=24):
    """Search timing x fractional CFO x integer-bin CFO.

    One full FFT batch per (frac, t0 chunk) serves every integer-bin
    hypothesis dk: refs for shift dk are just bins REF_SC+dk.
    """
    t0s = np.arange(0, SYM, t0_step)
    fracs = np.arange(-180.0, 180.0 + 1e-9, frac_step)
    dks = np.arange(-dk_span, dk_span + 1)
    n = np.arange(len(seg), dtype=np.float64)
    best = (-2, 0, 0.0, 0)  # metric, t0, frac, dk
    grid = np.zeros((len(fracs), len(dks), len(t0s)), np.float32)
    win_rel = np.arange(NFFT)[None, :]
    sym_off = np.arange(n_syms)[:, None] * SYM
    for fi, frac in enumerate(fracs):
        shifted = (seg * np.exp(-2j * np.pi * frac * n / FS)).astype(np.complex64)
        for c0 in range(0, len(t0s), t0_chunk):
            chunk = t0s[c0:c0 + t0_chunk]
            idx = chunk[:, None, None] + sym_off[None, :, :] + win_rel[None, :, :]
            F = np.fft.fft(shifted[idx], axis=2)  # (chunk, syms, NFFT)
            for di, dk in enumerate(dks):
                R = F[:, :, (REF_SC + dk) % NFFT]  # (chunk, syms, refs)
                d = R[:, 1:] * np.conj(R[:, :-1])
                th = 0.5 * np.angle((d.astype(np.complex128) ** 2).sum(axis=(1, 2)))
                d = d * np.exp(-1j * th)[:, None, None].astype(np.complex64)
                a = np.abs(d).sum(axis=(1, 2))
                m = (np.abs(d.real).sum(axis=(1, 2))
                     - np.abs(d.imag).sum(axis=(1, 2))) / (a + 1e-12)
                grid[fi, di, c0:c0 + len(chunk)] = m
                mi = int(np.argmax(m))
                if m[mi] > best[0]:
                    best = (float(m[mi]), int(chunk[mi]), float(frac), int(dk))
    return best, grid, t0s, fracs, dks


def refine(seg, n_syms, W, t0, cfo, t_rad=24, t_step=2, f_rad=45.0, f_step=5.0):
    n = np.arange(len(seg), dtype=np.float64)
    best = (-2, t0, cfo)
    for f in np.arange(cfo - f_rad, cfo + f_rad + 1e-9, f_step):
        shifted = (seg * np.exp(-2j * np.pi * f * n / FS)).astype(np.complex64)
        for t in range(max(0, t0 - t_rad), t0 + t_rad + 1, t_step):
            m, _ = dbpsk_metric(extract_refs(shifted, t, n_syms, W))
            if m > best[0]:
                best = (m, t, float(f))
    return best


def block_align(d):
    """Cyclic offset of the 32-symbol block from differential known bits.

    d[s, j]: compensated diff products. diff bit = 1 if Re(d)<0.
    Known differential bits exist where needle[n] and needle[n-1] both known
    (cyclically: bit31=0, bit0=0 so the wrap is known too).
    """
    nd = NEEDLE
    known = [(n, nd[n] ^ nd[(n - 1) % BLKSZ]) for n in range(BLKSZ)
             if nd[n] >= 0 and nd[(n - 1) % BLKSZ] >= 0]
    bits = (d.real < 0).astype(np.int8)  # [s, j]
    S = bits.shape[0]
    scores = np.zeros(BLKSZ)
    for off in range(BLKSZ):
        tot = hit = 0
        for n, want in known:
            # diff product d[s] compares symbol s+1 vs s -> d index of block
            # bit n at block offset off is s = (n - 1 - off) mod 32 + 32*b
            s0 = (n - 1 - off) % BLKSZ
            sel = bits[s0::BLKSZ, :]
            hit += int((sel == want).sum()); tot += sel.size
        scores[off] = hit / max(tot, 1)
    return int(np.argmax(scores)), scores


def per_ref_mer(d, off):
    """Unbiased MER dial per ref, via KNOWN training-word bits.

    Folding by sign(Re) biases hard at low SNR (pure noise folds to a
    ~6-7 dB pseudo-floor and the dial goes non-monotonic — caught when
    nrsc5's per-sideband MER ordered 91.9's sidebands the other way).
    Instead: at block-word positions where consecutive needle bits are
    known, the expected sign of Re(d) is known — average coherently.
    Pure noise then correctly reads -inf.
    """
    S = d.shape[0]
    exp_sign = np.zeros(S, np.float32)
    for n in range(BLKSZ):
        if NEEDLE[n] >= 0 and NEEDLE[(n - 1) % BLKSZ] >= 0:
            want = NEEDLE[n] ^ NEEDLE[(n - 1) % BLKSZ]
            s0 = (n - 1 - off) % BLKSZ
            exp_sign[s0::BLKSZ] = 1.0 if want == 0 else -1.0
    sel = exp_sign != 0
    c = d * exp_sign[:, None]           # zeroed rows dropped per block below
    # Phase-tolerant: channel phase wanders over seconds (nrsc5 tracks it
    # with per-ref Costas loops); assume coherence only WITHIN each
    # 32-symbol block (93 ms). Signal power from per-block means, unbiased
    # by the mean's own noise; noise from within-block scatter.
    n_blk = d.shape[0] // BLKSZ
    mus, var_acc, cnt = [], np.zeros(d.shape[1]), 0
    for b in range(n_blk):
        cb = c[b * BLKSZ:(b + 1) * BLKSZ]
        cb = cb[sel[b * BLKSZ:(b + 1) * BLKSZ] != 0]
        if len(cb) < 4:
            continue
        mu_b = cb.mean(axis=0)
        var_acc += (np.abs(cb - mu_b[None, :]) ** 2).sum(axis=0)
        cnt += len(cb) - 1
        mus.append((mu_b, len(cb)))
    var = var_acc / max(cnt, 1)
    sig = np.mean([np.abs(mu) ** 2 - var / k for mu, k in mus], axis=0)
    return 10.0 * np.log10(np.maximum(sig, 1e-12) / np.maximum(var, 1e-12) + 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--t-start", type=float, default=5.0)
    ap.add_argument("--syms", type=int, default=32, help="symbols for search")
    ap.add_argument("--long", type=int, default=512, help="symbols for H/MER extraction")
    ap.add_argument("--out-prefix", default=None)
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args()

    cap = Path(a.capture)
    prefix = a.out_prefix or cap.stem
    outdir = Path(__file__).parent / "out"
    outdir.mkdir(exist_ok=True)

    W = ref_matrix()
    n_load = (max(a.syms, a.long) + 2) * SYM
    seg = load_cs16(cap, a.t_start, n_load)
    print(f"loaded {len(seg)} samples ({len(seg)/FS:.2f}s) from t={a.t_start}s of {cap.name}")

    (m0, t0, frac0, dk0), grid, t0s, fracs, dks = coarse_search(seg, a.syms)
    cfo0 = frac0 + dk0 * SC_SPACING
    print(f"coarse: metric={m0:.3f} t0={t0} frac={frac0:+.0f} Hz dk={dk0:+d} "
          f"-> cfo={cfo0:+.0f} Hz (grid max {grid.max():.3f}, "
          f"median {np.median(grid):.3f})")

    m1, t1, cfo1 = refine(seg, a.syms, W, t0, cfo0)
    print(f"refine: metric={m1:.3f} t0={t1} cfo={cfo1:+.1f} Hz")

    n = np.arange(len(seg), dtype=np.float64)
    shifted = (seg * np.exp(-2j * np.pi * cfo1 * n / FS)).astype(np.complex64)
    R = extract_refs(shifted, t1, a.long, W)
    _, d = dbpsk_metric(R)

    off, scores = block_align(d)
    print(f"block align: offset={off} score={scores[off]:.3f} "
          f"(runner-up {sorted(scores)[-2]:.3f}, chance 0.5)")

    mer = per_ref_mer(d, off)
    print("per-ref MER (dB):")
    for sc, m in zip(REF_SC, mer):
        print(f"  sc {sc:+4d} ({sc*SC_SPACING/1000.:+7.1f} kHz): {m:5.1f}")
    lsb, usb = mer[REF_SC < 0], mer[REF_SC > 0]
    # LSB/USB here = negative/positive baseband frequency of the capture
    # (= lower/upper RF for a non-inverting SDR). NOTE: stock nrsc5's
    # lower/upper MER labels are spectrally INVERTED w.r.t. its cu8 input
    # (verified 7/18 by nulling one sideband: kill negative freqs -> its
    # "upper" craters). When cross-checking: our LSB ~ nrsc5 "upper".
    print(f"sideband medians: LSB {np.median(lsb):.1f} dB, USB {np.median(usb):.1f} dB")

    report = {
        "capture": cap.name, "t_start": a.t_start, "fs": FS,
        "search_syms": a.syms, "extract_syms": a.long,
        "coarse_metric": m0, "metric": m1, "t0": t1, "cfo_hz": cfo1,
        "block_offset": off, "block_score": float(scores[off]),
        "ref_sc": REF_SC.tolist(), "mer_db": mer.tolist(),
    }
    rp = outdir / f"{prefix}_lock.json"
    rp.write_text(json.dumps(report, indent=1))
    print(f"wrote {rp}")

    if not a.no_plots:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 2, figsize=(13, 9))
        ax = axes[0, 0]
        g2 = grid.max(axis=0)  # (dks, t0s), max over fractional CFO
        im = ax.imshow(g2, aspect="auto", origin="lower",
                       extent=[t0s[0], t0s[-1], dks[0], dks[-1]], cmap="magma")
        ax.set(title=f"coarse DBPSK metric (max {grid.max():.3f})",
               xlabel="t0 (samples)", ylabel="integer CFO (bins)")
        fig.colorbar(im, ax=ax)
        ax = axes[0, 1]
        neg, pos = REF_SC < 0, REF_SC > 0
        ax.plot(REF_SC[neg] * SC_SPACING / 1e3, mer[neg], "o-", color="C0")
        ax.plot(REF_SC[pos] * SC_SPACING / 1e3, mer[pos], "o-", color="C0")
        ax.set(title="per-ref MER dial", xlabel="offset (kHz)", ylabel="MER (dB)")
        ax.grid(True, alpha=.3)
        ax = axes[1, 0]
        u = d * np.sign(d.real + 1e-12)
        ax.plot(d.real.ravel(), d.imag.ravel(), ".", ms=1, alpha=.25)
        ax.set(title="diff products d (locked: +/-real)", xlabel="Re", ylabel="Im")
        ax.axvline(0, color="k", lw=.5); ax.axhline(0, color="k", lw=.5)
        ax = axes[1, 1]
        im = ax.imshow(20 * np.log10(np.abs(R.T) + 1e-9), aspect="auto",
                       origin="lower", cmap="viridis")
        ax.set(title="|H(f,t)| at refs (dB)", xlabel="symbol", ylabel="ref idx")
        fig.colorbar(im, ax=ax)
        fig.suptitle(f"{cap.name}  metric={m1:.3f}  cfo={cfo1:+.1f}Hz  "
                     f"block@{off} ({scores[off]:.2f})")
        fig.tight_layout()
        pp = outdir / f"{prefix}_lock.png"
        fig.savefig(pp, dpi=110)
        print(f"wrote {pp}")


if __name__ == "__main__":
    main()
