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
    """Внутренний helper: получает OAuth-токен по client credentials, если они заданы."""
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

    _token_cache["token"] = token
    _token_cache["exp"] = now + max(60, expires_in - 30)
    return token


async def _opensky_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Внутренний helper: GET к OpenSky с опциональным Bearer-токеном."""
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
    Внутренний helper: нормализует массив states из /states/all
    в список словарей.
    """
    states = raw.get("states") or []
    out = []

    for s in states:
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

async def _normalized_states_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float
) -> Dict[str, Any]:
    """Внутренний helper: возвращает нормализованные states по bbox."""
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    raw = await _opensky_get("/states/all", params)
    items = _normalize_states(raw)
    return {"bbox": params, "count": len(items), "states": items}


# ---------------------------
# MCP tools
# ---------------------------

@mcp.tool
def opensky_regions_catalog() -> Dict[str, Any]:
    """
    Каталог демонстрационных регионов.

    Назначение:
    - Дать агенту и пользователю готовые пресеты областей наблюдения,
      чтобы быстро стартовать без ручного ввода координат.
    - Каталог НЕ ограничивает пользователя: при необходимости можно
      задавать bbox вручную в других инструментах.

    Возвращает:
    - Список регионов с именем и bbox (lamin/lomin/lamax/lomax).
    - Примечание о назначении каталога.
    """
    return {
        "regions": [
            {
                "name": name,
                "bbox": {"lamin": box[0], "lomin": box[1], "lamax": box[2], "lomax": box[3]},
            }
            for name, box in REGIONS.items()
        ],
        "note": "Пресеты для демо. При необходимости задавайте bbox вручную.",
    }


@mcp.tool
async def opensky_live_states_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float, extended: int = 0
) -> Dict[str, Any]:
    """
    Живые сырые данные OpenSky в заданном прямоугольнике (bounding box).

    Когда использовать:
    - Для обучения работе с REST/JSON на «честном» ответе API.
    - Когда важно видеть исходный формат OpenSky без обработки.

    Параметры:
    - lamin, lomin, lamax, lomax: границы области в градусах.
    - extended: 0 или 1. Если 1, запрашивает расширенный формат states
      (если доступен для текущего режима доступа).

    Возвращает:
    - bbox: параметры запроса.
    - raw: исходный ответ OpenSky /states/all.
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
    """
    Живые данные OpenSky в bounding box, приведённые к удобному виду.

    Когда использовать:
    - Для запросов пользователя «перечисли самолёты…».
    - Для аналитических ответов, где важны скорость/высота/позывные.

    Отличие от opensky_live_states_bbox:
    - Этот инструмент нормализует индексные массивы OpenSky
      в список объектов с понятными полями.

    Возвращает:
    - bbox: параметры запроса.
    - count: количество обнаруженных бортов с координатами.
    - states: список объектов:
      callsign, icao24, lat, lon, alt_ft, speed_kmh, track_deg,
      on_ground, origin_country, last_contact.
    """
    return await _normalized_states_bbox(lamin, lomin, lamax, lomax)


@mcp.tool
async def opensky_airspace_summary_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float, top_n: int = 5
) -> Dict[str, Any]:
    """
    Краткая диспетчерская сводка по воздушной обстановке в области.

    Когда использовать:
    - Для вопросов «что происходит над…», «сколько самолётов…»,
      «кто самый быстрый/высокий…».
    - Для краткого сравнения нескольких областей.

    Логика:
    - Берёт нормализованные states по bbox.
    - Считает общее количество.
    - Формирует топ-N по скорости и высоте.
    - Считает частотность префиксов позывных.

    Параметры:
    - top_n: размер списков лидеров.

    Возвращает:
    - bbox, count,
    - top_by_speed, top_by_altitude,
    - top_callsign_prefixes,
    - note о том, что это живой срез.
    """
    data = await _normalized_states_bbox(lamin, lomin, lamax, lomax)
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
    # Для локального self-hosted теста по HTTP:
    mcp.run(transport="http", host="0.0.0.0", port=8000)
