from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastmcp import FastMCP


# ---------------------------
# Config
# ---------------------------

OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")

# Демо-пресеты. Их можно расширять, но они не обязательны для работы.
REGIONS: Dict[str, Tuple[float, float, float, float]] = {
    "moscow": (55.20, 36.90, 56.10, 38.30),
    "spb": (59.50, 29.70, 60.20, 31.20),
    "komi": (58.90, 44.90, 68.70, 66.70),
    "komi_wide": (58.50, 44.00, 69.20, 67.20),
}

mcp = FastMCP("opensky-live")


# ---------------------------
# OAuth (optional)
# ---------------------------

_token_cache: Dict[str, Any] = {"token": None, "exp": 0.0}


async def _get_bearer_token() -> Optional[str]:
    """Return cached OAuth token if client credentials exist; else None."""
    if not CLIENT_ID or not CLIENT_SECRET:
        return None

    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"]:
        return _token_cache["token"]

    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data=data)
        r.raise_for_status()
        payload = r.json()

    token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 1800))

    # небольшой буфер на истечение
    _token_cache["token"] = token
    _token_cache["exp"] = now + max(60, expires_in - 30)

    return token


async def _opensky_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    token = await _get_bearer_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{OPENSKY_BASE}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()


# ---------------------------
# Helpers
# ---------------------------

def _to_kmh(v_ms: Optional[float]) -> Optional[float]:
    return None if v_ms is None else v_ms * 3.6


def _to_ft(alt_m: Optional[float]) -> Optional[float]:
    return None if alt_m is None else alt_m * 3.28084


def _normalize_states(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    OpenSky /states/all returns index-based arrays.
    We'll map the stable subset we used in your demos.
    """
    states = raw.get("states") or []
    out = []

    for s in states:
        # индексы по документации OpenSky states
        icao24 = s[0]
        callsign = (s[1] or "").strip() or "UNKNOWN"
        origin_country = s[2]
        last_contact = s[4]
        lon = s[5]
        lat = s[6]
        baro_alt_m = s[7]
        on_ground = s[8]
        velocity_ms = s[9]
        true_track = s[10]

        if lat is None or lon is None:
            continue

        out.append({
            "icao24": icao24,
            "callsign": callsign,
            "origin_country": origin_country,
            "lat": lat,
            "lon": lon,
            "alt_ft": _to_ft(baro_alt_m),
            "speed_kmh": _to_kmh(velocity_ms),
            "track_deg": true_track,
            "on_ground": on_ground,
            "last_contact": last_contact,
        })

    return out


# ---------------------------
# MCP tools
# ---------------------------

@mcp.tool
def opensky_regions_catalog() -> Dict[str, Any]:
    """Каталог демонстрационных регионов с bounding box."""
    return {
        "regions": [
            {
                "name": name,
                "bbox": {"lamin": box[0], "lomin": box[1], "lamax": box[2], "lomax": box[3]},
            }
            for name, box in REGIONS.items()
        ],
        "note": "Каталог — удобные пресеты для демо. Можно задавать bbox вручную.",
    }


@mcp.tool
async def opensky_live_states_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float, extended: int = 0
) -> Dict[str, Any]:
    """
    Живые сырые данные OpenSky в заданном bounding box.
    Использует /states/all с ограничением области.
    """
    params: Dict[str, Any] = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    if extended:
        params["extended"] = 1
    raw = await _opensky_get("/states/all", params)
    return {"bbox": params, "raw": raw}


@mcp.tool
async def opensky_normalized_states_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float
) -> Dict[str, Any]:
    """Живые данные OpenSky в bounding box, нормализованные в список объектов."""
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    raw = await _opensky_get("/states/all", params)
    items = _normalize_states(raw)
    return {"bbox": params, "count": len(items), "states": items}


@mcp.tool
async def opensky_airspace_summary_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float, top_n: int = 5
) -> Dict[str, Any]:
    """
    Сводка по области: количество, топ по скорости и высоте, топ префиксов позывных.
    """
    data = await opensky_normalized_states_bbox(lamin, lomin, lamax, lomax)
    states = data["states"]

    by_speed = sorted(
        [s for s in states if s["speed_kmh"] is not None],
        key=lambda x: x["speed_kmh"],
        reverse=True,
    )[:top_n]

    by_alt = sorted(
        [s for s in states if s["alt_ft"] is not None],
        key=lambda x: x["alt_ft"],
        reverse=True,
    )[:top_n]

    prefixes: Dict[str, int] = {}
    for s in states:
        cs = s["callsign"]
        p = cs[:3] if cs and cs != "UNKNOWN" else "UNK"
        prefixes[p] = prefixes.get(p, 0) + 1

    top_prefixes = sorted(prefixes.items(), key=lambda x: x[1], reverse=True)[:top_n]

    return {
        "bbox": data["bbox"],
        "count": data["count"],
        "top_by_speed": by_speed,
        "top_by_altitude": by_alt,
        "top_callsign_prefixes": top_prefixes,
        "note": "Живой срез OpenSky на текущий момент.",
    }



if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
