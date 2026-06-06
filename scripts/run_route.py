#!/usr/bin/env python3
"""
run_route.py — optimise and print a manifest route.

Usage:
    python3 scripts/run_route.py
"""
import json, math, sys, re
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from courier_gps import Vec2, _latlon_to_xy
from geocoder import geocode_address
from route_optimiser import Stop, make_bubbles, classify_throats, sequence_bubble, optimise_route, _dist

PARCELS = [
    ("2 William O'Callaghan Place",    "NR19 2BU"),
    ("Gemma Le Claire",                "NR19 2DQ"),
    ("1 Hoe Lodge Cottages",           "NR19 2DQ"),
    ("3 Sheddick Court",               "NR19 2DT"),
    ("3 Sheddick Court",               "NR19 2DT"),
    ("Woodacre 3A Stanton Close",      "NR19 2DZ"),
    ("4A Sandy Lane",                  "NR19 2EA"),
    ("4A Sandy Lane",                  "NR19 2EA"),
    ("4A Sandy Lane",                  "NR19 2EA"),
    ("4A Sandy Lane",                  "NR19 2EA"),
    ("35 Sandy Lane",                  "NR19 2EB"),
    ("46 Sandy Lane",                  "NR19 2EB"),
    ("Norfolk",                        "NR19 2ED"),
    ("165 Links View Sandy Lane East", "NR19 2ED"),
    ("165 Links View Sandy Lane East", "NR19 2ED"),
    ("173 Links View",                 "NR19 2ED"),
    ("22 Northgate",                   "NR19 2EU"),
    ("NR19 2EU",                       "NR19 2EU"),
    ("NR19 2EU",                       "NR19 2EU"),
    ("14 Northgate",                   "NR19 2EU"),
    ("Toad Hall",                      "NR19 2EU"),
    ("3 Armstrong Drive",              "NR19 2EZ"),
    ("3 Dairy Crescent",               "NR19 2FD"),
    ("Flat 1 Dairy House Sandy Lane",  "NR19 2FE"),
    ("1 Magpie Court",                 "NR19 2FG"),
    ("28 Normandy Drive",              "NR19 2GB"),
    ("17 Stigands Gate",               "NR19 2HF"),
    ("7 Stigands Gate",                "NR19 2HF"),
    ("10 Boton Drive",                 "NR19 2HG"),
    ("14 Boton Drive",                 "NR19 2HG"),
    ("20",                             "NR19 2HQ"),
    ("Drift Farm",                     "NR19 2QD"),
    ("Solucki Old Bridge Gressenhall", "NR19 2QE"),
    ("Hall Farm Church Lane",          "NR19 2QF"),
    ("Columbine Cottage Church Lane",  "NR19 2QF"),
    ("Rabbits Foot Barn Holt Road",    "NR19 2QR"),
    ("2 Gingerbread Cottage",          "NR19 2QX"),
    ("5 Heath Road",                   "NR19 2RX"),
    ("11 Colin McLean Road",           "NR19 2RY"),
    ("13 Colin McLean Road",           "NR19 2RY"),
    ("24 Colin McLean Road",           "NR19 2RY"),
    ("43 Colin McLean Road",           "NR19 2RY"),
    ("20 Colin McLean Road",           "NR19 2RY"),
    ("4 Colin McLean Road",            "NR19 2RY"),
    ("21 Colin McLean Road",           "NR19 2RY"),
    ("28 Colin McLean Road",           "NR19 2RY"),
    ("4 Spelmans Meadow",              "NR19 2SL"),
    ("5 Spelmans Meadow St Hilda Road","NR19 2SL"),
    ("23",                             "NR19 2SP"),
    ("17 Acorn Way",                   "NR19 2SP"),
    ("2 Acorn Way",                    "NR19 2SP"),
    ("3 Oakapple Drive",               "NR19 2SR"),
    ("11 Oakapple Drive",              "NR19 2SR"),
    ("11 Oakapple Drive",              "NR19 2SR"),
    ("21 Oakwood Road",                "NR19 2SS"),
    ("2 Oakwood Close",                "NR19 2ST"),
    ("19 Oakwood Close",               "NR19 2ST"),
    ("3 Oakwood Close",                "NR19 2ST"),
    ("8 Oakwood Close",                "NR19 2ST"),
    ("19 Oakwood Close",               "NR19 2ST"),
    ("4 Oakwood Close",                "NR19 2ST"),
    ("1 Windsor Park",                 "NR19 2SU"),
    ("21 Townshend Road",              "NR19 2YD"),
    ("11 Townshend Road",              "NR19 2YD"),
    ("9 Townshend Road",               "NR19 2YD"),
    ("26 Townshend Road",              "NR19 2YD"),
]

