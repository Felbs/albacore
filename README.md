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

## Status

Campaign opened 2026-07-18, the same day the family's radiosonde
decoder got its first CRC-verified balloon. Cliff-edge IQ corpus
(the training data: stations at 7–10 dB MER, where every improvement
is audible) is being collected by the observatory now. Code changes
land here as each stage passes its A/B.

## Building

Unchanged from upstream for now — see
[doc/UPSTREAM_README.md](doc/UPSTREAM_README.md).
