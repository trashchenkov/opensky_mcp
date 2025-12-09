from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
import httpx

load_dotenv() 
# -------------
# FastMCP import
# -------------
try:
    # Современный пакет FastMCP
    from fastmcp import FastMCP
except ImportError:
    # Фолбэк на reference-реализацию из MCP SDK, если вдруг окружение другое
    from mcp.server.fastmcp import FastMCP  # type: ignore


# ---------------------------
# OpenSky endpoints
# ---------------------------

OPENSKY_BASE = "https://opensky-network.org/api"

# OAuth2 token endpoint (client credentials)
TOKEN_URL = (
    "https://auth.opensky-network.org/"
    "auth/realms/opensky-network/protocol/openid-connect/token"
)

CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")

# Таймауты можно регулировать
HTTP_TIMEOUT = float(os.getenv("OPENSKY_HTTP_TIMEOUT", "20"))


# ---------------------------
# Demo regions presets (optional)
# ---------------------------

REGIONS: Dict[str, Tuple[float, float, float, float]] = {
    "moscow": (55.20, 36.90, 56.10, 38.30),
    "spb": (59.50, 29.70, 60.20, 31.20),
    "komi": (58.90, 44.90, 68.70, 66.70),
    "komi_wide": (58.50, 44.00, 69.20, 67.20),
}


# ---------------------------
# Server
# ---------------------------

mcp = FastMCP("opensky-live")


# ---------------------------
# OAuth cache
# ---------------------------

_token_cache: Dict[str, Any] = {"token": None, "exp": 0.0}


async def _get_bearer_token() -> Optional[str]:
    """
    Внутренний helper:
    - Если client_id/secret не заданы, возвращает None (анонимный режим).
    - Иначе получает OAuth2 токен по client_credentials и кэширует его.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        return None

    now = time.time()
    token = _token_cache.get("token")
    exp = float(_token_cache.get("exp", 0))
    if token and now < exp:
        return token

    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(TOKEN_URL, data=data)
            r.raise_for_status()
            payload = r.json()

        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 1800))

        # небольшой буфер
        _token_cache["token"] = token
        _token_cache["exp"] = now + max(60, expires_in - 30)

        return token
    except Exception:
        # Если токен не берётся — пусть дальше будет анонимный режим.
        return None


# ---------------------------
# Common error formatter
# ---------------------------

def _err(where: str, kind: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "where": where,
            "kind": kind,
            "message": message,
            **extra,
        },
    }


# ---------------------------
# Low-level HTTP to OpenSky with soft errors
# ---------------------------

async def _opensky_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Внутренний helper:
    Делает GET к OpenSky и возвращает:
    - {"ok": True, "data": <json>}
    - {"ok": False, "error": {...}}
    """
    url = f"{OPENSKY_BASE}{path}"

    token = await _get_bearer_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            return {"ok": True, "data": r.json(), "url": url, "params": params}
    except httpx.ConnectError as e:
        return _err("opensky", "connect_error", str(e), url=url, params=params)
    except httpx.ReadTimeout as e:
        return _err("opensky", "timeout", str(e), url=url, params=params)
    except httpx.HTTPStatusError as e:
        status = getattr(e.response, "status_code", None)
        return _err("opensky", "http_status", str(e), status=status, url=url, params=params)
    except Exception as e:
        return _err("opensky", "unknown", str(e), url=url, params=params)


# ---------------------------
# Normalization helpers
# ---------------------------

def _to_kmh(v_ms: Optional[float]) -> Optional[float]:
    return None if v_ms is None else v_ms * 3.6

def _to_ft(alt_m: Optional[float]) -> Optional[float]:
    return None if alt_m is None else alt_m * 3.28084

