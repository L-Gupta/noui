#!/usr/bin/env python3
"""Operation: search_hotels

Searches for hotels on Expedia by executing API calls from inside
the authenticated Tabby browser session via CDP (Chrome DevTools Protocol).

This bypasses Akamai bot detection by making requests from the real browser
context rather than from a Python HTTP client.

Skill-variant entry point:
    python operations/search_hotels.py --destination Paris --check-in 2026-05-01 \\
        --check-out 2026-05-04 --guests 2 --rooms 1

Prints JSON to stdout on success; exits non-zero with a diagnostic on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse

import httpx
import websockets

CDP_LIST_URL = "http://localhost:9222/json"

_JS_EXTRACT_LISTINGS = r"""
(() => {
    const cards = document.querySelectorAll('[data-stid="lodging-card-responsive"]');
    const results = [];
    for (const card of [...cards].slice(0, 25)) {
        const text = card.innerText || '';

        const headings = card.querySelectorAll('h3');
        let name = '';
        for (const h of headings) {
            const t = h.textContent.trim();
            if (!t.startsWith('Photo gallery')) { name = t; break; }
        }
        if (!name && headings.length) {
            name = (headings[0].textContent || '').trim().replace(/^Photo gallery for /, '');
        }

        const prices = text.match(/\$[\d,]+/g) || [];
        const mainPrice = prices[0] || '';

        const nightlyMatch = text.match(/\$([\d,]+)\s*night/i);
        const nightly = nightlyMatch ? '$' + nightlyMatch[1] : mainPrice;

        const totalMatch = text.match(/\$([\d,]+)\s+total/i);
        const total = totalMatch ? '$' + totalMatch[1] : '';

        const ratingMatch = text.match(/([\d.]+)\s+out of 10/);
        const rating = ratingMatch ? ratingMatch[1] + '/10' : '';
        const ratingWord = text.match(/out of 10\n(\w+)/);
        const ratingLabel = ratingWord ? ratingWord[1] : '';

        const reviewMatch = text.match(/([\d,]+)\s+reviews/);
        const reviews = reviewMatch ? reviewMatch[1] : '';

        const link = (card.querySelector('a[href*="Hotel-Information"]') || card.querySelector('a') || {}).href || '';
        const refundable = text.includes('Fully refundable');

        if (name) {
            results.push({
                name, price_per_night: nightly, price_total: total,
                rating, rating_label: ratingLabel, reviews_count: reviews,
                refundable, url: link,
            });
        }
    }
    return JSON.stringify({total_on_page: cards.length, results});
})()
"""


async def _find_expedia_page() -> str | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(CDP_LIST_URL, timeout=5)
        targets = resp.json()
    for t in targets:
        if t.get("type") == "page" and "expedia.com" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    return None


async def _cdp_eval(ws_url: str, expression: str) -> dict:
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        resp = json.loads(await ws.recv())

    result = resp.get("result", {}).get("result", {})
    if result.get("type") != "string":
        raise RuntimeError(f"CDP eval failed: {json.dumps(result)[:300]}")
    return json.loads(result["value"])


async def _cdp_fetch(ws_url: str, url: str) -> dict:
    js = f"""
    fetch({json.dumps(url)}, {{ credentials: 'include' }})
      .then(r => r.text().then(t => JSON.stringify({{status: r.status, body: t}})))
    """
    data = await _cdp_eval(ws_url, js)
    if data["status"] != 200:
        raise RuntimeError(f"Expedia API returned {data['status']}: {data['body'][:300]}")
    return json.loads(data["body"])


_TYPEAHEAD_PARAMS = {
    "locale": "en_US",
    "siteid": "1",
    "dest": "true",
    "regiontype": "2000",
    "features": "ta_hierarchy|typeahead_v3",
    "maxresults": "1",
}


async def _resolve_destination(ws_url: str, destination: str) -> dict:
    query = urllib.parse.quote(destination)
    params = urllib.parse.urlencode(_TYPEAHEAD_PARAMS)
    url = f"https://www.expedia.com/api/v4/typeahead/{query}?{params}"

    data = await _cdp_fetch(ws_url, url)

    results = data.get("sr", data) if isinstance(data, dict) else data
    if not results or not isinstance(results, list):
        return {"name": destination}

    top = results[0]
    region_id = str(top.get("gaiaId") or top.get("regionId") or top.get("id") or "")
    lat = top.get("lat") or (top.get("coordinates") or {}).get("lat")
    lon = top.get("lon") or (top.get("coordinates") or {}).get("long")
    resolved_name = (
        top.get("fullName")
        or top.get("regionNames", {}).get("fullName")
        or top.get("name")
        or destination
    )
    return {
        "name": resolved_name,
        "region_id": region_id,
        "lat_long": f"{lat},{lon}" if lat and lon else None,
    }


async def _search_properties(
    ws_url: str,
    region_id: str,
    destination_name: str,
    check_in: str,
    check_out: str,
    guests: int,
    rooms: int,
) -> dict:
    params = {
        "destination": destination_name,
        "regionId": region_id,
        "d1": check_in,
        "startDate": check_in,
        "d2": check_out,
        "endDate": check_out,
        "adults": str(guests),
        "rooms": str(rooms),
        "sort": "RECOMMENDED",
    }
    search_url = "https://www.expedia.com/Hotel-Search?" + urllib.parse.urlencode(params)

    async with websockets.connect(ws_url) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Page.navigate",
                    "params": {"url": search_url},
                }
            )
        )
        await ws.recv()

    await asyncio.sleep(7)

    return await _cdp_eval(ws_url, _JS_EXTRACT_LISTINGS)


async def execute(
    destination: str,
    check_in: str,
    check_out: str,
    guests: int = 2,
    rooms: int = 1,
) -> dict:
    """Search for hotels on Expedia using the authenticated browser session."""
    ws_url = await _find_expedia_page()
    if not ws_url:
        return {"error": "No Expedia browser session found. Is Tabby running?"}

    dest_info = await _resolve_destination(ws_url, destination)
    region_id = dest_info.get("region_id", "")

    if not region_id:
        return {
            "error": f"Could not resolve destination: {destination}",
            "destination": dest_info,
        }

    search_data = await _search_properties(
        ws_url,
        region_id,
        dest_info["name"],
        check_in,
        check_out,
        guests,
        rooms,
    )

    params = {
        "destination": dest_info["name"],
        "regionId": region_id,
        "d1": check_in,
        "d2": check_out,
        "adults": str(guests),
        "rooms": str(rooms),
    }
    search_url = "https://www.expedia.com/Hotel-Search?" + urllib.parse.urlencode(params)

    return {
        "search_url": search_url,
        "destination": dest_info["name"],
        "region_id": region_id,
        "check_in": check_in,
        "check_out": check_out,
        "guests": guests,
        "rooms": rooms,
        "listings_count": search_data.get("total_on_page", 0),
        "listings": search_data.get("results", []),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="search_hotels",
        description="Search Expedia for hotels by destination and dates via Tabby CDP.",
    )
    parser.add_argument("--destination", required=True, help="City or place name (e.g. 'Paris').")
    parser.add_argument("--check-in", dest="check_in", required=True, help="YYYY-MM-DD.")
    parser.add_argument("--check-out", dest="check_out", required=True, help="YYYY-MM-DD.")
    parser.add_argument("--guests", type=int, default=2, help="Adult guests (default: 2).")
    parser.add_argument("--rooms", type=int, default=1, help="Number of rooms (default: 1).")
    parser.add_argument(
        "--profile-slug",
        dest="profile_slug",
        default=None,
        help="Tabby profile slug. Unused for CDP transport but accepted for contract parity.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(
            execute(
                destination=args.destination,
                check_in=args.check_in,
                check_out=args.check_out,
                guests=args.guests,
                rooms=args.rooms,
            )
        )
    except Exception as exc:
        print(f"search_hotels failed: {exc}", file=sys.stderr)
        return 1

    if isinstance(result, dict) and "error" in result:
        print(json.dumps(result, indent=2))
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
