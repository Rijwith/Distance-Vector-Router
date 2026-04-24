import ipaddress
import json
import os
import socket
import subprocess
import threading
import time


# -----------------------------
# Basic router configuration
# -----------------------------

ROUTER_ID = os.getenv("ROUTER_ID", os.getenv("HOSTNAME", "router"))
MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [ip.strip() for ip in os.getenv("NEIGHBORS", "").split(",") if ip.strip()]

PORT = int(os.getenv("PORT", "5000"))
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "2"))
ROUTE_TIMEOUT = int(os.getenv("ROUTE_TIMEOUT", "6"))

VERSION = 1.0
INFINITY = 16
DIRECT_ROUTE = "0.0.0.0"

# routing_table: { subnet: [distance, next_hop] }
routing_table = {}
route_timers  = {}

# neighbor_table: { neighbor_ip: { "last_seen": float, "routes": { subnet: distance } } }
# Keeps the most recent advertisement from every neighbor.
neighbor_table = {}

table_lock     = threading.RLock()
send_update_now = threading.Event()


# -----------------------------
# Helper functions
# -----------------------------

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {ROUTER_ID} | {message}", flush=True)


def run_route_command(command):
    subprocess.run(command, shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_local_interfaces():
    result = subprocess.run(["ip", "-4", "addr", "show"],
                            capture_output=True, text=True)
    interfaces = []
    current_iface = None

    for line in result.stdout.splitlines():
        line  = line.strip()
        parts = line.split()

        if parts and parts[0].rstrip(":").isdigit():
            current_iface = parts[1].rstrip(":").split("@")[0]
            continue

        if not line.startswith("inet ") or current_iface == "lo":
            continue

        iface = ipaddress.ip_interface(line.split()[1])
        if iface.ip.is_loopback:
            continue

        interfaces.append({
            "name":   current_iface,
            "ip":     str(iface.ip),
            "subnet": str(iface.network),
        })

    return interfaces


def choose_router_id_for_neighbor(neighbor_ip):
    addr = ipaddress.ip_address(neighbor_ip)
    for iface in read_local_interfaces():
        if addr in ipaddress.ip_network(iface["subnet"]):
            return iface["ip"]
    return MY_IP


# -----------------------------
# Routing table display
# -----------------------------

def print_routing_table(title="Current routing table"):
    with table_lock:
        rows = sorted(routing_table.items())
    log(title)
    if not rows:
        log("  No routes known yet.")
        return
    for subnet, (dist, hop) in rows:
        via = "directly connected" if hop == DIRECT_ROUTE else f"via {hop}"
        log(f"  {subnet:18} distance {dist:<2}  {via}")


# -----------------------------
# Directly connected networks
# -----------------------------

def add_directly_connected_routes():
    changed       = False
    current_direct = set()

    with table_lock:
        for iface in read_local_interfaces():
            subnet = iface["subnet"]
            current_direct.add(subnet)

            if routing_table.get(subnet) != [0, DIRECT_ROUTE]:
                routing_table[subnet] = [0, DIRECT_ROUTE]
                route_timers.pop(subnet, None)
                changed = True

            run_route_command(
                f"ip route replace {subnet} dev {iface['name']} 2>/dev/null")

        # Drop DIRECT entries whose interface was detached so they can be
        # re-learned via neighbors.
        for subnet in list(routing_table.keys()):
            if (routing_table[subnet][1] == DIRECT_ROUTE
                    and subnet not in current_direct):
                del routing_table[subnet]
                route_timers.pop(subnet, None)
                # Remove kernel route so Docker can re-attach the interface cleanly
                run_route_command(f"ip route del {subnet} 2>/dev/null")
                log(f"Interface for {subnet} detached. Will re-learn via neighbors.")
                changed = True

    if changed:
        print_routing_table("Directly connected networks updated")
        send_update_now.set()


# -----------------------------
# Bellman-Ford re-evaluation
# -----------------------------

def recompute_routing_table():
    """
    Re-runs Bellman-Ford using only *fresh* neighbor advertisements.
    Called immediately after a route expires so alternate paths are
    found without waiting for the next broadcast cycle.
    """
    now     = time.time()
    changed = False

    with table_lock:
        # Collect subnets advertised by neighbors that are still alive
        all_subnets = set()
        for info in neighbor_table.values():
            if now - info["last_seen"] <= ROUTE_TIMEOUT:
                all_subnets.update(info["routes"].keys())

        for subnet in all_subnets:
            current = routing_table.get(subnet)
            if current and current[1] == DIRECT_ROUTE:
                continue          # never touch directly connected routes

            best_dist = INFINITY
            best_hop  = None

            for neighbor_ip, info in neighbor_table.items():
                # Skip stale neighbors
                if now - info["last_seen"] > ROUTE_TIMEOUT:
                    continue
                if subnet not in info["routes"]:
                    continue
                candidate = min(info["routes"][subnet] + 1, INFINITY)
                if candidate < best_dist:
                    best_dist = candidate
                    best_hop  = neighbor_ip

            if best_hop is None or best_dist >= INFINITY:
                if subnet in routing_table and routing_table[subnet][1] != DIRECT_ROUTE:
                    run_route_command(f"ip route del {subnet} 2>/dev/null")
                    del routing_table[subnet]
                    route_timers.pop(subnet, None)
                    log(f"No alternate path to {subnet}. Route removed.")
                    changed = True
                continue

            if routing_table.get(subnet) != [best_dist, best_hop]:
                routing_table[subnet] = [best_dist, best_hop]
                route_timers[subnet]  = now
                run_route_command(
                    f"ip route replace {subnet} via {best_hop} 2>/dev/null")
                log(f"Alternate path to {subnet}: distance {best_dist} via {best_hop}.")
                changed = True

    if changed:
        print_routing_table("Routing table after recompute")
        send_update_now.set()


# -----------------------------
# Sending routing updates
# -----------------------------

def make_update_packet(neighbor_ip):
    routes = []
    with table_lock:
        for subnet, (dist, hop) in sorted(routing_table.items()):
            # Split Horizon: don't advertise a route back to its source.
            if hop == neighbor_ip:
                continue
            routes.append({
                "subnet":   subnet,
                "distance": min(dist, INFINITY),
            })
    return {
        "router_id": choose_router_id_for_neighbor(neighbor_ip),
        "version":   VERSION,
        "routes":    routes,
    }


def sync_kernel_routes():
    """Re-installs learned routes every cycle to recover from failed installs.
    Only installs if the next-hop is reachable on a current interface."""
    local_subnets = {iface["subnet"] for iface in read_local_interfaces()}
    with table_lock:
        for subnet, (dist, hop) in routing_table.items():
            if hop == DIRECT_ROUTE:
                continue
            # Only install if the next-hop's subnet is directly connected
            hop_addr = ipaddress.ip_address(hop)
            reachable = any(
                hop_addr in ipaddress.ip_network(s)
                for s in local_subnets
            )
            if reachable:
                run_route_command(
                    f"ip route replace {subnet} via {hop} 2>/dev/null")


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:
        add_directly_connected_routes()
        sync_kernel_routes()

        for neighbor_ip in NEIGHBORS:
            packet = make_update_packet(neighbor_ip)
            try:
                sock.sendto(json.dumps(packet).encode("utf-8"),
                            (neighbor_ip, PORT))
            except OSError as err:
                log(f"Could not send update to {neighbor_ip}: {err}")

        send_update_now.wait(UPDATE_INTERVAL)
        send_update_now.clear()


# -----------------------------
# Receiving routing updates
# -----------------------------

def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))

    log(f"Router started. Listening for DV-JSON updates on UDP port {PORT}.")
    log(f"My neighbors are: {', '.join(NEIGHBORS) if NEIGHBORS else 'none'}")

    while True:
        data, address = sock.recvfrom(4096)

        try:
            packet = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            log(f"Bad JSON from {address[0]}, skipping.")
            continue

        if packet.get("version") != VERSION:
            log(f"Version mismatch from {address[0]}, ignoring.")
            continue

        neighbor_ip = packet.get("router_id", address[0])
        routes      = packet.get("routes", [])

        if not isinstance(routes, list):
            log(f"Invalid routes field from {neighbor_ip}, ignoring.")
            continue

        update_logic(neighbor_ip, routes)


