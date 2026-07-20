#!/usr/bin/env python3
"""albacore night lab: LIVE paired A/B of interferer excision (7/20).

THE QUESTION: hd_excise.py resurrected a cliff specimen in-vitro
(sync=False -> full decode) by notching persistent sideband lines
before acquisition. Does the lever win on live RF, and does it ever
hurt a station that didn't need it?

THE DESIGN (paired, no time confound): each cycle captures ONE fresh
20 s specimen per station, then decodes the SAME bytes twice offline
with the stock referee — arm S untouched, arm E through detect+excise.
Same RF in both arms; any delta is the lever. The radio is held only
for the 20 s capture (lock 'night_ab' at lab priority 50, released
before decoding), so a human click (80) wins within one station.

Verdicts per pair:
  NO-LINES      nothing to excise (arms identical by construction;
                the E decode still runs as a null-check every 5th
                round to prove the plumbing itself is a no-op)
  RESURRECTION  stock failed sync, excised synced
  WIN / LOSS    excised audio beats / trails stock by > 1.0 s
  TIE           within 1.0 s
Anti-regression gate for promotion (morning read): zero LOSSes on
no-line stations, and net WIN+RESURRECTION on lined ones.

Night discipline: NO service heals (a Meteor pass or wedged service
just idles the lab — never Restart-Service under a live pass), yields
to wxTuna pass windows via the day lab's guard, stop file ends it.

Usage:
  hd_night_ab.py --until 2026-07-20T13:30:00Z
Stop early:  create Z:\\SDR_Agent_v2\\night_ab.stop
"""
import argparse, csv, os, time
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, r"Z:\src\albacore\lab")
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_field_survey as fs
import hd_day_lab2 as lab2            # sdr_held_by_other, DeviceYield
from hd_excise import detect_lines, excise

LOG = Path(r"Z:\SDR_Agent_v2\night_ab_log.txt")
CSV = Path(r"Z:\src\albacore\lab\out\night_ab.csv")
SUMMARY = Path(r"Z:\src\albacore\lab\out\night_ab_summary.md")
STOP = Path(r"Z:\SDR_Agent_v2\night_ab.stop")
TUNE = Path(r"Z:\src\gr-radiotuna\lab\radio_tune_table.json")
KEEP_DIR = Path(r"Z:\src\gr-radiotuna\lab\hd_cliff")
MAX_KEEP = 4                          # bound disk: keepers are ~240 MB
COLS = ["utc", "round", "mhz", "name", "ant", "ifgr", "rfgain",
        "cap_s", "lines", "lines_hz",
        "s_sync", "s_mer_lo", "s_ber", "s_audio_s",
        "e_sync", "e_mer_lo", "e_ber", "e_audio_s",
        "excise_xrt", "verdict", "kept"]


def log(m):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%SZ}  nAB {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def stations():
    """HD muxes with per-station antenna/gains from the fitted tune
    table (falls back to the discone defaults for unfitted rows)."""
    import json
    tt = json.loads(TUNE.read_text()).get("stations", {})
    st = json.loads(
        (Path(r"Z:\src\gr-radiotuna\lab\stations.json")).read_text())
    rows = []
    for s in st["stations"]:
        if not (s.get("hd") or s.get("programs")):
            continue
        e = tt.get(f"{s['mhz']:.1f}", {})
        ev = e.get("hd_evidence") or {}
        rows.append({"mhz": s["mhz"], "name": s.get("name") or "?",
                     "ant": e.get("hd_ant") or "Antenna C",
                     "ifgr": int(ev.get("ifgr") or 40),
                     "rfgain": str(ev.get("rfgain") or "5")})
    return rows


def cs16_load(path):
    raw = np.fromfile(path, dtype=np.int16)
    return raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)


def cs16_save(x, path):
    out = np.empty(2 * len(x), np.int16)
    out[0::2] = np.clip(x.real, -32767, 32767).astype(np.int16)
    out[1::2] = np.clip(x.imag, -32767, 32767).astype(np.int16)
    out.tofile(path)


