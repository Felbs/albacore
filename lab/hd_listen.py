#!/usr/bin/env python3
"""albacore live listening: SDR -> decimate -> nrsc5 STDIN -> speakers.

The growing-file trick races (nrsc5 decodes ~30x realtime, hits EOF and
exits); stdin cannot EOF while the pump lives, so this is the proper
realtime path. Audio WAV grows and mpv tails it. Ctrl+C (or closing the
console) stops everything and releases the SDR.

Run with radioconda python. ALBACORE env vars pass straight through to
the decoder — listen.bat sets ALBACORE=1.
"""
import argparse, os, subprocess, sys, threading, time
from pathlib import Path
import numpy as np

sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
import hd_radio

NRSC5 = os.environ.get("NRSC5_EXE", r"C:\Tools\nrsc5\nrsc5.exe")
MPV = hd_radio.MPV
LAB = Path(r"Z:\src\gr-radiotuna\lab")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mhz", type=float, required=True)
    ap.add_argument("--prog", type=int, default=0)
    a = ap.parse_args()

    sdr = None
    for attempt in range(6):
        try:
            sdr, st, RX = hd_radio.open_sdr(a.mhz, ifgr=40, rfgain="5")
            break
        except Exception as e:
            print(f"SDR busy ({str(e)[:60]}) - retry {attempt+1}/6 in 10s", flush=True)
            time.sleep(10)
    if sdr is None:
        print("could not get the SDR - is something recording?")
        return 1

    wav = LAB / "hd_live.wav"
    try:
        wav.unlink()
    except OSError:
        pass

    nr = subprocess.Popen([NRSC5, "-r", "-", "-o", str(wav), str(a.prog)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=False)
    stop = threading.Event()

    def reader():
        for line in iter(nr.stdout.readline, b""):
            try:
                print("  " + line.decode(errors="replace").rstrip(), flush=True)
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()

    print(f"=== live {a.mhz} MHz program {a.prog} via {Path(NRSC5).name} "
          f"(ALBACORE={os.environ.get('ALBACORE','0')}) ===", flush=True)
    buf = np.empty(2 * 65536, np.int16)
    mpv_started = False
    try:
        while not stop.is_set():
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret > 0:
                cu8 = hd_radio.cs16_to_cu8(hd_radio.decimate2_cs16(buf[:2 * r.ret]))
                try:
                    nr.stdin.write(cu8.tobytes())
                except (BrokenPipeError, OSError):
                    print("decoder exited")
                    break
            if not mpv_started and wav.exists() and wav.stat().st_size > 300_000:
                subprocess.Popen([MPV, str(wav), "--volume=110",
                                  "--keep-open=yes", "--force-seekable=yes"])
                mpv_started = True
    except KeyboardInterrupt:
        pass
    stop.set()
    try:
        nr.stdin.close()
    except Exception:
        pass
    nr.terminate()
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    print("stopped, SDR released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
