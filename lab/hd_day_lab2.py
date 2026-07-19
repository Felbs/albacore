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

v2.1 (slot-0 lessons, 13:37Z):
  - ONE SDR SESSION PER SLOT, retune/antenna-switch on the live stream.
    v2.0 opened/closed the device ~20x per slot and the SDRplay API
    service wedged after 7 ("no available RSP devices") — the rapid
    open/close storm is the service's hot-replug analog.
  - DUD-BURN after every open + 0.6 s settle-flush after every retune:
    the 1-2 captures right after a service heal read healthy rms but
    ~15 dB depressed pilot SNR (degraded early-session streaming) —
    they were polluting the science rows.
  - Capture wall-time logged (wall >> secs = sample starvation).

Methodology guards (unchanged): rotated station order, referee decode
(stock nrsc5), analog dials from the same specimen, fixed RFI probe,
specimens deleted after metric extraction, listener guard, one heal
per slot, knob-A/B regression tripwire every slot.

Usage:
  hd_day_lab2.py --until 2026-07-19T21:50:00Z [--start-at ...Z]
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
import hd_day_lab as v1          # listener guard, heal, three_way

LOG = Path(r"Z:\SDR_Agent_v2\hd_day_lab_log.txt")
CSV = Path(r"Z:\src\albacore\lab\out\antenna_cube.csv")
CAL = Path(r"Z:\src\gr-radiotuna\lab\hd_ant_cal.json")
TMPCAP = Path(r"Z:\src\albacore\lab\out\cube_specimen.cs16")
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


class Session:
    """One SDR session per slot: open once, retune per cell."""

    def __init__(self):
        hd_radio._ensure_sdr_dll_path()
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
        SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
        self.RX = SOAPY_SDR_RX
        self.sdr = SoapySDR.Device("driver=sdrplay")
        self.sdr.setSampleRate(self.RX, 0, fs.FS_CAP)
        try:
            self.sdr.setGainMode(self.RX, 0, False)
        except Exception:
            pass
        self.st = self.sdr.setupStream(self.RX, SOAPY_SDR_CS16)
        self.sdr.activateStream(self.st)
        self.buf = np.empty(2 * 262144, np.int16)
        # dud-burn: the first post-open stream may be zeros or degraded
        pk = self._flush(1.0)
        if pk < 20:
            log("  session dud (zeros) - reopening once")
            self.close()
            time.sleep(1)
            self.__init__()

    def _flush(self, secs):
        """Discard the settle window; return the peak seen."""
        n_want = int(secs * fs.FS_CAP)
        got, pk, t0 = 0, 0, time.time()
        while got < n_want and time.time() - t0 < secs * 2 + 2:
            r = self.sdr.readStream(self.st, [self.buf], 262144,
                                    timeoutUs=1000000)
            if r.ret > 0:
                got += r.ret
                pk = max(pk, int(np.abs(self.buf[:2 * r.ret]).max()))
        return pk

    def tune(self, mhz, ant, ifgr, rfgain):
        self.sdr.setFrequency(self.RX, 0, mhz * 1e6)
        self.sdr.setAntenna(self.RX, 0, ant)
        self.sdr.setGain(self.RX, 0, "IFGR", float(ifgr))
        try:
            self.sdr.writeSetting("rfgain_sel", str(rfgain))
        except Exception:
            pass
        self._flush(0.6)

    def capture(self, secs, path):
        n_want = int(secs * fs.FS_CAP)
        got, t0 = 0, time.time()
        with open(path, "wb") as f:
            while got < n_want and time.time() - t0 < secs * 2 + 10:
                r = self.sdr.readStream(self.st, [self.buf], 262144,
                                        timeoutUs=1000000)
                if r.ret > 0:
                    n = min(r.ret, n_want - got)
                    self.buf[:2 * n].tofile(f)
                    got += n
        return got / fs.FS_CAP, time.time() - t0

    def close(self):
        try:
            self.sdr.deactivateStream(self.st)
            self.sdr.closeStream(self.st)
        except Exception:
            pass
        self.sdr = None


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


