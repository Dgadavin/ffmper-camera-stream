# UDP Webcam Stream over WireGuard

Stream your webcam from a Raspberry Pi or macOS machine to a remote client over an encrypted WireGuard tunnel. Supports both direct connections and a hub topology (via EC2/VPS) for connecting machines behind NAT.

```
Direct mode:
[Server — RPi/macOS]            WireGuard Tunnel            [Client — macOS/Windows]
  Webcam → ffmpeg  ──── UDP/MPEG-TS over encrypted VPN ────→ GUI → Screen
  10.0.0.1                                                    10.0.0.2

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
| `--lossy` | off | Lossy-network mode: more frequent keyframes for faster error recovery (Starlink, LTE) |

---

## Client GUI Features

- **Device management** — Add, edit, delete camera devices (saved to `devices.json`)
- **Embedded video** — Stream renders directly in the application window
- **RTT stats overlay** — Enable "Show RTT stats" per device to see latency on the video (server must also use `--stats`)
- **Options per device** — keepalive, slow network mode, RTT stats
- **Cross-platform** — macOS and Windows (PyQt6)

---

## Slow / Unreliable Network

Use `--slow` on the server and enable "Slow network mode" on the client device:

| What | Normal | Slow mode |
|---|---|---|
| Resolution | 1280x720 | 640x480 |
| Bitrate | 2000k | 600k |
| FPS | 30 | 15 |
| Keyframe interval | 1s | 2s |
| Client jitter buffer | 100ms | 500ms |

---

## Keep-alive

The client sends a UDP heartbeat to the server every 2 seconds. The server waits for the first heartbeat before starting the stream, and pauses if heartbeats stop for 8 seconds. When the client reconnects, the stream resumes automatically.

To disable (e.g. for quick tests), use `--no-keepalive` on the server and uncheck "Send keepalive" on the client device.

Ports used:
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

- **Transport**: UDP unicast — server pushes to client IP
- **Container**: MPEG-TS — no seeking needed, resilient to packet loss
- **Codec**: H.264 — Pi GPU encoder (rpicam-vid) or libx264 ultrafast+zerolatency
- **Keep-alive**: UDP heartbeat side-channel on port 5010
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