def one_pair(s, rnd, null_check):
    """Capture once (radio held ~20 s), decode both arms offline."""
    import radio_lock
    held = lab2.sdr_held_by_other()
    if held:
        log(f"  {s['mhz']:5.1f} yielded ({held})")
        return
    if not radio_lock.acquire("night_ab", f"excision A/B {s['mhz']:.1f}",
                              50, wait_s=10):
        h = radio_lock.status() or {}
        log(f"  {s['mhz']:5.1f} radio reserved by {h.get('owner','?')} "
            f"- skipping")
        return
    try:
        out, secs, wall = lab2.cap_env(s["mhz"], s["ant"], s["ifgr"],
                                       s["rfgain"], 20, "nightab")
    except Exception as e:
        radio_lock.release("night_ab")
        if lab2.is_busy_error(e):
            log(f"  {s['mhz']:5.1f} device busy - skipping (no heals "
                f"at night)")
            return
        raise
    radio_lock.release("night_ab")     # decode phase needs no radio
    meta = Path(str(out) + ".json")
    try:
        if wall > secs * 1.3 or secs < 17:
            log(f"  {s['mhz']:5.1f} starved capture "
                f"({secs:.1f}s in {wall:.1f}s) - discarded")
            return
        sres = fs.nrsc5_replay(out, secs)
        x = cs16_load(out)
        t0 = time.time()
        lines = detect_lines(x, fs.FS_CAP, max_lines=3, thresh_db=15.0)
        if not lines:
            # ambient-line survey: strongest in-band line even below
            # the 15 dB excise gate, so a no-action night still maps
            # the ambient distribution (is the gate right, or is the
            # night just clean?)
            peak = detect_lines(x, fs.FS_CAP, max_lines=1, thresh_db=0.1)
            if peak:
                log(f"    ambient peak {peak[0][0]:+8.0f} Hz "
                    f"{peak[0][1]:4.1f} dB (below gate)")
        verdict, eres, xrt, kept = "NO-LINES", None, float("nan"), ""
        if lines or null_check:
            for f_line, _snr in lines:
                x = excise(x, fs.FS_CAP, f_line)
            xrt = secs / max(time.time() - t0, 1e-9)
            tmp = fs.TMP / "night_excised.cs16"
            cs16_save(x, tmp)
            eres = fs.nrsc5_replay(tmp, secs)
            tmp.unlink(missing_ok=True)
            if not lines:
                verdict = ("NULL-OK" if abs(eres["audio_s"]
                           - sres["audio_s"]) <= 1.0 else "NULL-DRIFT")
            elif not sres["sync"] and eres["sync"]:
                verdict = "RESURRECTION"
            elif eres["audio_s"] > sres["audio_s"] + 1.0:
                verdict = "WIN"
            elif eres["audio_s"] < sres["audio_s"] - 1.0:
                verdict = "LOSS"
            else:
                verdict = "TIE"
        if verdict in ("RESURRECTION", "WIN", "LOSS", "NULL-DRIFT"):
            keepers = sorted(KEEP_DIR.glob("nightab_keep_*.cs16"))
            if len(keepers) < MAX_KEEP:
                dest = KEEP_DIR / (f"nightab_keep_{s['mhz']:.1f}_"
                                   f"r{rnd}_{verdict}.cs16")
                Path(out).replace(dest)
                if meta.exists():
                    meta.replace(Path(str(dest) + ".json"))
                    meta = Path(str(dest) + ".json")
                out = dest
                kept = dest.name
        e = eres or {"sync": "", "mer_lo": "", "ber": "", "audio_s": ""}
        row = {"utc": datetime.now(timezone.utc).isoformat(),
               "round": rnd, "mhz": s["mhz"], "name": s["name"],
               "ant": lab2.ANT_NICK.get(s["ant"], s["ant"]),
               "ifgr": s["ifgr"], "rfgain": s["rfgain"],
               "cap_s": round(secs, 1), "lines": len(lines),
               "lines_hz": ";".join(f"{f:+.0f}" for f, _ in lines),
               "s_sync": int(sres["sync"]), "s_mer_lo": sres["mer_lo"],
               "s_ber": sres["ber"], "s_audio_s": round(sres["audio_s"], 1),
               "e_sync": (int(e["sync"]) if e["sync"] != "" else ""),
               "e_mer_lo": e["mer_lo"], "e_ber": e["ber"],
               "e_audio_s": (round(e["audio_s"], 1)
                             if e["audio_s"] != "" else ""),
               "excise_xrt": (round(xrt, 1) if xrt == xrt else ""),
               "verdict": verdict, "kept": kept}
        if not CSV.exists():
            with open(CSV, "w", newline="") as f:
                csv.DictWriter(f, COLS).writeheader()
        with open(CSV, "a", newline="") as f:
            csv.DictWriter(f, COLS).writerow(row)
        log(f"  {s['mhz']:5.1f} {s['name'][:8]:8s} lines={len(lines)} "
            f"stock({int(sres['sync'])},{sres['audio_s']:.1f}s) "
            + (f"excis({int(e['sync'])},{e['audio_s']:.1f}s) "
               if eres else "") + f"-> {verdict}"
            + (f" [{kept}]" if kept else ""))
    finally:
        if "nightab_keep" not in str(out):
            Path(out).unlink(missing_ok=True)
            meta.unlink(missing_ok=True)