START_ADDR = "Bridge House Gressenhall"
START_PC   = "NR19 2QE"
FINISH_KEY = ("toad hall", "NR19 2EU")
TRAVEL_MS  = 25 * 1000 / 3600   # 25 mph in m/s
DWELL_S    = 90                  # seconds per parcel

FARM_TOKENS     = {'farm','farmhouse','barn','barns','drift','grange','dairy farm'}
COTTAGE_TOKENS  = {'cottage','cottages','lodge','lodges'}
FLAT_TOKENS     = {'flat','apt','apartment'}
HOUSE_TOKENS    = {'house','hall','manor','villa','bungalow','chalet','holt'}
BUSINESS_TOKENS = {'ltd','limited','co.','services','solutions','group','centre','center'}

def prop_type(addr):
    a = addr.lower()
    if any(t in a for t in FLAT_TOKENS):     return 'FLAT'
    if any(t in a for t in FARM_TOKENS):     return 'FARM'
    if any(t in a for t in COTTAGE_TOKENS):  return 'COTTAGE'
    if any(t in a for t in HOUSE_TOKENS):    return 'HOUSE'
    if any(t in a for t in BUSINESS_TOKENS): return 'BUSINESS'
    if re.match(r'^\d+[a-z]?\s', a):         return 'HOUSE'
    return 'PROPERTY'


