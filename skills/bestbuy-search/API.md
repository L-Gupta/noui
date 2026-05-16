# Best Buy Search & Cart API

Five operations in two groups. No auth required for search; browser session
required for cart/checkout operations.

---

## Public API operations (no auth, plain httpx)

### `search_suggestions`

GET `https://www.bestbuy.com/suggest/v1/fragment/suggest/www`

**Args**
- `query` (str, required) — free-text query (e.g. "laptop")
- `count` (int, default 6) — max suggestions
- `search_variant` (str, default "A") — server experiment variant

**Returns** — Key fields per suggestion:

- `suggestions[i].term` — suggested search term
- `suggestions[i].category[]` — `{name, id}` category hints
- `suggestions[i].products[].skuId` — Best Buy skuIds for `lookup_products`

---

### `lookup_products`

GET `https://www.bestbuy.com/suggest/v1/fragment/products/www`

**Args**
- `skuids` (str, required) — comma-separated skuIds

**Returns** — Per-product under `products[]`:

| Field | Description |
|---|---|
| `skuid` | Best Buy skuId |
| `skushortlabel` | Short product title |
| `pdpUrl` | PDP path; prepend `https://www.bestbuy.com` |
| `imageUrl` | Image path |
| `customerrating_facet` | Avg rating string "0.0"–"5.0" |
| `numberofreviews_facet` | Review count string |

Price is **not** returned. Visit the PDP for live pricing.

---

## Browser operations (require NoUI backend + Chrome extension)

All browser operations call `POST http://localhost:8002/browser-commands/execute`
to drive the Chrome extension. Prerequisites:

1. `python cli/main.py start` — NoUI backend
2. Chrome extension connected (green dot)
3. Chrome tab open on `www.bestbuy.com`, signed in

---

### `open_product`

Navigates the browser to a Best Buy product page and waits for it to render.

**Args**
- `pdp_url` (str, required) — relative path from `lookup_products.pdpUrl` or
  full URL

**Returns**

```json
{
  "ready": true,
  "url": "https://www.bestbuy.com/product/...",
  "title": "Skullcandy - Dime 3 ...",
  "add_to_cart_visible": true
}
```

---

### `add_to_cart`

**HUMAN APPROVAL REQUIRED before running.**

Clicks the Add to Cart button, confirms the item was added, navigates to the
cart, and returns the cart summary.

**Args**
- `skip_navigation` (bool, default false)

**Returns on success**

```json
{
  "added": true,
  "method": "selector:button.add-to-cart-button",
  "cart_updated_confirmed": true,
  "cart_url": "https://www.bestbuy.com/cart",
  "cart": {
    "line_count": 1,
    "lines": [{"title": "...", "price": "$25.99"}],
    "subtotal": "$25.99",
    "url": "https://www.bestbuy.com/cart"
  },
  "payment_boundary_triggered": false
}
```

**Returns on failure**

```json
{"added": false, "error": "<reason>", "payment_boundary_triggered": false}
```

**Safety boundary**: operation aborts immediately if any of the following are
detected:
- URL contains `/checkout/r/payment` or similar
- `input[autocomplete='cc-number']` or other card-entry inputs are found
- A "Place Your Order" / "Pay Now" button is visible

---

### `proceed_to_checkout`

**HUMAN APPROVAL REQUIRED before running.**

Clicks Checkout from the cart, walks through safe sub-steps (fulfillment,
address, review), and **stops hard at the payment entry step**.

**Args**
- `max_steps` (int, default 6) — max sub-steps to auto-advance

**Returns when payment boundary reached** (expected path)

```json
{
  "checkout_reached": true,
  "payment_boundary": true,
  "stopped_at": "payment URL pattern detected",
  "steps_taken": 3,
  "current_url": "https://www.bestbuy.com/checkout/r/payment",
  "current_title": "Checkout | Best Buy",
  "message": "Agent stopped at payment step. Complete payment manually in the browser, then place your order."
}
```

**Returns when stuck on a sub-step** (address form needs input, etc.)

```json
{
  "checkout_reached": true,
  "payment_boundary": false,
  "stopped_at": "max_steps=6 or stuck on sub-step",
  "steps_taken": 2,
  "current_url": "https://www.bestbuy.com/checkout#bb-address",
  "message": "Agent stopped before reaching the payment step — the checkout form may need manual input."
}
```

**Hard stop conditions** (will never be bypassed):
- URL matches a payment path pattern
- A credit-card number / CVV / payment-method input is present in the DOM
- A "Place Your Order", "Place Order", "Pay Now", "Confirm Order", or
  "Submit Order" button is visible

---

## Hardcoded values (from recording)

| Value | Where |
|---|---|
| `X-CLIENT-ID: Search-Web-View` | `search_suggestions`, `lookup_products` |
| Desktop Chrome 148 User-Agent | `search_suggestions`, `lookup_products` |
| `searchVariant=A` | `search_suggestions` |
| `http://localhost:8002` | `open_product`, `add_to_cart`, `proceed_to_checkout` |

## Recording provenance

- Workflow session: `ca123295-c78c-4b06-9c70-124d54939533`
- Capture session: `19a9fc71-29c1-4075-9f1a-3e2ccf5b9591`
