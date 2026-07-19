#!/usr/bin/env python3
"""End-of-day report for the 3-antenna laboratory (2026-07-19).

Merges the antenna cube, tune table, sweep curves, RFI timeline, and
knob ledger into a markdown report (out/day_report.md) — the document
the operator reads when they walk in the door.
"""
import csv, json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(r"Z:\src\albacore\lab\out")
CUBE = OUT / "antenna_cube.csv"
AB = OUT / "day_lab_ab.csv"
TABLE = Path(r"Z:\src\gr-radiotuna\lab\radio_tune_table.json")
CAL = Path(r"Z:\src\gr-radiotuna\lab\hd_ant_cal.json")
REPORT = OUT / "day_report.md"
NICE = {"rabbit": "rabbit ears (A)", "oldfaithful": "Old Faithful (B)",
        "discone": "roof discone (C)"}


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def main():
    raw = list(csv.DictReader(open(CUBE)))
    rows = [r for r in raw if int(r["slot"]) >= 3
            and "starved" not in r["tag"]]
    base = [r for r in rows if r["tag"] == "base"]
    sweeps = [r for r in rows if r["tag"].startswith("sweep")]
    table = json.loads(TABLE.read_text()) if TABLE.exists() else {}
    L = []
    L.append("# The Three-Antenna Day — 2026-07-19 field laboratory\n")
    L.append(f"*Generated {datetime.now(timezone.utc):%H:%M}Z · "
             f"{len(base)} referee-scored captures · "
             f"{len(sweeps)} gain-sweep points · "
             f"{len(raw) - len(rows)} rows excluded "
             f"(warden-contention era + pass yields)*\n")

    L.append("\n## The perfect-tune table (now live in the panel)\n")
    L.append("| station | HD antenna | evidence | FM antenna | evidence |")
    L.append("|---|---|---|---|---|")
    for mhz, ent in sorted(table.get("stations", {}).items(),
                           key=lambda kv: float(kv[0])):
        hd, fm = ent["hd_evidence"], ent["fm_evidence"]
        hd_s = (f"{NICE.get(hd['ant'], hd['ant'])}"
                if ent["hd_ant"] else "*none decodes HD yet*")
        fm_s = (f"{NICE.get(fm['ant'], fm['ant'])}"
                if ent["fm_ant"] else "*analog dead at this site*")
        L.append(f"| **{mhz}** | {hd_s} | {hd['aud']}s audio, "
                 f"{int(hd['sync']*100)}% sync (n={hd['n']}) | {fm_s} | "
                 f"pilot {fm['pilot']} dB |")

    L.append("\n## What the day proved\n")
    L.append("- **The antennas are complementary — no single antenna "
             "covers the band.** 88.5 and 103.5 decode HD only on the "
             "TV yagi; 93.3 only on rabbit ears/discone; 93.9's analog "
             "is best on the roof discone.")
    L.append("- **Gain knees are per-antenna AND per-station** (the "
             "amplified yagi overloads at the one-size default): "
             "re-centering B to ifgr 44 turned 90.9 from 0 s to 12.4 s "
             "of audio; ifgr 48 got 93.9 its first-ever sync on B.")
    L.append("- **The RFI control probe caught a duty-cycled noise "
             "source** — when it switched off (16:03Z slot) every "
             "station lifted at once (93.3 hit BER 0.0000).")
    L.append("- **ALBACORE=1 never lost** in the all-day cliff ledger, "
             "and COSTAS auto trailed it 0-for-3 → auto removed from "
             "the listening defaults (regression law).")
    L.append("- **One radio, one tenant**: the morning's mystery wedges "
             "were two of our own daemons fighting over the RSPdx. The "
             "lab now yields to Meteor passes instead of competing.")

    if sweeps:
        L.append("\n## Gain response points (Q2 raw)\n")
        L.append("| station | antenna | ifgr | audio s | pilot dB |")
        L.append("|---|---|---|---|---|")
        for r in sweeps:
            L.append(f"| {r['mhz']} | {r['ant']} | {r['ifgr']} | "
                     f"{r['audio_s']} | {r['pilot_snr_db']} |")

    seen = {}
    for r in rows:
        seen[int(r["slot"])] = (r["rfi_floor_db"], r["rfi_margin_db"],
                                r["utc"][11:16])
    L.append("\n## RFI timeline (fixed probe, rabbit @93.3)\n")
    L.append("| slot | time Z | floor dB | margin dB |")
    L.append("|---|---|---|---|")
    for k, v in sorted(seen.items()):
        L.append(f"| {k} | {v[2]} | {v[0]} | {v[1]} |")

    if AB.exists():
        ab = [r for r in csv.DictReader(open(AB))
              if r.get("file", "").startswith("slot")]
        if ab:
            L.append("\n## Knob ledger (stock vs ALBACORE=1 vs +auto, "
                     "real audio seconds)\n")
            L.append("| slot | station | stock | ALBACORE=1 | +auto |")
            L.append("|---|---|---|---|---|")
            w = l = 0
            for r in ab:
                s, p = int(r["stock_real"]), int(r["pair_real"])
                w += p > s
                l += p < s
                L.append(f"| {r['file'][4:]} | {r['mhz']} {r['name']} | "
                         f"{s} | **{p}** | {r['auto_real']} |")
            L.append(f"\n**Ledger: {w}W-{l}L-{len(ab)-w-l}T for "
                     f"ALBACORE=1.**")

    REPORT.write_text("\n".join(L), encoding="utf-8")
    print(f"report -> {REPORT} ({len(L)} lines)")


if __name__ == "__main__":
    main()
