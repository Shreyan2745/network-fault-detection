"""
Network Fault Detection System - Prototype (real devices, no VM needed)

Run:
    pip install flask
    python app.py
Then open  http://localhost:5000  in your browser.

Edit the HOSTS list below with the IPs of the devices on YOUR network.
"""
import platform
import random
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, render_template, request

# ======================= CONFIG - edit this part =======================
# group: "gateway"  = your router / phone hotspot (exactly one)
#        "lan"      = devices on the same Wi-Fi (phones, other laptops)
#        "internet" = servers on the internet
HOSTS = [
    {"id": "gw",   "name": "Router / Hotspot", "ip": "192.168.1.1",  "group": "gateway"},
    {"id": "dev1", "name": "Phone 1",          "ip": "192.168.1.10", "group": "lan"},
    {"id": "dev2", "name": "Phone 2",          "ip": "192.168.1.11", "group": "lan"},
    {"id": "dev3", "name": "Phone 3",          "ip": "192.168.1.12", "group": "lan"},
    {"id": "inet1", "name": "Google DNS",      "ip": "8.8.8.8",      "group": "internet"},
    {"id": "inet2", "name": "Cloudflare DNS",  "ip": "1.1.1.1",      "group": "internet"},
]

INTERVAL = 2          # seconds between probe rounds
WINDOW = 10           # last N probes used for loss % and average RTT
DOWN_AFTER = 3        # consecutive missed pings -> DOWN
LOSS_LIMIT = 20       # packet loss % above this -> DEGRADED
LATENCY_LIMIT = 150   # average RTT (ms) above this -> HIGH LATENCY
# =======================================================================

OS = platform.system().lower()
RTT_RE = re.compile(r"(?:time|zeit|temps|tiempo)\s*[=<]\s*([\d.,]+)\s*ms", re.I)

app = Flask(__name__)
lock = threading.Lock()
state = {h["id"]: {"window": deque(maxlen=WINDOW), "history": deque(maxlen=30),
                   "misses": 0, "status": "CHECKING"} for h in HOSTS}
inject = {h["id"]: {"delay": 0, "loss": 0} for h in HOSTS}   # test-mode fault injection
alerts = deque(maxlen=50)
current_faults = []
covered_ids = set()