def rfi_probe(ses):
    """Noise floor on rabbit @93.3, fixed gains — the control."""
    ses.tune(93.3, "Antenna A", 40, "5")
    secs, wall = ses.capture(4, TMPCAP)
    raw = np.fromfile(TMPCAP, dtype=np.int16)
    x = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 32768.0
    N = 8192
    seg = x[: len(x) // N * N].reshape(-1, N)
    psd = np.fft.fftshift(
        (np.abs(np.fft.fft(seg * np.hanning(N), axis=1)) ** 2).mean(0))
    db = 10 * np.log10(psd + 1e-12)
    floor = float(np.median(db[4096 + 700:4096 + 1200]))
    sb = float(db[4096 + 357:4096 + 545].mean())
    return floor, sb - floor


def one_capture(ses, mhz, ant, ifgr, rfgain, tag, slot, rfi):
    ses.tune(mhz, ant, ifgr, rfgain)
    secs, wall = ses.capture(14, TMPCAP)
    if wall > secs * 1.3:
        log(f"  !! starvation: {mhz:.1f} {ANT_NICK[ant]} wall {wall:.1f}s "
            f"for {secs:.1f}s of samples")
    rms, inch = rf_facts(TMPCAP)
    res = fs.nrsc5_replay(TMPCAP, secs)
    psnr, asnr = fm_dials(TMPCAP)
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
    return row


def knob_ab(ses, mhz, ant, slot):
    """Anti-regression: 3-way decode of the cliffiest cell, every slot."""
    ifgr, rfg = cal_gains(ant, mhz)
    ses.tune(mhz, ant, ifgr, rfg)
    secs, wall = ses.capture(20, TMPCAP)
    res = fs.nrsc5_replay(TMPCAP, secs)
    if not res["sync"]:
        log(f"  knobA/B {mhz:.1f} {ANT_NICK[ant]}: no sync, skipped")
        return
    raw = np.fromfile(TMPCAP, dtype=np.int16)
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
        log(f"3-antenna day lab v2.1 armed; sleeping until {start:%H:%M}Z")
        while datetime.now(timezone.utc) < start:
            time.sleep(30)
    log(f"3-ANTENNA DAY LAB v2.1: {a.mhz} x {[ANT_NICK[x] for x in ANTS]}"
        f" every {a.slot_min}min until {until:%H:%M}Z (one session/slot)")
    CSV.parent.mkdir(exist_ok=True)
    if not CSV.exists():
        with open(CSV, "w", newline="") as f:
            csv.DictWriter(f, COLS).writeheader()
    cells = [(m, ant) for m in a.mhz for ant in ANTS]
    slot = 1                       # slot 0 ran under v2.0
    while datetime.now(timezone.utc) < until:
        slot_t0 = time.time()
        if v1.listener_running():
            log("user listening - slot skipped")
        else:
            ses = None
            healed = False
            try:
                try:
                    ses = Session()
                except Exception as e:
                    log(f"open fail ({str(e)[:40]}) - healing")
                    v1.heal_service()
                    healed = True
                    ses = Session()
                rfi = rfi_probe(ses)
                log(f"slot {slot}: RFI floor {rfi[0]:.1f} dB, "
                    f"margin {rfi[1]:+.1f} dB")
                order = (a.mhz[slot % len(a.mhz):]
                         + a.mhz[:slot % len(a.mhz)])
                cliffiest = (None, None, 1e9)
                for mhz in order:
                    for ant in ANTS:
                        ifgr, rfg = cal_gains(ant, mhz)
                        try:
                            row = one_capture(ses, mhz, ant, ifgr, rfg,
                                              "base", slot, rfi)
                        except Exception as e:
                            log(f"  {mhz:5.1f} {ANT_NICK[ant]} fail "
                                f"({str(e)[:40]})"
                                + ("" if healed else " - heal+reopen"))
                            if healed:
                                raise           # second failure ends slot
                            ses.close()
                            v1.heal_service()
                            healed = True
                            ses = Session()
                            continue
                        log(f"  {mhz:5.1f} {ANT_NICK[ant]:11s} "
                            f"sync={row['sync']} ber={row['ber']:.4f} "
                            f"aud={row['audio_s']:4.1f}s "
                            f"pilot={row['pilot_snr_db']:5.1f}dB")
                        mer = row["mer_lo"]
                        if row["sync"] and mer == mer and mer < cliffiest[2]:
                            cliffiest = (mhz, ant, mer)
                mhz, ant = cells[slot % len(cells)]
                ifgr0, rfg = cal_gains(ant, mhz)
                for d in (-4, +4):
                    ifgr = max(20, min(59, ifgr0 + d))
                    try:
                        row = one_capture(ses, mhz, ant, ifgr, rfg,
                                          f"sweep{d:+d}", slot, rfi)
                        log(f"  sweep {mhz:5.1f} {ANT_NICK[ant]} "
                            f"ifgr={ifgr:.0f} sync={row['sync']} "
                            f"aud={row['audio_s']:4.1f}s")
                    except Exception as e:
                        log(f"  sweep skip ({str(e)[:40]})")
                if cliffiest[0] is not None:
                    try:
                        knob_ab(ses, cliffiest[0], cliffiest[1], slot)
                    except Exception as e:
                        log(f"  knobA/B skip ({str(e)[:40]})")
            except Exception as e:
                log(f"slot {slot} aborted ({str(e)[:60]})")
            finally:
                if ses is not None:
                    ses.close()
                TMPCAP.unlink(missing_ok=True)
            log(f"slot {slot} done in {time.time()-slot_t0:.0f}s")
        slot += 1
        wait = a.slot_min * 60 - (time.time() - slot_t0)
        if wait > 0:
            time.sleep(wait)
    log("3-antenna day lab done")


if __name__ == "__main__":
    main()
