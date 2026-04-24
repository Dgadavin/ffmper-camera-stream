# Webcam Stream over WireGuard

Stream your webcam from a Raspberry Pi or macOS machine to a remote client over an encrypted WireGuard tunnel. Supports both direct connections and a hub topology (via EC2/VPS) for connecting machines behind NAT. Transport is UDP by default or **SRT** for links with packet loss (Starlink, LTE).

```
Direct mode:
[Server — RPi/macOS]            WireGuard Tunnel            [Client — macOS/Windows]
  Webcam → ffmpeg  ── UDP or SRT / MPEG-TS over encrypted VPN ─→ GUI → Screen
  10.0.0.1                                                        10.0.0.2

Hub mode (EC2 relay):
[Server — RPi/macOS]      [Hub — EC2/VPS]      [Client — macOS/Windows]
  Webcam → ffmpeg  ─────→   10.0.0.1     ─────→    GUI → Screen
  10.0.0.2                 (IP forwarding)           10.0.0.3
```

---

## Project Structure

```
├── server/
│   ├── server.py           # Camera capture & UDP streaming server
│   └── requirements.txt    # No pip deps (system ffmpeg only)
├── client/
│   ├── client.py           # Stream library (heartbeat, ffplay commands)
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

```bash
# Terminal 1 — client
cd client
python3 client_gui.py
# Add a device with IP: 127.0.0.1, then double-click it

# Terminal 2 — server
cd server
python3 server.py --host 127.0.0.1 --no-keepalive
```

---

## Server Options

| Flag | Default | Description |
|---|---|---|
| `--host` | `10.0.0.2` | Client IP to stream to |
| `--port` | `5000` | UDP destination port |
| `--port2` | off | Second port for play+save mode |
| `--device` | auto | Camera device (AVFoundation index on macOS, `/dev/videoN` or `libcamera:0` on Linux) |
| `--list-devices` | — | Print available cameras and exit |
| `--bitrate` | `2000k` / `600k` | Video bitrate |
| `--fps` | `30` / `15` | Frames per second |
| `--slow` | off | Slow-network mode (640x480, 600k, 15fps) |
| `--no-keepalive` | off | Disable heartbeat listener |
| `--heartbeat-port` | `5010` | Port to receive heartbeats on |
| `--stats` | off | Enable PING/PONG RTT measurement |
| `--sw` | off | Force software encoding (skip Pi hardware encoder) |
| `--lossy` | off | Lossy-network mode: 1200k bitrate, keyframes every 0.5s, one slice per UDP packet so a dropped packet damages only a strip of the frame (Starlink, LTE) |
| `--srt` | off | Use SRT instead of raw UDP — retransmits lost packets within a latency budget. Client must also enable SRT. Requires ffmpeg built with libsrt. |
| `--srt-latency` | `500` | SRT retransmission budget in ms. Raise to 1000+ if you still see decoder errors on Starlink handoffs. |

---

## Client GUI Features

- **Device management** — Add, edit, delete camera devices (saved to `devices.json`)
- **Embedded video** — Stream renders directly in the application window
- **RTT stats overlay** — Enable "Show RTT stats" per device to see latency on the video (server must also use `--stats`)
- **Options per device** — keepalive, slow network mode, lossy network mode, SRT transport, RTT stats
- **Cross-platform** — macOS and Windows (PyQt6)

Each server-side flag (`--slow`, `--lossy`, `--srt`) has a matching per-device checkbox in the GUI. The two must agree: enabling "Use SRT" on the device requires the server to be started with `--srt`, otherwise the handshake fails.

---

## Streaming Modes

Pick a mode based on what's wrong with your network. `--slow` addresses bandwidth; `--lossy` addresses packet loss; `--srt` eliminates packet loss by retransmission. They combine.

| Mode | Resolution | Bitrate | FPS | Keyframe | Use when |
|---|---|---|---|---|---|
| Default | 1280×720 | 2000k | 30 | 1s | LAN / good Wi-Fi |
| `--slow` | 640×480 | 600k | 15 | 2s | Narrow uplink (3G, DSL) |
| `--lossy` | 1280×720 | 1200k | 30 | 0.5s | Packet loss (Starlink, LTE) |
| `--slow --lossy` | 640×480 | 600k | 15 | 0.5s | Both bandwidth and loss |
| `+ --srt` | (unchanged) | (unchanged) | (unchanged) | (unchanged) | Retransmits lost packets — see next section |

On the client side, the matching per-device checkboxes in the GUI enlarge the jitter buffer (slow / lossy both → 500ms, default → 300ms) and switch transport protocol (SRT).

---

## Starlink / Lossy Networks (SRT)

Raw UDP over Starlink produces visible decode artifacts ("non-existing PPS 0 referenced", green/brown blocks, "Invalid level prefix") whenever a satellite handoff drops packets. **SRT solves this** by retransmitting lost packets within a latency budget (~500ms default). The decoder never sees a loss.

### Installing libsrt

Both ends need ffmpeg built with **libsrt**. Verify with:

```bash
ffmpeg -hide_banner -protocols | grep -w srt
```

A single `srt` line (not `srtp`) means it's supported.

**macOS** — the default Homebrew formula does not include SRT. Either:

- Install a prebuilt static binary with libsrt from [evermeet.cx/ffmpeg](https://evermeet.cx/ffmpeg/) and put it ahead of Homebrew's on `PATH`, or
- Rebuild from the homebrew-ffmpeg tap: `brew tap homebrew-ffmpeg/ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-srt` (needs current Xcode Command Line Tools).

**Linux / Raspberry Pi** — recent `apt install ffmpeg` already includes libsrt.

### Usage

Start the **client first** (it's the SRT listener), then the server. If the server runs first, its caller will hit "Input/output error" because nothing is listening.

```bash
# Client (GUI): edit device → tick "Use SRT (reliable transport)" → double-click to connect
python3 client_gui.py

