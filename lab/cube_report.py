#!/usr/bin/env python3
"""Compact report over the 3-antenna day-lab cube (antenna_cube.csv).

Sections: per-station antenna league (HD audio-s + analog pilot SNR),
gain-sweep response points, RFI timeline, knob A/B ledger + regression
flags, and data-health counts. Designed for the 25-min check-in loop.
"""
import csv
from collections import defaultdict
from pathlib import Path

OUT = Path(r"Z:\src\albacore\lab\out")
CUBE = OUT / "antenna_cube.csv"
AB = OUT / "day_lab_ab.csv"


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def main():
    if not CUBE.exists():
        print("no cube data yet")
        return
    raw = list(csv.DictReader(open(CUBE)))
    # hygiene: slots 0-2 = the warden-contention era; +starved = the
    # radio was contended/taken (e.g. a Meteor pass) mid-capture
    rows = [r for r in raw if int(r["slot"]) >= 3
            and "starved" not in r["tag"]]
    base = [r for r in rows if r["tag"] == "base"]
    print(f"cube rows: {len(rows)} clean of {len(raw)} "
          f"({len(raw)-len(rows)} excluded: contention-era/starved) | "
          f"{len(base)} base, {len(rows)-len(base)} sweep | slots: "
          f"{len({r['slot'] for r in rows})}")

    # per-station antenna league
    league = defaultdict(lambda: defaultdict(list))
    for r in base:
        league[r["mhz"]][r["ant"]].append(r)
    print("\n== antenna league (mean HD audio-s | sync% | MER | pilot dB "
          "| inch dB) ==")
    for mhz in sorted(league, key=float):
        print(f" {mhz} MHz")
        for ant in ("rabbit", "oldfaithful", "discone"):
            rs = league[mhz].get(ant, [])
            if not rs:
                continue
            aud = [fnum(r["audio_s"]) or 0 for r in rs]
            syn = [int(r["sync"]) for r in rs]
            mer = [m for r in rs if (m := fnum(r["mer_lo"])) is not None]
            pil = [p for r in rs if (p := fnum(r["pilot_snr_db"])) is not None]
            inch = [i for r in rs if (i := fnum(r["inch_db"])) is not None]
            print(f"   {ant:11s} n={len(rs):2d}  aud {sum(aud)/len(aud):5.1f}s"
                  f"  sync {100*sum(syn)/len(syn):3.0f}%"
                  f"  MER {sum(mer)/len(mer):5.1f}" if mer else
                  f"   {ant:11s} n={len(rs):2d}  aud {sum(aud)/len(aud):5.1f}s"
                  f"  sync {100*sum(syn)/len(syn):3.0f}%  MER   n/a",
                  end="")
            print(f"  pilot {sum(pil)/len(pil):5.1f}" if pil else
                  "  pilot   n/a", end="")
            print(f"  inch {sum(inch)/len(inch):5.1f}" if inch else "")

    # sweep points
    sweeps = [r for r in rows if r["tag"].startswith("sweep")]
    if sweeps:
        print("\n== gain-sweep points (mhz ant ifgr -> aud s / pilot dB) ==")
        for r in sweeps:
            print(f"   {r['mhz']:>5s} {r['ant']:11s} ifgr {r['ifgr']:>4s} "
                  f"{r['tag']:7s} aud {r['audio_s']:>5s}s "
                  f"pilot {r['pilot_snr_db']:>5s}")

    # RFI timeline (one per slot)
    seen = {}
    for r in rows:
        seen[r["slot"]] = (r["rfi_floor_db"], r["rfi_margin_db"],
                           r["utc"][11:16])
    print("\n== RFI timeline (slot: floor / margin) ==")
    print("   " + "  ".join(f"s{k}@{v[2]}Z {v[0]}/{v[1]}"
                            for k, v in sorted(seen.items(), key=lambda kv:
                                               int(kv[0]))))

    # knob A/B ledger
    if AB.exists():
        ab = list(csv.DictReader(open(AB)))
        ab = [r for r in ab if r.get("file", "").startswith("slot")]
        if ab:
            print("\n== knob A/B (stock vs ALBACORE=1 vs auto) ==")
            wins = loss = 0
            for r in ab:
                s, p = int(r["stock_real"]), int(r["pair_real"])
                mark = ""
                if p > s:
                    wins += 1
                elif p < s:
                    loss += 1
                    mark = "  << REGRESSION"
                print(f"   {r['file']:7s} {r['mhz']:>5s} {r['name']:20s} "
                      f"stock {s:2d}s pair {p:2d}s auto "
                      f"{r['auto_real']:>2s}s{mark}")
            print(f"   ledger: {wins}W {loss}L "
                  f"{len(ab)-wins-loss}T for ALBACORE=1")


if __name__ == "__main__":
    main()
