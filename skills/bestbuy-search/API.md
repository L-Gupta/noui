# Best Buy Search API

Two operations. No auth required.

## `search_suggestions`

GET `https://www.bestbuy.com/suggest/v1/fragment/suggest/www`

**Args**
- `query` (str, required) — free-text query (e.g. "laptop")
- `count` (int, default 6) — max suggestions
- `search_variant` (str, default "A") — server experiment variant

**Returns** parsed JSON. Key fields per suggestion:

- `suggestionResponse.suggestions[i].term` — the suggested search term
- `suggestionResponse.suggestions[i].category[]` — `{name, id}` category hints
- `suggestionResponse.suggestions[i].products[].skuId` — Best Buy skuIds to feed
  into `lookup_products`

## `lookup_products`

GET `https://www.bestbuy.com/suggest/v1/fragment/products/www`

**Args**
- `skuids` (str, required) — comma-separated skuIds (or Python list)

**Returns** parsed JSON. Per-product fields under `products[]`:

| Field | Description |
|---|---|
| `skuid` | Best Buy skuId. |
| `skushortlabel` | Short product title (brand + model + key specs). |
| `pdpUrl` | Path of the product detail page. Prepend `https://www.bestbuy.com`. |
| `imageUrl` | Path under `https://pisces.bbystatic.com/image/`. |
| `altText` | Alt text describing the primary image. |
| `customerrating_facet` | Average customer rating, "0.0"-"5.0" string. |
| `numberofreviews_facet` | Review count as a string. |

## Hardcoded values (from recording)

| Value | Where |
|---|---|
| `X-CLIENT-ID: Search-Web-View` | both ops |
| Desktop Chrome 148 User-Agent | both ops |
| `searchVariant=A` | `search_suggestions` (recording default) |

## Out of scope

Price is **not** returned by either endpoint. Best Buy fetches price
separately via `POST /gateway/graphql` (operation
`PlpView_ProductListItem_Init`) keyed on `customerId`, cart timestamp,
and visitor cohort. That GraphQL request body is ~18kB of fragments and
is excluded from this skill to keep operations small and stable.

For live price, follow the `pdpUrl` returned by `lookup_products`.

## Recording provenance

- Workflow session: `ca123295-c78c-4b06-9c70-124d54939533`
- Capture session: `19a9fc71-29c1-4075-9f1a-3e2ccf5b9591`
- HAR shows ~84 GraphQL POSTs and ~36 autocomplete GETs against
  `bestbuy.com`; this skill exposes the two stable, schema-light GETs.