# Server (after client is connected):
python3 server.py --host <CLIENT_IP> --srt
```

To tune the retransmission window for particularly bad satellite handoffs:
```bash
python3 server.py --host <CLIENT_IP> --srt --srt-latency 1000
```
Higher latency = more recovery headroom = more end-to-end delay. For a one-way camera feed, 500–1000ms is imperceptible.

---

## Keep-alive

The client sends a UDP heartbeat to the server every 2 seconds. The server waits for the first heartbeat before starting the stream, and pauses if heartbeats stop for 8 seconds. When the client reconnects, the stream resumes automatically.

In SRT mode, the GUI client delays the first heartbeat by ~1s to ensure its SRT listener has bound the port before the server's caller tries to connect.

To disable (e.g. for quick tests), use `--no-keepalive` on the server and uncheck "Send keepalive" on the client device.

Ports used (same for UDP and SRT — SRT runs on top of UDP):

- `5000` — video stream
- `5001` — video stream (save, when using `--port2`)
- `5010` — heartbeat channel

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

## Architecture

- **Transport**: UDP unicast by default (fire-and-forget), or SRT (retransmission within a latency budget) when both ends opt in. Server pushes to client IP; with SRT the server is the caller and the client is the listener.
- **Container**: MPEG-TS — no seeking needed, resilient to packet loss
- **Codec**: H.264 — Pi GPU encoder (rpicam-vid) or libx264 ultrafast+zerolatency. In `--lossy` mode libx264 emits one slice per UDP packet (`slice-max-size=1300`) so a dropped packet damages a single horizontal strip instead of the whole frame.
- **Keep-alive**: UDP heartbeat side-channel on port 5010. The GUI client waits to send the first heartbeat until its SRT listener is bound, so the server's caller doesn't race ahead.
- **Encryption**: WireGuard (ChaCha20-Poly1305) wraps all traffic
- **Client rendering**: ffmpeg decodes stream → raw RGB frames → PyQt6 QLabel

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Server waits forever | Client heartbeat not reaching — check IP, or use `--no-keepalive` |
| No camera found | `python3 server/server.py --list-devices` |
| Camera permission denied | System Settings → Privacy → Camera → enable Terminal |
| Stream freezes | Try `--slow` on server + "Slow network mode" on client |
| `Address already in use` | `pkill -f ffmpeg` |
| Tunnel not working | `wg show` on both machines; `ping 10.0.0.1` from client |
| Pi camera VIDIOC error | Ensure rpicam-vid is installed; don't use `--device /dev/video0` for Pi camera module |
| GUI blank/no text (macOS) | Ensure PyQt6 is installed: `pip install PyQt6` |
| `non-existing PPS 0 referenced` / block artifacts | Packet loss is damaging IDRs. Add `--lossy` on the server, or switch to `--srt` to eliminate loss entirely. |
| `Invalid level prefix` / `error while decoding MB` with SRT on | A packet couldn't be retransmitted within the latency budget. Raise it: `--srt-latency 1000`. |
| Server: `srt://…: Input/output error` | Client's SRT listener wasn't up when the caller connected. Start the client first, then the server. |
| Server: `--srt requested but ffmpeg was not built with libsrt` | See [Installing libsrt](#installing-libsrt). |