def summarize():
    import collections
    if not CSV.exists():
        return
    rows = list(csv.DictReader(open(CSV)))
    c = collections.Counter(r["verdict"] for r in rows)
    by = collections.defaultdict(collections.Counter)
    for r in rows:
        by[r["mhz"]][r["verdict"]] += 1
    md = ["# Night excision A/B — " +
          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
          "", f"pairs: {len(rows)}   " +
          "  ".join(f"{k}:{v}" for k, v in sorted(c.items())), "",
          "| MHz | verdicts |", "|---|---|"]
    for mhz in sorted(by, key=float):
        md.append(f"| {mhz} | " + " ".join(
            f"{k}:{v}" for k, v in sorted(by[mhz].items())) + " |")
    md += ["", "Promotion gate: 0 LOSS + 0 NULL-DRIFT, net "
           "WIN/RESURRECTION on lined stations."]
    SUMMARY.write_text("\n".join(md), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", required=True)
    ap.add_argument("--gap-s", type=float, default=25,
                    help="pause between stations (service-storm law)")
    ap.add_argument("--round-gap-s", type=float, default=600)
    a = ap.parse_args()
    until = datetime.fromisoformat(a.until.replace("Z", "+00:00"))
    ss = stations()
    log(f"NIGHT EXCISION A/B: {len(ss)} HD muxes, until "
        f"{until:%H:%MZ}, stop file {STOP}")
    rnd = 0
    while datetime.now(timezone.utc) < until and not STOP.exists():
        rnd += 1
        order = ss[rnd % len(ss):] + ss[:rnd % len(ss)]   # rotate start
        log(f"round {rnd}")
        for s in order:
            if datetime.now(timezone.utc) >= until or STOP.exists():
                break
            try:
                one_pair(s, rnd, null_check=(rnd % 5 == 0))
            except Exception as e:
                log(f"  {s['mhz']:5.1f} ERROR {str(e)[:90]}")
            time.sleep(a.gap_s)
        summarize()
        t_gap = time.time()
        while (time.time() - t_gap < a.round_gap_s
               and not STOP.exists()):
            time.sleep(5)          # stop file honored during round gap
    summarize()
    log("night A/B done - summary at " + str(SUMMARY))


if __name__ == "__main__":
    main()
