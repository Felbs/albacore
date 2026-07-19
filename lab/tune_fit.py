#!/usr/bin/env python3
"""Q3: fit the perfect-tune table from the day-lab antenna cube.

For every station, pick per-mode winners from clean base rows:
  hd:  antenna with the best mean referee audio-seconds (tiebreak: sync
       rate, then MER); gains = the cal that produced the winning rows.
  fm:  antenna with the best mean pilot SNR (analog quality dial).

Writes out/radio_tune_table.json — consumed by the ALBACORE TUNA RADIO
panel to auto-pick the antenna per station. Report to stdout.
"""
import csv, json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(r"Z:\src\albacore\lab\out")
CUBE = OUT / "antenna_cube.csv"
TABLE = Path(r"Z:\src\gr-radiotuna\lab\radio_tune_table.json")
ANT_PORT = {"rabbit": "Antenna A", "oldfaithful": "Antenna B",
            "discone": "Antenna C"}


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def main():
    rows = [r for r in csv.DictReader(open(CUBE))
            if int(r["slot"]) >= 3 and "starved" not in r["tag"]
            and r["tag"] == "base"]
    cube = defaultdict(lambda: defaultdict(list))
    for r in rows:
        cube[r["mhz"]][r["ant"]].append(r)
    table = {"fitted_utc": datetime.now(timezone.utc).isoformat(),
             "n_rows": len(rows), "stations": {}}
    for mhz, ants in sorted(cube.items(), key=lambda kv: float(kv[0])):
        cand = []
        for ant, rs in ants.items():
            aud = sum(fnum(r["audio_s"]) or 0 for r in rs) / len(rs)
            syn = sum(int(r["sync"]) for r in rs) / len(rs)
            mer = [m for r in rs if (m := fnum(r["mer_lo"])) is not None]
            pil = [p for r in rs
                   if (p := fnum(r["pilot_snr_db"])) is not None]
            cand.append({"ant": ant, "n": len(rs), "aud": round(aud, 1),
                         "sync": round(syn, 2),
                         "mer": round(sum(mer) / len(mer), 1) if mer else None,
                         "pilot": round(sum(pil) / len(pil), 1) if pil else None,
                         "ifgr": fnum(rs[-1]["ifgr"]),
                         "rfgain": rs[-1]["rfgain"]})
        hd = max(cand, key=lambda c: (c["aud"], c["sync"], c["mer"] or -99))
        fm = max(cand, key=lambda c: c["pilot"] or -99)
        table["stations"][mhz] = {
            "hd_ant": ANT_PORT[hd["ant"]] if hd["aud"] > 0 else None,
            "hd_evidence": hd,
            "fm_ant": ANT_PORT[fm["ant"]] if (fm["pilot"] or 0) > 6 else None,
            "fm_evidence": fm,
            "candidates": cand}
        hd_s = f"{hd['ant']}({hd['aud']}s)" if hd["aud"] > 0 else "none-yet"
        fm_s = (f"{fm['ant']}({fm['pilot']}dB)"
                if (fm["pilot"] or 0) > 6 else "none-yet")
        print(f" {mhz:>5s} MHz  HD -> {hd_s:22s} FM -> {fm_s}")
    TABLE.write_text(json.dumps(table, indent=1))
    print(f"\ntable -> {TABLE} ({len(rows)} clean base rows)")


if __name__ == "__main__":
    main()
