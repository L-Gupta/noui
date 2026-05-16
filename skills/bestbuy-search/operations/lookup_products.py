#!/usr/bin/env python3
"""Best Buy product-card lookup by skuId.

Given one or more Best Buy skuIds (typically pulled from
`search_suggestions`), returns full product cards: short title, image,
PDP URL, customer rating, and review count.

Note: this endpoint does not include price. Price requires a separate
GraphQL call against `/gateway/graphql` (not exposed by this skill).
The PDP URL can be combined with the host to send the user to the
product page for live pricing.
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


def _normalize_skuids(skuids: str | list[str]) -> str:
    if isinstance(skuids, list):
        return ",".join(s.strip() for s in skuids if s and s.strip())
    return ",".join(s.strip() for s in str(skuids).split(",") if s.strip())


async def execute(skuids: str | list[str]) -> dict:
    """Look up Best Buy product cards by skuId.

    Args:
        skuids: A single comma-separated string ("6619147,6667498") or a
            Python list of skuId strings. Order is preserved.

    Returns:
        Parsed JSON. Top-level shape::

            {
              "count": 3,
              "products": [
                {
                  "skuid": "6619147",
                  "skushortlabel": "Lenovo - IdeaPad Slim 3x ...",
                  "pdpUrl": "/product/lenovo-ideapad-slim-3x.../JJGSH82JL5",
                  "imageUrl": "BestBuy_US/images/products/<uuid>.jpg",
                  "altText": "Lenovo - IdeaPad Slim 3x ...",
                  "customerrating_facet": "4.7",
                  "numberofreviews_facet": "276"
                }, ...
              ]
            }

        Prepend `BESTBUY_HOST` to `pdpUrl` for a clickable URL.
    """
    csv = _normalize_skuids(skuids)
    if not csv:
        return {"count": 0, "products": [], "warning": "no skuids supplied"}

    url = f"{BESTBUY_HOST}/suggest/v1/fragment/products/www"
    params = {"skuids": csv}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS, params=params, timeout=20.0)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lookup_products",
        description="Best Buy product-card lookup by skuId.",
    )
    p.add_argument(
        "--skuids",
        required=True,
        help='Comma-separated skuIds (e.g. "6619147,6667498,12349296").',
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(execute(skuids=args.skuids))
    except Exception as exc:
        print(f"lookup_products failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
