# Webcam Stream over WireGuard

Stream your webcam from a Raspberry Pi or macOS machine to a remote client over an encrypted WireGuard tunnel. Supports both direct connections and a hub topology (via EC2/VPS) for connecting machines behind NAT. Transport is plain UDP MPEG-TS — video and heartbeat/RTT control share a **single UDP port** so a one-flow tunnel (WireGuard, nanoping, etc.) is enough.

```
Direct mode:
[Server — RPi/macOS]            WireGuard Tunnel            [Client — macOS/Windows]
  Webcam → ffmpeg  ── UDP / MPEG-TS over encrypted VPN ─→ GUI → Screen
  10.0.0.1                                                  10.0.0.2

Hub mode (EC2 relay):
[Server — RPi/macOS]      [Hub — EC2/VPS]      [Client — macOS/Windows]
  Webcam → ffmpeg  ─────→   10.0.0.1     ─────→    GUI → Screen
  10.0.0.2                 (IP forwarding)           10.0.0.3
```

---

## Project Structure

```
├── server/
│   ├── server.py           # Camera capture, single-port video + heartbeat forwarder
│   └── requirements.txt    # No pip deps (system ffmpeg only)
├── client/
│   ├── client.py           # Stream library: HeartbeatSender (CLI) + UdpDemuxer (GUI)
│   ├── client_gui.py       # PyQt6 GUI application
│   └── requirements.txt    # PyQt6
├── wg-setup.sh             # WireGuard VPN setup script
└── README.md
```

---

## Requirements

**Server (Raspberry Pi):**
```bash
sudo apt install ffmpeg
# rpicam-vid is pre-installed on Raspberry Pi OS
```

**Server (macOS):**
```bash
brew install ffmpeg
```

**Client (macOS / Windows):**
```bash
# macOS
brew install ffmpeg

# Windows
# Download ffmpeg from ffmpeg.org and add to PATH

# Both platforms — install Python dependencies:
cd client
pip install -r requirements.txt
```

---

## Quick Start

### 1. WireGuard Setup (skip for LAN testing)

