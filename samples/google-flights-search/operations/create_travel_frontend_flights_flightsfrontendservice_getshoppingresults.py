"""Operation: search_flights
Method: POST
Path: /_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetShoppingResults
"""

from __future__ import annotations

import json
import urllib.parse
import pathlib

import httpx

from noui_runtime.auth import get_auth_headers

BASE_URL = "https://www.google.com"
_MANIFEST = json.loads((pathlib.Path(__file__).parent.parent / "manifest.json").read_text())
TABBY_PROFILE_ID: str | None = _MANIFEST.get("auth", {}).get("tabby_profile_id")

_QS = {
    "f.sid": "-7456312471093751493",
    "bl": "boq_travel-frontend-flights-ui_20260330.02_p2",
    "hl": "en",
    "soc-app": "162",
    "soc-platform": "1",
    "soc-device": "1",
    "rt": "c",
}

_TRIP_ONE_WAY = 2
_TRIP_ROUND = 1


async def execute(
    origin_entity_id: str,
    destination_entity_id: str,
    departure_date: str,
    return_date: str = "",
    adults: int = 1,
    csrf_token: str = "",
) -> dict:
    """Search for available flights. Returns options with prices, airlines, and
    schedules for the given route and dates.

    Use origin_entity_id and destination_entity_id from search_airports_cities
    (e.g. '/m/022pfm' for São Paulo, '/m/06gmr' for Rio de Janeiro).
    Dates must be in YYYY-MM-DD format.
    Leave return_date empty for a one-way search.
    """
    trip_type = _TRIP_ROUND if return_date else _TRIP_ONE_WAY

    legs = [
        [
            [[[origin_entity_id, 5]]], [[[destination_entity_id, 5]]],
            None, 0, None, None, departure_date,
            None, None, None, None, None, None, None, 3,
        ],
    ]
    if return_date:
        legs.append([
            [[[destination_entity_id, 5]]], [[[origin_entity_id, 5]]],
            None, 0, None, None, return_date,
            None, None, None, None, None, None, None, 3,
        ])

    inner = [
        [],
        [
            None, None, trip_type, None, [], 1, [adults, 0, 0, 0],
            None, None, None, None, None, None,
            legs,
            None, None, None, 1,
        ],
        0, 0, 0, 1,
    ]
    freq = json.dumps([None, json.dumps(inner)])
    body_parts = {"f.req": freq}
    if csrf_token:
        body_parts["at"] = csrf_token
    body = urllib.parse.urlencode(body_parts)

    auth_headers = await get_auth_headers(TABBY_PROFILE_ID) if TABBY_PROFILE_ID else {}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        **auth_headers,
    }

    url = f"{BASE_URL}/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, params=_QS, headers=headers, content=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}
