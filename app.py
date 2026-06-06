from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
import paramiko
import threading
import re
import sqlite3
from passlib.hash import sha512_crypt
from ssh import SSHManager

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

ssh = SSHManager()
vm_credentials = {}

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

DB_FILE        = "vms.db"
NETWORK_PREFIX = "192.168.0."
GATEWAY        = "192.168.0.1"
CLOUD_IMAGE    = "/var/lib/libvirt/images/jammy-server-cloudimg-amd64.img"

SAFE_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')

ip_lock = threading.Lock()

# ─────────────────────────────────────────────
# DATABASE INIT
# ─────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ip_pool (
            ip      TEXT PRIMARY KEY,
            vm_name TEXT,
            status  TEXT DEFAULT 'free'
        )
    """)
    conn.commit()
    conn.close()


def seed_ips():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    for i in range(101, 255):
        ip = f"{NETWORK_PREFIX}{i}"
        cur.execute(
            "INSERT OR IGNORE INTO ip_pool (ip, status) VALUES (?, 'free')",
            (ip,)
        )
    conn.commit()
    conn.close()


init_db()
seed_ips()

# ─────────────────────────────────────────────
# IP ALLOCATION (THREAD SAFE + ATOMIC)
# ─────────────────────────────────────────────

def allocate_ip(vm_name):
    with ip_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        cur.execute("""
            SELECT ip FROM ip_pool
            WHERE status = 'free'
            ORDER BY ip ASC
            LIMIT 1
        """)
        row = cur.fetchone()

        if not row:
            conn.close()
            return None

        ip = row[0]

        cur.execute("""
            UPDATE ip_pool
            SET status = 'used', vm_name = ?
            WHERE ip = ? AND status = 'free'
        """, (vm_name, ip))

        conn.commit()
        conn.close()
        return ip


def release_ip(vm_name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        UPDATE ip_pool
        SET status = 'free', vm_name = NULL
        WHERE vm_name = ?
    """, (vm_name,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────

def validate_vm_params(data):
    name     = data.get("name", "")
    username = data.get("username", "")
    password = data.get("password", "")

    if not SAFE_NAME.match(name):
        return "Invalid VM name"
    if not SAFE_NAME.match(username):
        return "Invalid username"
    if len(password) < 4:
        return "Password too short"

    try:
        cpu  = int(data.get("cpu", 0))
        ram  = int(data.get("ram", 0))
        disk = int(data.get("disk", 0))
        if not (1 <= cpu <= 64):
            return "CPU must be 1–64"
        if not (1 <= ram <= 128):
            return "RAM must be 1–128 GB"
        if not (5 <= disk <= 2000):
            return "Disk must be 5–2000 GB"
    except (ValueError, TypeError):
        return "cpu, ram, disk must be integers"

    return None


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/create_vm", methods=["POST"])
def create_vm():
    name = None
    try:
        data = request.json or {}

        err = validate_vm_params(data)
        if err:
            return jsonify({"status": "error", "message": err}), 400

        name     = data["name"]
        username = data["username"]
        password = data["password"]
        cpu      = int(data["cpu"])
        ram      = int(data["ram"])
        disk     = int(data["disk"])

        base_path = f"/var/lib/libvirt/images/{name}_config"
        vm_disk   = f"/var/lib/libvirt/images/{name}.qcow2"

        hashed_password = sha512_crypt.hash(password)

        # ── Allocate static IP ───────────────────────────────────
        vm_ip = allocate_ip(name)
        if not vm_ip:
            return jsonify({"status": "error", "message": "No IP available"}), 500

        # ── Network config ───────────────────────────────────────
        network_config = f"""version: 2
ethernets:
  eth0:
    dhcp4: false
    addresses: [{vm_ip}/24]
    gateway4: {GATEWAY}
    nameservers:
      addresses: [1.1.1.1, 8.8.8.8]
"""

        # ── user-data: Ubuntu ────────────────────────────────────
        user_data = f"""#cloud-config
hostname: {name}

users:
  - name: {username}
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: sudo
    shell: /bin/bash
    passwd: {hashed_password}
    lock_passwd: false

ssh_pwauth: true

packages:
  - qemu-guest-agent
  - openssh-server
  - net-tools

package_update: true

runcmd:
  - systemctl enable ssh
  - systemctl start ssh
  - systemctl enable qemu-guest-agent
  - systemctl start qemu-guest-agent
"""

        meta_data = f"""instance-id: {name}
local-hostname: {name}
"""

        ssh.run(f"mkdir -p {base_path}")
        ssh.run(f"cat << 'EOF' > {base_path}/user-data\n{user_data}\nEOF")
        ssh.run(f"cat << 'EOF' > {base_path}/meta-data\n{meta_data}\nEOF")
        ssh.run(f"cat << 'EOF' > {base_path}/network-config\n{network_config}\nEOF")

        ssh.run(
            f"mkisofs -output {base_path}/seed.iso "
            f"-volid cidata -joliet -rock "
            f"{base_path}/user-data "
            f"{base_path}/meta-data "
            f"{base_path}/network-config"
        )

        ssh.run(
            f"qemu-img create -f qcow2 -b {CLOUD_IMAGE} -F qcow2 {vm_disk} {disk}G"
        )

        # ── virt-install ─────────────────────────────────────────
        result = ssh.run(
            f"virt-install "
            f"--name {name} "
            f"--memory {ram * 1024} "
            f"--vcpus {cpu} "
            f"--disk {vm_disk},format=qcow2 "
            f"--disk {base_path}/seed.iso,device=cdrom "
            f"--network network=default,model=virtio "
            f"--graphics none "
            f"--import "
            f"--os-variant ubuntu22.04 "
            f"--noautoconsole"
        )

        print(f"[virt-install] {name}: {result}")  # always log to Flask console

        # ── Verify VM was actually created ───────────────────────
        verify = ssh.run(f"virsh domstate {name}")
        print(f"[virsh domstate] {name}: {verify}")

        if "running" not in verify.lower() and "shut off" not in verify.lower():
            release_ip(name)
            return jsonify({
                "status": "error",
                "message": f"virt-install failed: {result or '(no output — check server logs)'}"
            }), 500

        vm_credentials[name] = {
            "ip": vm_ip,
            "username": username,
            "password": password
        }

        return jsonify({
            "status": "success",
            "vm": name,
            "ip": vm_ip,
            "output": result
        })

    except Exception as e:
        if name:
            release_ip(name)
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────
# IP LOOKUP
# ─────────────────────────────────────────────

@app.route("/vm_ip/<name>")
def vm_ip(name):
    if not SAFE_NAME.match(name):
        return jsonify({"ip": "Invalid name"}), 400

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT ip FROM ip_pool WHERE vm_name = ?", (name,))
    row = cur.fetchone()
    conn.close()

    if row:
        return jsonify({"ip": row[0]})

    vm = vm_credentials.get(name)
    if vm:
        return jsonify({"ip": vm["ip"]})

    return jsonify({"ip": "Unknown"})


# ─────────────────────────────────────────────
# VM LIST
# ─────────────────────────────────────────────

@app.route("/vms")
def get_vms():
    try:
        raw = ssh.run("virsh list --all --name")
        return jsonify([v.strip() for v in raw.split("\n") if v.strip()])
    except Exception:
        return jsonify([])


# ─────────────────────────────────────────────
# DELETE
# ─────────────────────────────────────────────

@app.route("/delete/<name>")
def delete_vm(name):
    if not SAFE_NAME.match(name):
        return jsonify({"error": "Invalid name"}), 400

    try:
        ssh.run(f"virsh destroy {name}")
    except Exception:
        pass

    ssh.run(f"virsh undefine {name} --remove-all-storage")
    ssh.run(f"rm -rf /var/lib/libvirt/images/{name}_config")
    release_ip(name)
    vm_credentials.pop(name, None)

    return jsonify({"status": "success"})


# ─────────────────────────────────────────────
# START / STOP
# ─────────────────────────────────────────────

@app.route("/start/<name>")
def start_vm(name):
    if not SAFE_NAME.match(name):
        return jsonify({"error": "Invalid name"}), 400
    return jsonify({"output": ssh.run(f"virsh start {name}")})


@app.route("/stop/<name>")
def stop_vm(name):
    if not SAFE_NAME.match(name):
        return jsonify({"error": "Invalid name"}), 400
    return jsonify({"output": ssh.run(f"virsh shutdown {name}")})


# ─────────────────────────────────────────────
# WEBSOCKET SSH TERMINAL
# ─────────────────────────────────────────────

ssh_sessions = {}


def _forward_ssh(chan, sid):
    try:
        while not chan.exit_status_ready():
            if chan.recv_ready():
                data = chan.recv(4096).decode("utf-8", errors="ignore")
                socketio.emit("ssh_output", {"data": data}, room=sid)
    except Exception as e:
        print(f"[SSH forward] {e}")
    finally:
        socketio.emit("ssh_output", {"data": "\r\n[Connection closed]\r\n"}, room=sid)
        chan.close()


@socketio.on("start_ssh")
def handle_start_ssh(data):
    sid     = request.sid
    vm_name = data.get("vm_name", "").strip()

    if not SAFE_NAME.match(vm_name):
        emit("ssh_status", {"status": "error", "message": "Invalid VM name."})
        return

    vm = vm_credentials.get(vm_name)
    if not vm:
        emit("ssh_status", {"status": "error", "message": "VM credentials not found."})
        return

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=vm["ip"],
            username=vm["username"],
            password=vm["password"],
            timeout=10
        )
        chan = client.invoke_shell(term="xterm")
        ssh_sessions[sid] = {"channel": chan, "client": client}
        threading.Thread(target=_forward_ssh, args=(chan, sid), daemon=True).start()
        emit("ssh_status", {"status": "connected"})
    except Exception as e:
        emit("ssh_status", {"status": "error", "message": str(e)})


@socketio.on("ssh_input")
def handle_ssh_input(data):
    session = ssh_sessions.get(request.sid)
    if not session:
        return
    try:
        session["channel"].send(data["data"])
    except Exception as e:
        emit("ssh_output", {"data": f"\r\n[Send error: {e}]\r\n"})


@socketio.on("disconnect")
def handle_disconnect():
    session = ssh_sessions.pop(request.sid, None)
    if session:
        try:
            session["channel"].close()
            session["client"].close()
        except Exception:
            pass


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────

if __name__ == "__main__":
    socketio.run(app, debug=True, port=5000)