See the [WireGuard Setup](#wireguard-setup) section below.

### 2. Start the Server (Raspberry Pi)

```bash
cd server
python3 server.py --host <CLIENT_IP> --no-keepalive
```

### 3. Start the Client GUI

```bash
cd client
python3 client_gui.py
```

1. Click **+ Add** to create a new device
2. Enter a name, the server's IP address, port, and options
3. **Double-click** the device to connect — the video stream appears in the right panel

---

## Localhost Test (no WireGuard)

The GUI binds the listening port locally, so the server can't bind the same one. Pass `--bind-port 0` so the server uses an ephemeral source port:

```bash
# Terminal 1 — client
cd client
python3 client_gui.py
# Add a device with IP: 127.0.0.1, port: 5000, then double-click it

# Terminal 2 — server
cd server
python3 server.py --host 127.0.0.1 --bind-port 0 --no-keepalive
```

---

## Server Options

| Flag | Default | Description |
|---|---|---|
| `--host` | `10.0.0.2` | Client IP to stream to |
| `--port` | `5000` | UDP destination port on the client |
| `--port2` | off | Optional second destination port (forwarder fan-out) |
| `--bind-port` | same as `--port` | Local UDP bind port. Set to `0` for ephemeral when something else (nanoping, an old instance) already holds `--port`. |
| `--device` | auto | Camera device (AVFoundation index on macOS, `/dev/videoN` or `libcamera:0` on Linux) |
| `--list-devices` | — | Print available cameras and exit |
| `--bitrate` | `2000k` / `1200k` / `600k` | Video bitrate (default / lossy / slow) |
| `--fps` | `30` / `15` | Frames per second (slow mode forces 15 unless overridden) |
| `--slow` | off | Slow-network mode (640x480, 600k, 15fps, 2s keyframes) |
| `--no-keepalive` | off | Don't pause the stream when the client goes silent. The forwarder still answers PINGs — RTT stats keep working. |
| `--sw` | off | Force software encoding (skip Pi hardware encoder) |
| `--lossy` | off | Lossy-network mode: 1200k bitrate, keyframes every 0.5s, one slice per UDP packet so a dropped packet damages only a strip of the frame (Starlink, LTE) |

---

## Client GUI Features

- **Device management** — Add, edit, delete camera devices (saved to `devices.json`)
- **Embedded video** — Stream renders directly in the application window
- **RTT stats overlay** — Tick "Show RTT stats" per device to see latency on the video. Works independently of keepalive — `PING` doubles as the RTT probe.
- **Options per device** — keepalive, slow network mode, lossy network mode, RTT stats
- **Cross-platform** — macOS and Windows (PyQt6)

Each server-side flag (`--slow`, `--lossy`) has a matching per-device checkbox in the GUI. Settings on the two ends should agree (e.g. enabling "Slow network mode" on the device makes sense when the server is started with `--slow` so the bitrate/jitter buffer pair).

---

## Streaming Modes

Pick a mode based on what's wrong with your network. `--slow` addresses bandwidth; `--lossy` addresses packet loss. They combine.

| Mode | Resolution | Bitrate | FPS | Keyframe | Use when |
|---|---|---|---|---|---|
| Default | 1280×720 | 2000k | 30 | 1s | LAN / good Wi-Fi |
| `--slow` | 640×480 | 600k | 15 | 2s | Narrow uplink (3G, DSL) |
| `--lossy` | 1280×720 | 1200k | 30 | 0.5s | Packet loss (Starlink, LTE) |
| `--slow --lossy` | 640×480 | 600k | 15 | 0.5s | Both bandwidth and loss |

On the client side, the matching per-device checkboxes enlarge the jitter buffer (slow / lossy both → 500ms, default → 300ms).

---

## Single-port multiplex (heartbeat shares the video port)

Video MPEG-TS datagrams and heartbeat `PING`/`PONG` ride the **same UDP socket**. The first byte tells them apart:

- `0x47` (MPEG-TS sync) → write to ffmpeg's stdin for decoding
- ASCII `P` (`PING:` / `PONG:` / `ALIVE`) → control plane

This means a tunnel only has to forward one UDP flow per direction — no separate heartbeat port. Useful for NAT, simpler WireGuard rules, and minimal nanoping configurations.

### Roles

| Side | Component | Owns the socket |
|---|---|---|
| Server | `VideoUdpForwarder` ([server/server.py](server/server.py)) | Pumps ffmpeg stdout to UDP datagrams; replies `PONG:<ts>` to incoming `PING:<ts>` |
| Client (GUI) | `UdpDemuxer` ([client/client.py](client/client.py)) | Listens on the video port; forwards TS to ffmpeg stdin; sends `PING`/`ALIVE` from the same socket |
| Client (CLI) | `HeartbeatSender` ([client/client.py](client/client.py)) | Hands the listen port to ffplay; runs heartbeat from a separate ephemeral source port |

### Stats vs. keepalive

These are independent now:

| Settings | Effect |
|---|---|
| keepalive on, stats off | Client sends `ALIVE` every 2s. Server (without `--no-keepalive`) pauses the stream after 8s of silence. |
| keepalive off, stats on | Client sends `PING:<ts>`; server replies `PONG:<ts>`. RTT shows up in the overlay. Server doesn't pause if PONGs go missing. |
| both on | `PING` doubles as keepalive — same behaviour as stats-on. |
| both off | No control traffic. Server (with `--no-keepalive`) just streams blindly. |

---

## WireGuard Setup

### Option A — Hub mode (recommended for internet)

Use when machines are in different locations / behind NAT.

#### 1. Hub (EC2)

```bash
sudo bash wg-setup.sh --role hub
```
Copy the **hub public key**. Open port **51820/udp** in EC2 security group.

#### 2. Server (Pi / Mac with webcam)

```bash
sudo bash wg-setup.sh --role server --hub-ip <EC2_PUBLIC_IP>
```
Paste the hub public key when prompted. Copy the **server public key**.

#### 3. Client (viewer machine)

```bash
sudo bash wg-setup.sh --role client --hub-ip <EC2_PUBLIC_IP>
```
Paste the hub public key when prompted. Copy the **client public key**.

#### 4. Back on the Hub — add peers

Edit `/etc/wireguard/wg0.conf` and add:
```ini
[Peer]
PublicKey  = <SERVER_PUBLIC_KEY>
AllowedIPs = 10.0.0.2/32

[Peer]
PublicKey  = <CLIENT_PUBLIC_KEY>
AllowedIPs = 10.0.0.3/32
```
Reload: `sudo wg syncconf wg0 <(sudo wg-quick strip wg0)`

### Option B — Direct mode (LAN or public IP)

```bash
# Server
sudo bash wg-setup.sh --role server-direct

# Client
sudo bash wg-setup.sh --role client-direct --server-ip <SERVER_PUBLIC_IP>
```
Then add the client peer on the server as described above.

---

## Ports

| Port | Purpose |
|---|---|
| `5000` | Video stream **+ heartbeat/RTT** (single-port mux) |
| `5001` | Optional second video destination when using `--port2` |
| `51820` | WireGuard handshake (UDP) |

---

## Architecture

- **Transport**: UDP unicast (fire-and-forget). Server pushes MPEG-TS datagrams to client IP.
- **Container**: MPEG-TS — no seeking needed, resilient to packet loss, self-syncing on the `0x47` byte (also what lets us multiplex with PING/PONG).
- **Codec**: H.264 — Pi GPU encoder (rpicam-vid) or libx264 ultrafast+zerolatency. In `--lossy` mode libx264 emits one slice per UDP packet (`slice-max-size=1300`) so a dropped packet damages a single horizontal strip instead of the whole frame.
- **Server pipeline**: ffmpeg writes MPEG-TS to `pipe:1`; the Python forwarder chunks the byte stream into 1316-byte UDP datagrams (7 × 188-byte TS packets, fits inside any tunnel MTU) and ships them to one or more destinations.
- **Client pipeline (GUI)**: a Python demuxer owns the UDP socket, feeds video bytes into ffmpeg's stdin, and answers PINGs on the same socket. ffmpeg decodes to raw RGB → PyQt6 QLabel.
- **Encryption**: WireGuard (ChaCha20-Poly1305) wraps all traffic.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Address already in use` on bind | Another process holds the port — old `server.py`, nanoping, or another tunnel. `sudo lsof -i :<port>` to find it; or pass `--bind-port 0` to use an ephemeral port. |
| Server waits forever | Client heartbeat not reaching — check IP/port, or use `--no-keepalive`. |
| RTT overlay shows `— ms (waiting for PONG)` | The PING return path isn't working. Check the log line `[CLIENT] sent N PINGs ... (rtt_last=None)` to confirm PINGs are leaving; if so, the tunnel doesn't route the reverse direction back to the server's bind port. |
| No camera found | `python3 server/server.py --list-devices` |
| Camera permission denied | System Settings → Privacy → Camera → enable Terminal |
| Stream freezes | Try `--slow --lossy` on server + matching checkboxes on client |
| Tunnel not working | `wg show` on both machines; `ping 10.0.0.1` from client |
| Pi camera VIDIOC error | Ensure rpicam-vid is installed; don't use `--device /dev/video0` for Pi camera module |
| GUI blank/no text (macOS) | Ensure PyQt6 is installed: `pip install PyQt6` |
| `non-existing PPS 0 referenced` / block artifacts | Packet loss is damaging IDRs. Add `--lossy` on the server to shrink keyframe interval and slice size. |
