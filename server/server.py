#!/usr/bin/env python3
"""
UDP Video Streaming Server — Webcam capture over WireGuard (macOS / Linux / Raspberry Pi)
=========================================================================================
Captures the local webcam and streams MPEG-TS over UDP.
Auto-detects platform:
  - macOS:  AVFoundation capture
  - Linux:  V4L2 capture (Raspberry Pi, USB webcams, etc.)

Includes keep-alive: stops streaming if client heartbeat stops arriving.
Includes slow-network mode: lower bitrate, smaller resolution, more keyframes.

Requirements:
    macOS:  brew install ffmpeg
    Linux:  sudo apt install ffmpeg

Usage:
    python3 server.py                              # auto-detect webcam, stream to 10.0.0.2:5000
    python3 server.py --host 10.0.0.2              # explicit peer IP
    python3 server.py --device 1                   # pick camera (index on macOS, /dev/videoN on Linux)
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
import platform
import glob as globmod


# ── constants ─────────────────────────────────────────────────────────────────

HEARTBEAT_PORT      = 5010        # client sends UDP heartbeats here
HEARTBEAT_INTERVAL  = 2           # client sends every N seconds
HEARTBEAT_TIMEOUT   = 8           # server pauses stream after N seconds of silence
HEARTBEAT_MAGIC     = b"ALIVE"


IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"


# ── helpers ───────────────────────────────────────────────────────────────────

def check_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("[ERROR] ffmpeg not found. Install it:")
        if IS_MACOS:
            print("        brew install ffmpeg")
        else:
            print("        sudo apt install ffmpeg")
        sys.exit(1)


def has_libcamera() -> bool:
    """Check if rpicam-vid or libcamera-vid is available (Raspberry Pi camera module)."""
    return shutil.which("rpicam-vid") is not None or shutil.which("libcamera-vid") is not None


def libcamera_bin() -> str:
    """Return the available libcamera capture binary name."""
    if shutil.which("rpicam-vid"):
        return "rpicam-vid"
    return "libcamera-vid"


def has_hw_encoder() -> bool:
    """Check if h264_v4l2m2m hardware encoder is available (Raspberry Pi)."""
    if not IS_LINUX:
        return False
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True
    )
    return "h264_v4l2m2m" in result.stdout


def is_v4l2_capture_capable(device: str) -> bool:
    """Check if a V4L2 device supports video capture (not just metadata)."""
    result = subprocess.run(
        ["v4l2-ctl", "--device", device, "--info"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    return "Video Capture" in result.stdout


def list_devices():
    if IS_MACOS:
        print("[*] Querying AVFoundation devices...\n")
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True
        )
        print(result.stderr)
    else:
        if has_libcamera():
            print(f"[*] Raspberry Pi camera detected ({libcamera_bin()}).\n")
            result = subprocess.run(
                [libcamera_bin(), "--list-cameras"],
                capture_output=True, text=True
            )
            print(result.stdout or result.stderr)

        print("[*] Querying V4L2 devices...\n")
        devices = sorted(globmod.glob("/dev/video*"))
        if not devices:
            print("  No /dev/video* devices found.")
            return
        for dev in devices:
            if not is_v4l2_capture_capable(dev):
                continue
            result = subprocess.run(
                ["v4l2-ctl", "--device", dev, "--info"],
                capture_output=True, text=True
            )
            card = ""
            for line in result.stdout.splitlines():
                if "Card type" in line:
                    card = line.split(":", 1)[1].strip()
                    break
            print(f"  {dev}  —  {card}")


def detect_default_camera() -> str:
    if IS_MACOS:
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
    else:
        # Raspberry Pi camera module — use libcamera
        if has_libcamera():
            print(f"[SERVER] Pi camera detected (via {libcamera_bin()})")
            return "libcamera:0"

        # USB webcam fallback — find first capture-capable V4L2 device
        devices = sorted(globmod.glob("/dev/video*"))
        for dev in devices:
            if is_v4l2_capture_capable(dev):
                print(f"[SERVER] Using V4L2 device: {dev}  (override with --device)")
                return dev
        print("[SERVER] No video device found; defaulting to /dev/video0.")
        print("         Run with --list-devices to see all options.")
        return "/dev/video0"


def detect_v4l2_format(device: str) -> str | None:
    """Return 'mjpeg' if the device supports it, or None to let ffmpeg auto-negotiate."""
    result = subprocess.run(
        ["v4l2-ctl", "--device", device, "--list-formats"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    output = result.stdout.lower()
    if "mjpeg" in output or "motion-jpeg" in output:
        return "mjpeg"
    return None


# ── command builders ──────────────────────────────────────────────────────────

def _resolve_video_params(slow: bool, framerate: int, lossy: bool = False):
    if slow:
        return "640x480", framerate * 2
    if lossy:
        # More frequent keyframes → faster recovery after packet loss
        return "1280x720", max(framerate // 2, 1)
    return "1280x720", framerate


def _udp_url(host: str, port: int) -> str:
    """Build a UDP URL with small packet size for lower latency."""
    # 7 × 188-byte TS packets = 1316 bytes — flushes to network sooner,
    # fits inside any tunnel MTU, and avoids IP fragmentation
    return f"udp://{host}:{port}?pkt_size=1316"


def build_libcamera_cmds(
    host: str,
    port: int,
    bitrate: str,
    framerate: int,
    port2: int | None,
    slow: bool,
    lossy: bool = False,
):
    """
    Build a (rpicam-vid command, ffmpeg command) pair.
    rpicam-vid captures + H.264 encodes on the Pi GPU, outputs to stdout.
    ffmpeg reads the raw H.264 from stdin and repackages as MPEG-TS over UDP.
    """
    video_size, keyframe_interval = _resolve_video_params(slow, framerate, lossy)
    width, height = video_size.split("x")

    # Convert bitrate string like "2000k" to bits/s for rpicam-vid
    bitrate_bps = bitrate.replace("k", "000").replace("M", "000000")

    capture_cmd = [
        libcamera_bin(),
        "--codec", "h264",          # H.264 encoding
        "--libav-format", "mpegts", # MPEG-TS container with proper timestamps (Pi 5)
        "--width", width,
        "--height", height,
        "--framerate", str(framerate),
        "--bitrate", bitrate_bps,
        "--profile", "baseline",
        "--level", "4.1",
        "--intra", str(keyframe_interval),
        "--inline",            # SPS/PPS before every IDR — critical for UDP
        "--flush",             # flush output after each frame
        "--nopreview",
        "--timeout", "0",     # run indefinitely
        "--output", "-",      # stdout
    ]

    url1 = _udp_url(host, port)
    if port2:
        url2 = _udp_url(host, port2)
        output_args = [
            "-map", "0:v",
            "-f", "tee",
            f"[f=mpegts]{url1}|[f=mpegts]{url2}",
        ]
    else:
        output_args = ["-map", "0:v", "-f", "mpegts", url1]

    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostats",
        "-f", "mpegts",
        "-i", "pipe:0",      # read MPEG-TS from stdin (rpicam-vid outputs mpegts on Pi 5)
        "-c:v", "copy",      # no re-encode — already H.264 from the Pi GPU
        "-an",
        "-mpegts_flags", "+resend_headers",   # repeat PAT/PMT frequently
        "-muxrate", "0",                       # VBR — don't pad with nulls
        "-muxdelay", "0",                      # no muxer buffering (default 0.7s!)
        "-muxpreload", "0",
        "-flush_packets", "1",
        *output_args,
    ]

    return capture_cmd, ffmpeg_cmd


def build_ffmpeg_cmd(
    device: str,
    host: str,
    port: int,
    bitrate: str,
    framerate: int,
    port2: int | None,
    slow: bool,
    hw_encode: bool,
    lossy: bool = False,
) -> list:
    video_size, keyframe_interval = _resolve_video_params(slow, framerate, lossy)

    url1 = _udp_url(host, port)
    if port2:
        url2 = _udp_url(host, port2)
        output_args = [
            "-map", "0:v",
            "-f", "tee",
            f"[f=mpegts]{url1}|[f=mpegts]{url2}",
        ]
    else:
        output_args = ["-map", "0:v", "-f", "mpegts", url1]

    # ── platform-specific input ──
    if IS_MACOS:
        input_args = [
            # Always capture at 30fps — AVFoundation is picky about fractional rates.
            # If a lower fps is requested, -r will drop frames after capture.
            "-f", "avfoundation",
            "-framerate", "30",
            "-video_size", video_size,
            "-probesize", "10M",
            "-i", f"{device}:",
        ]
    else:
        # Linux USB webcam — V4L2
        v4l2_fmt = detect_v4l2_format(device)
        input_args = [
            "-f", "v4l2",
            *(["-input_format", v4l2_fmt] if v4l2_fmt else []),
            "-framerate", str(framerate),
            "-video_size", video_size,
            "-i", device,
        ]

    # ── encoder selection ──
    if hw_encode:
        encode_args = [
            "-c:v", "h264_v4l2m2m",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-g", str(keyframe_interval),
            "-pix_fmt", "yuv420p",
        ]
    else:
        encode_args = [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", "500k",
            "-g", str(keyframe_interval),
            "-pix_fmt", "yuv420p",
        ]

    return [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostats",
        *input_args,
        *encode_args,
        "-r", str(framerate),
        "-an",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-flush_packets", "1",
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
                data, addr = self._sock.recvfrom(128)
                if data == HEARTBEAT_MAGIC:
                    if self.last_seen == 0:
                        print(f"[SERVER] Client connected from {addr[0]}")
                    self.last_seen = time.time()
                elif data.startswith(b"PING:"):
                    if self.last_seen == 0:
                        print(f"[SERVER] Client connected from {addr[0]}")
                    self.last_seen = time.time()
                    # Echo timestamp back for RTT measurement
                    self._sock.sendto(b"PONG:" + data[5:], addr)
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
        description="Webcam → UDP stream server (macOS / Linux / Raspberry Pi)"
    )
    parser.add_argument("--list-devices", action="store_true",
                        help="List available camera devices and exit.")
    parser.add_argument("--device",   default=None,
                        help="Camera device. macOS: AVFoundation index (0, 1). Linux: V4L2 path (/dev/video0) or 'libcamera:0'. Auto-detected if not set.")
    parser.add_argument("--host",     default="10.0.0.2",
                        help="Client IP to stream to (default: 10.0.0.2)")
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
    parser.add_argument("--stats", action="store_true",
                        help="Enable RTT measurement via heartbeat PING/PONG")
    parser.add_argument("--sw", action="store_true",
                        help="Force software encoding (libx264) even when hardware is available")
    parser.add_argument("--lossy", action="store_true",
                        help="Lossy-network mode (Starlink, LTE): smaller UDP packets to avoid fragmentation, more frequent keyframes for faster error recovery")
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

    # Determine capture mode
    use_libcamera = device.startswith("libcamera:")

    if use_libcamera:
        plat_tag = f"Linux/{libcamera_bin()} (Pi GPU H.264)"
        enc_tag  = "Pi GPU (via rpicam-vid)"
    elif IS_MACOS:
        plat_tag = "macOS/AVFoundation"
        enc_tag  = "libx264 (sw)"
    else:
        plat_tag = "Linux/V4L2"
        hw_encode = not args.sw and has_hw_encoder()
        enc_tag   = "h264_v4l2m2m (hw)" if hw_encode else "libx264 (sw)"

    print()
    print("=" * 56)
    print(f"  UDP Webcam Streaming Server  [{plat_tag}]")
    print("=" * 56)
    print(f"  Camera     : {device}")
    print(f"  Encoder    : {enc_tag}")
    print(f"  Target     : udp://{args.host}:{args.port}" + (f" + :{args.port2}" if args.port2 else ""))
    print(f"  Bitrate    : {bitrate}   FPS: {fps}   Slow mode: {args.slow}")
    print(f"  Keep-alive : {'disabled' if args.no_keepalive else f'enabled (port {args.heartbeat_port}, timeout {HEARTBEAT_TIMEOUT}s)'}")
    print(f"  Lossy mode : {'ON (small pkts + frequent keyframes)' if args.lossy else 'off'}")
    print(f"  Stats      : {'enabled (PING/PONG)' if args.stats else 'off'}")
    print(f"  Press Ctrl+C to stop.")
    print("=" * 56)
    print()

    # ── keep-alive setup ──
    heartbeat = None
    if not args.no_keepalive:
        heartbeat = HeartbeatListener(args.heartbeat_port)
        heartbeat.start()
        print("[SERVER] Waiting for client heartbeat before starting stream...")
        while not heartbeat.is_alive():
            time.sleep(0.5)
        print("[SERVER] Client alive — starting stream.")

    # ── process management ──
    capture_proc = None   # only used in libcamera pipe mode
    proc = None           # the ffmpeg process (always present)

    def _start_stream():
        nonlocal capture_proc, proc
        if use_libcamera:
            cap_cmd, ff_cmd = build_libcamera_cmds(
                args.host, args.port, bitrate, fps, args.port2, args.slow, args.lossy,
            )
            capture_proc = subprocess.Popen(cap_cmd, stdout=subprocess.PIPE)
            proc = subprocess.Popen(ff_cmd, stdin=capture_proc.stdout, stdout=subprocess.PIPE)
            capture_proc.stdout.close()  # allow SIGPIPE if ffmpeg exits
        else:
            hw = not args.sw and has_hw_encoder() if IS_LINUX else False
            cmd = build_ffmpeg_cmd(device, args.host, args.port, bitrate, fps, args.port2, args.slow, hw, args.lossy)
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def _stop_stream():
        nonlocal capture_proc, proc
        if capture_proc:
            kill_proc(capture_proc)
            capture_proc = None
        if proc:
            kill_proc(proc)
            proc = None

    def _shutdown(sig, frame):
        print("\n[SERVER] Shutting down...")
        _stop_stream()
        if heartbeat:
            heartbeat.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        _start_stream()

        while True:
            # Check if ffmpeg died on its own
            ret = proc.poll()
            if ret is not None:
                if ret != 0:
                    print(f"\n[ERROR] ffmpeg exited with code {ret}")
                    if IS_MACOS:
                        print("  Tips:")
                        print("  - List cameras        : python3 server.py --list-devices")
                        print("  - Grant camera access : System Settings → Privacy → Camera → allow Terminal/iTerm")
                        print("  - Try lower res       : python3 server.py --slow")
                    else:
                        print("  Tips:")
                        print("  - List cameras        : python3 server.py --list-devices")
                        print("  - Try lower res       : python3 server.py --slow")
                        print("  - Try software encode : python3 server.py --sw")
                break

            # Keep-alive check
            if heartbeat and not heartbeat.is_alive():
                if heartbeat.last_seen > 0:
                    print("[SERVER] Client heartbeat lost — pausing stream...")
                    _stop_stream()
                    print("[SERVER] Waiting for client to reconnect...")
                    while not heartbeat.is_alive():
                        time.sleep(0.5)
                    print("[SERVER] Client reconnected — restarting stream.")
                    _start_stream()

            time.sleep(1)

    except FileNotFoundError as e:
        print(f"[ERROR] Required binary not found: {e.filename}")
        sys.exit(1)


if __name__ == "__main__":
    main()
