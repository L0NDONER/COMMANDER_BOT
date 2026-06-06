"""
navigation/router.py — FastAPI router, included by web_app.py.

Serves:
  GET  /navigation
  GET  /navigation/navigation.js
  POST /api/navigation/optimise
  POST /api/navigation/next
  POST /api/navigation/scan
"""
import json
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from courier_gps import Vec2, _latlon_to_xy
from geocoder import geocode_address
from route_optimiser import Stop, _dist, optimise_route

POSTCODES: dict = {}
for _f in sorted(_HERE.glob("postcodes/*.json")):
    _d = json.load(open(_f))
    if _d.get("coords") and _d["coords"][0]:
        POSTCODES[_d["postcode"]] = _d

SESSIONS: dict = {}

TRAVEL_MS   = 25 * 1000 / 3600
DWELL_S     = 90
THROAT_STEP = 2.0

_FLAT     = {"flat", "apt", "apartment"}
_FARM     = {"farm", "farmhouse", "barn", "barns", "drift", "grange", "dairy farm"}
_COTTAGE  = {"cottage", "cottages", "lodge", "lodges"}
_HOUSE    = {"house", "hall", "manor", "villa", "bungalow", "chalet", "holt"}
_BUSINESS = {"ltd", "limited", "co.", "services", "solutions", "group", "centre", "center"}


def _prop_type(addr: str) -> str:
    a = addr.lower()
    if any(t in a for t in _FLAT): return "FLAT"
    if any(t in a for t in _FARM): return "FARM"
    if any(t in a for t in _COTTAGE): return "COTTAGE"
    if any(t in a for t in _HOUSE): return "HOUSE"
    if any(t in a for t in _BUSINESS): return "BUSINESS"
    if re.match(r"^\d+[a-z]?\s", a): return "HOUSE"
    return "PROPERTY"


def _bubble_name(pd: dict, postcode: str) -> str:
    streets = pd.get("streets") or []
    if streets:
        return streets[0].title()
    return postcode


def _build_stop(s: Stop, index: int, elapsed: float, pkgs: int,
                pd: dict, lat: float, lon: float) -> dict:
    t_str = f"{int(elapsed // 3600)}h{int((elapsed % 3600) // 60):02d}m"
    throat_m = None
    if s.throat_depth is not None:
        throat_m = 0 if s.throat_depth == 0 else int(s.throat_depth * THROAT_STEP)
    meta: dict = {}
    if pd:
        meta["pattern"]          = pd.get("pattern")
        meta["segment_a"]        = pd.get("segment_a")
        meta["segment_b"]        = pd.get("segment_b")
        meta["segment_c"]        = pd.get("segment_c")
        meta["entry"]            = pd.get("preferred_entry") or "—"
        meta["exit"]             = pd.get("preferred_exit") or "—"
        meta["direction"]        = pd.get("estate_direction") or "—"
        meta["delivery_side"]    = pd.get("delivery_side")
        meta["no_uturn"]         = pd.get("no_uturn", False)
        meta["descending"]       = pd.get("descending", False)
        meta["internal_order"]   = pd.get("internal_order") or []
        meta["turning_point"]    = pd.get("turning_point")
        meta["reverse_required"] = pd.get("reverse_required") or []
        meta["raynham_ride"]     = pd.get("raynham_ride")
        meta["prominent_landmark"] = pd.get("prominent_landmark")
        meta["streets"]          = pd.get("streets") or []
        if pd.get("dominant_throat") or pd.get("functional_throat"):
            meta["throat_label"] = pd.get("dominant_throat") or pd.get("functional_throat")
            meta["throat_type"]  = (
                "functional" if pd.get("functional_throat") and not pd.get("dominant_throat")
                else "dominant"
            )
    return {
        "drop":             index + 1,
        "index":            index,
        "address":          s.address,
        "postcode":         s.postcode,
        "lat":              lat,
        "lon":              lon,
        "bubble":           _bubble_name(pd, s.postcode),
        "time_str":         t_str,
        "prop_type":        _prop_type(s.address),
        "pkgs":             pkgs,
        "throat_distance_m": throat_m,
        "no_uturn":         (not s.uturn_side) if s.uturn_side is not None else False,
        "meta":             meta,
    }


router = APIRouter()


@router.get("/navigation")
async def nav_page():
    return FileResponse(_HERE / "static" / "navigation.html")


@router.get("/navigation/navigation.js")
async def nav_js():
    return FileResponse(_HERE / "static" / "navigation.js", media_type="application/javascript")


class Parcel(BaseModel):
    addr: str
    pc: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    barcode: Optional[str] = None


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
    query: Optional[str] = None
    barcode: Optional[str] = None


