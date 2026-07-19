#!/usr/bin/env python3
"""albacore lab v2: the THREE-ANTENNA all-day laboratory (2026-07-19).

The rig has three antennas on the RSPdx, software-selectable:
    Antenna A = rabbit ears (ANT111E)
    Antenna B = "Old Faithful" TV antenna
    Antenna C = discone on the roof

Research questions (design notes in gr-radiotuna docs/SCIENCE.md):
  Q1  Which antenna wins per station, and does the winner drift with
      time of day?           -> antenna x station x time quality cube
  Q2  What is the optimal gain per (antenna, station)?
                             -> rotating mini IFGR sweeps = response curves
  Q3  Perfect-tune algorithm: given a station, choose antenna + gains
      from the cube, beat the fixed-antenna baseline.
  Q4  RFI timeline (fixed probe = the control instrument) + one knob A/B
      (stock vs ALBACORE=1 vs +auto) per slot on the best capture.

Methodology guards:
  - Station order ROTATES each slot so time-of-day never confounds the
    station comparison; antenna order is fixed A->B->C within a station
    (a station takes ~70 s, far faster than propagation drift).
  - Every capture gets BOTH sciences: HD referee decode (stock nrsc5:
    sync/MER/BER/audio-s) and the fm_stereo analog dials (pilot SNR /
    audio SNR) — HD and analog antenna quality from one specimen.
  - Raw RF facts (rms, in-channel-vs-floor dB) are logged per capture so
    RF-side changes separate from decode-side changes.
  - Captures are DELETED after metric extraction (14 s = 166 MB; a kept
    day would be ~50 GB). The metrics ARE the experiment.
  - LISTENER GUARD: hd_listen running -> skip the slot, never steal the
    radio. One service self-heal per slot on capture failure.

Usage:
  hd_day_lab2.py --start-at 2026-07-19T13:30:00Z --until 2026-07-19T21:50:00Z
"""
import argparse, csv, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\albacore\lab")
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_field_survey as fs
import hd_radio
import fm_stereo
import hd_day_lab as v1          # rfi_probe, three_way, heal_service, guard

LOG = Path(r"Z:\SDR_Agent_v2\hd_day_lab_log.txt")
CSV = Path(r"Z:\src\albacore\lab\out\antenna_cube.csv")
CAL = Path(r"Z:\src\gr-radiotuna\lab\hd_ant_cal.json")
ANTS = ["Antenna A", "Antenna B", "Antenna C"]
ANT_NICK = {"Antenna A": "rabbit", "Antenna B": "oldfaithful",
            "Antenna C": "discone"}
COLS = ["utc", "slot", "mhz", "ant", "ifgr", "rfgain", "tag", "rms",
        "inch_db", "sync", "ber", "mer_lo", "mer_hi", "audio_s",
        "pilot_snr_db", "fm_audio_snr_db", "rfi_floor_db", "rfi_margin_db"]


