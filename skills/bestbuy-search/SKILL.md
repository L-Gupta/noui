---
name: bestbuy-search
description: "Search Best Buy for products. Use when the user wants to find items on Best Buy — e.g. \"find laptops on Best Buy\", \"search Best Buy for usb c cable\", \"show me Best Buy gaming headsets under $100\". Two operations: search_suggestions (query → suggested terms + skuIds) and lookup_products (skuIds → product cards). No auth required."
---

# Best Buy Search

Anonymous Best Buy product search. Two operations; chain them.

## Usage

Typical flow for "find <product> on Best Buy":

1. Resolve the query to suggested terms and skuIds:
   ```bash
   python operations/search_suggestions.py --query "laptop" --count 6
   ```
   Each entry in `suggestionResponse.suggestions[]` carries a `term`, a
   list of categories, and a `products[]` list of `skuId`s.

2. Look up product cards for the skuIds you care about:
   ```bash
   python operations/lookup_products.py --skuids "6619147,6667498,12349296"
   ```
   Each entry has `skushortlabel` (title), `pdpUrl`, `imageUrl`,
   `customerrating_facet`, `numberofreviews_facet`.

Responses are printed as JSON on stdout.

## Operations

### `search_suggestions`

Best Buy search-term autocomplete. Returns suggested queries plus
matching skuIds.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--query` | string | yes | — | Free-text query (e.g. "laptop"). |
| `--count` | int | no | `6` | Max number of suggestions. |
| `--search-variant` | string | no | `A` | Server experiment variant; recording used "A". |

Response shape:

```json
{
  "suggestionResponse": {
    "spellCheck": { "correctedQuery": "", "correctlySpelled": true, "originalQuery": "laptop" },
    "count": 6,
    "suggestions": [
      {
        "term": "laptop",
        "category": [{"name": "All Laptops", "id": "pcmcat138500050001"}],
        "products": [{"skuId": "6619147", "type": "searchSuggestion"}, ...]
      }
    ]
  }
}
```

### `lookup_products`

Resolve skuIds to full product cards.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--skuids` | string | yes | — | Comma-separated skuIds (e.g. `"6619147,6667498"`). |

Response shape:

```json
{
  "count": 3,
  "products": [
    {
      "skuid": "6619147",
      "skushortlabel": "Lenovo - IdeaPad Slim 3x - Copilot+ PC - 15.3\" 2k Touchscreen Laptop - ...",
      "pdpUrl": "/product/lenovo-ideapad-slim-3x.../JJGSH82JL5",
      "imageUrl": "BestBuy_US/images/products/<uuid>.jpg",
      "altText": "Lenovo - IdeaPad Slim 3x - ...",
      "customerrating_facet": "4.7",
      "numberofreviews_facet": "276"
    }
  ]
}
```

Prepend `https://www.bestbuy.com` to `pdpUrl` to get the clickable URL.

## Notes

- No authentication required. Both endpoints are public, anonymous.
- These endpoints are the same ones Best Buy's site uses to render the
  header search box and the type-ahead product carousel; they are
  optimised for autocomplete-style "top-N" lookups, not full PLP grids.
- Price is intentionally not included — Best Buy serves price through a
  separate GraphQL call (`/gateway/graphql`) keyed on `customerId`,
  cart state, and visitor cohort. To get a live price for a skuId,
  navigate to `https://www.bestbuy.com{pdpUrl}`.
- Both operations send a desktop-Chrome User-Agent and the `Search-Web-View`
  client id observed during recording. If Best Buy changes the wire
  contract, re-record the workflow via `/noui-record-workflow`.
