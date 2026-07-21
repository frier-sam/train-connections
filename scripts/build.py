#!/usr/bin/env python3
"""Turn the raw DataMeet GeoJSON into compact frontend data.

Input  (data/): stations.json, trains.json  (GeoJSON FeatureCollections)
Output (web/data/): stations.json, trains.json, cities.json

Two key ideas:
1. Every vertex of a train's LineString sits exactly on a station coordinate,
   so we recover each train's full stop sequence by matching vertices back to
   stations, and from that build the connection graph.
2. A metro is many stations (Delhi = New Delhi + Old Delhi + Nizamuddin + Anand
   Vihar + ...). We cluster nearby stations into a CITY (greedy, ~10 km around
   the most-connected anchor) and build the reachability graph at the city
   level, so "reaching Delhi" means reaching any of its stations and a transfer
   may arrive at one station and leave from another in the same city.
"""
import json
import math
import os
from collections import defaultdict

CITY_RADIUS_KM = 10.0    # stations within this of an anchor join its city
# A major hub city is a real interchange: many trains AND many lines radiating
# out (distinct neighbouring cities), so mid-corridor stops that every train
# passes through don't qualify.
CITY_HUB_MIN_TRAINS = 120
CITY_HUB_MIN_LINES = 4

RAW_STATIONS = "data/stations.json"
RAW_TRAINS = "data/trains.json"
OUT_STATIONS = "web/data/stations.json"
OUT_TRAINS = "web/data/trains.json"
OUT_CITIES = "web/data/cities.json"


def load(p):
    return json.load(open(p))["features"]


def key(lng, lat):
    return (round(lng, 3), round(lat, 3))


def haversine(lat1, lng1, lat2, lng2):
    R, r = 6371.0, math.pi / 180
    dlat = (lat2 - lat1) * r
    dlng = (lng2 - lng1) * r
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def cluster_cities(rec):
    """Greedy spatial clustering of connected stations into cities.

    Anchor = highest-degree unassigned station; it absorbs every unassigned
    station within CITY_RADIUS_KM. Membership is by distance to the anchor
    (not transitive), which prevents chaining a whole corridor into one city.
    Returns (city_of: dict station->city id, cities: list of member-index lists).
    """
    connected = [i for i in range(len(rec)) if rec[i][6] > 0]
    CELL = 0.12  # ~13 km grid; a 3x3 window (±13 km) safely covers a 10 km radius
    grid = defaultdict(list)

    def cell(i):
        return (int(rec[i][4] / CELL), int(rec[i][5] / CELL))

    for i in connected:
        grid[cell(i)].append(i)

    city_of = {}
    cities = []
    for a in sorted(connected, key=lambda i: -rec[i][6]):   # by reachable-destinations
        if a in city_of:
            continue
        cid = len(cities)
        members = [a]
        city_of[a] = cid
        cx, cy = cell(a)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for b in grid.get((cx + dx, cy + dy), ()):
                    if b in city_of:
                        continue
                    if haversine(rec[a][4], rec[a][5], rec[b][4], rec[b][5]) <= CITY_RADIUS_KM:
                        city_of[b] = cid
                        members.append(b)
        cities.append(members)
    return city_of, cities