def log(m):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%SZ}  v2 {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def cal_gains(ant, mhz):
    try:
        acal = json.loads(CAL.read_text()).get(ant, {})
        ent = acal.get(f"{mhz:.1f}") or acal.get("_default")
        if ent:
            return float(ent["ifgr"]), str(ent["rfgain"])
    except Exception:
        pass
    return 40.0, "5"


def rf_facts(cs16_path):
    """Cheap RF truth straight off the capture: level + in-channel SNR."""
    raw = np.fromfile(cs16_path, dtype=np.int16, count=2 * 3_000_000)
    x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    rms = float(np.sqrt((np.abs(x) ** 2).mean()))
    N = 1 << 14
    seg = x[: len(x) // N * N].reshape(-1, N)
    psd = np.fft.fftshift(
        (np.abs(np.fft.fft(seg * np.hanning(N), axis=1)) ** 2).mean(0))
    fax = np.fft.fftshift(np.fft.fftfreq(N, 1 / fs.FS_CAP))
    db = 10 * np.log10(psd + 1e-9)
    inch = float(db[np.abs(fax) < 90e3].mean()
                 - np.median(db[(np.abs(fax) > 1.05e6) & (np.abs(fax) < 1.3e6)]))
    return rms, inch


def fm_dials(cs16_path, secs=8.0):
    """Analog truth dials from the same specimen (fm_stereo v2 bench)."""
    dem = fm_stereo.FMStereo()
    tele = {}
    read = 0
    n_want = int(secs * fs.FS_CAP) * 2
    with open(cs16_path, "rb") as f:
        while read < n_want:
            raw = np.fromfile(f, dtype=np.int16, count=2 * 262144)
            if len(raw) < 4:
                break
            read += len(raw)
            _, tele = dem.feed(fm_stereo.decimate2_cs16(raw))
    return (tele.get("pilot_snr_db", float("nan")),
            tele.get("audio_snr_db", float("nan")))


def one_capture(mhz, ant, ifgr, rfgain, tag, slot, rfi):
    os.environ["HD_ANT"] = ant
    os.environ["HD_IFGR"] = str(ifgr)
    os.environ["HD_RFGAIN"] = str(rfgain)
    try:
        out, secs, wall = fs.capture(mhz, 14, f"cube_{ANT_NICK[ant]}")
    finally:
        os.environ.pop("HD_IFGR", None)
        os.environ.pop("HD_RFGAIN", None)
    try:
        rms, inch = rf_facts(out)
        res = fs.nrsc5_replay(out, secs)
        psnr, asnr = fm_dials(out)
        row = {"utc": datetime.now(timezone.utc).isoformat(), "slot": slot,
               "mhz": mhz, "ant": ANT_NICK[ant], "ifgr": ifgr,
               "rfgain": rfgain, "tag": tag, "rms": round(rms, 1),
               "inch_db": round(inch, 1), "sync": int(res["sync"]),
               "ber": res["ber"], "mer_lo": res["mer_lo"],
               "mer_hi": res["mer_hi"], "audio_s": round(res["audio_s"], 1),
               "pilot_snr_db": psnr, "fm_audio_snr_db": asnr,
               "rfi_floor_db": round(rfi[0], 1),
               "rfi_margin_db": round(rfi[1], 1)}
        with open(CSV, "a", newline="") as f:
            csv.DictWriter(f, COLS).writerow(row)
        return out, row
    finally:
        # metrics extracted; the 166 MB specimen has served its purpose
        Path(out).unlink(missing_ok=True)
        Path(str(out) + ".json").unlink(missing_ok=True)


def knob_ab(mhz, ant, slot):
    """Anti-regression instrument: 20 s specimen on the chosen cell,
    3-way decode (stock vs ALBACORE=1 vs +auto Costas) judged by real
    audio seconds. The cert law: small A/Bs miss regressions, so this
    runs EVERY slot, all day, and flags any knob loss loudly."""
    os.environ["HD_ANT"] = ant
    out, secs, wall = fs.capture(mhz, 20, "knobab")
    try:
        res = fs.nrsc5_replay(out, secs)
        if not res["sync"]:
            log(f"  knobA/B {mhz:.1f} {ANT_NICK[ant]}: no sync, skipped")
            return
        raw = np.fromfile(out, dtype=np.int16)
        cu8p = Path(r"Z:\src\albacore\lab\out") / "daylab.cu8"
        hd_radio.cs16_to_cu8(hd_radio.decimate2_cs16(raw)).tofile(cu8p)
        ab = v1.three_way(cu8p)
        if not v1.CSV.exists():
            with open(v1.CSV, "w", newline="") as f:
                csv.writer(f).writerow(["utc", "mhz", "name", "ber",
                                        "stock_real", "pair_real",
                                        "auto_real", "file"])
        with open(v1.CSV, "a", newline="") as f:
            csv.writer(f).writerow(
                [datetime.now(timezone.utc).isoformat(), mhz,
                 f"{res['name'][:8]}@{ANT_NICK[ant]}", res["ber"],
                 ab["stock"], ab["pair"], ab["auto"], f"slot{slot}"])
        log(f"  knobA/B {mhz:.1f} {ANT_NICK[ant]}: stock {ab['stock']}s "
            f"pair {ab['pair']}s auto {ab['auto']}s")
        if ab["pair"] < ab["stock"]:
            log(f"  !! REGRESSION FLAG slot {slot}: ALBACORE=1 "
                f"{ab['pair']}s < stock {ab['stock']}s on {mhz:.1f} "
                f"{ANT_NICK[ant]}")
    finally:
        Path(out).unlink(missing_ok=True)
        Path(str(out) + ".json").unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-at")
    ap.add_argument("--until", required=True)
    ap.add_argument("--slot-min", type=float, default=25)
    ap.add_argument("--mhz", nargs="+", type=float,
                    default=[93.3, 90.9, 91.9, 93.9, 88.5, 103.5])
    a = ap.parse_args()
    until = datetime.fromisoformat(a.until.replace("Z", "+00:00"))
    if a.start_at:
        start = datetime.fromisoformat(a.start_at.replace("Z", "+00:00"))
        log(f"3-antenna day lab armed; sleeping until {start:%H:%M}Z")
        while datetime.now(timezone.utc) < start:
            time.sleep(30)
    log(f"3-ANTENNA DAY LAB RUNNING: {a.mhz} x {[ANT_NICK[x] for x in ANTS]}"
        f" every {a.slot_min}min until {until:%H:%M}Z")
    CSV.parent.mkdir(exist_ok=True)
    if not CSV.exists():
        with open(CSV, "w", newline="") as f:
            csv.DictWriter(f, COLS).writeheader()
    cells = [(m, ant) for m in a.mhz for ant in ANTS]   # sweep round-robin
    slot = 0
    while datetime.now(timezone.utc) < until:
        slot_t0 = time.time()
        if v1.listener_running():
            log("user listening - slot skipped")
        else:
            rfi = v1.rfi_probe()
            log(f"slot {slot}: RFI floor {rfi[0]:.1f} dB, margin {rfi[1]:+.1f} dB")
            healed = False
            order = a.mhz[slot % len(a.mhz):] + a.mhz[:slot % len(a.mhz)]
            # knob-A/B target: the synced cell with the LOWEST MER — the
            # cliff is where rescue knobs matter, strong signals hide them
            cliffiest = (None, None, 1e9)
            for mhz in order:
                for ant in ANTS:
                    ifgr, rfg = cal_gains(ant, mhz)
                    try:
                        out, row = one_capture(mhz, ant, ifgr, rfg,
                                               "base", slot, rfi)
                        log(f"  {mhz:5.1f} {ANT_NICK[ant]:11s} "
                            f"sync={row['sync']} ber={row['ber']:.4f} "
                            f"aud={row['audio_s']:4.1f}s "
                            f"pilot={row['pilot_snr_db']:5.1f}dB")
                        mer = row["mer_lo"]
                        if row["sync"] and mer == mer and mer < cliffiest[2]:
                            cliffiest = (mhz, ant, mer)
                    except Exception as e:
                        if not healed and not v1.listener_running():
                            log(f"  {mhz:5.1f} {ANT_NICK[ant]} wedge - healing")
                            v1.heal_service()
                            healed = True
                        else:
                            log(f"  {mhz:5.1f} {ANT_NICK[ant]} skip "
                                f"({str(e)[:40]})")
            # rotating mini gain sweep: one (station, antenna) cell per slot
            mhz, ant = cells[slot % len(cells)]
            ifgr0, rfg = cal_gains(ant, mhz)
            for d in (-4, +4):
                ifgr = max(20, min(59, ifgr0 + d))
                try:
                    _, row = one_capture(mhz, ant, ifgr, rfg,
                                         f"sweep{d:+d}", slot, rfi)
                    log(f"  sweep {mhz:5.1f} {ANT_NICK[ant]} ifgr={ifgr:.0f}"
                        f" sync={row['sync']} aud={row['audio_s']:4.1f}s")
                except Exception as e:
                    log(f"  sweep skip ({str(e)[:40]})")
            if cliffiest[0] is not None:
                try:
                    knob_ab(cliffiest[0], cliffiest[1], slot)
                except Exception as e:
                    log(f"  knobA/B skip ({str(e)[:40]})")
            log(f"slot {slot} done in {time.time()-slot_t0:.0f}s")
        slot += 1
        wait = a.slot_min * 60 - (time.time() - slot_t0)
        if wait > 0:
            time.sleep(wait)
    log("3-antenna day lab done")


if __name__ == "__main__":
    main()
