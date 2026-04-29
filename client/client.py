#!/usr/bin/env python3
"""
UDP Video Streaming Client — Receive & play (single-port multiplex)
====================================================================
Listens on a UDP port and plays the incoming MPEG-TS stream in real time.
The same port carries heartbeat ALIVE / PING-PONG packets — there is no
separate heartbeat channel, so a single nanoping/wg flow is enough.

Two consumers live in this codebase:
  - GUI (client_gui.py): UdpDemuxer (below) owns the listening socket
    and pipes MPEG-TS into ffmpeg's stdin. PING/PONG flows on the same
    socket → symmetric port pair, plays nicely with nanoping.
  - CLI (this file's main): hands the UDP socket to ffplay/ffmpeg, and
    runs HeartbeatSender from a separate ephemeral source port. Simple,
    works through plain WireGuard. For nanoping use the GUI.

Requirements:
    brew install ffmpeg        # macOS
    sudo apt install ffmpeg    # Linux

Usage:
    python3 client.py
    python3 client.py --server-host 10.0.0.1
    python3 client.py --server-host 10.0.0.1 --save out.mp4
    python3 client.py --no-play --save out.mp4
    python3 client.py --slow
    python3 client.py --no-keepalive
    python3 client.py --server-host 127.0.0.1 --no-keepalive
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

HEARTBEAT_INTERVAL = 2           # seconds between heartbeat packets
HEARTBEAT_MAGIC    = b"ALIVE"
TS_SYNC            = 0x47        # MPEG-TS packet sync byte
PKT_SIZE           = 1316        # 7 × 188-byte TS packets


# ── keep-alive sender (CLI: ephemeral source port, separate socket) ──────────

class HeartbeatSender:
    """
    Sends a small UDP packet to the server every few seconds.
    Server uses this to know the client is alive and ready to receive.
    In stats mode, sends PING:<timestamp_ms> and listens for PONG replies
    to measure round-trip time.
    """
    def __init__(self, host: str, port: int, stats: bool = False):
        self.host        = host
        self.port        = port
        self.stats       = stats
        self._sock       = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop_event = threading.Event()
        self._lock       = threading.Lock()
        self.rtt_last    = None   # ms
        self.rtt_min     = None
        self.rtt_max     = None
        self._rtt_sum    = 0.0
        self._rtt_count  = 0

    def start(self):
        threading.Thread(target=self._send_run, daemon=True).start()
        if self.stats:
            self._sock.settimeout(1.0)
            threading.Thread(target=self._recv_run, daemon=True).start()
            threading.Thread(target=self._print_run, daemon=True).start()
        mode = "PING/PONG (stats)" if self.stats else "ALIVE"
        print(f"[CLIENT] Sending heartbeats ({mode}) to {self.host}:{self.port} every {HEARTBEAT_INTERVAL}s")

    def _send_run(self):
        while not self._stop_event.is_set():
            try:
                if self.stats:
                    ts = int(time.time() * 1000)
                    self._sock.sendto(f"PING:{ts}".encode(), (self.host, self.port))
                else:
                    self._sock.sendto(HEARTBEAT_MAGIC, (self.host, self.port))
            except Exception:
                pass
            self._stop_event.wait(HEARTBEAT_INTERVAL)

    def _recv_run(self):
        while not self._stop_event.is_set():
            try:
                data, _ = self._sock.recvfrom(128)
                if data.startswith(b"PONG:"):
                    sent_ts = int(data[5:])
                    rtt = time.time() * 1000 - sent_ts
                    with self._lock:
                        self.rtt_last = rtt
                        self._rtt_count += 1
                        self._rtt_sum += rtt
                        if self.rtt_min is None or rtt < self.rtt_min:
                            self.rtt_min = rtt
                        if self.rtt_max is None or rtt > self.rtt_max:
                            self.rtt_max = rtt
            except socket.timeout:
                continue
            except Exception:
                break

    def _print_run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(5)
            if self._stop_event.is_set():
                break
            with self._lock:
                if self.rtt_last is not None:
                    avg = self._rtt_sum / self._rtt_count
                    print(f"[STATS] RTT: {self.rtt_last:.0f}ms  "
                          f"avg: {avg:.0f}ms  min: {self.rtt_min:.0f}ms  "
                          f"max: {self.rtt_max:.0f}ms  ({self._rtt_count} pings)")

    def stop(self):
        self._stop_event.set()
        try:
            self._sock.close()
        except Exception:
            pass


# ── single-socket demuxer (GUI: video + heartbeat share one port) ────────────

class UdpDemuxer:
    """
    Owns the listening UDP socket. Demultiplexes incoming datagrams:
      - first byte 0x47        → MPEG-TS, written to ffmpeg's stdin
      - prefix b"PONG:"        → RTT sample, exposed via rtt_* attrs
    Periodically sends ALIVE / PING:<ts> from the same socket. Because the
    server's reply comes back to the same (port, IP), the heartbeat path
    stays inside whatever single nanoping/wg flow already routes the video.

    Attribute names (rtt_last, rtt_min, rtt_max, _rtt_sum, _rtt_count, _lock)
    match HeartbeatSender so the GUI's stats overlay can read either.
    """
    def __init__(self, server_host: str, port: int, ff_stdin, *,
                 keepalive: bool = True, stats: bool = False):
        self.server_addr = (server_host, port)
        self.port        = port
        self.ff_stdin    = ff_stdin
        self.keepalive   = keepalive
        self.stats       = stats
        self._stop       = threading.Event()
        self._lock       = threading.Lock()
        self.rtt_last    = None
        self.rtt_min     = None
        self.rtt_max     = None
        self._rtt_sum    = 0.0
        self._rtt_count  = 0

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        except OSError:
            pass
        self._sock.bind(("0.0.0.0", port))
        self._sock.settimeout(1.0)

    def start(self):
        threading.Thread(target=self._recv_loop, daemon=True).start()
        # Stats also need periodic outbound packets — PING doubles as RTT probe
        # AND keepalive, so we run the ping loop whenever either is on.
        if self.keepalive or self.stats:
            threading.Thread(target=self._ping_loop, daemon=True).start()
        mode = "PING/PONG (stats)" if self.stats \
            else "ALIVE" if self.keepalive else "off"
        print(f"[CLIENT] UDP socket bound :{self.port}  heartbeat: {mode}")

    def _recv_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            if data[0] == TS_SYNC:
                # MPEG-TS datagram → forward to ffmpeg's stdin.
                try:
                    self.ff_stdin.write(data)
                    self.ff_stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    break
            elif data.startswith(b"PONG:") and self.stats:
                try:
                    sent_ts = int(data[5:])
                except ValueError:
                    continue
                rtt = time.time() * 1000 - sent_ts
                with self._lock:
                    first = self.rtt_last is None
                    self.rtt_last = rtt
                    self._rtt_count += 1
                    self._rtt_sum += rtt
                    if self.rtt_min is None or rtt < self.rtt_min:
                        self.rtt_min = rtt
                    if self.rtt_max is None or rtt > self.rtt_max:
                        self.rtt_max = rtt
                if first:
                    print(f"[CLIENT] first PONG received — RTT {rtt:.0f}ms (return path OK)")

    def _ping_loop(self):
        sent = 0
        while not self._stop.is_set():
            try:
                if self.stats:
                    ts = int(time.time() * 1000)
                    self._sock.sendto(f"PING:{ts}".encode(), self.server_addr)
                else:
                    self._sock.sendto(HEARTBEAT_MAGIC, self.server_addr)
                sent += 1
                # Periodic visibility so the user can tell PINGs are leaving
                # even when no PONGs come back (return path broken / nanoping
                # one-way wiring).
                if self.stats and sent % 5 == 1:
                    print(f"[CLIENT] sent {sent} PING{'s' if sent != 1 else ''} "
                          f"to {self.server_addr[0]}:{self.server_addr[1]} "
                          f"(rtt_last={self.rtt_last})")
            except Exception:
                pass
            self._stop.wait(HEARTBEAT_INTERVAL)

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass


# ── ffmpeg/ffplay command builders ────────────────────────────────────────────

def ffplay_cmd(port: int, slow: bool) -> list:
    extra = ["-fflags", "nobuffer", "-flags", "low_delay"] if not slow else []

    return [
        "ffplay",
        "-loglevel",        "error",             # suppress PPS/SPS warnings during stream join
        "-probesize",       "10M",
        "-analyzeduration", "2000000",
        *extra,
        "-fflags",          "+discardcorrupt",   # silently skip corrupt packets
        "-sync",            "ext",
        "-framedrop",                            # drop late frames instead of freezing
        "-max_delay",       "500000" if slow else "300000",   # µs — 300ms absorbs Starlink/mobile jitter
        "-window_title",    "Live Camera Stream",
        # buffer_size raises the kernel SO_RCVBUF (default ~200KB on macOS) so Starlink
        # handoff bursts don't overflow the socket. fifo_size is ffmpeg's internal ring.
        f"udp://0.0.0.0:{port}?overrun_nonfatal=1&fifo_size=50000000&buffer_size=65536000",
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
        description="UDP stream client (CLI) — heartbeat shares the video port"
    )
    parser.add_argument("--port",     type=int, default=5000,
                        help="UDP port to listen on (default: 5000). Must match server's --port.")
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
    parser.add_argument("--server-port", type=int, default=None,
                        help="Heartbeat destination port on the server (default: --port). Override only if the server's --bind-port differs from --port.")
    parser.add_argument("--stats", action="store_true",
                        help="Enable RTT measurement via heartbeat PING/PONG (use with --stats on server)")
    args = parser.parse_args()

    if args.no_play and not args.save:
        print("[ERROR] --no-play requires --save (nothing to do otherwise).")
        sys.exit(1)

    check_deps(play=not args.no_play)

    server_port = args.server_port if args.server_port is not None else args.port

    print()
    print("=" * 56)
    print("  UDP Stream Client (CLI)")
    print("=" * 56)
    print(f"  Listening  : udp://0.0.0.0:{args.port}")
    print(f"  Playback   : {'no' if args.no_play else 'yes (ffplay window)'}")
    print(f"  Save to    : {args.save or 'no'}")
    print(f"  Slow mode  : {args.slow}")
    print(f"  Keep-alive : {'disabled' if args.no_keepalive else f'sending to {args.server_host}:{server_port}'}")
    print(f"  Stats      : {'enabled (RTT measurement)' if args.stats else 'off'}")
    if args.save and not args.no_play:
        print(f"  Save port  : {args.port + 1}  (server must use --port2 {args.port + 1})")
    print(f"  Waiting for server stream... (Ctrl+C to stop)")
    print("=" * 56)
    print()

    # ── start heartbeat ──
    # Stats mode sends PINGs even with --no-keepalive (PING doubles as RTT probe).
    heartbeat = None
    if not args.no_keepalive or args.stats:
        heartbeat = HeartbeatSender(args.server_host, server_port, stats=args.stats)
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
