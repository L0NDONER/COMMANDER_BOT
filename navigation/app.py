#!/usr/bin/env python3
"""
navigation/app.py — route optimisation + step-through navigation API.

Endpoints:
  POST /api/navigation/optimise  — build session from manifest
  POST /api/navigation/next      — advance to next stop
  POST /api/navigation/scan      — find stop by address/postcode fragment
"""
import json
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from courier_gps import Vec2, _latlon_to_xy
from geocoder import geocode_address
from route_optimiser import Stop, _dist, optimise_route

POSTCODES: dict = {}
for _f in sorted(Path(__file__).parent.glob("postcodes/*.json")):
    _d = json.load(open(_f))
    if _d.get("coords") and _d["coords"][0]:
        POSTCODES[_d["postcode"]] = _d

COMPLEXES: dict = {}  # postcode → complex schema (campus sub-dict)
for _f in sorted(Path(__file__).parent.glob("complexes/*.json")):
    _d = json.load(open(_f))
    _c = _d.get("campus", _d)
    if _c.get("postcode") and _c.get("coords"):
        COMPLEXES[_c["postcode"]] = _c

SESSIONS: dict = {}

TRAVEL_MS = 25 * 1000 / 3600
DWELL_S = 90

_FLAT = {"flat", "apt", "apartment"}
_FARM = {"farm", "farmhouse", "barn", "barns", "drift", "grange", "dairy farm"}
_COTTAGE = {"cottage", "cottages", "lodge", "lodges"}
_HOUSE = {"house", "hall", "manor", "villa", "bungalow", "chalet", "holt"}
_BUSINESS = {"ltd", "limited", "co.", "services", "solutions", "group", "centre", "center"}


def _match_complex_cluster(addr: str, complex_schema: dict) -> dict | None:
    """Return the matching cluster/spur dict from a complex schema, or None."""
    a = addr.lower()
    for c in complex_schema.get("clusters", []):
        if any(alias in a for alias in c.get("aliases", [])):
            return c
    for s in complex_schema.get("farm_spurs", []):
        if any(alias in a for alias in s.get("aliases", [])):
            return s
    return None


def _prop_type(addr: str) -> str:
    a = addr.lower()
    if any(t in a for t in _FLAT): return "FLAT"
    if any(t in a for t in _FARM): return "FARM"
    if any(t in a for t in _COTTAGE): return "COTTAGE"
    if any(t in a for t in _HOUSE): return "HOUSE"
    if any(t in a for t in _BUSINESS): return "BUSINESS"
    if re.match(r"^\d+[a-z]?\s", a): return "HOUSE"
    return "PROPERTY"


def _build_stop(s: Stop, index: int, elapsed: float, pkgs: int, pd: dict,
                complex_meta: dict | None = None) -> dict:
    t_str = f"{int(elapsed // 3600)}h{int((elapsed % 3600) // 60):02d}m"
    throat = None
    if s.throat_depth is not None:
        throat = "entry" if s.throat_depth == 0 else f"{s.throat_depth * 2}m"
    meta: dict = {}
    if pd:
        meta["pattern"] = pd.get("pattern")
        meta["segment_a"] = pd.get("segment_a")
        meta["segment_b"] = pd.get("segment_b")
        meta["segment_c"] = pd.get("segment_c")
        meta["entry"] = pd.get("preferred_entry") or "—"
        meta["exit"] = pd.get("preferred_exit") or "—"
        meta["direction"] = pd.get("estate_direction") or "—"
        meta["delivery_side"] = pd.get("delivery_side")
        meta["no_uturn"] = pd.get("no_uturn", False)
        meta["descending"] = pd.get("descending", False)
        meta["internal_order"] = pd.get("internal_order") or []
        meta["turning_point"] = pd.get("turning_point")
        meta["reverse_required"] = pd.get("reverse_required") or []
        meta["raynham_ride"] = pd.get("raynham_ride")
        meta["prominent_landmark"] = pd.get("prominent_landmark")
        if pd.get("dominant_throat") or pd.get("functional_throat"):
            meta["throat_label"] = pd.get("dominant_throat") or pd.get("functional_throat")
            meta["throat_type"] = (
                "functional" if pd.get("functional_throat") and not pd.get("dominant_throat")
                else "dominant"
            )
        meta["streets"] = pd.get("streets") or []
    if complex_meta:
        meta["complex_name"] = complex_meta.get("complex_name")
        meta["complex_throat"] = complex_meta.get("preferred_throat")
        meta["complex_spine"] = complex_meta.get("spine")
        meta["complex_cluster"] = complex_meta.get("cluster_name")
        meta["complex_door"] = complex_meta.get("door_id")
        meta["complex_side"] = complex_meta.get("side")
        meta["complex_walk"] = complex_meta.get("walk_required", False)
        meta["complex_security"] = complex_meta.get("security_check", False)
        meta["complex_note"] = complex_meta.get("note")
        meta["complex_constraints"] = complex_meta.get("constraints")
    return {
        "index": index,
        "address": s.address,
        "postcode": s.postcode,
        "time_str": t_str,
        "prop_type": _prop_type(s.address),
        "pkgs": pkgs,
        "throat": throat,
        "no_uturn": (not s.uturn_side) if s.uturn_side is not None else False,
        "meta": meta,
    }


app = FastAPI(title="Navigation")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "navigation.html")


class Parcel(BaseModel):
    addr: str
    pc: str


