# UDP Webcam Stream over WireGuard

Stream your webcam from a macOS machine to a remote client over an encrypted WireGuard tunnel. Supports both direct connections and a hub topology (via EC2/VPS) for connecting machines behind NAT.

```
Direct mode:
[Server — macOS]                WireGuard Tunnel              [Client — Linux/macOS]
  Webcam → ffmpeg  ──── UDP/MPEG-TS over encrypted VPN ────→ ffplay → Screen
  10.0.0.1                                                    10.0.0.2

Hub mode (EC2 relay):
[Server — macOS]          [Hub — EC2/VPS]          [Client — Linux/macOS]
  Webcam → ffmpeg  ─────→   10.0.0.1     ─────→    ffplay → Screen
  10.0.0.2                 (IP forwarding)           10.0.0.3
```

---

## Requirements

**Server (macOS):**
```bash
brew install ffmpeg wireguard-tools
```

**Client (Linux):**
```bash
sudo apt install ffmpeg wireguard
```

**Hub (EC2/VPS, Linux):**
```bash
sudo apt install wireguard
```

---

## WireGuard Setup

### Option A — Hub mode (recommended for internet)

Use this when machines are in different locations / behind NAT. An EC2 or VPS instance acts as a relay.

#### 1. Hub (EC2)

```bash
sudo bash wg-setup.sh --role hub
```
Copy the **hub public key** printed at the end. Open port **51820/udp** in EC2 security group.

#### 2. Server (Mac with webcam)

```bash
sudo bash wg-setup.sh --role server --hub-ip <EC2_PUBLIC_IP>
```
When prompted, paste the hub public key. Copy the **server public key** printed at the end.

#### 3. Client (viewer machine)

```bash
sudo bash wg-setup.sh --role client --hub-ip <EC2_PUBLIC_IP>
```
When prompted, paste the hub public key. Copy the **client public key** printed at the end.

For additional clients, assign a different WireGuard IP:
```bash
sudo bash wg-setup.sh --role client --hub-ip <EC2_PUBLIC_IP> --wg-ip 10.0.0.4
```

#### 4. Back on the Hub — add peers

Edit `/etc/wireguard/wg0.conf` and add a `[Peer]` block for each machine:
```ini
[Peer]
PublicKey  = <SERVER_PUBLIC_KEY>
AllowedIPs = 10.0.0.2/32

[Peer]
PublicKey  = <CLIENT_PUBLIC_KEY>
AllowedIPs = 10.0.0.3/32
```
Reload without restart:
```bash
sudo wg syncconf wg0 <(sudo wg-quick strip wg0)
```

#### 5. Verify

```bash
# From server or client:
ping 10.0.0.1   # hub
ping 10.0.0.2   # server (from client)
ping 10.0.0.3   # client (from server)
```

### Option B — Direct mode (LAN or public IP)

Use this when the server has a public IP or both machines are on the same network.

#### On the SERVER machine

```bash
sudo bash wg-setup.sh --role server-direct
```
Copy the **server public key** printed at the end.

#### On the CLIENT machine

```bash
sudo bash wg-setup.sh --role client-direct --server-ip <SERVER_PUBLIC_IP>
```
When prompted, paste the server public key. Copy the **client public key** printed at the end.

#### Back on the SERVER — add the client peer

Edit `/etc/wireguard/wg0.conf` and add:
```ini
[Peer]
PublicKey  = <CLIENT_PUBLIC_KEY>
AllowedIPs = 10.0.0.2/32
```
Reload without restart:
```bash
sudo wg syncconf wg0 <(sudo wg-quick strip wg0)
```

---

## Open firewall ports

```bash
sudo ufw allow 51820/udp   # WireGuard handshake (hub or server-direct)
```

The stream and heartbeat travel inside the WireGuard tunnel, so no extra ports needed.

---

## Start streaming

In all examples below, use the appropriate WireGuard IPs for your setup:
- **Hub mode:** server is `10.0.0.2`, client is `10.0.0.3`
- **Direct mode:** server is `10.0.0.1`, client is `10.0.0.2`

### Basic usage — play only

**Client first:**
```bash
python3 client.py --server-host 10.0.0.2
```

**Then server:**
```bash
python3 server.py --host 10.0.0.3
```

### Play + save to file

**Client:**
```bash
python3 client.py --server-host 10.0.0.2 --save recording.mp4
```

**Server** (must stream to two ports):
```bash
python3 server.py --host 10.0.0.3 --port2 5001
```

### Save only (no window)

