#!/usr/bin/env python3
"""Best Buy search-suggestion autocomplete.

Takes a free-text query (e.g. "laptop", "usb c cable") and returns ranked
search-term suggestions, each with a category list and a small set of
matching `skuId`s. Pass those skuIds to `lookup_products` for full
product cards (title, image, rating, PDP URL).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

BESTBUY_HOST = "https://www.bestbuy.com"

_HEADERS = {
    "X-CLIENT-ID": "Search-Web-View",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BESTBUY_HOST}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


async def execute(
    query: str,
    count: int = 6,
    search_variant: str = "A",
) -> dict:
    """Best Buy search-term autocomplete.

    Args:
        query: Free-text query (e.g. "laptop", "usb c cable").
        count: Max number of suggestions to return (default 6).
        search_variant: Server-side experiment variant. Recording used "A".

    Returns:
        Parsed JSON. Top-level shape::

            {
              "suggestionResponse": {
                "spellCheck": {...},
                "count": int,
                "suggestions": [
                  {
                    "term": "laptop",
                    "category": [{"name": "...", "id": "..."}, ...],
                    "products": [{"skuId": "6619147", ...}, ...]
                  }, ...
                ]
              }
            }

        Pull `skuId`s from `suggestions[i].products[*].skuId` and feed them
        to `lookup_products` to get titles, images, ratings, and PDP URLs.
    """
    url = f"{BESTBUY_HOST}/suggest/v1/fragment/suggest/www"
    params = {
        "query": query,
        "count": str(count),
        "searchVariant": search_variant,
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS, params=params, timeout=20.0)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search_suggestions",
        description="Best Buy autocomplete: query -> suggested terms + skuIds.",
    )
    p.add_argument("--query", required=True, help='Free-text query (e.g. "laptop").')
    p.add_argument("--count", type=int, default=6, help="Max suggestions (default 6).")
    p.add_argument(
        "--search-variant",
        dest="search_variant",
        default="A",
        help="Server experiment variant (default A).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(
            execute(
                query=args.query,
                count=args.count,
                search_variant=args.search_variant,
            )
        )
    except Exception as exc:
        print(f"search_suggestions failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
