#!/usr/bin/env python3
"""Usage: python3 serial_log.py <PORT> <seconds> [command_chars] [end_chars]

Opens PORT at 115200, waits 2 s (opening resets the Mega), sends each command
char on its own line, then prints every received line for <seconds> seconds.
Everything is also appended to tilt_log.txt with timestamps.
Set SETTLE=<seconds> to wait longer before sending commands (default 2).
"""
import os
import sys
import time
from datetime import datetime

try:
    import serial
except ImportError:  # no pyserial: minimal stdlib fallback (macOS/Linux)
    import os
    import select
    import termios
    import tty

    class _Serial:
        def __init__(self, port, baud, timeout=0.1):
            self.timeout = timeout
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
            tty.setraw(self.fd)
            attrs = termios.tcgetattr(self.fd)
            speed = getattr(termios, f"B{baud}")
            attrs[4] = attrs[5] = speed
            attrs[2] |= termios.CLOCAL | termios.CREAD | termios.HUPCL
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)

        def write(self, data):
            os.write(self.fd, data)

        def read(self, n):
            r, _, _ = select.select([self.fd], [], [], self.timeout)
            return os.read(self.fd, n) if r else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            os.close(self.fd)

    class serial:  # noqa: N801 - mimic module name
        Serial = _Serial

LOG = "tilt_log.txt"


def log(f, direction, text):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    f.write(f"[{stamp}] {direction} {text}\n")
    f.flush()


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    port, seconds = sys.argv[1], float(sys.argv[2])
    cmds = sys.argv[3] if len(sys.argv) > 3 else ""
    end_cmds = sys.argv[4] if len(sys.argv) > 4 else ""

    with open(LOG, "a") as f, serial.Serial(port, 115200, timeout=0.1) as ser:
        log(f, "--", f"open {port} for {seconds}s cmds={cmds!r}")
        settle = float(os.environ.get("SETTLE", "2"))
        time.sleep(settle)
        for c in cmds:
            ser.write((c + "\n").encode())
            log(f, ">>", c)
            time.sleep(0.3)

        end = time.time() + seconds
        buf = b""
        while time.time() < end:
            buf += ser.read(256)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(errors="replace").rstrip("\r")
                print(text, flush=True)
                log(f, "<<", text)
        for c in end_cmds:
            ser.write((c + "\n").encode())
            log(f, ">>", c)
            t2 = time.time() + 1.0
            while time.time() < t2:
                buf += ser.read(256)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace").rstrip("\r")
            print(text, flush=True)
            log(f, "<<", text)
        if buf.strip():
            text = buf.decode(errors="replace").rstrip()
            print(text, flush=True)
            log(f, "<<", text)


if __name__ == "__main__":
    main()