@router.post("/api/navigation/optimise")
async def optimise(req: OptimiseRequest):
    all_pc = sorted(set(p.pc for p in req.parcels if p.pc in POSTCODES))
    if req.start_pc in POSTCODES and req.start_pc not in all_pc:
        all_pc.append(req.start_pc)
    if not all_pc:
        raise HTTPException(400, "No known postcodes in manifest")

    coords  = [POSTCODES[pc]["coords"] for pc in all_pc]
    ref_lat = sum(c[0] for c in coords) / len(coords)
    ref_lon = sum(c[1] for c in coords) / len(coords)

    finish_key = (
        (req.finish_addr.lower().strip(), req.finish_pc)
        if req.finish_addr and req.finish_pc else None
    )

    parcel_count: defaultdict = defaultdict(int)
    parcel_barcode: dict = {}
    parcel_latlon: dict = {}
    for p in req.parcels:
        key = (p.addr.lower().strip(), p.pc)
        parcel_count[key] += 1
        if p.barcode:
            parcel_barcode[p.barcode.strip().upper()] = key
        if p.lat is not None and p.lon is not None:
            parcel_latlon[key] = (p.lat, p.lon)

    stops: list = []
    finish_stop = None
    seen: set = set()
    for p in req.parcels:
        key = (p.addr.lower().strip(), p.pc)
        if key in seen:
            continue
        seen.add(key)
        if p.pc not in POSTCODES:
            continue
        # Use provided coords or geocode
        if key in parcel_latlon:
            lat, lon = parcel_latlon[key]
            pos = _latlon_to_xy(ref_lat, ref_lon, lat, lon)
        else:
            geo = geocode_address(p.addr, p.pc, ref_lat, ref_lon)
            pos = geo["vec2"] if geo else _latlon_to_xy(ref_lat, ref_lon, *POSTCODES[p.pc]["coords"])
            lat, lon = POSTCODES[p.pc]["coords"]
        s = Stop(
            label=f"{p.addr}, {p.pc}", position=pos, postcode=p.pc, address=p.addr,
            descending=bool(POSTCODES.get(p.pc, {}).get("descending")),
        )
        s._latlon = (lat, lon)
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
            xy = _latlon_to_xy(ref_lat, ref_lon, lm["lat"], lm["lon"])
            obs.append(_Obj(xy.x, xy.y, lm["size"]))
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
    addr_to_drop: dict = {}
    barcode_to_drop: dict = {}

    for i, s in enumerate(route):
        key  = (s.address.lower().strip(), s.postcode)
        pkgs = parcel_count.get(key, 1)
        elapsed += _dist(prev_pos, s.position) / TRAVEL_MS + DWELL_S * pkgs
        prev_pos = s.position
        lat, lon = getattr(s, "_latlon", POSTCODES.get(s.postcode, {}).get("coords", [0, 0]))
        entry = _build_stop(s, i, elapsed, pkgs, POSTCODES.get(s.postcode, {}), lat, lon)
        stop_list.append(entry)
        addr_to_drop[key] = entry["drop"]

    # Build barcode → drop index from original parcel list
    for bc, key in parcel_barcode.items():
        if key in addr_to_drop:
            barcode_to_drop[bc] = addr_to_drop[key]

    session_id = str(uuid.uuid4())[:8]
    SESSIONS[session_id] = {
        "stops":           stop_list,
        "total":           len(stop_list),
        "barcode_to_drop": barcode_to_drop,
        "addr_to_drop":    addr_to_drop,
    }

    return {
        "session_id":  session_id,
        "total_stops": len(stop_list),
        "total_time":  f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m",
        "stops":       stop_list,
    }


@router.post("/api/navigation/next")
async def next_stop(req: NextRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    nxt = req.current_index + 1
    if nxt >= session["total"]:
        return {"done": True}
    return {"done": False, "stop": session["stops"][nxt]}


@router.post("/api/navigation/scan")
async def scan(req: ScanRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Barcode lookup (exact match)
    if req.barcode:
        bc = req.barcode.strip().upper()
        drop = session["barcode_to_drop"].get(bc)
        if drop is not None:
            stop = session["stops"][drop - 1]
            return {
                "drop":             stop["drop"],
                "address":          stop["address"],
                "postcode":         stop["postcode"],
                "lat":              stop["lat"],
                "lon":              stop["lon"],
                "bubble":           stop["bubble"],
                "throat_distance_m": stop["throat_distance_m"],
                "no_uturn":         stop["no_uturn"],
                "time_str":         stop["time_str"],
            }
        raise HTTPException(404, f"Barcode {bc} not in manifest")

    # Text search fallback
    if req.query:
        q = req.query.strip().lower()
        matches = [
            s for s in session["stops"]
            if q in s["address"].lower() or q in s["postcode"].lower()
        ]
        return {"matches": matches}

    raise HTTPException(400, "Provide barcode or query")
