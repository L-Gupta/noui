#!/usr/bin/env python3
"""Search Airbnb place autocomplete.

Takes a free-text query and returns place suggestions with place_id and
display name, ready to pass to search_listings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

AIRBNB_HOST = "https://www.airbnb.com"
AIRBNB_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"

_HEADERS = {
    "X-Airbnb-API-Key": AIRBNB_API_KEY,
    "X-Airbnb-Supports-Airlock-V2": "true",
    "X-CSRF-Without-Token": "1",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{AIRBNB_HOST}/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


async def execute(
    query: str,
    num_results: int = 10,
    locale: str = "en",
    currency: str = "USD",
) -> dict:
    """Autocomplete Airbnb places.

    Args:
        query: Free-text place name (e.g. "San Francisco", "Rio de Janeiro").
        num_results: Max suggestions to return (default 10).
        locale: Response locale (default "en").
        currency: Response currency code (default "USD").

    Returns:
        Parsed JSON with a `results` list. Each entry typically carries
        `location.google_place_id` and a display string.
    """
    params = {
        "locale": locale,
        "currency": currency,
        "key": AIRBNB_API_KEY,
        "language": locale,
        "num_results": str(num_results),
        "user_input": query,
        "api_version": "1.2.0",
        "vertical_refinement": "homes",
        "region": "-1",
        "options": (
            "should_filter_by_vertical_refinement|hide_nav_results|should_show_stays|simple_search"
        ),
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Prime anonymous session cookies.
        await client.get(AIRBNB_HOST + "/", headers={"User-Agent": _HEADERS["User-Agent"]})
        resp = await client.get(
            f"{AIRBNB_HOST}/api/v2/autocompletes-personalized",
            headers=_HEADERS,
            params=params,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search_places",
        description="Airbnb place autocomplete — returns place_id suggestions.",
    )
    p.add_argument("--query", required=True, help='Free-text place name (e.g. "Paris").')
    p.add_argument("--num-results", dest="num_results", type=int, default=10)
    p.add_argument("--locale", default="en")
    p.add_argument("--currency", default="USD")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(
            execute(
                query=args.query,
                num_results=args.num_results,
                locale=args.locale,
                currency=args.currency,
            )
        )
    except Exception as exc:
        print(f"search_places failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
