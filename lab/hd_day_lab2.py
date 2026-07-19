#!/usr/bin/env python3
"""albacore lab v2.1: the THREE-ANTENNA all-day laboratory (2026-07-19).

The rig has three antennas on the RSPdx, software-selectable:
    Antenna A = rabbit ears (ANT111E)
    Antenna B = "Old Faithful" TV antenna
    Antenna C = discone on the roof

Research questions (design notes in gr-radiotuna docs/SCIENCE.md sec 7):
  Q1  Which antenna wins per station, and does the winner drift with
      time of day?           -> antenna x station x time quality cube
  Q2  What is the optimal gain per (antenna, station)?
                             -> rotating mini IFGR sweeps = response curves
  Q3  Perfect-tune algorithm: given a station, choose antenna + gains
      from the cube, beat the fixed-antenna baseline.
  Q4  RFI timeline (fixed probe = the control instrument) + one knob A/B
      (stock vs ALBACORE=1 vs +auto) per slot on the cliffiest cell.

v2.2 (the REAL slot-0/1/2 lesson, 14:40Z): the "wedges" and the
"starvation" were the WARDEN — a second daemon rotating its own SDR
test campaigns into every gap the lab left. One radio, single-tenant,
two uncoordinated clients. The warden stands down today; the lab
design returns to the PROVEN envelope (open/close per capture at
<=1.3/min pace, exactly what the 7/19 stress runs did flawlessly):
  - ONE ANTENNA PER SLOT (rotate A->B->C across slots) — no live
    antenna switching at all; each antenna samples every station
    every 3 slots (75 min), rotation debiases time drift.
  - Paced cycles (>=45 s each), starved captures (wall >> secs, e.g.
    a wxTuna Meteor pass takes the radio — passes outrank us by
    design) are tagged +starved and excluded from analysis.
  - Slots 0-2 (contention era) stay in the CSV but are excluded from
    the league; v2.2 numbering starts at slot 3.

v2.3 (coexistence guard, 14:45Z): the warden stood down, but wxTuna's
watch daemon still owns the dial during every Meteor pass — and it
MUST (137.9 MHz LRPT passes are unrepeatable). So the lab stops
competing for the one radio and YIELDS instead. Before every slot AND
every capture it consults wxTuna's own heartbeat (wxsat_status.json,
the same file its panel reads) and stands off for the whole predicted
pass window (rec_start..los, warden's 3-min buffer). A yielded
slot/capture is logged "yielded", never "failed", counts toward
nothing, and — critically — NEVER triggers a service heal: restarting
SDRplayAPIService under a live pass is exactly the harm we avoid. This
consults, never opens, the device; the reactive net (the SoapySDR
"no available RSP" signature) catches any pass we didn't predict.

Methodology guards (unchanged): rotated station order, referee decode
(stock nrsc5), analog dials from the same specimen, fixed RFI probe,
specimens deleted after metric extraction, listener guard, one heal
per slot, knob-A/B regression tripwire every slot.

Usage:
  hd_day_lab2.py --until 2026-07-19T21:50:00Z [--start-at ...Z]
"""
import argparse, csv, json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\albacore\lab")
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_field_survey as fs
import hd_radio
import fm_stereo
import hd_day_lab as v1          # listener guard, heal, three_way

LOG = Path(r"Z:\SDR_Agent_v2\hd_day_lab_log.txt")
CSV = Path(r"Z:\src\albacore\lab\out\antenna_cube.csv")
CAL = Path(r"Z:\src\gr-radiotuna\lab\hd_ant_cal.json")
TMPCAP = Path(r"Z:\src\albacore\lab\out\cube_specimen.cs16")
ANTS = ["Antenna A", "Antenna B", "Antenna C"]
ANT_NICK = {"Antenna A": "rabbit", "Antenna B": "oldfaithful",
            "Antenna C": "discone"}
# Coexistence: wxTuna's watch daemon publishes this heartbeat (its panel
# reads it too). We consult it to yield the one radio during Meteor passes.
WXSTATUS = Path(r"Z:\src\wxTuna\lab\wxsat_status.json")
YIELD_BUFFER_S = 180     # stand off the dial this long before a pass AOS
WX_TTL_S = 180           # wxsat heartbeat older than this = stale (ignore state)
BUSY_SIG = ("no available RSP", "SoapySDR::Device::make")
COLS = ["utc", "slot", "mhz", "ant", "ifgr", "rfgain", "tag", "rms",
        "inch_db", "sync", "ber", "mer_lo", "mer_hi", "audio_s",
        "pilot_snr_db", "fm_audio_snr_db", "rfi_floor_db", "rfi_margin_db"]