# ---------------------------- probing ----------------------------
def ping(ip):
    """Send one ICMP echo request. Returns RTT in ms, or None if no reply."""
    if OS == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    elif OS == "darwin":
        cmd = ["ping", "-c", "1", "-t", "1", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return None
    # "TTL=" only appears in a real echo reply (Windows prints
    # "Destination host unreachable" with exit code 0, so we can't trust that)
    if "ttl=" not in out.lower():
        return None
    m = RTT_RE.search(out)
    return float(m.group(1).replace(",", ".")) if m else 0.5


def probe(host):
    rtt = ping(host["ip"])
    inj = inject[host["id"]]
    if rtt is not None:
        if inj["loss"] and random.uniform(0, 100) < inj["loss"]:
            return None
        rtt += inj["delay"]
    return rtt


# ---------------------------- detection ----------------------------
def metrics(s):
    samples = list(s["window"])
    if not samples:
        return None, None
    ok = [r for r in samples if r is not None]
    loss = 100 * (len(samples) - len(ok)) / len(samples)
    avg = sum(ok) / len(ok) if ok else None
    return loss, avg


def classify(s):
    if s["misses"] >= DOWN_AFTER:
        return "DOWN"
    loss, avg = metrics(s)
    if len(s["window"]) < 3:
        return "UP" if s["window"] and s["window"][-1] is not None else "CHECKING"
    if loss > LOSS_LIMIT:
        return "DEGRADED"
    if avg is not None and avg > LATENCY_LIMIT:
        return "HIGH LATENCY"
    return "UP"


def find_faults():
    """Turn per-host states into faults, and localize link failures."""
    by_group = lambda g: [h for h in HOSTS if h["group"] == g]
    gw, lan, inet = by_group("gateway"), by_group("lan"), by_group("internet")
    down = lambda hs: hs and all(state[h["id"]]["status"] == "DOWN" for h in hs)
    faults, covered = [], set()

    if down(gw):
        covered = {h["id"] for h in HOSTS}
        faults.append({"key": "LINK:local", "type": "LOCAL LINK DOWN", "target": "This laptop ↔ Router",
                       "detail": "Gateway unreachable, so every device behind it is cut off", "level": "down"})
    elif down(inet):
        covered = {h["id"] for h in inet}
        faults.append({"key": "LINK:isp", "type": "INTERNET LINK DOWN", "target": "Router ↔ ISP",
                       "detail": "LAN is fine but all internet hosts are unreachable", "level": "down"})

    for h in HOSTS:
        if h["id"] in covered:
            continue
        s = state[h["id"]]
        loss, avg = metrics(s)
        if s["status"] == "DOWN":
            faults.append({"key": f"DOWN:{h['id']}", "type": "HOST DOWN", "target": h["name"],
                           "detail": f"{s['misses']} consecutive pings missed ({h['ip']})", "level": "down"})
        elif s["status"] == "DEGRADED":
            faults.append({"key": f"DEG:{h['id']}", "type": "DEGRADED", "target": h["name"],
                           "detail": f"{loss:.0f}% packet loss", "level": "warn"})
        elif s["status"] == "HIGH LATENCY":
            faults.append({"key": f"LAT:{h['id']}", "type": "HIGH LATENCY", "target": h["name"],
                           "detail": f"avg RTT {avg:.0f} ms", "level": "warn"})
    return faults, covered


def monitor_loop():
    global current_faults, covered_ids
    prev = {}
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        while True:
            t0 = time.time()
            results = list(pool.map(probe, HOSTS))      # all hosts probed in parallel
            now = datetime.now().strftime("%H:%M:%S")
            with lock:
                for h, rtt in zip(HOSTS, results):
                    s = state[h["id"]]
                    if rtt is not None and s["misses"] >= DOWN_AFTER:
                        s["window"].clear()   # host just came back: start fresh, no false "DEGRADED"
                    s["window"].append(rtt)
                    s["history"].append(rtt)
                    s["misses"] = 0 if rtt is not None else s["misses"] + 1
                    s["status"] = classify(s)
                faults, covered = find_faults()
                cur = {f["key"]: f for f in faults}
                for k, f in cur.items():
                    if k not in prev:
                        alerts.appendleft({"time": now, **f})
                for k, f in prev.items():
                    if k not in cur:
                        alerts.appendleft({"time": now, "type": "RECOVERED", "target": f["target"],
                                           "detail": f"{f['type'].title()} cleared", "level": "ok"})
                prev, current_faults, covered_ids = cur, faults, covered
            time.sleep(max(0, INTERVAL - (time.time() - t0)))


# ---------------------------- web ----------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    with lock:
        hosts, rtts, losses = [], [], []
        for h in HOSTS:
            s = state[h["id"]]
            loss, avg = metrics(s)
            st = s["status"]
            if h["id"] in covered_ids and h["group"] != "gateway":
                st = "UNREACHABLE"      # not its own fault: the link in front of it failed
            last = s["history"][-1] if s["history"] else None
            hosts.append({**h, "status": st, "rtt": last, "avg": avg, "loss": loss,
                          "history": list(s["history"]), "inject": inject[h["id"]]})
            if avg is not None:
                rtts.append(avg)
            if loss is not None:
                losses.append(loss)
        up = sum(1 for x in hosts if x["status"] == "UP")
        return jsonify({
            "time": datetime.now().strftime("%H:%M:%S"),
            "hosts": hosts,
            "faults": current_faults,
            "alerts": list(alerts),
            "summary": {"up": up, "total": len(hosts), "faults": len(current_faults),
                        "avg_rtt": sum(rtts) / len(rtts) if rtts else None,
                        "loss": sum(losses) / len(losses) if losses else None},
            "config": {"interval": INTERVAL, "down_after": DOWN_AFTER,
                       "loss_limit": LOSS_LIMIT, "latency_limit": LATENCY_LIMIT},
        })


@app.route("/inject", methods=["POST"])
def do_inject():
    """Test mode: add artificial delay / packet loss to one host (like tc netem)."""
    d = request.get_json(force=True)
    with lock:
        if d.get("id") == "all":
            for v in inject.values():
                v.update(delay=0, loss=0)
        elif d.get("id") in inject:
            inject[d["id"]] = {"delay": max(0, float(d.get("delay", 0))),
                               "loss": min(100, max(0, float(d.get("loss", 0))))}
    return jsonify(ok=True)


if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    print("Dashboard running at  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