def main():
    stations = load(RAW_STATIONS)
    trains = load(RAW_TRAINS)

    # Index only stations that have coordinates.
    idx = {}
    rec = []
    lookup = {}
    for f in stations:
        g = f["geometry"]
        if not g:
            continue
        p = f["properties"]
        code = p["code"]
        if code in idx:
            continue
        lng, lat = g["coordinates"]
        i = len(rec)
        idx[code] = i
        rec.append([code, p.get("name") or code, p.get("state") or "",
                    p.get("zone") or "", round(lat, 5), round(lng, 5)])
        lookup.setdefault(key(lng, lat), i)

    out_trains = []
    reachable = defaultdict(set)
    adjacent = defaultdict(set)
    train_count = defaultdict(int)
    for f in trains:
        g = f["geometry"]
        if not g or g["type"] != "LineString":
            continue
        p = f["properties"]
        stops = []
        for lng, lat in g["coordinates"]:
            si = lookup.get(key(lng, lat))
            if si is None:
                continue
            if not stops or stops[-1] != si:
                stops.append(si)
        if len(stops) < 2:
            continue
        uniq = set(stops)
        for s in uniq:
            reachable[s] |= uniq
            train_count[s] += 1
        for a, b in zip(stops, stops[1:]):
            adjacent[a].add(b)
            adjacent[b].add(a)
        dur = (p.get("duration_h") or 0) * 60 + (p.get("duration_m") or 0)
        out_trains.append([
            p.get("number") or "", p.get("name") or "", p.get("type") or "",
            p.get("zone") or "", p.get("distance") or 0, dur, stops,
        ])

    # Per-station stats (deg/trn/jdeg/hub). city column appended after clustering.
    for i, r in enumerate(rec):
        s = reachable.get(i)
        r += [len(s) - 1 if s else 0, train_count.get(i, 0),
              len(adjacent.get(i, ())), 0]   # hub recomputed at city level below

    # ---- cluster into cities ----
    city_of, city_members = cluster_cities(rec)
    for i, r in enumerate(rec):
        r.append(city_of.get(i, -1))   # station column 10 = city id (-1 if unconnected)

    # ---- city-level reachability graph ----
    city_reach = defaultdict(set)      # city -> set of directly-reachable cities
    city_trains = defaultdict(set)     # city -> set of trains touching it
    city_adj = defaultdict(set)        # city -> physically-adjacent cities (lines)
    for t_idx, tr in enumerate(out_trains):
        seq = []                       # ordered unique cities along the route
        for s in tr[6]:
            c = city_of[s]
            if not seq or seq[-1] != c:
                seq.append(c)
        cset = set(seq)
        for c in cset:
            city_reach[c] |= cset
            city_trains[c].add(t_idx)
        for x, y in zip(seq, seq[1:]):
            city_adj[x].add(y)
            city_adj[y].add(x)

    # Build city records. The display name/position come from the busiest
    # "real" station in the cluster (skip technical points like bridges/yards/
    # cabins that often out-rank the terminal), so Chennai shows as CHENNAI
    # CENTRAL, not BASIN BRIDGE JN.
    TECH = ("BRIDGE", "CABIN", "YARD", "BLOCK", "PANEL", "GOODS", "SORTING",
            "LINK", "COACHING", "DEPOT", "LOCO", "WORKS", "HALT")
    # Suburban through-stations can out-count the actual terminal (Kopar Road
    # beats Kalyan Jn by a few passing trains), so among members close to the
    # top train count, prefer a name that reads like a real terminal.
    MAJOR = (" JN", "JUNCTION", "CENTRAL", "TERMINUS", " CITY", "CANT", " CTL")

    def representative(members):
        real = [m for m in members if not any(w in rec[m][1].upper() for w in TECH)]
        pool = real or members
        top = max(rec[m][7] for m in pool)
        near = [m for m in pool if rec[m][7] >= 0.85 * top]
        majors = [m for m in near if any(w in rec[m][1].upper() for w in MAJOR)]
        return max(majors or pool, key=lambda m: rec[m][7])

    # Well-known metros get their real city name (the raw data only has
    # station names, so Mumbai would otherwise surface as "KOPAR ROAD" or
    # "MUMBAI CST"). A metro claims the busiest cluster with a member within
    # METRO_KM of its centre.
    METRO_KM = 12.0
    METROS = {
        "MUMBAI": (18.940, 72.835), "DELHI": (28.642, 77.219),
        "KOLKATA": (22.583, 88.342), "CHENNAI": (13.082, 80.275),
        "BENGALURU": (12.978, 77.570), "HYDERABAD": (17.434, 78.501),
        "AHMEDABAD": (23.027, 72.601), "PUNE": (18.529, 73.874),
        "JAIPUR": (26.919, 75.788), "LUCKNOW": (26.831, 80.923),
        "KANPUR": (26.454, 80.351), "NAGPUR": (21.152, 79.089),
        "PATNA": (25.602, 85.137), "BHOPAL": (23.268, 77.401),
        "INDORE": (22.717, 75.868), "VARANASI": (25.320, 82.987),
        "AMRITSAR": (31.634, 74.873), "COIMBATORE": (11.001, 76.966),
        "KOCHI": (9.985, 76.285), "THIRUVANANTHAPURAM": (8.487, 76.952),
        "VISAKHAPATNAM": (17.720, 83.290), "SURAT": (21.206, 72.837),
        "VADODARA": (22.310, 73.181), "GUWAHATI": (26.181, 91.746),
        "BHUBANESWAR": (20.266, 85.844), "CHANDIGARH": (30.720, 76.780),
        "AGRA": (27.157, 78.008), "GWALIOR": (26.215, 78.180),
        "JODHPUR": (26.288, 73.020), "MADURAI": (9.920, 78.120),
        "RAIPUR": (21.250, 81.630), "RANCHI": (23.365, 85.335),
        "JAMMU": (32.716, 74.865), "DEHRADUN": (30.317, 78.032),
        "PRAYAGRAJ": (25.446, 81.825), "VIJAYAWADA": (16.518, 80.619),
        "MYSURU": (12.308, 76.646),
    }
    metro_name = {}     # cid -> metro name
    for name, (mlat, mlng) in METROS.items():
        best, best_trn = None, -1
        for cid, members in enumerate(city_members):
            if any(haversine(mlat, mlng, rec[m][4], rec[m][5]) <= METRO_KM
                   for m in members):
                trn = len(city_trains.get(cid, ()))
                if trn > best_trn:
                    best, best_trn = cid, trn
        if best is not None:
            metro_name[best] = name

    cities = []
    for cid, members in enumerate(city_members):
        rep = representative(members)
        deg = len(city_reach.get(cid, ())) - 1
        trn = len(city_trains.get(cid, ()))
        lines = len(city_adj.get(cid, ()))
        hub = 1 if (trn >= CITY_HUB_MIN_TRAINS and lines >= CITY_HUB_MIN_LINES) else 0
        cities.append([
            metro_name.get(cid, rec[rep][1]),   # name (metro override if known)
            rec[rep][4], rec[rep][5],           # lat, lng
            rec[rep][2],                        # state
            deg, trn, hub, len(members), lines,
        ])

    json.dump({"cols": ["code", "name", "state", "zone", "lat", "lng",
                        "deg", "trn", "jdeg", "hub", "city"],
               "rows": rec}, open(OUT_STATIONS, "w"), separators=(",", ":"))
    json.dump({"cols": ["number", "name", "type", "zone", "dist", "dur", "stops"],
               "rows": out_trains}, open(OUT_TRAINS, "w"), separators=(",", ":"))
    json.dump({"cols": ["name", "lat", "lng", "state", "deg", "trn", "hub", "nst", "lines"],
               "rows": cities}, open(OUT_CITIES, "w"), separators=(",", ":"))

    cdeg = [c[4] for c in cities]
    cdeg_s = sorted(cdeg)
    n = len(cdeg_s)
    hubs = sorted((c for c in cities if c[6]), key=lambda c: -c[5])
    multi = sum(1 for c in cities if c[7] > 1)
    print(f"stations: {len(rec)}  ({sum(1 for r in rec if r[6])} connected)")
    print(f"trains:   {len(out_trains)}")
    print(f"cities:   {len(cities)}  ({multi} multi-station)")
    print(f"city degree pct: p50={cdeg_s[n//2]} p90={cdeg_s[int(n*.9)]} "
          f"p99={cdeg_s[int(n*.99)]} max={cdeg_s[-1]}")
    print(f"major hub cities: {len(hubs)}  e.g. " +
          ", ".join(f"{h[0]}({h[7]}st)" for h in hubs[:10]))
    print(f"output:   {os.path.getsize(OUT_STATIONS)//1024}KB stations, "
          f"{os.path.getsize(OUT_TRAINS)//1024}KB trains, "
          f"{os.path.getsize(OUT_CITIES)//1024}KB cities")


if __name__ == "__main__":
    main()
