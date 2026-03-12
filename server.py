#!/usr/bin/env python3
"""
UDP Video Streaming Server — Webcam capture over WireGuard (macOS)
==================================================================
Captures the local webcam via AVFoundation and streams MPEG-TS over UDP.
Includes keep-alive: stops streaming if client heartbeat stops arriving.
Includes slow-network mode: lower bitrate, smaller resolution, more keyframes.

Requirements:
    brew install ffmpeg

Usage:
    python3 server.py                              # auto-detect webcam, stream to 10.0.0.2:5000
    python3 server.py --host 10.0.0.2              # explicit WireGuard peer IP
    python3 server.py --device 1                   # pick camera by AVFoundation index
    python3 server.py --list-devices               # show all available cameras
    python3 server.py --slow                       # slow-network mode (low bitrate/fps)
    python3 server.py --host 10.0.0.2 --port2 5001 # also stream to second port (play+save)
"""

import subprocess
import argparse
import sys
import shutil
import signal
import re
import socket
import threading
import time


# ── constants ─────────────────────────────────────────────────────────────────

HEARTBEAT_PORT      = 5010        # client sends UDP heartbeats here
HEARTBEAT_INTERVAL  = 2           # client sends every N seconds
HEARTBEAT_TIMEOUT   = 8           # server pauses stream after N seconds of silence
HEARTBEAT_MAGIC     = b"ALIVE"


# ── helpers ───────────────────────────────────────────────────────────────────

def check_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("[ERROR] ffmpeg not found. Install it:")
        print("        brew install ffmpeg")
        sys.exit(1)


def list_devices():
    print("[*] Querying AVFoundation devices...\n")
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True
    )
    print(result.stderr)


def detect_default_camera() -> str:
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True
    )
    matches = re.findall(r"\[(\d+)\].*(?:camera|facetime|webcam|capture)", result.stderr, re.IGNORECASE)
    if matches:
        print(f"[SERVER] Detected cameras: {matches}")
        print(f"[SERVER] Using device index: {matches[0]}  (override with --device)")
        return matches[0]
    print("[SERVER] Could not parse device list; defaulting to device index 0.")
    print("         Run with --list-devices to see all options.")
    return "0"


# ── ffmpeg command builder ────────────────────────────────────────────────────

def build_ffmpeg_cmd(
    device_index: str,
    host: str,
    port: int,
    bitrate: str,
    framerate: int,
    port2: int | None,
    slow: bool,
) -> list:
    av_input = f"{device_index}:"

    # Slow-network overrides: lower resolution, fps, bitrate, more keyframes
    if slow:
        video_size        = "640x480"
        keyframe_interval = framerate * 2
    else:
        video_size        = "1280x720"
        keyframe_interval = framerate

    if port2:
        output_args = [
            "-map", "0:v",
            "-f", "tee",
            f"[f=mpegts]udp://{host}:{port}|[f=mpegts]udp://{host}:{port2}",
        ]
    else:
        output_args = ["-map", "0:v", "-f", "mpegts", f"udp://{host}:{port}"]

    return [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostats",

        # Always capture at 30fps — AVFoundation is picky about fractional rates.
        # If a lower fps is requested, -r will drop frames after capture.
        "-f", "avfoundation",
        "-framerate", "30",
        "-video_size", video_size,
        "-probesize", "10M",
        "-i", av_input,

        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-r", str(framerate),        # output frame rate (drops frames if < 30)
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", "500k",
        "-g", str(keyframe_interval),
        "-pix_fmt", "yuv420p",
        "-an",

        *output_args,
    ]


# ── keep-alive listener ───────────────────────────────────────────────────────

class HeartbeatListener:
    """
    Listens for UDP heartbeat packets from the client.
    Tracks last-seen time so the main loop can pause/resume streaming.
    """
    def __init__(self, port: int):
        self.port          = port
        self.last_seen     = 0.0          # epoch seconds; 0 = never seen
        self._sock         = None
        self._thread       = None
        self._stop_event   = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[SERVER] Heartbeat listener on udp://0.0.0.0:{self.port}")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(64)
                if data == HEARTBEAT_MAGIC:
                    if self.last_seen == 0:
                        print(f"[SERVER] Client connected from {addr[0]}")
                    self.last_seen = time.time()
            except socket.timeout:
                continue
            except Exception:
                break

    def is_alive(self) -> bool:
        if self.last_seen == 0:
            return False
        return (time.time() - self.last_seen) < HEARTBEAT_TIMEOUT

    def stop(self):
        self._stop_event.set()
        if self._sock:
            self._sock.close()