class OptimiseRequest(BaseModel):
    parcels: list[Parcel]
    start_addr: str
    start_pc: str
    finish_addr: Optional[str] = None
    finish_pc: Optional[str] = None


class NextRequest(BaseModel):
    session_id: str
    current_index: int


class ScanRequest(BaseModel):
    session_id: str
    query: str


@app.post("/api/navigation/optimise")
async def optimise(req: OptimiseRequest):
    all_pc = sorted(set(p.pc for p in req.parcels if p.pc in POSTCODES))
    if req.start_pc in POSTCODES and req.start_pc not in all_pc:
        all_pc.append(req.start_pc)
    if not all_pc and not any(p.pc in COMPLEXES for p in req.parcels):
        raise HTTPException(400, "No known postcodes in manifest")

    # Seed ref coords from known postcodes; fall back to complex coords if needed
    coords = [POSTCODES[pc]["coords"] for pc in all_pc]
    if not coords:
        for p in req.parcels:
            if p.pc in COMPLEXES:
                coords.append(COMPLEXES[p.pc]["coords"])
                break
    ref_lat = sum(c[0] for c in coords) / len(coords)
    ref_lon = sum(c[1] for c in coords) / len(coords)

    finish_key = (req.finish_addr.lower().strip(), req.finish_pc) if req.finish_addr and req.finish_pc else None

    parcel_count: defaultdict = defaultdict(int)
    for p in req.parcels:
        parcel_count[(p.addr.lower().strip(), p.pc)] += 1

    # parcel key → complex_meta dict for stops that hit a known complex
    complex_meta_map: dict = {}

    stops: list = []
    finish_stop = None
    seen: set = set()
    for p in req.parcels:
        key = (p.addr.lower().strip(), p.pc)
        if key in seen:
            continue
        seen.add(key)
        if p.pc not in POSTCODES and p.pc not in COMPLEXES:
            continue
        if p.pc in POSTCODES:
            geo = geocode_address(p.addr, p.pc, ref_lat, ref_lon)
            pos = geo["vec2"] if geo else _latlon_to_xy(ref_lat, ref_lon, *POSTCODES[p.pc]["coords"])
        else:
            cx = COMPLEXES[p.pc]
            pos = _latlon_to_xy(ref_lat, ref_lon, *cx["coords"])
            cluster = _match_complex_cluster(p.addr, cx)
            complex_meta_map[key] = {
                "complex_name": cx["name"],
                "preferred_throat": cx.get("preferred_throat"),
                "spine": cx.get("spine"),
                "cluster_name": cluster["name"] if cluster else None,
                "door_id": cluster.get("door_id") if cluster else None,
                "side": cluster.get("side") if cluster else None,
                "walk_required": cluster.get("walk_required", False) if cluster else False,
                "security_check": cluster.get("security_check", False) if cluster else False,
                "note": cluster.get("note") if cluster else None,
                "constraints": cluster.get("constraints") if cluster else None,
            }
        s = Stop(
            label=f"{p.addr}, {p.pc}", position=pos, postcode=p.pc, address=p.addr,
            descending=bool(POSTCODES.get(p.pc, {}).get("descending")),
        )
        if finish_key and key == finish_key:
            finish_stop = s
        else:
            stops.append(s)

    class _Obj:
        def __init__(self, x, y, sz):
            self.position = Vec2(x, y)
            self.size = sz

    class _World:
        def __init__(self, obs):
            self.objects = obs

    obs = []
    for pc in all_pc:
        for lm in POSTCODES[pc].get("landmarks") or []:
            if "lat" not in lm:
                continue
            xy = _latlon_to_xy(ref_lat, ref_lon, lm["lat"], lm["lon"])
            obs.append(_Obj(xy.x, xy.y, lm.get("size", 1.0)))
    world = _World(obs)

    start_geo = geocode_address(req.start_addr, req.start_pc, ref_lat, ref_lon)
    if not start_geo:
        raise HTTPException(400, f"Could not geocode start: {req.start_addr}")
    start_pos = start_geo["vec2"]

    route = optimise_route(stops, world, start_pos, 0.0)
    if finish_stop:
        route.append(finish_stop)

    elapsed = 0.0
    prev_pos = start_pos
    stop_list = []
    for i, s in enumerate(route):
        key = (s.address.lower().strip(), s.postcode)
        pkgs = parcel_count.get(key, 1)
        elapsed += _dist(prev_pos, s.position) / TRAVEL_MS + DWELL_S * pkgs
        prev_pos = s.position
        stop_list.append(_build_stop(s, i, elapsed, pkgs, POSTCODES.get(s.postcode, {}),
                                    complex_meta=complex_meta_map.get(key)))

    session_id = str(uuid.uuid4())[:8]
    SESSIONS[session_id] = {"stops": stop_list, "total": len(stop_list)}

    return {
        "session_id": session_id,
        "total_stops": len(stop_list),
        "total_time": f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m",
        "stops": stop_list,
    }


@app.post("/api/navigation/next")
async def next_stop(req: NextRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    nxt = req.current_index + 1
    if nxt >= session["total"]:
        return {"done": True}
    return {"done": False, "stop": session["stops"][nxt]}


@app.post("/api/navigation/scan")
async def scan(req: ScanRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    q = req.query.strip().lower()
    matches = [
        s for s in session["stops"]
        if q in s["address"].lower() or q in s["postcode"].lower()
    ]
    return {"matches": matches}
