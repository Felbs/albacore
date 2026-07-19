#!/usr/bin/env python3
"""albacore lab: all-day field laboratory (runs while the user is at work).

Each slot:
  1. LISTENER GUARD: if hd_listen.py is running, skip the whole slot
     silently (never steal the SDR or restart the service under the user).
  2. RFI PROBE: short capture on Antenna A at 93.3 -> noise-floor dB.
     All-day timeline of the burst-noise source for correlation hunting.
  3. SPECIMENS: 20 s captures on Antenna C for each station.
  4. FIELD A/B: every specimen that syncs gets a 3-way replay decode
     (stock vs ALBACORE=1 vs +COSTAS_BW=auto) judged by LISTEN real
     seconds, appended to day_lab_ab.csv — the daytime field
     certification builds itself.

Usage: hd_day_lab.py --start-at 2026-07-19T13:30:00Z --until 2026-07-19T22:30:00Z
"""
import argparse, csv, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\albacore\lab")
sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_field_survey as fs
import hd_radio
import importlib.util
_s = importlib.util.spec_from_file_location("meter", r"Z:\src\albacore\lab\hd_audio_meter.py")
meter = importlib.util.module_from_spec(_s)
_s.loader.exec_module(meter)

EXE = r"Z:\src\albacore\build\src\nrsc5.exe"
LOG = Path(r"Z:\SDR_Agent_v2\hd_day_lab_log.txt")
CSV = Path(r"Z:\src\albacore\lab\out\day_lab_ab.csv")
FS_CAP = hd_radio.FS_CAP
NFFT = 8192


def log(m):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%SZ}  {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def listener_running():
    try:
        r = subprocess.run(["powershell", "-Command",
            "(Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match 'hd_listen'} | Measure-Object).Count"],
            capture_output=True, text=True, timeout=60)
        return int(r.stdout.strip() or 0) > 0
    except Exception:
        return False


def heal_service():
    subprocess.run(["powershell", "-Command",
                    "Restart-Service SDRplayAPIService -Force"],
                   capture_output=True, timeout=60)
    time.sleep(6)


def rfi_probe():
    """Noise floor on Antenna A at 93.3 (the RFI instrument)."""
    os.environ["HD_ANT"] = "Antenna A"
    os.environ["HD_IFGR"] = "40"
    os.environ["HD_RFGAIN"] = "5"
    try:
        out, secs, wall = fs.capture(93.3, 4, "rfiprobe")
        raw = np.fromfile(out, dtype=np.int16)
        Path(out).unlink()
        Path(str(out) + ".json").unlink(missing_ok=True)
        x = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 32768.0
        seg = x[: len(x) // NFFT * NFFT].reshape(-1, NFFT)
        w = np.hanning(NFFT).astype(np.float32)
        psd = np.fft.fftshift((np.abs(np.fft.fft(seg * w, axis=1)) ** 2).mean(0))
        db = 10 * np.log10(psd + 1e-12)
        floor = float(np.median(db[4096 + 700:4096 + 1200]))
        sb = float(db[4096 + 357:4096 + 545].mean())
        return floor, sb - floor
    except Exception as e:
        log(f"rfi-probe fail ({str(e)[:40]})")
        return float("nan"), float("nan")
    finally:
        os.environ["HD_ANT"] = "Antenna C"
        os.environ["HD_IFGR"] = "30"
        os.environ["HD_RFGAIN"] = "7"


def three_way(cu8):
    res = {}
    for tag, cfg in (("stock", {}), ("pair", {"ALBACORE": "1"}),
                     ("auto", {"ALBACORE": "1", "ALBACORE_COSTAS_BW": "auto"})):
        e = dict(os.environ)
        e["PATH"] = r"C:\msys64\mingw64\bin;" + e["PATH"]
        for k in ("ALBACORE", "ALBACORE_COSTAS_BW"):
            e.pop(k, None)
        e.update(cfg)
        wav = Path(r"Z:\src\albacore\lab\out") / "daylab.wav"
        if wav.exists():
            wav.unlink()
        subprocess.run([EXE, "-r", str(cu8), "-o", str(wav), "0"],
                       capture_output=True, timeout=300, env=e)
        real = 0
        if wav.exists() and wav.stat().st_size > 44:
            x, fsr = meter.load_wav(wav)
            rows = meter.per_second_metrics(x, fsr)
            if rows:
                meter.judge(rows)
                real = sum(r["ok"] for r in rows)
        res[tag] = real
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-at")
    ap.add_argument("--until", required=True)
    ap.add_argument("--slot-min", type=float, default=25)
    ap.add_argument("--mhz", nargs="+", type=float, default=[93.3, 90.9, 91.9, 93.9])
    a = ap.parse_args()
    os.environ.setdefault("HD_ANT", "Antenna C")
    os.environ.setdefault("HD_IFGR", "30")
    os.environ.setdefault("HD_RFGAIN", "7")
    until = datetime.fromisoformat(a.until.replace("Z", "+00:00"))
    if a.start_at:
        start = datetime.fromisoformat(a.start_at.replace("Z", "+00:00"))
        log(f"day lab armed; sleeping until {start:%H:%M}Z")
        while datetime.now(timezone.utc) < start:
            time.sleep(30)
    log(f"day lab RUNNING: {a.mhz} every {a.slot_min}min until {until:%H:%M}Z")
    CSV.parent.mkdir(exist_ok=True)
    new_csv = not CSV.exists()
    with open(CSV, "a", newline="") as f:
        wcsv = csv.writer(f)
        if new_csv:
            wcsv.writerow(["utc", "mhz", "name", "ber", "stock_real", "pair_real", "auto_real", "file"])
    while datetime.now(timezone.utc) < until:
        slot_t0 = time.time()
        if listener_running():
            log("user listening - slot skipped")
        else:
            floor, margin = rfi_probe()
            log(f"RFI probe (ant A): floor {floor:.1f} dB, 93.3 margin {margin:+.1f} dB")
            healed = False
            for mhz in a.mhz:
                for attempt in (0, 1):
                    try:
                        out, secs, wall = fs.capture(mhz, 20, "daylab")
                        res = fs.nrsc5_replay(out, secs)
                        line = (f"{mhz:5.1f} {res['name'][:10]:10s} sync={res['sync']} "
                                f"ber {res['ber']:.4f}")
                        if res["sync"]:
                            raw = np.fromfile(out, dtype=np.int16)
                            cu8p = Path(r"Z:\src\albacore\lab\out") / "daylab.cu8"
                            hd_radio.cs16_to_cu8(hd_radio.decimate2_cs16(raw)).tofile(cu8p)
                            ab = three_way(cu8p)
                            line += f"  A/B real s: stock {ab['stock']} pair {ab['pair']} auto {ab['auto']}"
                            with open(CSV, "a", newline="") as f:
                                csv.writer(f).writerow(
                                    [datetime.now(timezone.utc).isoformat(), mhz,
                                     res["name"], res["ber"], ab["stock"], ab["pair"],
                                     ab["auto"], Path(out).name])
                        log(line)
                        break
                    except Exception as e:
                        if attempt == 0 and not healed and not listener_running():
                            log(f"{mhz:5.1f} wedge - healing")
                            heal_service()
                            healed = True
                        else:
                            log(f"{mhz:5.1f} skip ({str(e)[:40]})")
                            break
        wait = a.slot_min * 60 - (time.time() - slot_t0)
        if wait > 0:
            time.sleep(wait)
    log("day lab done")


if __name__ == "__main__":
    main()
