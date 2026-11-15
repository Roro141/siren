#!/usr/bin/env python3
"""Persistent serial session (opening the port resets the Mega, so open it once).

  python3 serial_session.py <PORT>            # run in background; holds the port
  python3 serial_session.py send <cmds> <seconds> [end_cmds] [delay]
      waits [delay] s, sends cmds (one char per line), prints new lines for
      <seconds>, then sends end_cmds and prints their output.

Lines are appended to tilt_log.txt (timestamped) and session.log (raw).
Commands go through the file cmd.queue.
"""
import os
import sys
import time
from datetime import datetime

import serial

LOG, RAW, QUEUE = "tilt_log.txt", "session.log", "cmd.queue"


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def daemon(port):
    open(QUEUE, "w").close()
    with serial.Serial(port, 115200, timeout=0.05) as ser, open(LOG, "a") as lf, open(RAW, "a") as rf:
        lf.write(f"[{stamp()}] -- session open {port}\n")
        buf = b""
        while True:
            # outgoing commands
            if os.path.getsize(QUEUE):
                with open(QUEUE, "r+") as q:
                    cmds = q.read()
                    q.seek(0)
                    q.truncate()
                for c in cmds:
                    if c.strip():
                        ser.write((c + "\n").encode())
                        lf.write(f"[{stamp()}] >> {c}\n")
                        time.sleep(0.2)
                lf.flush()
            buf += ser.read(512)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(errors="replace").rstrip("\r")
                lf.write(f"[{stamp()}] << {text}\n")
                rf.write(text + "\n")
            lf.flush()
            rf.flush()


def send(cmds, seconds, end_cmds="", delay=0.0):
    time.sleep(delay)
    pos = os.path.getsize(RAW)
    with open(QUEUE, "a") as q:
        q.write(cmds)

    def pump(until):
        nonlocal pos
        while time.time() < until:
            with open(RAW) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            time.sleep(0.1)

    pump(time.time() + seconds)
    if end_cmds:
        with open(QUEUE, "a") as q:
            q.write(end_cmds)
        pump(time.time() + 1.5)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "send":
        a = sys.argv[2:]
        send(a[0], float(a[1]), a[2] if len(a) > 2 else "", float(a[3]) if len(a) > 3 else 0.0)
    elif len(sys.argv) == 2:
        daemon(sys.argv[1])
    else:
        print(__doc__)
