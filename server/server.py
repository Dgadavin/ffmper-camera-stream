#!/usr/bin/env python3
"""
UDP Video Streaming Server — Webcam capture (single-port multiplex)
====================================================================
Captures the local webcam and streams MPEG-TS over UDP. A single UDP
socket carries both the video datagrams and the heartbeat PING/PONG —
no second port required, friendly to nanoping/wg flow rules.

Auto-detects platform:
  - macOS:           AVFoundation capture
  - Linux (USB cam): V4L2 capture
  - Linux (Pi cam):  libcamera/rpicam-vid (GPU H.264)

Requirements:
    macOS:  brew install ffmpeg
    Linux:  sudo apt install ffmpeg

Usage:
    python3 server.py                                # auto-detect, stream to 10.0.0.2:5000
    python3 server.py --host 10.0.0.2                # explicit peer IP
    python3 server.py --device 1                     # pick camera
    python3 server.py --list-devices                 # show all available cameras
    python3 server.py --slow                         # slow-network mode
    python3 server.py --host 10.0.0.2 --port2 5001   # also fan out to a second port
    python3 server.py --bind-port 5005 --no-keepalive  # localhost test (avoid bind clash)
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

HEARTBEAT_TIMEOUT = 8           # pause stream after N seconds of client silence
HEARTBEAT_MAGIC   = b"ALIVE"
TS_SYNC           = 0x47        # MPEG-TS packet sync byte
PKT_SIZE          = 1316        # 7 × 188-byte TS packets — fits inside any tunnel MTU


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
        if has_libcamera():
            print(f"[SERVER] Pi camera detected (via {libcamera_bin()})")
            return "libcamera:0"

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
    # Lossy always pulls keyframe interval down so Starlink/LTE can recover
    # within ~0.5s, independent of resolution. Slow picks resolution; the
    # two flags are orthogonal.
    resolution = "640x480" if slow else "1280x720"
    if lossy:
        keyframe_interval = max(framerate // 2, 1)     # ~0.5s
    elif slow:
        keyframe_interval = framerate * 2              # ~2s — saves bitrate
    else:
        keyframe_interval = framerate                  # ~1s
    return resolution, keyframe_interval


def build_libcamera_cmds(
    bitrate: str,
    framerate: int,
    slow: bool,
    lossy: bool = False,
):
    """
    Build a (rpicam-vid command, ffmpeg command) pair.
    rpicam-vid captures + H.264 encodes on the Pi GPU, outputs to stdout.
    ffmpeg reads the raw H.264 from stdin and repackages as MPEG-TS on stdout
    (so the Python forwarder can chunk the byte stream into UDP datagrams).
    """
    video_size, keyframe_interval = _resolve_video_params(slow, framerate, lossy)
    width, height = video_size.split("x")

    # Convert bitrate string like "2000k" to bits/s for rpicam-vid
    bitrate_bps = bitrate.replace("k", "000").replace("M", "000000")

    capture_cmd = [
        libcamera_bin(),
        "--codec", "h264",
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

    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostats",
        "-f", "mpegts",
        "-i", "pipe:0",      # read MPEG-TS from stdin
        "-c:v", "copy",      # no re-encode — already H.264 from the Pi GPU
        # Inline SPS/PPS before every keyframe. On Pi 5 rpicam-vid uses libav/libx264
        # where --inline is a no-op, so headers otherwise live only in extradata and
        # late-joining UDP receivers never see them → "non-existing PPS" on decode.
        "-bsf:v", "dump_extra=freq=keyframe",
        "-an",
        "-mpegts_flags", "+resend_headers",   # repeat PAT/PMT frequently
        "-muxrate", "0",                       # VBR — don't pad with nulls
        "-muxdelay", "0",                      # no muxer buffering (default 0.7s!)
        "-muxpreload", "0",
        "-flush_packets", "1",
        "-map", "0:v",
        "-f", "mpegts",
        "pipe:1",            # MPEG-TS to stdout — the forwarder ships it as UDP
    ]

    return capture_cmd, ffmpeg_cmd


def build_ffmpeg_cmd(
    device: str,
    bitrate: str,
    framerate: int,
    slow: bool,
    hw_encode: bool,
    lossy: bool = False,
) -> list:
    video_size, keyframe_interval = _resolve_video_params(slow, framerate, lossy)

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
        # Hardware encoders don't always emit SPS/PPS in-band; force it so
        # post-loss recovery doesn't wait for a stream restart.
        bsf_args = ["-bsf:v", "dump_extra=freq=keyframe"]
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
        if lossy:
            # slice-max-size: each slice (NAL) fits in one UDP datagram of our
            # PKT_SIZE=1316, so a single lost packet damages one horizontal strip
            # instead of the whole frame.
            encode_args += ["-x264-params", "slice-max-size=1300"]
        bsf_args = []

    return [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostats",
        *input_args,
        *encode_args,
        *bsf_args,
        "-r", str(framerate),
        "-an",
        # Resend PAT/PMT on every keyframe so a late-joining or post-loss client
        # can recover the container structure without waiting for the next cycle.
        "-mpegts_flags", "+resend_headers",
        "-muxrate", "0",          # VBR — don't pad with null packets
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-flush_packets", "1",
        "-map", "0:v",
        "-f", "mpegts",
        "pipe:1",
    ]


# ── single-port video forwarder + heartbeat responder ────────────────────────

class VideoUdpForwarder:
    """
    Owns one UDP socket that does double duty:
      - sends MPEG-TS datagrams (read from ffmpeg's stdout) to each destination
      - receives ALIVE / PING:<ts> heartbeats and replies PONG:<ts> on the same socket

    The control loop runs continuously; the video pump can be attached/detached
    so a paused stream (client disconnected) can release ffmpeg without
    tearing down the socket.
    """
    def __init__(self, bind_port: int, destinations: list):
        self.bind_port    = bind_port
        self.destinations = destinations  # list of (host, port)
        self.last_seen    = 0.0
        self._stop_ctrl   = threading.Event()
        self._stop_video  = threading.Event()
        self._sock        = None
        self._video_thread = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        except OSError:
            pass
        self._sock.bind(("0.0.0.0", self.bind_port))
        self._sock.settimeout(1.0)
        threading.Thread(target=self._control_loop, daemon=True).start()
        dests = ", ".join(f"{h}:{p}" for h, p in self.destinations)
        print(f"[SERVER] UDP socket on :{self.bind_port}  →  {dests}")

    def attach_video(self, ff_stdout):
        self._stop_video.clear()
        self._video_thread = threading.Thread(
            target=self._video_pump, args=(ff_stdout,), daemon=True,
        )
        self._video_thread.start()

    def detach_video(self):
        self._stop_video.set()
        if self._video_thread:
            self._video_thread.join(timeout=2)
            self._video_thread = None

    def _video_pump(self, ff_stdout):
        # MPEG-TS muxer emits 188-byte aligned packets; we accumulate until we
        # have PKT_SIZE bytes (7 packets) before flushing one UDP datagram.
        # read1 returns whatever's available in the underlying syscall — no
        # waiting for a full PKT_SIZE worth of bytes to land in BufferedReader.
        buf = b""
        while not self._stop_video.is_set():
            try:
                chunk = ff_stdout.read1(PKT_SIZE - len(buf))
            except (ValueError, OSError):
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= PKT_SIZE:
                for dst in self.destinations:
                    try:
                        self._sock.sendto(buf[:PKT_SIZE], dst)
                    except Exception:
                        pass
                buf = buf[PKT_SIZE:]

    def _control_loop(self):
        while not self._stop_ctrl.is_set():
            try:
                data, addr = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data == HEARTBEAT_MAGIC:
                if self.last_seen == 0:
                    print(f"[SERVER] Client connected from {addr[0]}:{addr[1]}")
                self.last_seen = time.time()
            elif data.startswith(b"PING:"):
                if self.last_seen == 0:
                    print(f"[SERVER] Client connected from {addr[0]}:{addr[1]}")
                self.last_seen = time.time()
                try:
                    self._sock.sendto(b"PONG:" + data[5:], addr)
                except Exception:
                    pass

    def is_alive(self) -> bool:
        if self.last_seen == 0:
            return False
        return (time.time() - self.last_seen) < HEARTBEAT_TIMEOUT

    def stop(self):
        self._stop_ctrl.set()
        self._stop_video.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


# ── main ──────────────────────────────────────────────────────────────────────

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


def main():
    parser = argparse.ArgumentParser(
        description="Webcam → UDP stream server (single-port: video + heartbeat multiplexed)"
    )
    parser.add_argument("--list-devices", action="store_true",
                        help="List available camera devices and exit.")
    parser.add_argument("--device",   default=None,
                        help="Camera device. macOS: AVFoundation index (0, 1). Linux: V4L2 path (/dev/video0) or 'libcamera:0'. Auto-detected if not set.")
    parser.add_argument("--host",     default="10.0.0.2",
                        help="Client IP to stream to (default: 10.0.0.2)")
    parser.add_argument("--port",     type=int, default=5000,
                        help="UDP destination port on the client (default: 5000)")
    parser.add_argument("--port2",    type=int, default=None,
                        help="Optional second destination port for fan-out (e.g. play+save on the client)")
    parser.add_argument("--bind-port", type=int, default=None,
                        help="Local UDP bind port (default: --port). Override only if you need a different bind, e.g. localhost test.")
    parser.add_argument("--bitrate",  default=None,
                        help="Video bitrate. Defaults: 2000k normal, 1200k lossy, 600k slow.")
    parser.add_argument("--fps",      type=int, default=30,
                        help="Frames per second (default: 30; use 15 for slow links)")
    parser.add_argument("--slow",     action="store_true",
                        help="Slow-network mode: 640x480, 600k bitrate, 15fps, more keyframes")
    parser.add_argument("--no-keepalive", action="store_true",
                        help="Disable keep-alive (stream even if client is not responding)")
    parser.add_argument("--sw", action="store_true",
                        help="Force software encoding (libx264) even when hardware is available")
    parser.add_argument("--lossy", action="store_true",
                        help="Lossy-network mode (Starlink, LTE): smaller UDP packets, more frequent keyframes")
    args = parser.parse_args()

    check_ffmpeg()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    # Per-mode defaults. Lossy gets a lower bitrate than normal — fewer packets
    # per second means fewer chances for Starlink/LTE handoff bursts to destroy one.
    if args.slow:
        fps     = args.fps     if args.fps != 30 else 15
        bitrate = args.bitrate or "600k"
    elif args.lossy:
        fps     = args.fps
        bitrate = args.bitrate or "1200k"
    else:
        fps     = args.fps
        bitrate = args.bitrate or "2000k"

    device = args.device or detect_default_camera()
    bind_port = args.bind_port if args.bind_port is not None else args.port

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

    destinations = [(args.host, args.port)]
    if args.port2:
        destinations.append((args.host, args.port2))

    print()
    print("=" * 56)
    print(f"  UDP Webcam Streaming Server  [{plat_tag}]")
    print("=" * 56)
    print(f"  Camera     : {device}")
    print(f"  Encoder    : {enc_tag}")
    dests_str = " + ".join(f"{h}:{p}" for h, p in destinations)
    print(f"  Target     : udp://{dests_str}  (bound :{bind_port})")
    print(f"  Bitrate    : {bitrate}   FPS: {fps}   Slow mode: {args.slow}")
    print(f"  Keep-alive : {'disabled' if args.no_keepalive else f'enabled (timeout {HEARTBEAT_TIMEOUT}s, same socket)'}")
    print(f"  Lossy mode : {'ON (small pkts + frequent keyframes)' if args.lossy else 'off'}")
    print(f"  Press Ctrl+C to stop.")
    print("=" * 56)
    print()

    # ── socket up first; ffmpeg comes online once a client is alive ──
    forwarder = VideoUdpForwarder(bind_port, destinations)
    forwarder.start()

    if not args.no_keepalive:
        print("[SERVER] Waiting for client heartbeat before starting stream...")
        while not forwarder.is_alive():
            time.sleep(0.5)
        print("[SERVER] Client alive — starting stream.")

    # ── process management ──
    capture_proc = None   # only used in libcamera pipe mode
    proc = None           # the ffmpeg process (always present)

    def _start_stream():
        nonlocal capture_proc, proc
        if use_libcamera:
            cap_cmd, ff_cmd = build_libcamera_cmds(bitrate, fps, args.slow, args.lossy)
            capture_proc = subprocess.Popen(cap_cmd, stdout=subprocess.PIPE)
            proc = subprocess.Popen(ff_cmd, stdin=capture_proc.stdout, stdout=subprocess.PIPE)
            capture_proc.stdout.close()  # let SIGPIPE propagate if ffmpeg exits
        else:
            hw = not args.sw and has_hw_encoder() if IS_LINUX else False
            cmd = build_ffmpeg_cmd(device, bitrate, fps, args.slow, hw, args.lossy)
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        forwarder.attach_video(proc.stdout)

    def _stop_stream():
        nonlocal capture_proc, proc
        # Kill capture (rpicam-vid) first so ffmpeg sees stdin EOF and exits cleanly,
        # then kill ffmpeg, then join the forwarder pump (it'll drop out on EOF).
        if capture_proc:
            kill_proc(capture_proc)
            capture_proc = None
        if proc:
            kill_proc(proc)
            proc = None
        forwarder.detach_video()

    def _shutdown(sig, frame):
        print("\n[SERVER] Shutting down...")
        _stop_stream()
        forwarder.stop()
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
            if not args.no_keepalive and not forwarder.is_alive():
                if forwarder.last_seen > 0:
                    print("[SERVER] Client heartbeat lost — pausing stream...")
                    _stop_stream()
                    print("[SERVER] Waiting for client to reconnect...")
                    while not forwarder.is_alive():
                        time.sleep(0.5)
                    print("[SERVER] Client reconnected — restarting stream.")
                    _start_stream()

            time.sleep(1)

    except FileNotFoundError as e:
        print(f"[ERROR] Required binary not found: {e.filename}")
        sys.exit(1)


if __name__ == "__main__":
    main()
