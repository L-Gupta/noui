"""Operation: get_flight_prices_calendar
Method: POST
Path: /_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetCalendarPicker
"""

from __future__ import annotations

import json
import pathlib
import urllib.parse

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


async def execute(
    origin_entity_id: str,
    destination_entity_id: str,
    calendar_start: str,
    calendar_end: str,
    csrf_token: str = "",
) -> dict:
    """Return a price calendar for a round-trip route across a date range.

    Use origin_entity_id and destination_entity_id from search_airports_cities
    (e.g. '/m/022pfm' for São Paulo, '/m/06gmr' for Rio de Janeiro).
    Dates must be in YYYY-MM-DD format.
    """
    inner = [
        None,
        [
            None,
            None,
            1,
            None,
            [],
            1,
            [1, 0, 0, 0],
            None,
            None,
            None,
            None,
            None,
            None,
            [
                [[[[origin_entity_id, 5]]], [[[destination_entity_id, 5]]], None, 0],
                [[[[destination_entity_id, 5]]], [[[origin_entity_id, 5]]], None, 0],
            ],
            None,
            None,
            None,
            1,
        ],
        [calendar_start, calendar_end],
        None,
        [7, 7],
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

    url = f"{BASE_URL}/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetCalendarPicker"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, params=_QS, headers=headers, content=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}