def _normalize_states(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Нормализует ответ OpenSky /states/all:
    states — список массивов с фиксированными индексами.
    Возвращает список объектов с понятными полями.
    """
    states = raw.get("states") or []
    out: List[Dict[str, Any]] = []

    for s in states:
        # Индексы OpenSky states
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
    """Внутренний helper без декораторов — чтобы tool не вызывал tool."""
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    raw = await _opensky_get("/states/all", params)

    if not raw.get("ok"):
        return raw

    items = _normalize_states(raw["data"])
    return {
        "ok": True,
        "bbox": params,
        "count": len(items),
        "states": items,
    }


async def _airspace_summary_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float, top_n: int = 5
) -> Dict[str, Any]:
    """Внутренний helper: сводка по bbox."""
    data = await _normalized_states_bbox(lamin, lomin, lamax, lomax)

    if not data.get("ok"):
        return data

    states = data["states"]

    by_speed = sorted(
        [s for s in states if s.get("speed_kmh") is not None],
        key=lambda x: x["speed_kmh"],
        reverse=True,
    )[:top_n]

    by_alt = sorted(
        [s for s in states if s.get("alt_ft") is not None],
        key=lambda x: x["alt_ft"],
        reverse=True,
    )[:top_n]

    prefixes: Dict[str, int] = {}
    for s in states:
        cs = s.get("callsign") or "UNKNOWN"
        p = cs[:3] if cs != "UNKNOWN" else "UNK"
        prefixes[p] = prefixes.get(p, 0) + 1

    top_prefixes = sorted(prefixes.items(), key=lambda x: x[1], reverse=True)[:top_n]

    return {
        "ok": True,
        "bbox": data["bbox"],
        "count": data["count"],
        "top_by_speed": by_speed,
        "top_by_altitude": by_alt,
        "top_callsign_prefixes": top_prefixes,
        "note": "Живой срез OpenSky на текущий момент.",
        "disclaimer": "origin_country — это страна регистрации ICAO24, а не маршрут рейса.",
    }


# ---------------------------
# MCP tools
# ---------------------------
@mcp.tool
@mcp.tool
async def opensky_ping_plus(
    generic_url: str = "https://example.com",
) -> dict:
    """
    Диагностика исходящего доступа из окружения MCP-сервера.

    Проверяет:
    1) generic_url — общий интернет
    2) https://opensky-network.org/ — доступность домена/статического ресурса
    3) OAuth2 token endpoint OpenSky — только если заданы OPENSKY_CLIENT_ID/SECRET
    4) https://opensky-network.org/api/states/all — доступность API

    Как читать:
    - generic ok=false -> вероятно нет общего egress из Cloud
    - generic ok=true, opensky_domain ok=false -> проблема с доступом к домену OpenSky
    - opensky_domain ok=true, opensky_api ok=false -> блок/ограничение именно API
    - opensky_auth ok=false при наличии кредов -> проблема OAuth2 из этого окружения
    """

    import os
    import time
    import httpx

    OPENSKY_DOMAIN_URL = "https://opensky-network.org/"
    OPENSKY_API_URL = "https://opensky-network.org/api/states/all"
    OPENSKY_AUTH_URL = (
        "https://auth.opensky-network.org/auth/realms/opensky-network/"
        "protocol/openid-connect/token"
    )

    opensky_params = {
        "lamin": 55.2, "lomin": 36.9,
        "lamax": 56.1, "lomax": 38.3
    }

    def ok_result(url: str, status: int, ms: int, extra: dict | None = None):
        d = {"ok": True, "url": url, "status": status, "ms": ms}
        if extra:
            d.update(extra)
        return d

    def err_result(url: str, e: Exception, extra: dict | None = None):
        d = {
            "ok": False,
            "url": url,
            "error_type": type(e).__name__,
            "detail": str(e) or "",
        }
        if extra:
            d.update(extra)
        return d

    results = {}

    client_id = os.getenv("OPENSKY_CLIENT_ID")
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET")

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:

        # 1) Generic интернет
        t0 = time.time()
        try:
            r = await client.get(generic_url)
            results["generic"] = ok_result(generic_url, r.status_code, int((time.time()-t0)*1000))
        except Exception as e:
            results["generic"] = err_result(generic_url, e)

        # 2) Статический ресурс OpenSky
        t1 = time.time()
        try:
            r = await client.get(OPENSKY_DOMAIN_URL)
            results["opensky_domain"] = ok_result(OPENSKY_DOMAIN_URL, r.status_code, int((time.time()-t1)*1000))
        except Exception as e:
            results["opensky_domain"] = err_result(OPENSKY_DOMAIN_URL, e)

        # 3) OAuth2 token endpoint (если есть креды)
        if client_id and client_secret:
            t2 = time.time()
            try:
                data = {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
                r = await client.post(
                    OPENSKY_AUTH_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                results["opensky_auth"] = ok_result(
                    OPENSKY_AUTH_URL,
                    r.status_code,
                    int((time.time()-t2)*1000),
                )
            except Exception as e:
                results["opensky_auth"] = err_result(OPENSKY_AUTH_URL, e)
        else:
            results["opensky_auth"] = {
                "ok": False,
                "skipped": True,
                "reason": "Missing OPENSKY_CLIENT_ID/OPENSKY_CLIENT_SECRET",
                "url": OPENSKY_AUTH_URL,
            }

        # 4) API OpenSky
        t3 = time.time()
        try:
            r = await client.get(OPENSKY_API_URL, params=opensky_params)
            results["opensky_api"] = ok_result(
                OPENSKY_API_URL,
                r.status_code,
                int((time.time()-t3)*1000),
                extra={"params": opensky_params},
            )
        except Exception as e:
            results["opensky_api"] = err_result(
                OPENSKY_API_URL,
                e,
                extra={"params": opensky_params},
            )

    # Вердикт
    if not results["generic"]["ok"]:
        verdict = "Похоже, у сервера нет исходящего доступа в интернет."
    elif not results["opensky_domain"]["ok"]:
        verdict = "Интернет есть, но домен OpenSky недоступен из этого окружения."
    elif results["opensky_auth"].get("skipped"):
        # кредов нет — пропускаем auth-диагностику
        if not results["opensky_api"]["ok"]:
            verdict = "Домен OpenSky доступен, но API недоступен из этого окружения."
        else:
            verdict = "Интернет и OpenSky API доступны (OAuth2 не проверялся — нет кредов)."
    else:
        if not results["opensky_auth"]["ok"] and not results["opensky_api"]["ok"]:
            verdict = "Домен OpenSky доступен, но auth и API недоступны из этого окружения."
        elif not results["opensky_auth"]["ok"]:
            verdict = "Домен и API доступны, но есть проблема с OAuth2 endpoint."
        elif not results["opensky_api"]["ok"]:
            verdict = "Домен и auth доступны, но API недоступен из этого окружения."
        else:
            verdict = "И общий интернет, и домен, и auth, и API OpenSky доступны."

    return {"ok": True, "results": results, "verdict": verdict}



@mcp.tool
def opensky_regions_catalog() -> Dict[str, Any]:
    """
    Каталог демонстрационных регионов.

    Назначение:
    - Быстрые пресеты bbox для показательных демо.
    - Каталог не ограничивает пользователя: bbox можно задавать вручную.

    Возвращает:
    - regions: список объектов {name, bbox}.
    """
    return {
        "ok": True,
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
    Сырые живые данные OpenSky по bbox.

    Когда использовать:
    - Для демонстрации «честного» REST/JSON ответа.
    - Для учебных задач по парсингу.

    Параметры:
    - lamin/lomin/lamax/lomax: границы области.
    - extended: 0/1 — запрос расширенного формата states (если доступен).

    Возвращает:
    - ok=true + raw исходного ответа OpenSky,
      либо ok=false + подробная ошибка.
    """
    params: Dict[str, Any] = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    if extended:
        params["extended"] = 1

    raw = await _opensky_get("/states/all", params)
    if not raw.get("ok"):
        return raw

    return {
        "ok": True,
        "bbox": params,
        "raw": raw["data"],
        "note": "Сырые данные OpenSky /states/all.",
    }


