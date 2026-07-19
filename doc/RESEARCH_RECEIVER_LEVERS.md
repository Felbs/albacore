# Receiver levers for albacore — deep-research synthesis (2026-07-19)

Multi-agent research sweep: 5 angles, 21 sources fetched, 102 claims extracted,
25 adversarially verified (3-vote): **23 confirmed, 2 refuted**. Primary sources
are iBiquity/Xperi patents (the system designers' own receiver playbook), the
Hoeher/Kaiser/Robertson 2D channel-estimation literature, the Dream DRM receiver
docs, and the 802.11a CSI-weighting literature.

## Ranked implementation plan

### 1. CSI-weighted Viterbi branch metrics — THE lever (high gain, low difficulty)
- iBiquity's designed-in architecture (US7724850; US10355908: LLR = soft metric ×
  CSI weight): every demodulated subcarrier weighted by `CSIweight = a*/σ²`
  (conjugate channel gain over noise variance), **interpolated across the 18 data
  subcarriers between each flanking reference pair** — per-SUBCARRIER, not
  per-partition (our current knob) or per-sideband (stock).
- Independent literature (Akay & Ayanoglu, 802.11a): **~10 dB BER/PER gain** on
  frequency-selective channels; a simplified **|H|-only weight quantized to 3
  bits** achieves nearly the same gain without a noise estimate.
- REFUTED (0-3): "CSI weighting is unconditionally safe on flat channels."
  → ship gated + replay-A/B like every knob.

### 2. 2×1D channel estimation upgrade (moderate difficulty, feeds #1)
- iBiquity reference receiver: strip known DBPSK data from each ref subcarrier,
  **11-tap FIR smoothing in time, then a cascade of four 3-tap FIRs across
  frequency** (US7724850) — far more filtering than nrsc5's flanking-ref linear
  interpolation.
- Hoeher/Kaiser/Robertson: 2D beats 1D in MSE and overhead; **two cascaded 1-D
  filters are "virtually as good as true 2-D"**.
- Dream DRM data point: simple linear pilot interpolation costs **~7 dB** on
  carriers not coinciding with pilots at low SNR; Wiener interpolation wins
  except on CPU cost.

### 3. Median-filter bias correction (small, do during #1)
- Our median/MAD robust tracking is the canonical industry approach (iBiquity
  uses a 5-tap median in the channel-estimate path for step-preservation), BUT:
  the median introduces **gain ≈ 0.76 (noise variance underestimated by 1.2 dB)**
  and US9106472 explicitly claims a bias-correction function before estimates
  feed CSI weights. Apply the correction when wiring #1.

### 4. Sideband quality estimator + soft-metric combining (already mostly held)
- MRC diversity (antenna) combines at the **Viterbi branch-metric level**
  (WO2013070486) — validating that all combining lives in the soft-metric domain,
  which NRSC-5's complementary punctured pair code already implements across
  sidebands. Useful import: **US7221917's sideband quality estimator** (square,
  ~1 s LPF, power ratio) as a cheap continuous sideband-health dial.
- REFUTED (1-2): characterizing US7221917 as pure "sideband abandonment" —
  its detector is sound; its hard 30 dB threshold + LO-shift remedy are coarse.
  Use the detector, not the remedy.

### 5. First-adjacent analog cancellation (highest difficulty, conditional payoff)
- US9178548 (FAC): notch the FM interferer + parametric filter + **blend gated by
  measured interference ratio** (`c = clamp(5·ratio − 0.75)`), explicitly so FAC
  never hurts interference-free reception ("at some point FAC processing does
  more harm than good").
- US6259893: a software-implementable constant-envelope canceller core —
  normalize, conjugate-multiply, LPF, re-multiply (assumes interferer dominates
  by ~6 dB; multipath caveats).
- **Prerequisite: per-station splatter fingerprint** — measure whether our weak
  stations are interference-limited (FAC pays) or noise-limited (FAC is inert or
  harmful). DC-area evidence so far leans noise-limited.

## Audio-quality measurement (the apparatus)
- **Zero published claims survived** on PEAQ/ViSQOL/POLQA applicability to HDC
  artifacts or decoder-metric→perception mapping. The field is open.
- Recommended design = what we already started: decoder-side proxies (frame CRC
  rate, BER, MER, erasure rate) + blind output-audio scoring (LISTEN%: silence /
  splice / bandwidth-collapse gates) + **local calibration against listening
  spot-checks** — the TV-side law (output-layer measurement beats chain
  counters), now confirmed twice in one night (concealment-silence discovery;
  playback-layer static).

## Lever #1 implemented and measured (2026-07-19, same night)

`ALBACORE_CSI=1` — per-subcarrier |G|²/partition-mean weighting on the soft
bits, using the interpolated channel gain the equalizer already computes
(clamped [1/8, 4]). Byte-identical with the gate off. **Measured verdict on
the full synthetic battery (AWGN ladder, partition jam, 4-bin within-partition
jam, two-ray sweeping-notch fading): SAFE but NEUTRAL — ties the knob pair
everywhere, no separation even at the fading cliff.** The FEC+interleaver
absorbs narrow damage regardless of weighting, and the robust-tracking pair
already extracts the channel's available margin. The literature's ~10 dB
figure evidently lives on faster/deeper mobile fading than this battery (or
this market) produces. Keep the knob; revisit with real mobile-fade captures.

**The night's actual headline came from the same experiment**: on the two-ray
fading channel (0.9 echo, 0.8 Hz doppler), stock nrsc5 collapses at +3 dB
added noise (9/27 real seconds, then 0) while **the ALBACORE=1 pair holds a
PERFECT 27/27 real listenable seconds through +8 dB — ≥5 dB of margin on
fading channels**, the pair's third proven channel class (AWGN-neutral,
jam-rescue, fading-rescue). Mechanism consistent throughout: notch-swept
reference subcarriers derail the mean-based tracking feedback; median/MAD
estimators don't care.

## Open questions carried forward
- How much of the ~10 dB CSI gain survives at NRSC-5 cliff SNR (802.11a figure
  is at BER 1e-5)? Answer: replay A/B on the hd_cliff corpus.
- Are our weak stations splatter- or noise-limited? Fingerprint before FAC.
- Coherent-vs-differential ref detection and impulse blanking: no surviving
  claims; revisit in later iBiquity filings / DAB literature.

*Caveat: patents document designed architectures, not measured NRSC-5 field
gains; the one quantified number (~10 dB) is an 802.11a simulation. Every lever
ships env-gated behind the replay A/B + LISTEN% harness, per project law.*