```bash
python3 client.py --server-host 10.0.0.2 --no-play --save recording.mp4
python3 server.py --host 10.0.0.3
```

---

## Slow / unreliable network

Use `--slow` on both sides. This enables:

| What | Normal | Slow mode |
|---|---|---|
| Resolution | 1280x720 | 640x480 |
| Bitrate | 2000k | 600k |
| FPS | 30 | 15 |
| Keyframe interval | 1s | 2s |
| Client jitter buffer | 100ms | 500ms |
| Frame drop on late frames | no | yes |

```bash
python3 client.py --server-host 10.0.0.2 --slow
python3 server.py --host 10.0.0.3 --slow
```

You can also tune manually:
```bash
python3 server.py --host 10.0.0.3 --bitrate 800k --fps 20
```

---

## Keep-alive

The client sends a small `ALIVE` UDP heartbeat to the server every 2 seconds.
The server waits for the first heartbeat before starting the stream, and pauses
if heartbeats stop arriving for 8 seconds. When the client reconnects, the stream
resumes automatically.

To disable (e.g. for quick tests):
```bash
python3 client.py --no-keepalive
python3 server.py --no-keepalive
```

Ports used:
- `5000` — video stream (play)
- `5001` — video stream (save, only when using `--save` with playback)
- `5010` — heartbeat channel

---

## Localhost test (no WireGuard)

```bash
# Terminal 1
python3 client.py --server-host 127.0.0.1 --no-keepalive

# Terminal 2
python3 server.py --host 127.0.0.1 --no-keepalive
```

---

## All options

### server.py

| Flag | Default | Description |
|---|---|---|
| `--host` | `10.0.0.2` | Client IP to stream to |
| `--port` | `5000` | UDP destination port |
| `--port2` | off | Second port for play+save mode |
| `--device` | auto | AVFoundation camera index |
| `--list-devices` | — | Print available cameras and exit |
| `--bitrate` | `2000k` / `600k` | Video bitrate |
| `--fps` | `30` / `15` | Frames per second |
| `--slow` | off | Slow-network mode (low res, low bitrate) |
| `--no-keepalive` | off | Disable heartbeat listener |
| `--heartbeat-port` | `5010` | Port to receive heartbeats on |

### client.py

| Flag | Default | Description |
|---|---|---|
| `--port` | `5000` | UDP port to listen on |
| `--server-host` | `10.0.0.1` | Server IP to send heartbeats to |
| `--save` | off | Also save stream to this file |
| `--no-play` | off | Skip playback window (requires --save) |
| `--slow` | off | Slow-network mode (larger jitter buffer) |
| `--no-keepalive` | off | Disable heartbeat sender |
| `--heartbeat-port` | `5010` | Port to send heartbeats to |

### wg-setup.sh

| Flag | Description |
|---|---|
| `--role hub` | Set up EC2/VPS as WireGuard relay with IP forwarding |
| `--role server` | Set up webcam machine, connects to hub |
| `--role client` | Set up viewer machine, connects to hub |
| `--role server-direct` | Set up server for direct connection (no hub) |
| `--role client-direct` | Set up client for direct connection (no hub) |
| `--hub-ip <IP>` | Public IP of the hub (required for server/client roles) |
| `--server-ip <IP>` | Public IP of the server (required for client-direct) |
| `--wg-ip <IP>` | Custom WireGuard IP for client (default: 10.0.0.3) |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Server waits forever to start | Client heartbeat not reaching server — check `--server-host`, or use `--no-keepalive` |
| No camera found | `python3 server.py --list-devices` |
| Camera permission denied | System Settings → Privacy & Security → Camera → enable Terminal/iTerm |
| Stream freezes/corrupts | Try `--slow` on both sides |
| `Address already in use` | `pkill -f ffmpeg; pkill -f ffplay` |
| Tunnel not working | `wg show` on both machines; `ping 10.0.0.1` from client |
| Peers can't reach each other via hub | Check `sysctl net.ipv4.ip_forward` is `1` on the hub |
| EC2 connection refused | Open port `51820/udp` in the EC2 security group |

---

## Architecture

- **Transport**: UDP unicast — server pushes to client IP
- **Container**: MPEG-TS — no seeking needed, resilient to packet loss
- **Codec**: H.264 `ultrafast + zerolatency` — optimized for live streaming
- **Keep-alive**: UDP heartbeat side-channel on port 5010
- **Encryption**: WireGuard (ChaCha20-Poly1305) wraps all traffic
- **Latency**: ~200–500ms normal; ~500ms–1s in slow mode
