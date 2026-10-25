"""Capture a bounded Master UART log, optionally resetting into the application."""
import argparse
from pathlib import Path
import sys
import time

import serial


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--seconds", type=int, default=75)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    uart = serial.Serial(port=None, baudrate=115200, timeout=0.25)
    uart.dtr = False
    uart.rts = False
    uart.port = args.port
    with uart, args.output.open("wb") as log:
        if args.reset:
            # Keep GPIO0 inactive; pulse EN only, booting the installed app.
            uart.rts = True
            time.sleep(0.1)
            uart.rts = False
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            data = uart.read(4096)
            if data:
                log.write(data)
                log.flush()
                sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    print(f"\nUART capture complete: {args.output}")


if __name__ == "__main__":
    main()
