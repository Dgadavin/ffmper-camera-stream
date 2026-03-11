# UDP Webcam Stream over WireGuard

Stream your webcam from a macOS server to a client over an encrypted WireGuard tunnel.

```
[Server — macOS]                WireGuard Tunnel              [Client — Linux/macOS]
  Webcam → ffmpeg  ──── UDP/MPEG-TS over encrypted VPN ────→ ffplay → Screen
  10.0.0.1                                                    10.0.0.2
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

---

## Step 1 — Set up WireGuard

### On the SERVER machine
```bash
sudo bash wg-setup.sh --role server
```
Copy the **server public key** printed at the end.

### On the CLIENT machine
```bash
sudo bash wg-setup.sh --role client --server-ip <SERVER_PUBLIC_IP>
```
When prompted, paste the server public key.
Copy the **client public key** printed at the end.

### Back on the SERVER — add the client peer
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

### Verify the tunnel
```bash
# From client:
ping 10.0.0.1
```

---

## Step 2 — Open firewall ports (server)

```bash
sudo ufw allow 51820/udp   # WireGuard handshake
```

The stream and heartbeat travel inside the WireGuard tunnel, so no extra ports needed.

---

## Step 3 — Start streaming

### Basic usage — play only

**Client first:**
```bash
python3 client.py --server-host 10.0.0.1
```

**Then server:**
```bash
python3 server.py --host 10.0.0.2
```

### Play + save to file

**Client:**
```bash
python3 client.py --server-host 10.0.0.1 --save recording.mp4
```

**Server** (must stream to two ports):
```bash
python3 server.py --host 10.0.0.2 --port2 5001
```

### Save only (no window)

```bash
python3 client.py --server-host 10.0.0.1 --no-play --save recording.mp4
python3 server.py --host 10.0.0.2
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
python3 client.py --server-host 10.0.0.1 --slow
python3 server.py --host 10.0.0.2 --slow
```

You can also tune manually:
```bash
python3 server.py --host 10.0.0.2 --bitrate 800k --fps 20
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
| `--server-host` | `10.0.0.2` | Server IP to send heartbeats to |
| `--save` | off | Also save stream to this file |
| `--no-play` | off | Skip playback window (requires --save) |
| `--slow` | off | Slow-network mode (larger jitter buffer) |
| `--no-keepalive` | off | Disable heartbeat sender |
| `--heartbeat-port` | `5010` | Port to send heartbeats to |

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

---

## Architecture

- **Transport**: UDP unicast — server pushes to client IP
- **Container**: MPEG-TS — no seeking needed, resilient to packet loss
- **Codec**: H.264 `ultrafast + zerolatency` — optimized for live streaming
- **Keep-alive**: UDP heartbeat side-channel on port 5010
- **Encryption**: WireGuard (ChaCha20-Poly1305) wraps all traffic
- **Latency**: ~200–500ms normal; ~500ms–1s in slow mode
