#!/usr/bin/env bash
# =============================================================================
#  WireGuard Setup Script
#  Supports three roles:
#    hub    — EC2/VPS relay with public IP, forwards traffic between peers
#    server — machine with webcam, connects to hub, runs server.py
#    client — viewer machine, connects to hub, runs client.py
#
#  Network layout:
#    Hub    (EC2 relay)     : WireGuard IP 10.0.0.1  public port 51820/udp
#    Server (streams webcam): WireGuard IP 10.0.0.2
#    Client (watches stream): WireGuard IP 10.0.0.3  (or higher for more clients)
#
#  Usage:
#    HUB (EC2):   sudo bash wg-setup.sh --role hub
#    SERVER (Mac): sudo bash wg-setup.sh --role server --hub-ip <EC2_PUBLIC_IP>
#    CLIENT:       sudo bash wg-setup.sh --role client --hub-ip <EC2_PUBLIC_IP> [--wg-ip 10.0.0.3]
#
#  Legacy (direct, no hub):
#    SERVER:       sudo bash wg-setup.sh --role server-direct
#    CLIENT:       sudo bash wg-setup.sh --role client-direct --server-ip <SERVER_PUBLIC_IP>
# =============================================================================

set -euo pipefail

WG_IF="wg0"
WG_PORT=51820
HUB_WG_IP="10.0.0.1"
HUB_WG_CIDR="${HUB_WG_IP}/24"
KEY_DIR="/etc/wireguard"

# ── helpers ───────────────────────────────────────────────────────────────────
usage() {
    echo "Usage:"
    echo "  Hub (EC2):       sudo bash wg-setup.sh --role hub"
    echo "  Server (webcam): sudo bash wg-setup.sh --role server --hub-ip <EC2_PUBLIC_IP>"
    echo "  Client (viewer): sudo bash wg-setup.sh --role client --hub-ip <EC2_PUBLIC_IP> [--wg-ip 10.0.0.3]"
    echo ""
    echo "  Direct (no hub):"
    echo "  Server:          sudo bash wg-setup.sh --role server-direct"
    echo "  Client:          sudo bash wg-setup.sh --role client-direct --server-ip <SERVER_PUBLIC_IP>"
    exit 1
}

check_root() {
    [[ $EUID -eq 0 ]] || { echo "[ERROR] Run as root: sudo bash $0"; exit 1; }
}

bring_up_wg() {
    mkdir -p "$KEY_DIR"
    wg-quick down "$WG_IF" 2>/dev/null || true
    wg-quick up "$WG_IF"
    # Enable auto-start on boot (Linux only; macOS needs a LaunchDaemon)
    if command -v systemctl &>/dev/null; then
        systemctl enable "wg-quick@${WG_IF}" 2>/dev/null || true
    fi
}

install_wg() {
    if command -v wg &>/dev/null; then
        echo "[*] WireGuard already installed."
        return
    fi
    echo "[*] Installing WireGuard..."
    case "$(uname -s)" in
        Darwin)
            if command -v brew &>/dev/null; then
                brew install wireguard-tools
            else
                echo "[ERROR] Homebrew not found. Install WireGuard manually: brew install wireguard-tools"
                exit 1
            fi
            ;;
        Linux)
            apt-get update -qq
            apt-get install -y wireguard
            ;;
        *)
            echo "[ERROR] Unsupported OS: $(uname -s). Install WireGuard manually."
            exit 1
            ;;
    esac
}

gen_keypair() {
    local role=$1
    local privkey_file="$KEY_DIR/${role}_private.key"
    local pubkey_file="$KEY_DIR/${role}_public.key"

    if [[ ! -f "$privkey_file" ]]; then
        echo "[*] Generating keypair for $role..."
        mkdir -p "$KEY_DIR"
        wg genkey | tee "$privkey_file" | wg pubkey > "$pubkey_file"
        chmod 600 "$privkey_file"
    else
        echo "[*] Keypair for $role already exists — reusing."
    fi

    PRIVATE_KEY=$(cat "$privkey_file")
    PUBLIC_KEY=$(cat "$pubkey_file")
}

