#!/usr/bin/env python3
"""A chatty stand-in for a server: a ready line, then a request log, colored
like a real one, with an occasional error.

    fake_server.py [--port 8080] [--delay 1] [--every 0.5] [--error-every 7]
"""

import argparse
import random
import sys
import time

GREEN, RED, DIM, RESET = "\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[0m"
PATHS = ("/", "/health", "/api/items", "/api/items/42", "/static/app.js")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--delay", type=float, default=1.0, help="seconds before it's ready")
    parser.add_argument("--every", type=float, default=0.5, help="seconds between requests")
    parser.add_argument("--error-every", type=int, default=7, help="every Nth request fails")
    args = parser.parse_args()

    print("starting up ...", flush=True)
    time.sleep(args.delay)
    print(f"{GREEN}Serving HTTP on 0.0.0.0 port {args.port}{RESET}", flush=True)
    n = 0
    try:
        while True:
            time.sleep(args.every)
            n += 1
            path = random.choice(PATHS)
            if args.error_every and n % args.error_every == 0:
                print(f"{RED}[ERROR]{RESET} GET {path}: database timeout (request {n})", flush=True)
            else:
                print(f"{DIM}{time.strftime('%H:%M:%S')}{RESET} GET {path} 200", flush=True)
    except KeyboardInterrupt:
        print("shutting down", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
