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

## Accuracy pass (2026-07-19, multi-seed replication + re-analysis)

Every headline claim re-tested across seeds/configs; interpretations corrected
where the data demanded it.

**Replicated (multi-seed):**
- **Pair fading margin** (18 slow-fade trials: 2 delays × 3 seeds × 3 rungs at
  0.8 Hz doppler): stock partial at +3 dB (9–17 s), dead from +5 dB; the pair
  scored a perfect 27/27 real seconds in **all 18 trials through +7 dB**.
  ≥4 dB slow-fading margin: CONFIRMED.
- **MMSE AWGN-cliff win** (5 seeds at the +9 dB cliff rung): pair median 1
  real second vs MMSE median 8 — MMSE won in 5/5 seeds. MMSE+CSI is slightly
  below MMSE alone (CSI remains ≤neutral). CONFIRMED, magnitude ~sub-dB of
  effective margin concentrated exactly at the cliff.
- **MMSE fading regression** (3 seeds, +9 dB): pair 26/26/26 vs MMSE 4/7/11.
  CONFIRMED — the stale-λ mechanism is real; keep MMSE off on dynamic channels.

**New boundary discovered:** at **5 Hz doppler** (≈ driving speed at FM
frequencies) the two-ray channel defeats EVERY configuration — stock and all
knobs, 0/0 at every noise level, both delays, all seeds. Slow fading is the
pair's win; **fast fading is beyond the current receiver architecture
entirely** — which is precisely the regime the reference receiver's 11-tap
time-domain channel filter (lever #2) and true CSI exist for. The mobile-fade
frontier is now measured, not hypothesized.

**Interpretations corrected:**
- **91.9 re-diagnosis, twice**: the afternoon capture decodes 43/43 s of real
  audio through stock at BER 0.042 (91.9 was always decodable with content);
  the evening all-silence decodes prompted a source-dead-air hypothesis that
  silence-structure analysis then REFUTED (bursty silence runs + fragments of
  quiet real content = heavy degradation over quiet programming). The field
  rescue stands as a frame-decode rescue (~2 dB); audible-content value for
  that specific window is unproven.
- **Median bias correction (US9106472): not applicable to current code** —
  our median lives only in the global tracking fit, not in channel-amplitude
  estimates. It becomes mandatory only if channel estimates are ever
  median-filtered.
- **"Frequency-only estimation" framing softened**: nrsc5 block-averages ref
  amplitudes over 32 symbols and IIR-tracks phases — crude time filtering
  exists; the true gap is the sliding data-stripped 11-tap FIR + frequency
  smoothing cascade.

**New instrument:** the lab MER dial is now calibrated against the decoder:
`nrsc5_MER ≈ 0.60 × dial + 3.3` (rms residual 1.0 dB over six sideband pairs
spanning −1.3…17.9 dB; validated out-of-sample on cliff2 within 0.7 dB). The
IQ-only dial predicts decoder MER without decoding — the Knob-of-Time sensor.

## Open questions carried forward
- How much of the ~10 dB CSI gain survives at NRSC-5 cliff SNR (802.11a figure
  is at BER 1e-5)? Answer: replay A/B on the hd_cliff corpus.
- Are our weak stations splatter- or noise-limited? Fingerprint before FAC.
- Coherent-vs-differential ref detection and impulse blanking: no surviving
  claims; revisit in later iBiquity filings / DAB literature.

*Caveat: patents document designed architectures, not measured NRSC-5 field
gains; the one quantified number (~10 dB) is an 802.11a simulation. Every lever
ships env-gated behind the replay A/B + LISTEN% harness, per project law.*

## Night-shift autopsy (2026-07-19, fd=5 Hz, zero added noise)
Stock: 20 sync-loss events (thrash). ALBACORE=1 pair: **sync held
continuously — zero losses — but MER −7…−13 dB throughout.** The 5 Hz wall
is therefore two stacked failures: stock dies at the SYNC layer (already
solved by robust tracking), and the remaining failure is pure
channel-estimation lag with lock held. Costas-gain speedup and
decision-directed re-estimation target exactly this layer.

## THE WALL FALLS (night shift, 2026-07-19 ~04:30)
`ALBACORE_COSTAS_BW=<mult>` scales the per-ref Costas loop bandwidth.
Measured on two-ray a=0.9 fading (real listenable seconds of ~27):
fd=5 Hz: pair 0 -> x2/x4/x8 ALL 27/27. fd=8 Hz: x8 = 27/27. The
estimation-lag hypothesis from the autopsy is CONFIRMED and SOLVED for
fixed known doppler. The cost is equally textbook: at +8 dB AWGN (static
channel) x4 collapses 27->0 - wide loops track noise. Endgame: adaptive
bandwidth from per-block phase-innovation variance (next build). With it,
the receiver would hold from parked-car to highway speeds automatically.