def main():
    pcs = {}
    for f in sorted(Path(__file__).parent.glob('postcodes/*.json')):
        d = json.load(open(f))
        if d.get('coords') and d['coords'][0]:
            pcs[d['postcode']] = d

    all_pc = sorted(set(pc for _, pc in PARCELS if pc in pcs))
    coords = [pcs[pc]['coords'] for pc in all_pc]
    ref_lat = sum(c[0] for c in coords) / len(coords)
    ref_lon = sum(c[1] for c in coords) / len(coords)

    parcel_count = defaultdict(int)
    for addr, pc in PARCELS:
        parcel_count[(addr.lower().strip(), pc)] += 1

    stops = []
    seen = set()
    finish_stop = None
    for addr, pc in PARCELS:
        key = (addr.lower().strip(), pc)
        if key in seen: continue
        seen.add(key)
        if pc not in pcs: continue
        geo = geocode_address(addr, pc, ref_lat, ref_lon)
        pos = geo['vec2'] if geo else _latlon_to_xy(ref_lat, ref_lon, *pcs[pc]['coords'])
        s = Stop(label=f"{addr}, {pc}", position=pos, postcode=pc, address=addr,
                 descending=bool(pcs.get(pc, {}).get('descending')))
        if key == FINISH_KEY:
            finish_stop = s
        else:
            stops.append(s)

    class Obj:
        def __init__(self, x, y, sz): self.position = Vec2(x, y); self.size = sz
    class WorldImpl:
        def __init__(self, obs): self.objects = obs

    all_obs = []
    for pc in all_pc:
        for lm in pcs[pc].get('landmarks') or []:
            xy = _latlon_to_xy(ref_lat, ref_lon, lm['lat'], lm['lon'])
            all_obs.append(Obj(xy.x, xy.y, lm['size']))
    world = WorldImpl(all_obs)

    start_geo = geocode_address(START_ADDR, START_PC, ref_lat, ref_lon)
    start_pos = start_geo['vec2']

    route = optimise_route(stops, world, start_pos, 0.0)
    if finish_stop:
        route.append(finish_stop)

    elapsed = 0
    prev_pos = start_pos
    cur_pc   = None

    print(f"  Start : {START_ADDR} ({START_PC})")
    print(f"  Finish: Toad Hall (NR19 2EU)")
    print(f"  Manifest: {len(PARCELS)} parcels  →  {len(route)} stops\n")
    print("═" * 92)

    for i, s in enumerate(route):
        key    = (s.address.lower().strip(), s.postcode)
        pkgs   = parcel_count.get(key, 1)
        travel = _dist(prev_pos, s.position) / TRAVEL_MS
        elapsed += travel + DWELL_S * pkgs
        prev_pos = s.position
        t_str  = f"{int(elapsed//3600)}h{int((elapsed%3600)//60):02d}m"
        throat = f"⚠ THROAT@{s.throat_depth*2}m" if s.throat_depth is not None else ""
        uturn  = "" if s.uturn_side else "⚠ NO-UTURN"
        ptype  = prop_type(s.address)

        if s.postcode != cur_pc:
            cur_pc = s.postcode
            pd = pcs.get(cur_pc, {})
            streets   = ', '.join(pd.get('streets') or ['—'])
            direction = pd.get('estate_direction') or '—'
            pref_in   = pd.get('preferred_entry') or '—'
            pref_out  = pd.get('preferred_exit')  or '—'
            print(f"\n┌─ {cur_pc}  {streets}")
            print(f"│  visits={pd.get('visit_count',0)}  last_seen={pd.get('last_seen','—')}  density={pd.get('typical_density','—')}")
            print(f"│  entry={pref_in}  exit={pref_out}")
            if pd.get('pattern'):
                print(f"│  pattern={pd['pattern']}  side={pd.get('delivery_side','—')}")
                if pd.get('segment_a'): print(f"│    A: {pd['segment_a']}")
                if pd.get('segment_b'): print(f"│    B: {pd['segment_b']}")
                if pd.get('segment_c'): print(f"│    C: {pd['segment_c']}")
            if pd.get('dominant_throat') or pd.get('functional_throat'):
                throat_label = pd.get('dominant_throat') or pd.get('functional_throat')
                throat_type  = 'functional_throat' if pd.get('functional_throat') and not pd.get('dominant_throat') else 'throat'
                no_u  = '  ⚠ NO-UTURN' if pd.get('no_uturn') else ''
                desc  = '  ↓ descending' if pd.get('descending') else ''
                print(f"│  {throat_type}={throat_label}  side={pd.get('delivery_side','—')}{no_u}{desc}")
            if pd.get('turning_point'):
                rev = ', '.join(pd.get('reverse_required') or [])
                print(f"│  turning_point={pd['turning_point']}  reverse=[{rev}]")
            if pd.get('internal_order'):
                print(f"│  order={' → '.join(pd['internal_order'])}")
            rr = pd.get('raynham_ride') or {}
            if rr:
                print(f"│  raynham_ride: intercept={rr.get('intercept','—')}  approach={rr.get('approach','—')}")
                print(f"│    flow={rr.get('flow','—')}")
                flags_rr = []
                if rr.get('walk_of_shame'): flags_rr.append('WALK-OF-SHAME')
                if rr.get('no_uturn'):      flags_rr.append('NO-UTURN')
                if flags_rr: print(f"│    ⚠ {' '.join(flags_rr)}")
            if pd.get('prominent_landmark'):
                print(f"│  landmark={pd['prominent_landmark']}")
            print(f"│  direction={direction}")
            crumbs = pd.get('breadcrumbs') or []
            if crumbs:
                for c in crumbs[-2:]:
                    print(f"│  [{c.get('date','?')}] {c.get('entry','?')} → {c.get('next_postcode','?')}  (manifest {c.get('manifest_id','?')})")
            else:
                print(f"│  breadcrumbs: none")
            print(f"└{'─'*62}")

        flags = '  ' + '  '.join(filter(None, [throat, uturn]))
        print(f"  {i+1:>2}  {t_str}  [{ptype:<8}]  {pkgs}pkg  {s.address:<40}{flags}")

    print(f"\n{'═'*92}")
    print(f"  Total: {int(elapsed//3600)}h {int((elapsed%3600)//60)}m  |  {len(PARCELS)} parcels  |  {len(route)} stops")


if __name__ == "__main__":
    main()