@mcp.tool
async def opensky_normalized_states_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float
) -> Dict[str, Any]:
    """
    Нормализованный список самолётов по bbox.

    Когда использовать:
    - Для запросов «перечисли самолёты над ...».

    Возвращает:
    - ok=true + bbox + count + states,
      либо ok=false + подробная ошибка.

    Поля states:
    - callsign, icao24, origin_country, lat, lon,
      alt_ft (может отсутствовать), speed_kmh (может отсутствовать),
      track_deg, on_ground, last_contact.
    """
    return await _normalized_states_bbox(lamin, lomin, lamax, lomax)


@mcp.tool
async def opensky_airspace_summary_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float, top_n: int = 5
) -> Dict[str, Any]:
    """
    Диспетчерская сводка по воздушной обстановке в bbox.

    Когда использовать:
    - «что происходит над ...»
    - «сколько самолётов ...»
    - «кто лидеры по скорости/высоте ...»

    Возвращает:
    - ok=true + count + лидеры + префиксы,
      либо ok=false + подробная ошибка.
    """
    return await _airspace_summary_bbox(lamin, lomin, lamax, lomax, top_n=top_n)


@mcp.tool
async def opensky_airspace_summary_region(region: str, top_n: int = 5) -> Dict[str, Any]:
    """
    Сводка по региону из каталога.

    Назначение:
    - Удобный «шорткат» для демо-областей.
    - Если регион неизвестен, вернёт ok=false с подсказкой.

    Параметры:
    - region: имя из каталога opensky_regions_catalog.
    """
    if region not in REGIONS:
        return _err(
            "server",
            "unknown_region",
            f"Регион '{region}' не найден в каталоге.",
            available=list(REGIONS.keys()),
        )

    lamin, lomin, lamax, lomax = REGIONS[region]
    return await _airspace_summary_bbox(lamin, lomin, lamax, lomax, top_n=top_n)


@mcp.tool
async def net_healthcheck() -> Dict[str, Any]:
    """
    Диагностика выхода во внешний интернет из окружения MCP-сервера.

    Используй, если инструменты с внешними API падают.
    Возвращает результаты простых GET к нейтральным доменам.
    """
    targets = [
        "https://example.com",
        "https://httpbin.org/get",
    ]

    results = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for url in targets:
                try:
                    r = await client.get(url)
                    results.append({"url": url, "ok": True, "status": r.status_code})
                except Exception as e:
                    results.append({
                        "url": url,
                        "ok": False,
                        "error_type": type(e).__name__,
                        "detail": str(e),
                    })
        return {"ok": True, "results": results}
    except Exception as e:
        return _err("network", "healthcheck_failed", str(e))


@mcp.tool
async def opensky_healthcheck() -> Dict[str, Any]:
    """
    Диагностика доступности OpenSky из окружения MCP-сервера.

    Делает тестовый запрос /states/all по небольшому bbox.
    Полезно отличать проблему OpenSky от общего запрета egress.
    """
    params = {"lamin": 55.5, "lomin": 37.2, "lamax": 55.9, "lomax": 37.8}
    raw = await _opensky_get("/states/all", params)

    if not raw.get("ok"):
        return raw

    data = raw["data"]
    return {
        "ok": True,
        "time": data.get("time"),
        "states_is_null": data.get("states") is None,
        "note": "Если states_is_null=true — это может быть отсутствие покрытия в данный момент.",
    }



if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=port)