def log(m):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%SZ}  v2 {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


class DeviceYield(Exception):
    """Another SDR client holds the RSPdx — coexist (yield), never heal."""


def is_busy_error(e):
    """True if an exception carries the SoapySDR device-busy signature — the
    same string the warden matched: 'no available RSP devices found'."""
    s = str(e)
    return any(sig in s for sig in BUSY_SIG)


def _iso(v):
    try:
        return (datetime.fromisoformat(v.replace("Z", "+00:00"))
                if v else None)
    except Exception:
        return None


def sdr_held_by_other(now=None):
    """Coexistence guard for the one-radio rig. Returns a reason string when
    another SDR client — above all wxTuna's watch daemon during a Meteor LRPT
    pass — holds or is about to need the RSPdx, else None. Never opens the
    device: it reads wxTuna's own heartbeat (wxsat_status.json). Passes
    outrank the lab by design, so we stand off for the whole predicted
    window (rec_start-buffer .. los+buffer), the same reservation the warden
    honored. No status file (watcher not running) -> proceed normally."""
    now = now or datetime.now(timezone.utc)
    try:
        st = json.loads(WXSTATUS.read_text())
    except Exception:
        return None
    rec, los = _iso(st.get("rec_start")), _iso(st.get("next_los"))
    if rec and los and (rec - timedelta(seconds=YIELD_BUFFER_S)
                        <= now <= los + timedelta(seconds=YIELD_BUFFER_S)):
        return (f"wxTuna Meteor pass {st.get('next_sat', '?')} "
                f"{rec:%H:%M}-{los:%H:%M}Z")
    upd = _iso(st.get("updated"))
    if (upd and (now - upd).total_seconds() < WX_TTL_S
            and st.get("state") == "recording"):
        return f"wxTuna recording {st.get('sat', '?')}"
    return None


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


def cap_env(mhz, ant, ifgr, rfgain, secs, tag):
    """Paced fs.capture with env-pinned antenna/gains; returns
    (path, secs, wall). Caller deletes the file."""
    os.environ["HD_ANT"] = ant
    os.environ["HD_IFGR"] = str(ifgr)
    os.environ["HD_RFGAIN"] = str(rfgain)
    try:
        return fs.capture(mhz, secs, tag)
    finally:
        os.environ.pop("HD_IFGR", None)
        os.environ.pop("HD_RFGAIN", None)


def one_capture(mhz, ant, ifgr, rfgain, tag, slot, rfi):
    held = sdr_held_by_other()
    if held:
        raise DeviceYield(held)
    try:
        out, secs, wall = cap_env(mhz, ant, ifgr, rfgain, 14, "cube")
    except Exception as e:
        if is_busy_error(e):
            raise DeviceYield("RSPdx busy (no available RSP device)")
        raise
    starved = wall > secs * 1.3 or secs < 12
    try:
        if starved:
            log(f"  !! starved capture: {mhz:.1f} {ANT_NICK[ant]} "
                f"{secs:.1f}s in {wall:.1f}s wall (excluded)")
            tag += "+starved"
            rms = inch = psnr = asnr = float("nan")
            res = {"sync": 0, "ber": float("nan"), "mer_lo": float("nan"),
                   "mer_hi": float("nan"), "audio_s": 0.0}
        else:
            rms, inch = rf_facts(out)
            res = fs.nrsc5_replay(out, secs)
            psnr, asnr = fm_dials(out)
        row = {"utc": datetime.now(timezone.utc).isoformat(), "slot": slot,
               "mhz": mhz, "ant": ANT_NICK[ant], "ifgr": ifgr,
               "rfgain": rfgain, "tag": tag,
               "rms": rms if rms != rms else round(rms, 1),
               "inch_db": inch if inch != inch else round(inch, 1),
               "sync": int(res["sync"]), "ber": res["ber"],
               "mer_lo": res["mer_lo"], "mer_hi": res["mer_hi"],
               "audio_s": round(res["audio_s"], 1),
               "pilot_snr_db": psnr, "fm_audio_snr_db": asnr,
               "rfi_floor_db": round(rfi[0], 1),
               "rfi_margin_db": round(rfi[1], 1)}
        with open(CSV, "a", newline="") as f:
            csv.DictWriter(f, COLS).writerow(row)
        return row
    finally:
        Path(out).unlink(missing_ok=True)
        Path(str(out) + ".json").unlink(missing_ok=True)


def knob_ab(mhz, ant, slot):
    """Anti-regression: 3-way decode of the cliffiest cell, every slot."""
    held = sdr_held_by_other()
    if held:
        raise DeviceYield(held)
    ifgr, rfg = cal_gains(ant, mhz)
    try:
        out, secs, wall = cap_env(mhz, ant, ifgr, rfg, 20, "knobab")
    except Exception as e:
        if is_busy_error(e):
            raise DeviceYield("RSPdx busy (no available RSP device)")
        raise
    try:
        _knob_ab_inner(out, secs, mhz, ant, slot)
    finally:
        Path(out).unlink(missing_ok=True)
        Path(str(out) + ".json").unlink(missing_ok=True)


def _knob_ab_inner(out, secs, mhz, ant, slot):
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
        log(f"  !! REGRESSION FLAG slot {slot}: ALBACORE=1 {ab['pair']}s "
            f"< stock {ab['stock']}s on {mhz:.1f} {ANT_NICK[ant]}")


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
        log(f"3-antenna day lab v2.3 armed; sleeping until {start:%H:%M}Z")
        while datetime.now(timezone.utc) < start:
            time.sleep(30)
    log(f"3-ANTENNA DAY LAB v2.3: {a.mhz}, one antenna per slot "
        f"(A->B->C), every {a.slot_min}min until {until:%H:%M}Z")
    CSV.parent.mkdir(exist_ok=True)
    if not CSV.exists():
        with open(CSV, "w", newline="") as f:
            csv.DictWriter(f, COLS).writeheader()
    slot = 3                       # 0-2 = the contention era (excluded)
    while datetime.now(timezone.utc) < until:
        slot_t0 = time.time()
        held = sdr_held_by_other()
        if v1.listener_running():
            log("user listening - slot skipped")
        elif held:
            log(f"slot {slot} yielded - {held} (pass outranks the lab)")
        else:
            ant = ANTS[slot % 3]
            healed = False
            try:
                rfi = v1.rfi_probe()
                log(f"slot {slot} [{ANT_NICK[ant]}]: RFI floor "
                    f"{rfi[0]:.1f} dB, margin {rfi[1]:+.1f} dB")
                time.sleep(6)      # pace: stay in the proven envelope
                order = (a.mhz[slot % len(a.mhz):]
                         + a.mhz[:slot % len(a.mhz)])
                cliffiest = (None, 1e9)
                for mhz in order:
                    cyc_t0 = time.time()
                    ifgr, rfg = cal_gains(ant, mhz)
                    try:
                        row = one_capture(mhz, ant, ifgr, rfg,
                                          "base", slot, rfi)
                        log(f"  {mhz:5.1f} {ANT_NICK[ant]:11s} "
                            f"sync={row['sync']} ber={row['ber']:.4f} "
                            f"aud={row['audio_s']:4.1f}s "
                            f"pilot={row['pilot_snr_db']:5.1f}dB")
                        mer = row["mer_lo"]
                        if row["sync"] and mer == mer and mer < cliffiest[1]:
                            cliffiest = (mhz, mer)
                    except DeviceYield as y:
                        log(f"  {mhz:5.1f} {ANT_NICK[ant]:11s} yielded "
                            f"({str(y)[:48]})")
                    except Exception as e:
                        log(f"  {mhz:5.1f} {ANT_NICK[ant]} fail "
                            f"({str(e)[:40]})"
                            + ("" if healed else " - healing"))
                        if healed:
                            raise       # second failure ends the slot
                        v1.heal_service()
                        healed = True
                        time.sleep(8)
                        try:            # burner: eat the degraded session
                            b, _, _ = cap_env(93.3, ant, 40, "5", 3, "burn")
                            Path(b).unlink(missing_ok=True)
                            Path(str(b) + ".json").unlink(missing_ok=True)
                        except Exception:
                            pass
                    # pace every cycle to >=45 s regardless of outcome
                    rest = 45 - (time.time() - cyc_t0)
                    if rest > 0:
                        time.sleep(rest)
                # rotating mini gain sweep on THIS slot's antenna
                mhz = a.mhz[(slot // 3) % len(a.mhz)]
                ifgr0, rfg = cal_gains(ant, mhz)
                for d in (-4, +4):
                    cyc_t0 = time.time()
                    ifgr = max(20, min(59, ifgr0 + d))
                    try:
                        row = one_capture(mhz, ant, ifgr, rfg,
                                          f"sweep{d:+d}", slot, rfi)
                        log(f"  sweep {mhz:5.1f} {ANT_NICK[ant]} "
                            f"ifgr={ifgr:.0f} sync={row['sync']} "
                            f"aud={row['audio_s']:4.1f}s "
                            f"pilot={row['pilot_snr_db']:5.1f}dB")
                    except DeviceYield as y:
                        log(f"  sweep {mhz:5.1f} {ANT_NICK[ant]} yielded "
                            f"({str(y)[:40]})")
                    except Exception as e:
                        log(f"  sweep skip ({str(e)[:40]})")
                    rest = 45 - (time.time() - cyc_t0)
                    if rest > 0:
                        time.sleep(rest)
                if cliffiest[0] is not None:
                    try:
                        knob_ab(cliffiest[0], ant, slot)
                    except DeviceYield as y:
                        log(f"  knobA/B yielded ({str(y)[:40]})")
                    except Exception as e:
                        log(f"  knobA/B skip ({str(e)[:40]})")
            except Exception as e:
                log(f"slot {slot} aborted ({str(e)[:60]})")
            log(f"slot {slot} done in {time.time()-slot_t0:.0f}s")
        slot += 1
        wait = a.slot_min * 60 - (time.time() - slot_t0)
        if wait > 0:
            time.sleep(wait)
    log("3-antenna day lab done")


if __name__ == "__main__":
    main()
