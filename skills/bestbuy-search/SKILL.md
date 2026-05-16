---
name: bestbuy-search
description: "Search Best Buy for products and add them to cart with human confirmation. Use when the user wants to find, select, and purchase items on Best Buy — e.g. 'find laptops on Best Buy', 'add this headset to my Best Buy cart', 'search Best Buy for USB-C hubs'. Five operations in a supervised workflow: search_suggestions → lookup_products → open_product → add_to_cart (human confirms) → proceed_to_checkout (human confirms). Agent stops at the payment step; the human places the final order."
---

# Best Buy Search & Cart

Supervised Best Buy shopping workflow. Two public API operations for product
discovery, three browser operations for cart and checkout. Human confirmation
is required before any cart or checkout action.

## Confirmation Requirements (MUST follow)

| Step | What to show the user | What to ask |
|---|---|---|
| Before `add_to_cart` | Product title, Best Buy URL, and price (or link to PDP) | "Should I add [product] to your Best Buy cart?" |
| Before `proceed_to_checkout` | Full cart contents and subtotal from `add_to_cart` output | "Your cart has [items] totalling [subtotal]. Shall I proceed to checkout?" |

The agent **stops at the payment step** and will not click any payment button
or type any payment credentials. The human completes payment manually.

## Prerequisites (browser operations only)

- NoUI backend running: `python cli/main.py start`
- Chrome extension connected (green dot at `localhost:8002`)
- Active Chrome tab signed in to Best Buy

## Full Workflow

```
1. search_suggestions  --query "wireless earbuds" --count 6
        ↓  (skuIds from suggestions[0].products)
2. lookup_products     --skuids "6580352,6628905,6501047"
        ↓  (pick best match → pdpUrl)
   [SHOW user the product options; ask which one to buy]
        ↓  (user picks one)
3. open_product        --pdp-url "/product/.../SKU"
        ↓
   [SHOW user the product title + URL; ask "Add to cart?"]
        ↓  (user confirms)
4. add_to_cart
        ↓
   [SHOW user the cart summary; ask "Proceed to checkout?"]
        ↓  (user confirms)
5. proceed_to_checkout
        ↓
   Agent stops at payment step → user completes payment manually
```

## Operations

### `search_suggestions`

Best Buy search-term autocomplete. Returns suggested queries plus matching
skuIds. No browser required.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--query` | string | yes | — | Free-text query (e.g. "laptop"). |
| `--count` | int | no | `6` | Max number of suggestions. |
| `--search-variant` | string | no | `A` | Server experiment variant. |

Response shape:

```json
{
  "suggestionResponse": {
    "count": 6,
    "suggestions": [
      {
        "term": "wireless earbuds",
        "category": [{"name": "Headphones", "id": "pcmcat144700050004"}],
        "products": [{"skuId": "6580352"}, ...]
      }
    ]
  }
}
```

### `lookup_products`

Resolve skuIds to product cards. No browser required.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--skuids` | string | yes | — | Comma-separated skuIds. |

Response shape (per product):

```json
{
  "skuid": "6580352",
  "skushortlabel": "Skullcandy - Dime 3 True Wireless In-Ear Earbuds ...",
  "pdpUrl": "/product/skullcandy-dime-3.../6580352",
  "customerrating_facet": "4.3",
  "numberofreviews_facet": "328"
}
```

Prepend `https://www.bestbuy.com` to `pdpUrl` for the full URL. Note: price
is not returned by this endpoint — visit the PDP for live pricing.

### `open_product`

Navigate the active browser tab to a Best Buy product detail page. Call this
before `add_to_cart` to ensure the correct product is loaded.

| Flag | Type | Required | Description |
|---|---|---|---|
| `--pdp-url` | string | yes | Relative pdpUrl (e.g. `"/product/.../6580352"`) or full URL. |

Response: `{"ready": bool, "url": str, "title": str, "add_to_cart_visible": bool}`

### `add_to_cart`

**REQUIRES HUMAN APPROVAL before calling.**

Clicks the Add to Cart button on the currently open product page, waits for
cart confirmation, then returns the cart contents.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--skip-navigation` | flag | no | `false` | Assume browser is already on the product page. |

Response on success:

```json
{
  "added": true,
  "cart_url": "https://www.bestbuy.com/cart",
  "cart": {
    "line_count": 1,
    "lines": [{"title": "...", "price": "$..."}],
    "subtotal": "$25.99"
  },
  "payment_boundary_triggered": false
}
```

Response on failure: `{"added": false, "error": "...", "payment_boundary_triggered": bool}`

Safety: the operation aborts immediately if any payment-form indicator
(credit card field, payment URL, "Place Order" button) is detected.

### `proceed_to_checkout`

**REQUIRES HUMAN APPROVAL before calling.**

Clicks Checkout from the cart, walks through safe sub-steps (fulfillment,
address, review), and stops hard at the payment step. Never clicks any
payment button or enters any credentials.

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--max-steps` | int | no | `6` | Max sub-steps to auto-advance. |

Response when payment boundary reached (expected success):

```json
{
  "checkout_reached": true,
  "payment_boundary": true,
  "stopped_at": "payment URL pattern in .../checkout/r/payment",
  "steps_taken": 3,
  "current_url": "https://www.bestbuy.com/checkout/r/payment",
  "message": "Agent stopped at payment step. Complete payment manually in the browser, then place your order."
}
```

After receiving this, the agent must inform the user that they should
complete payment in the browser and place the order themselves.

## Notes

- Price is not included in `search_suggestions` or `lookup_products`. Visit
  the `pdpUrl` for live pricing before confirming add-to-cart.
- Best Buy may prompt for a sign-in before add-to-cart. If the browser is
  not signed in, `add_to_cart` will return an error with guidance.
- If the product requires selecting a size, color, or variant (e.g. laptop
  storage tier), the `add_to_cart` button may not appear until a variant is
  selected. Open the PDP in the browser and make that selection first.
- The `proceed_to_checkout` payment boundary detection covers URL patterns,
  credit-card input fields, and "Place Your Order" button text. If Best Buy
  changes its checkout UI, the stop may fire earlier or the step may require
  manual navigation.
- Re-record the workflow via `/noui-record-workflow` if Best Buy rotates the
  autocomplete API endpoints.
