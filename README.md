# albacore 🐟📻

**The premium cut of Radio Tuna: an instrumented fork of
[nrsc5](https://github.com/theori-io/nrsc5) that opens the HD Radio
decoder the way [gr-atscplus](https://github.com/Felbs/Software-TV-Tuner)
opened ATSC television.**

Part of the Tuna family:
[TV Tuna](https://github.com/Felbs/Software-TV-Tuner) →
[Radio Tuna](https://github.com/Felbs/gr-radiotuna) →
[wxTuna](https://github.com/Felbs/wxTuna) →
[aeroTuna](https://github.com/Felbs/aeroTuna) →
[hamTuna](https://github.com/Felbs/hamTuna) → **albacore**.
Same thesis every time: every decoder secretly knows how well it's
doing — surface that truth-dial and close the loop on it.

## Lineage

This is a fork of **[theori-io/nrsc5](https://github.com/theori-io/nrsc5)**,
the open-source NRSC-5 (HD Radio) receiver — full upstream history
preserved, upstream remote intact, and their original README kept at
[doc/UPSTREAM_README.md](doc/UPSTREAM_README.md). All the hard decoder
work is theirs; albacore's mission is to *instrument* it. GPL-3.0,
same as upstream (see [LICENSE](LICENSE)).

## Why fork a working decoder?

Because NRSC-5 is OFDM, and OFDM transmits its own answer key: known
**reference subcarriers** sprinkled through the time-frequency grid.
A stock receiver uses them just enough to demodulate. An *instrumented*
receiver treats them as a live map of everything the antenna, the
walls, and the multipath did to the signal — and closes a loop on it.
This worked on broadcast television (measurably: watchable TV from
rabbit ears and scanner antennas); HD Radio's grid is finer.

The campaign, in planned order — each stage gated by a replay A/B
against stock nrsc5 on frozen cliff-edge IQ captures:

1. **Truth-dial taps** — export per-subcarrier SNR, Viterbi path
   metrics, and partition-level BER continuously (the `fs_err_rms`
   of HD).
2. **Confidence-guided erasures** — soft information into the
   deinterleaver/Viterbi where stock decodes hard (the lever that won
   our ADS-B and radiosonde campaigns).
3. **Confidence-weighted sideband combining** — the duplicated
   upper/lower sideband data, merged by measured quality rather than
   blindly.
4. **The universal-antenna outer loop** — per-station gain and antenna
   calibration against the dials, learned and re-learned forever
   (the Knob of Time; HD provably has hour-curves too).

## The grid, and how we found it

*A field log for radio engineers. The waveform is fully documented in
NRSC-5 (and in upstream's source), but we wanted to find it the way
you'd find an undocumented one — measure first, read the spec as a
referee afterward. That discipline caught real bugs in our own
receiver math, and the dead ends are the educational part.*

### What the FM IBOC grid is

Hybrid FM HD Radio is OFDM hiding in the shoulders of an ordinary
analog FM broadcast. All parameters below are at nrsc5's native
complex rate of 744,187.5 Hz (= 135/64 × 352,800):

| Parameter | Value |
|---|---|
| FFT length (useful symbol) | 2048 samples |
| Guard interval | 112 samples — **tapered, not a plain CP** |
| Total symbol | 2160 samples → 344.53 symbols/s (2.902 ms) |
| Subcarrier spacing | 744187.5 / 2048 = **363.373 Hz** |
| Active subcarriers (hybrid MP1) | ±356…±546 → sidebands at ±129.4…±198.4 kHz |
| Partition | 19 subcarriers = 18 data + 1 reference |
| Reference subcarriers | every 19th: ±356, ±375, … ±546 (11 per sideband) |
| Reference modulation | DBPSK, a known 32-bit word per L1 block |
| L1 block / frame | 32 symbols / 16 blocks (512 symbols, 1.486 s) |

The digital sidebands sit ~20 dB below the analog FM host and ride
out beyond ±129 kHz where the analog energy has died off. Extended
service modes (MP2/MP3/MP11…) add partitions *inward* toward the
analog host; the outer reference comb stays put, which makes it the
universal handle across modes.

### Dead end #1: cyclic-prefix autocorrelation

The textbook OFDM blind-acquisition move — correlate the signal with
itself delayed by the FFT length, look for the guard-interval ridge —
returned metrics indistinguishable from noise. Not a bug: **NRSC-5's
guard is not a copy.** The transmitter applies a raised-sine ramp
over the 112-sample guard region and overlap-adds adjacent symbols
(windowed OFDM, for spectral containment next to the analog host).
There is no clean repeated segment to correlate against, only a
tapered crossfade. Upstream nrsc5 uses the guard correlation just
once, coarsely, at acquisition — then hands everything to the
reference subcarriers. Lesson: **on this waveform, the refs are the
acquisition tool, not the CP.**

### Dead end #2: finding the reference comb by power

On TV (ATSC) the pilot is boosted, so a power spectrum betrays it.
Here the reference subcarriers transmit at the *same* power as data
subcarriers — an averaged PSD shows two smooth 70-kHz-wide shoulders
with no comb structure at all. Power tells you the sidebands exist
(and locates their edges to ±1 bin, which is how we confirmed the
grid-to-FFT-bin alignment); it cannot find the refs. Only their
*modulation* distinguishes them.

### What worked: DBPSK-ness of the reference diff products

Capture at exactly 4× native (2,976,750 S/s) so one FFT bin = one
subcarrier with no resampling. Then hypothesize (symbol timing t₀ ×
fractional CFO × integer-bin CFO) and score each hypothesis by how
DBPSK-like the 22 reference bins are across consecutive symbols:

    d[s,j] = R[s,j] · conj(R[s−1,j])      (diff product per ref j)
    metric = (Σ|Re d| − Σ|Im d|) / Σ|d|   (≈0 for noise, →1 locked)

One full FFT per candidate window serves every integer-CFO hypothesis
(shift the ref comb, not the signal), so the 3-D search is cheap.

Two subtleties earned their scars:

1. **OFDM symbols restart phase at every symbol start.** If you model
   a subcarrier as a continuous tone, you'll derive a per-symbol
   phase advance of 2πk·112/2048 from the guard stride and
   "compensate" for it — scrambling each reference by a different
   constant angle and burying the lock in noise. Each symbol is an
   independent cyclic block; the diff products are already ±real.
   (This one bug cost the first afternoon. A synthetic transmitter
   with known truth found it in minutes — build the synthetic first.)
2. **Residual CFO rotates all diff products by one common angle**
   (2π·Δf·T_sym). It's sign-invariant under DBPSK, so estimate it
   blindly as ½·arg(Σd²) and derotate before scoring. The metric then
   self-corrects for most of a subcarrier spacing of CFO error, and
   the angle hands you a free fine-CFO estimate.

Block alignment falls out afterward: differentially decode each ref's
bit stream and cyclically correlate against the known bits of the
32-bit block word (`sync.c` in the source keeps seven fixed bits plus
structure; the wrap bit is known too). Chance is 0.5; a real lock
scores ~1.0.

### Validation against the referee

On a strong local station the whole chain agrees with stock nrsc5 run
on the same capture: our refined CFO came out **+75.0 Hz** and nrsc5
reported **"Frequency offset: 75 Hz"**; block-word correlation hit
1.000. On a cliff specimen the per-ref MER dial matched nrsc5's
per-sideband MER within tenths of a dB.

Two estimator lessons the referee taught us:

- **Don't fold.** A first MER estimator removed the DBPSK modulation
  with `sign(Re)` before measuring scatter. Folded noise doesn't read
  as no-signal — it reads as a ~6–7 dB pseudo-floor, and the dial goes
  *non-monotonic* at low SNR (a dead sideband out-scored a weak live
  one). The fix: after block alignment the training-word bits are
  known, so average diff products coherently with known signs — pure
  noise then correctly reads −∞. Expect the diff-product estimator to
  sit ~3 dB below a per-symbol MER at low SNR (noise doubles in the
  product).
- **Referee labels can lie.** On one asymmetric specimen our dial and
  nrsc5 ranked the sidebands oppositely. A surgical A/B — null one
  sideband in software, feed both versions to nrsc5 — showed stock
  nrsc5's lower/upper MER labels are **spectrally inverted** with
  respect to its cu8 baseband input (kill negative frequencies and
  its "upper" craters). Our dial had the physics right: that station's
  upper RF sideband is buried under the first-adjacent neighbor's
  analog splatter — strong in power, ruinous in MER, precisely what a
  sideband-combining loop needs to know.

The lab scripts live in [`lab/`](lab/): `hd_ref_lock.py` (the
search + lock + per-ref MER dial + H(f,t) extraction),
`hd_grid_diag.py` (PSD shoulder/edge diagnostics), and
`hd_synth_test.py` (the synthetic transmitter that keeps the math
honest).

## Status

Campaign opened 2026-07-18; by that evening the fork was building
(MSYS2/MinGW64 on Windows — build the `faad2_external` and
`rtlsdr_external` targets before the main build) and the first two
decoder knobs had passed their A/B:

- `ALBACORE_ROBUST_TRACK=1` — median/MAD-trimmed tracking estimators.
  Stock's mean-based global phase correction feeds back into every
  reference subcarrier's Costas loop, so a few interference-poisoned
  refs collapse the whole receiver; the trimmed estimators don't.
- `ALBACORE_PART_WEIGHT=1` — soft-demod confidence per 19-carrier
  partition instead of per sideband (10× finer).

Both default off; with knobs off the binary's output is byte-identical
to stock. **Ship them as a pair**: alone, each is inert or erratic;
together, on a cliff-edge capture with a one-partition +15 dB jammer
(the `lab/hd_ladder.py` harness), they took stock's 26.9 s of decoded
audio across six trials to 100.3 s — a 3.7× rescue, with clean-signal
behavior unchanged. nrsc5's printed BER is a pre-FEC channel metric
and barely moves with soft-decision changes — judge A/Bs on decoded
audio seconds instead.

Stage-2 addendum: `ALBACORE_ERASE=2` (with the pair) turns partitions
whose confidence floors out into exact soft-zero Viterbi erasures
instead of weak ±1s. Gentle floors are a small free win (one +10%
audio case, never worse in testing); aggressive floors (5+) destroy
decodes — the Viterbi still extracts value from weight-1 bits unless
they're actively misleading. Default off.

**Field-validated the same evening** on a live capture of a station
with real one-sided sideband damage (`lab/hd_field_survey.py`): during
a natural fade the pair decoded 9 s of audio where stock produced
zero, and on a 90 s specimen nudged to the cliff with mild AWGN the
pair was worth ~2 dB of margin — at +2 dB added noise, stock decodes
nothing and the pair delivers 49 s of music. Replay A/B on identical
captures is the only honest field protocol; live conditions swing
minute to minute.

## Building

Unchanged from upstream for now — see
[doc/UPSTREAM_README.md](doc/UPSTREAM_README.md).