# ── hub role (EC2 relay) ─────────────────────────────────────────────────────
setup_hub() {
    echo
    echo "=== Setting up WireGuard HUB (relay) ==="
    gen_keypair "hub"
    local hub_pub=$PUBLIC_KEY
    local hub_priv=$PRIVATE_KEY

    # Enable IP forwarding so peers can reach each other through the hub
    echo "[*] Enabling IP forwarding..."
    sysctl -w net.ipv4.ip_forward=1 > /dev/null
    # Make persistent across reboots
    if [[ -f /etc/sysctl.conf ]]; then
        if ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf; then
            echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
        fi
    fi

    cat > "$KEY_DIR/$WG_IF.conf" <<EOF
[Interface]
Address    = $HUB_WG_CIDR
ListenPort = $WG_PORT
PrivateKey = $hub_priv

# ── Add peer blocks below for each machine that connects ──
# Server (webcam machine):
# [Peer]
# PublicKey  = <SERVER_PUBLIC_KEY>
# AllowedIPs = 10.0.0.2/32
#
# Client (viewer machine):
# [Peer]
# PublicKey  = <CLIENT_PUBLIC_KEY>
# AllowedIPs = 10.0.0.3/32
EOF

    chmod 600 "$KEY_DIR/$WG_IF.conf"
    bring_up_wg

    echo
    echo "============================================================"
    echo "  HUB WireGuard is UP"
    echo "  WireGuard IP : $HUB_WG_IP"
    echo "  Listen port  : $WG_PORT/udp  (open this in EC2 security group!)"
    echo "  IP forwarding: enabled"
    echo
    echo "  *** HUB PUBLIC KEY (give this to server + clients) ***"
    echo "  $hub_pub"
    echo
    echo "  Next steps:"
    echo "  1. Run wg-setup.sh --role server --hub-ip <THIS_EC2_IP> on the webcam machine."
    echo "  2. Run wg-setup.sh --role client --hub-ip <THIS_EC2_IP> on each viewer."
    echo "  3. Each will print its public key — add a [Peer] block here for each:"
    echo "       [Peer]"
    echo "       PublicKey  = <PEER_PUBLIC_KEY>"
    echo "       AllowedIPs = <PEER_WG_IP>/32"
    echo "  4. Reload: wg syncconf $WG_IF <(wg-quick strip $WG_IF)"
    echo "============================================================"
}

# ── server role (webcam machine, connects to hub) ────────────────────────────
setup_server() {
    local hub_public_ip=$1
    echo
    echo "=== Setting up WireGuard SERVER (webcam → hub) ==="
    gen_keypair "server"
    local server_pub=$PUBLIC_KEY
    local server_priv=$PRIVATE_KEY

    echo
    read -rp "Paste the HUB public key (from hub setup output): " hub_pub_key
    [[ -z "$hub_pub_key" ]] && { echo "[ERROR] Hub public key is required."; exit 1; }

    cat > "$KEY_DIR/$WG_IF.conf" <<EOF
[Interface]
Address    = 10.0.0.2/24
PrivateKey = $server_priv

[Peer]
PublicKey  = $hub_pub_key
Endpoint   = ${hub_public_ip}:${WG_PORT}
AllowedIPs = 10.0.0.0/24
PersistentKeepalive = 25
EOF

    chmod 600 "$KEY_DIR/$WG_IF.conf"
    bring_up_wg

    echo
    echo "============================================================"
    echo "  SERVER WireGuard is UP"
    echo "  WireGuard IP : 10.0.0.2"
    echo "  Connected to : hub at ${hub_public_ip}:${WG_PORT}"
    echo
    echo "  *** SERVER PUBLIC KEY (give this to the hub) ***"
    echo "  $server_pub"
    echo
    echo "  Next steps:"
    echo "  1. On the hub, add this [Peer] block to $KEY_DIR/$WG_IF.conf:"
    echo "       [Peer]"
    echo "       PublicKey  = $server_pub"
    echo "       AllowedIPs = 10.0.0.2/32"
    echo "  2. Reload hub: wg syncconf $WG_IF <(wg-quick strip $WG_IF)"
    echo "  3. Test: ping $HUB_WG_IP"
    echo "  4. Start streaming: python3 server.py --host <CLIENT_WG_IP>"
    echo "============================================================"
}

# ── client role (viewer, connects to hub) ────────────────────────────────────
setup_client() {
    local hub_public_ip=$1
    local client_wg_ip=$2
    echo
    echo "=== Setting up WireGuard CLIENT (viewer → hub) ==="
    gen_keypair "client"
    local client_pub=$PUBLIC_KEY
    local client_priv=$PRIVATE_KEY

    echo
    read -rp "Paste the HUB public key (from hub setup output): " hub_pub_key
    [[ -z "$hub_pub_key" ]] && { echo "[ERROR] Hub public key is required."; exit 1; }

    cat > "$KEY_DIR/$WG_IF.conf" <<EOF
[Interface]
Address    = ${client_wg_ip}/24
PrivateKey = $client_priv

[Peer]
PublicKey  = $hub_pub_key
Endpoint   = ${hub_public_ip}:${WG_PORT}
AllowedIPs = 10.0.0.0/24
PersistentKeepalive = 25
EOF

    chmod 600 "$KEY_DIR/$WG_IF.conf"
    bring_up_wg

    echo
    echo "============================================================"
    echo "  CLIENT WireGuard is UP"
    echo "  WireGuard IP : $client_wg_ip"
    echo "  Connected to : hub at ${hub_public_ip}:${WG_PORT}"
    echo
    echo "  *** CLIENT PUBLIC KEY (give this to the hub) ***"
    echo "  $client_pub"
    echo
    echo "  Next steps:"
    echo "  1. On the hub, add this [Peer] block to $KEY_DIR/$WG_IF.conf:"
    echo "       [Peer]"
    echo "       PublicKey  = $client_pub"
    echo "       AllowedIPs = ${client_wg_ip}/32"
    echo "  2. Reload hub: wg syncconf $WG_IF <(wg-quick strip $WG_IF)"
    echo "  3. Test: ping $HUB_WG_IP"
    echo "  4. Receive stream: python3 client.py --server-host 10.0.0.2"
    echo "============================================================"
}

