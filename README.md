# Assignment 4: Custom Distance-Vector Router

This project implements a simple Distance-Vector routing daemon in Python using the assignment's DV-JSON packet format over UDP port `5000`.

## Files

- `router.py` - the complete routing daemon.
- `Dockerfile` - builds an Alpine Linux router image with Python and `iproute2`.
- `docker-compose.yml` - creates the three-router triangle topology.

## Run the Topology

Create the assignment networks once:

```powershell
.\setup-networks.ps1
```

The script uses `.254` as each Docker bridge gateway so `.1` and `.2` remain available for router interfaces.

Then start the routers:

```powershell
docker compose up --build
```

In another terminal, inspect routes:

```powershell
docker exec router_a ip route
docker exec router_b ip route
docker exec router_c ip route
```

Stop one router to test convergence after failure:

```powershell
docker stop router_c
docker logs -f router_a
docker logs -f router_b
```

Bring it back:

```powershell
docker compose up -d router_c
```

Clean up:

```powershell
docker compose down
```

## Design Summary

Each router starts by discovering its directly connected Docker networks from Linux interface data. Direct networks enter the routing table with distance `0` and next hop `SELF`.

Every `BROADCAST_INTERVAL` seconds, the router sends this packet format to each configured neighbor:

```json
{
  "router_id": "10.0.1.1",
  "version": 1.0,
  "routes": [
    {
      "subnet": "10.0.1.0/24",
      "distance": 0
    }
  ]
}
```

The daemon chooses a per-neighbor `router_id`, so the advertised next-hop IP is reachable on the link where the packet is sent. For example, Router A uses `10.0.1.1` when talking to Router B and `10.0.3.1` when talking to Router C.

When a router receives an update, it runs Bellman-Ford:

```text
candidate_distance = neighbor_advertised_distance + 1
```

The local table is changed when the candidate is a new route, a shorter route, or an update to a route that already uses that neighbor as next hop. Kernel routes are installed with:

```text
ip route replace <subnet> via <neighbor_ip>
```

## Split Horizon and Failure Handling

The implementation uses Split Horizon. If Router A learned a subnet from Router B, Router A does not advertise that subnet back to Router B. This prevents two neighboring routers from convincing each other that the other has a valid path after a failure, which is the Count to Infinity problem.

Routes learned from neighbors also age out. If no refresh arrives before `ROUTE_TIMEOUT`, the route is removed from the daemon table and from the Linux routing table. Then the router sends a triggered update with its remaining valid routes.

## Example Convergence Log

Expected steady-state examples:

```text
router_a: 10.0.1.0/24 distance=0 direct on eth0
router_a: 10.0.3.0/24 distance=0 direct on eth1
router_a: 10.0.2.0/24 distance=1 via 10.0.1.2
```

After stopping Router C, Router A should keep its direct Docker networks and continue to learn `10.0.2.0/24` through Router B. Routes that depended specifically on Router C are removed from the kernel route table after timeout.

## GitHub Link

https://github.com/Rijwith/Distance-Vector-Router.git
