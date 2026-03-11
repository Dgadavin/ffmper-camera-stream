#!/usr/bin/env python3
"""
UDP Video Streaming Client — Receive & play over WireGuard
==========================================================
Listens on a UDP port and plays the incoming MPEG-TS stream in real time.
Sends UDP heartbeats to the server so it knows the client is alive.
Includes jitter buffer tuning for slow/unreliable networks.

Requirements:
    brew install ffmpeg        # macOS
    sudo apt install ffmpeg    # Linux

Usage:
    python3 client.py                                          # play live (heartbeats to 10.0.0.1)
    python3 client.py --server-host 10.0.0.1                   # explicit server IP for heartbeats
    python3 client.py --server-host 10.0.0.1 --save out.mp4   # play + save (server needs --port2 5001)
    python3 client.py --no-play --save out.mp4                 # save only, no playback window
    python3 client.py --slow                                   # larger jitter buffer for bad links
    python3 client.py --no-keepalive                           # disable heartbeat sender
    python3 client.py --server-host 127.0.0.1 --no-keepalive  # localhost test
"""

import subprocess
import argparse
import sys
import shutil
import signal
import socket
import threading
import time


# ── constants ─────────────────────────────────────────────────────────────────

HEARTBEAT_PORT     = 5010
HEARTBEAT_INTERVAL = 2           # seconds between heartbeat packets
HEARTBEAT_MAGIC    = b"ALIVE"


# ── keep-alive sender ─────────────────────────────────────────────────────────

class HeartbeatSender:
    """
    Sends a small UDP packet to the server every few seconds.
    Server uses this to know the client is alive and ready to receive.
    """
    def __init__(self, host: str, port: int):
        self.host        = host
        self.port        = port
        self._sock       = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread     = None
        self._stop_event = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[CLIENT] Sending heartbeats to {self.host}:{self.port} every {HEARTBEAT_INTERVAL}s")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._sock.sendto(HEARTBEAT_MAGIC, (self.host, self.port))
            except Exception:
                pass
            self._stop_event.wait(HEARTBEAT_INTERVAL)

    def stop(self):
        self._stop_event.set()
        self._sock.close()


# ── ffmpeg/ffplay command builders ────────────────────────────────────────────

def ffplay_cmd(port: int, slow: bool) -> list:
    extra = ["-fflags", "nobuffer", "-flags", "low_delay"] if not slow else []

    return [
        "ffplay",
        "-loglevel",        "warning",
        "-probesize",       "5M",
        "-analyzeduration", "1000000",
        *extra,
        "-sync",            "ext",
        "-framedrop",                            # drop late frames instead of freezing
        "-max_delay",       "500000" if slow else "100000",   # µs
        "-window_title",    "Live Camera Stream",
        f"udp://0.0.0.0:{port}",
    ]


def ffmpeg_save_cmd(port: int, save_path: str) -> list:
    return [
        "ffmpeg", "-loglevel", "warning",
        "-probesize",       "5M",
        "-analyzeduration", "1000000",
        "-fflags",          "nobuffer",
        "-flags",           "low_delay",
        "-f",               "mpegts",
        "-i",               f"udp://0.0.0.0:{port}",
        "-c:v",             "copy",
        "-f",               "mp4",
        "-movflags",        "+faststart",
        save_path,
    ]


# ── process helpers ───────────────────────────────────────────────────────────

def check_deps(play: bool):
    if not shutil.which("ffmpeg"):
        print("[ERROR] ffmpeg not found.")
        print("        macOS : brew install ffmpeg")
        print("        Linux : sudo apt install ffmpeg")
        sys.exit(1)
    if play and not shutil.which("ffplay"):
        print("[ERROR] ffplay not found (it ships with ffmpeg).")
        sys.exit(1)


def kill_proc(proc):
    if proc is None:
        return
    try:
        proc.stdin.write(b"q\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="UDP stream client — plays the server's webcam feed"
    )
    parser.add_argument("--port",     type=int, default=5000,
                        help="UDP port to listen on (default: 5000). Must match server.")
    parser.add_argument("--save",     default=None, metavar="FILE",
                        help="Save stream to file (e.g. recording.mp4). Can combine with playback.")
    parser.add_argument("--no-play",  action="store_true",
                        help="Don't open a playback window (only useful with --save)")
    parser.add_argument("--slow",     action="store_true",
                        help="Slow-network mode: larger jitter buffer, frame drop enabled")
    parser.add_argument("--no-keepalive", action="store_true",
                        help="Disable heartbeat sender (server must use --no-keepalive too)")
    parser.add_argument("--server-host", default="10.0.0.1",
                        help="Server IP to send heartbeats to (default: 10.0.0.1). Use 127.0.0.1 for localhost test.")
    parser.add_argument("--heartbeat-port", type=int, default=HEARTBEAT_PORT,
                        help=f"UDP port to send heartbeats to (default: {HEARTBEAT_PORT})")
    args = parser.parse_args()

    if args.no_play and not args.save:
        print("[ERROR] --no-play requires --save (nothing to do otherwise).")
        sys.exit(1)

    check_deps(play=not args.no_play)

    print()
    print("=" * 56)
    print("  UDP Stream Client")
    print("=" * 56)
    print(f"  Listening  : udp://0.0.0.0:{args.port}")
    print(f"  Playback   : {'no' if args.no_play else 'yes (ffplay window)'}")
    print(f"  Save to    : {args.save or 'no'}")
    print(f"  Slow mode  : {args.slow}")
    print(f"  Keep-alive : {'disabled' if args.no_keepalive else f'sending to {args.server_host}:{args.heartbeat_port}'}")
    if args.save and not args.no_play:
        print(f"  Save port  : {args.port + 1}  (server must use --port2 {args.port + 1})")
    print(f"  Waiting for server stream... (Ctrl+C to stop)")
    print("=" * 56)
    print()

    # ── start heartbeat ──
    heartbeat = None
    if not args.no_keepalive:
        heartbeat = HeartbeatSender(args.server_host, args.heartbeat_port)
        heartbeat.start()

    # ── launch ffplay / ffmpeg ──
    play_proc = None
    save_proc = None

    def _shutdown(sig, frame):
        print("\n[CLIENT] Stopping...")
        kill_proc(play_proc)
        kill_proc(save_proc)
        if heartbeat:
            heartbeat.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        if args.no_play and args.save:
            save_proc = subprocess.Popen(ffmpeg_save_cmd(args.port, args.save), stdin=subprocess.PIPE)
            save_proc.wait()

        elif args.save:
            play_proc = subprocess.Popen(ffplay_cmd(args.port,          args.slow), stdin=subprocess.PIPE)
            save_proc = subprocess.Popen(ffmpeg_save_cmd(args.port + 1, args.save), stdin=subprocess.PIPE)
            play_proc.wait()
            kill_proc(save_proc)

        else:
            play_proc = subprocess.Popen(ffplay_cmd(args.port, args.slow), stdin=subprocess.PIPE)
            play_proc.wait()

    except FileNotFoundError:
        print("[ERROR] ffplay/ffmpeg not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
