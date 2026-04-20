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
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "5"))
ROUTE_TIMEOUT = int(os.getenv("ROUTE_TIMEOUT", "15"))

VERSION = 1.0
INFINITY = 16
DIRECT_ROUTE = "0.0.0.0"


# Routing table format:
# {
#   "10.0.1.0/24": [distance, next_hop]
# }
#
# next_hop is 0.0.0.0 for directly connected networks.
routing_table = {}
route_timers = {}

table_lock = threading.RLock()
send_update_now = threading.Event()


# -----------------------------
# Small helper functions
# -----------------------------

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {ROUTER_ID} | {message}", flush=True)


def run_route_command(command):
    os.system(command)


def read_local_interfaces():
    """
    Reads the container's IPv4 interfaces.
    Returns entries like:
    {"name": "eth0", "ip": "10.0.1.1", "subnet": "10.0.1.0/24"}
    """
    result = subprocess.run(["ip", "-4", "addr", "show"], capture_output=True, text=True)

    interfaces = []
    current_interface = None

    for line in result.stdout.splitlines():
        line = line.strip()
        parts = line.split()

        if parts and parts[0].rstrip(":").isdigit():
            current_interface = parts[1].rstrip(":").split("@")[0]
            continue

        if not line.startswith("inet ") or current_interface == "lo":
            continue

        cidr = line.split()[1]
        interface = ipaddress.ip_interface(cidr)

        if interface.ip.is_loopback:
            continue

        interfaces.append(
            {
                "name": current_interface,
                "ip": str(interface.ip),
                "subnet": str(interface.network),
            }
        )

    return interfaces


def choose_router_id_for_neighbor(neighbor_ip):
    """
    A router has two Docker interfaces. The router_id in the packet should be
    the IP address that the receiving neighbor can use as its next hop.
    """
    neighbor_address = ipaddress.ip_address(neighbor_ip)

    for interface in read_local_interfaces():
        local_network = ipaddress.ip_network(interface["subnet"])
        if neighbor_address in local_network:
            return interface["ip"]

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

    for subnet, (distance, next_hop) in rows:
        if next_hop == DIRECT_ROUTE:
            route_text = "directly connected"
        else:
            route_text = f"via {next_hop}"

        log(f"  {subnet:18} distance {distance:<2}  {route_text}")


# -----------------------------
# Directly connected networks
# -----------------------------

def add_directly_connected_routes():
    """
    Adds the Docker networks attached to this container.
    These routes always have distance 0.
    """
    changed = False

    with table_lock:
        for interface in read_local_interfaces():
            subnet = interface["subnet"]

            if routing_table.get(subnet) != [0, DIRECT_ROUTE]:
                routing_table[subnet] = [0, DIRECT_ROUTE]
                changed = True

            run_route_command(f"ip route replace {subnet} dev {interface['name']}")

    if changed:
        print_routing_table("Found my directly connected networks")
        send_update_now.set()


# -----------------------------
# Sending routing updates
# -----------------------------

def make_update_packet(neighbor_ip):
    routes = []

    with table_lock:
        for subnet, (distance, next_hop) in sorted(routing_table.items()):
            # Split Horizon:
            # If this route came from this neighbor, do not send it back there.
            if next_hop == neighbor_ip:
                continue

            routes.append(
                {
                    "subnet": subnet,
                    "distance": min(distance, INFINITY),
                }
            )

    return {
        "router_id": choose_router_id_for_neighbor(neighbor_ip),
        "version": VERSION,
        "routes": routes,
    }


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:
        add_directly_connected_routes()

        for neighbor_ip in NEIGHBORS:
            packet = make_update_packet(neighbor_ip)

            try:
                sock.sendto(json.dumps(packet).encode("utf-8"), (neighbor_ip, PORT))
            except OSError as error:
                log(f"Could not send an update to {neighbor_ip}. Reason: {error}")

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
            log(f"Received a packet from {address[0]}, but it was not valid JSON.")
            continue

        if packet.get("version") != VERSION:
            log(f"Ignored an update from {address[0]} because the version did not match.")
            continue

        neighbor_ip = packet.get("router_id", address[0])
        routes = packet.get("routes", [])

        if not isinstance(routes, list):
            log(f"Ignored an update from {neighbor_ip} because the routes field was invalid.")
            continue

        update_logic(neighbor_ip, routes)


# -----------------------------
# Bellman-Ford routing logic
# -----------------------------

def update_logic(neighbor_ip, routes_from_neighbor):
    changed = False
    now = time.time()

    for route in routes_from_neighbor:
        try:
            subnet = str(ipaddress.ip_network(route["subnet"], strict=False))
            neighbor_distance = int(route["distance"])
        except (KeyError, TypeError, ValueError):
            log(f"Skipped a bad route from {neighbor_ip}: {route}")
            continue

        new_distance = min(neighbor_distance + 1, INFINITY)

        with table_lock:
            current_route = routing_table.get(subnet)

            # Do not replace a network that is physically connected to this router.
            if current_route and current_route[1] == DIRECT_ROUTE:
                continue

            # Same route from the same neighbor: just refresh the timer.
            if (
                current_route
                and current_route[1] == neighbor_ip
                and current_route[0] == new_distance
            ):
                route_timers[subnet] = now
                continue

            should_use_route = (
                current_route is None
                or new_distance < current_route[0]
                or current_route[1] == neighbor_ip
            )

            if not should_use_route:
                continue

            if new_distance >= INFINITY:
                if current_route and current_route[1] == neighbor_ip:
                    run_route_command(f"ip route del {subnet} 2>/dev/null")
                    del routing_table[subnet]
                    route_timers.pop(subnet, None)
                    log(f"Removed {subnet}; {neighbor_ip} says it is no longer reachable.")
                    changed = True
                continue

            routing_table[subnet] = [new_distance, neighbor_ip]
            route_timers[subnet] = now

            run_route_command(f"ip route replace {subnet} via {neighbor_ip}")
            log(f"Learned route to {subnet}: distance {new_distance}, next hop {neighbor_ip}.")
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
        now = time.time()
        changed = False

        with table_lock:
            for subnet, last_update_time in list(route_timers.items()):
                if now - last_update_time <= ROUTE_TIMEOUT:
                    continue

                distance, next_hop = routing_table.get(subnet, [None, None])

                if next_hop and next_hop != DIRECT_ROUTE:
                    run_route_command(f"ip route del {subnet} 2>/dev/null")
                    del routing_table[subnet]
                    del route_timers[subnet]

                    log(f"Route expired: {subnet} through {next_hop} was not refreshed in time.")
                    changed = True

        if changed:
            print_routing_table("Routing table after removing expired routes")
            send_update_now.set()


# -----------------------------
# Program entry point
# -----------------------------

if __name__ == "__main__":
    add_directly_connected_routes()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=remove_expired_routes, daemon=True).start()

    listen_for_updates()
