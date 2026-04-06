"""Operation: search_airports_cities
Method: POST
Path: /_/FlightsFrontendUi/data/batchexecute
RPC: H028ib (airport/city autocomplete)
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

# Infrastructure params from recording — hardcoded
_QS = {
    "rpcids": "H028ib",
    "source-path": "/travel/flights",
    "f.sid": "-7456312471093751493",
    "bl": "boq_travel-frontend-flights-ui_20260330.02_p2",
    "hl": "en",
    "soc-app": "162",
    "soc-platform": "1",
    "soc-device": "1",
    "rt": "c",
}


async def execute(
    query: str,
    csrf_token: str = "",
) -> dict:
    """Search for airports and cities by name.

    Returns matching locations with their Google entity IDs
    (e.g. '/m/022pfm' for São Paulo) needed by the flight search tools.
    """
    freq = json.dumps([[["H028ib", json.dumps([query, [1, 2, 3, 5], None, [2], 1]), None, "generic"]]])
    body_parts = {"f.req": freq}
    if csrf_token:
        body_parts["at"] = csrf_token
    body = urllib.parse.urlencode(body_parts)

    auth_headers = await get_auth_headers(TABBY_PROFILE_ID) if TABBY_PROFILE_ID else {}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        **auth_headers,
    }

    url = f"{BASE_URL}/_/FlightsFrontendUi/data/batchexecute"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, params=_QS, headers=headers, content=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}
