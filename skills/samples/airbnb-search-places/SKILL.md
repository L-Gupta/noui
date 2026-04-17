---
name: airbnb-search-places
description: "Search Airbnb for places and stay listings. Use when the user wants to find Airbnb rentals — e.g. \"find Airbnbs in San Francisco May 12-22 for 2 adults\", \"search Airbnb places for Paris\", \"list Airbnb stays in Rio de Janeiro next weekend\". Two operations: search_places (autocomplete → place_id) and search_listings (place_id + dates + guests → listings). No auth required."
---

# Airbnb Search

Anonymous Airbnb search. Two operations; chain them.

## Usage

Typical flow for "find Airbnbs in <place> from <date> to <date> for N adults":

1. Resolve the place to a Google place_id:
   ```bash
   python operations/search_places.py --query "San Francisco"
   ```
   Pick the first result's `location.google_place_id` and `display_name`.
2. Search listings with those plus dates and guest counts:
   ```bash
   python operations/search_listings.py \
     --place-id "ChIJIQBpAG2ahYAR_6128GcTUEo" \
     --query "San Francisco, California, United States" \
     --checkin 2026-05-12 --checkout 2026-05-22 \
     --adults 2
   ```

Responses are printed as JSON on stdout.

## Operations

### `search_places`

Airbnb place autocomplete. Returns place suggestions with Google place_id and display name.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--query` | string | yes | — | Free-text place name (e.g. "San Francisco"). |
| `--num-results` | int | no | `10` | Max number of suggestions. |
| `--locale` | string | no | `en` | Response locale. |
| `--currency` | string | no | `USD` | Response currency. |

Response shape: `{ "autocomplete_terms": [ { "display_name": "...", "location": { "google_place_id": "..." }, ... } ] }`.

### `search_listings`

Search stays for a place + dates + guest composition.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--place-id` | string | yes | — | Google place_id from `search_places`. |
| `--query` | string | yes | — | Human-readable place name that pairs with `--place-id`. |
| `--checkin` | string | yes | — | Check-in date, `YYYY-MM-DD`. |
| `--checkout` | string | yes | — | Check-out date, `YYYY-MM-DD`. |
| `--adults` | int | no | `1` | |
| `--children` | int | no | `0` | |
| `--infants` | int | no | `0` | |
| `--pets` | int | no | `0` | |
| `--locale` | string | no | `en` | |
| `--currency` | string | no | `USD` | Response currency (e.g. `USD`, `BRL`, `EUR`). |

Response shape: parsed GraphQL — listings under `data.presentation.staysSearch.results.searchResults[]`. Each entry has price (`structuredDisplayPrice`), rating (`avgRatingLocalized`), and listing metadata.

## Notes

- No authentication required. Uses Airbnb's public web API key embedded in `operations/*.py`.
- `search_listings` uses Airbnb's persisted GraphQL query identified by a SHA256 hash baked into `operations/search_listings.py`. If Airbnb rotates the hash (on frontend releases), calls fail with `PersistedQueryNotFound` — re-record the workflow via `/noui-record-workflow` to refresh it.