# ── main ──────────────────────────────────────────────────────────────────────

def kill_proc(proc):
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


def main():
    parser = argparse.ArgumentParser(
        description="Webcam → UDP stream server for macOS (over WireGuard)"
    )
    parser.add_argument("--list-devices", action="store_true",
                        help="List available AVFoundation camera devices and exit.")
    parser.add_argument("--device",   default=None,
                        help="AVFoundation video device index (e.g. 0, 1). Auto-detected if not set.")
    parser.add_argument("--host",     default="10.0.0.2",
                        help="Client WireGuard IP to stream to (default: 10.0.0.2)")
    parser.add_argument("--port",     type=int, default=5000,
                        help="UDP destination port (default: 5000)")
    parser.add_argument("--port2",    type=int, default=None,
                        help="Second UDP port for play+save mode on client")
    parser.add_argument("--bitrate",  default=None,
                        help="Video bitrate. Defaults: 2000k normal, 600k slow mode.")
    parser.add_argument("--fps",      type=int, default=30,
                        help="Frames per second (default: 30; use 15 for slow links)")
    parser.add_argument("--slow",     action="store_true",
                        help="Slow-network mode: 640x480, 600k bitrate, 15fps, more keyframes")
    parser.add_argument("--no-keepalive", action="store_true",
                        help="Disable keep-alive (stream even if client is not responding)")
    parser.add_argument("--heartbeat-port", type=int, default=HEARTBEAT_PORT,
                        help=f"UDP port to receive client heartbeats on (default: {HEARTBEAT_PORT})")
    args = parser.parse_args()

    check_ffmpeg()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    # Slow-mode defaults
    if args.slow:
        fps     = args.fps     if args.fps != 30 else 15
        bitrate = args.bitrate or "600k"
    else:
        fps     = args.fps
        bitrate = args.bitrate or "2000k"

    device = args.device or detect_default_camera()

    print()
    print("=" * 56)
    print("  UDP Webcam Streaming Server  [macOS]")
    print("=" * 56)
    print(f"  Camera     : AVFoundation device {device}")
    print(f"  Target     : udp://{args.host}:{args.port}" + (f" + :{args.port2}" if args.port2 else ""))
    print(f"  Bitrate    : {bitrate}   FPS: {fps}   Slow mode: {args.slow}")
    print(f"  Keep-alive : {'disabled' if args.no_keepalive else f'enabled (port {args.heartbeat_port}, timeout {HEARTBEAT_TIMEOUT}s)'}")
    print(f"  Press Ctrl+C to stop.")
    print("=" * 56)
    print()

    cmd = build_ffmpeg_cmd(device, args.host, args.port, bitrate, fps, args.port2, args.slow)

    # ── keep-alive setup ──
    heartbeat = None
    if not args.no_keepalive:
        heartbeat = HeartbeatListener(args.heartbeat_port)
        heartbeat.start()
        print("[SERVER] Waiting for client heartbeat before starting stream...")
        while not heartbeat.is_alive():
            time.sleep(0.5)
        print("[SERVER] Client alive — starting stream.")

    proc = None

    def _shutdown(sig, frame):
        print("\n[SERVER] Shutting down...")
        if proc:
            kill_proc(proc)
        if heartbeat:
            heartbeat.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

        while True:
            # Check if ffmpeg died on its own
            ret = proc.poll()
            if ret is not None:
                if ret != 0:
                    print(f"\n[ERROR] ffmpeg exited with code {ret}")
                    print("  Tips:")
                    print("  - List cameras        : python3 server.py --list-devices")
                    print("  - Grant camera access : System Settings → Privacy → Camera → allow Terminal/iTerm")
                    print("  - Try lower res       : python3 server.py --slow")
                    print("  - Try device index 1  : python3 server.py --device 1")
                break

            # Keep-alive check
            if heartbeat and not heartbeat.is_alive():
                if heartbeat.last_seen > 0:
                    # Was connected before, now lost
                    print("[SERVER] Client heartbeat lost — pausing stream...")
                    kill_proc(proc)
                    proc = None
                    print("[SERVER] Waiting for client to reconnect...")
                    while not heartbeat.is_alive():
                        time.sleep(0.5)
                    print("[SERVER] Client reconnected — restarting stream.")
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

            time.sleep(1)

    except FileNotFoundError:
        print("[ERROR] ffmpeg not found. Run: brew install ffmpeg")
        sys.exit(1)


if __name__ == "__main__":
    main()