# -----------------------------
# Bellman-Ford update logic
# -----------------------------

def update_logic(neighbor_ip, routes_from_neighbor):
    changed = False
    now     = time.time()

    # Store fresh advertisement for this neighbor
    with table_lock:
        advert = {}
        for r in routes_from_neighbor:
            try:
                s = str(ipaddress.ip_network(r["subnet"], strict=False))
                advert[s] = int(r["distance"])
            except (KeyError, TypeError, ValueError):
                pass
        neighbor_table[neighbor_ip] = {"last_seen": now, "routes": advert}

    for route in routes_from_neighbor:
        try:
            subnet            = str(ipaddress.ip_network(route["subnet"], strict=False))
            neighbor_distance = int(route["distance"])
        except (KeyError, TypeError, ValueError):
            log(f"Skipped bad route from {neighbor_ip}: {route}")
            continue

        new_distance = min(neighbor_distance + 1, INFINITY)

        with table_lock:
            current = routing_table.get(subnet)

            # Never overwrite a directly connected route.
            if current and current[1] == DIRECT_ROUTE:
                continue

            # Same neighbor, same distance — just refresh the timer.
            if current and current[1] == neighbor_ip and current[0] == new_distance:
                route_timers[subnet] = now
                continue

            should_update = (
                current is None
                or new_distance < current[0]
                or current[1] == neighbor_ip
            )

            if not should_update:
                continue

            if new_distance >= INFINITY:
                if current and current[1] == neighbor_ip:
                    run_route_command(f"ip route del {subnet} 2>/dev/null")
                    del routing_table[subnet]
                    route_timers.pop(subnet, None)
                    log(f"Removed {subnet}; {neighbor_ip} says unreachable.")
                    changed = True
                continue

            routing_table[subnet] = [new_distance, neighbor_ip]
            route_timers[subnet]  = now
            run_route_command(
                f"ip route replace {subnet} via {neighbor_ip} 2>/dev/null")
            log(f"Learned {subnet}: distance {new_distance} via {neighbor_ip}.")
            changed = True

    if changed:
        print_routing_table()
        send_update_now.set()


# -----------------------------
# Route timeout handling
# -----------------------------

def remove_expired_routes():
    while True:
        time.sleep(2)
        now     = time.time()
        changed = False

        with table_lock:
            for subnet, last_time in list(route_timers.items()):
                if now - last_time <= ROUTE_TIMEOUT:
                    continue

                dist, hop = routing_table.get(subnet, [None, None])
                if hop and hop != DIRECT_ROUTE:
                    run_route_command(f"ip route del {subnet} 2>/dev/null")
                    del routing_table[subnet]
                    del route_timers[subnet]
                    log(f"Route expired: {subnet} via {hop}.")
                    changed = True

        if changed:
            print_routing_table("Routing table after expiry")
            # Immediately find alternate paths using fresh neighbor data.
            recompute_routing_table()


# -----------------------------
# Entry point
# -----------------------------

if __name__ == "__main__":
    add_directly_connected_routes()

    threading.Thread(target=broadcast_updates,   daemon=True).start()
    threading.Thread(target=remove_expired_routes, daemon=True).start()

    listen_for_updates()