# ── legacy direct roles (no hub) ─────────────────────────────────────────────
setup_server_direct() {
    echo
    echo "=== Setting up WireGuard SERVER (direct, no hub) ==="
    gen_keypair "server"
    local server_pub=$PUBLIC_KEY
    local server_priv=$PRIVATE_KEY

    cat > "$KEY_DIR/$WG_IF.conf" <<EOF
[Interface]
Address    = 10.0.0.1/24
ListenPort = $WG_PORT
PrivateKey = $server_priv

# ── Add the client peer block below after running wg-setup.sh on the client ──
# [Peer]
# PublicKey  = <CLIENT_PUBLIC_KEY>
# AllowedIPs = 10.0.0.2/32
EOF

    chmod 600 "$KEY_DIR/$WG_IF.conf"
    bring_up_wg

    echo
    echo "============================================================"
    echo "  SERVER WireGuard is UP (direct mode)"
    echo "  WireGuard IP : 10.0.0.1"
    echo "  Listen port  : $WG_PORT/udp  (open this in your firewall!)"
    echo
    echo "  *** SERVER PUBLIC KEY (give this to the client) ***"
    echo "  $server_pub"
    echo
    echo "  Next steps:"
    echo "  1. Run wg-setup.sh --role client-direct --server-ip <THIS_IP> on the client."
    echo "  2. Add the client's [Peer] block to $KEY_DIR/$WG_IF.conf."
    echo "  3. Reload: wg syncconf $WG_IF <(wg-quick strip $WG_IF)"
    echo "============================================================"
}

setup_client_direct() {
    local server_public_ip=$1
    echo
    echo "=== Setting up WireGuard CLIENT (direct, no hub) ==="
    gen_keypair "client"
    local client_pub=$PUBLIC_KEY
    local client_priv=$PRIVATE_KEY

    echo
    read -rp "Paste the SERVER public key (from server setup output): " server_pub_key
    [[ -z "$server_pub_key" ]] && { echo "[ERROR] Server public key is required."; exit 1; }

    cat > "$KEY_DIR/$WG_IF.conf" <<EOF
[Interface]
Address    = 10.0.0.2/24
PrivateKey = $client_priv

[Peer]
PublicKey  = $server_pub_key
Endpoint   = ${server_public_ip}:${WG_PORT}
AllowedIPs = 10.0.0.1/32
PersistentKeepalive = 25
EOF

    chmod 600 "$KEY_DIR/$WG_IF.conf"
    bring_up_wg

    echo
    echo "============================================================"
    echo "  CLIENT WireGuard is UP (direct mode)"
    echo "  WireGuard IP : 10.0.0.2"
    echo "  Connected to : ${server_public_ip}:${WG_PORT}"
    echo
    echo "  *** CLIENT PUBLIC KEY (give this to the server) ***"
    echo "  $client_pub"
    echo
    echo "  Next steps:"
    echo "  1. On the server, add this [Peer] block to $KEY_DIR/$WG_IF.conf:"
    echo "       [Peer]"
    echo "       PublicKey  = $client_pub"
    echo "       AllowedIPs = 10.0.0.2/32"
    echo "  2. Reload server: wg syncconf $WG_IF <(wg-quick strip $WG_IF)"
    echo "  3. Test tunnel  : ping 10.0.0.1"
    echo "============================================================"
}

# ── argument parsing ──────────────────────────────────────────────────────────
ROLE=""
HUB_IP=""
SERVER_IP=""
CLIENT_WG_IP="10.0.0.3"

while [[ $# -gt 0 ]]; do
    case $1 in
        --role)       ROLE="$2";           shift 2 ;;
        --hub-ip)     HUB_IP="$2";         shift 2 ;;
        --server-ip)  SERVER_IP="$2";      shift 2 ;;
        --wg-ip)      CLIENT_WG_IP="$2";   shift 2 ;;
        -h|--help)    usage ;;
        *) echo "[WARN] Unknown arg: $1"; shift ;;
    esac
done

check_root
install_wg

case "$ROLE" in
    hub)
        setup_hub
        ;;
    server)
        [[ -z "$HUB_IP" ]] && { echo "[ERROR] --hub-ip required for server role."; usage; }
        setup_server "$HUB_IP"
        ;;
    client)
        [[ -z "$HUB_IP" ]] && { echo "[ERROR] --hub-ip required for client role."; usage; }
        setup_client "$HUB_IP" "$CLIENT_WG_IP"
        ;;
    server-direct)
        setup_server_direct
        ;;
    client-direct)
        [[ -z "$SERVER_IP" ]] && { echo "[ERROR] --server-ip required for client-direct role."; usage; }
        setup_client_direct "$SERVER_IP"
        ;;
    *) usage ;;
esac
