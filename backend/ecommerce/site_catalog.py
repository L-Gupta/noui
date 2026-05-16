"""Top-20 global e-commerce site catalog and site profile schema.

Each `SiteProfile` describes how the agent should drive a single retailer:
search URL template, selector hints, login URL, locale, currency, and the
forbidden action labels that gate any final checkout/payment side effect.

The registry is purposely declarative so the list of supported sites and
their per-site safety metadata can be changed without touching the agent
core.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlparse


@dataclass(frozen=True)
class SiteProfile:
    """Declarative profile for a single e-commerce site.

    Attributes:
        slug: Stable lowercase identifier used in API requests and audit logs.
        name: Human-readable site name.
        domains: One or more domains owned by the site. The first entry is
            treated as the canonical domain. Regional mirrors live under
            ``regional_domains``.
        regional_domains: Optional list of additional domains (e.g. country
            variants) that should resolve to this profile.
        home_url: Site landing URL used when no other context is available.
        search_url_template: URL template for keyword search. The literal
            substring ``{query}`` is replaced with the URL-encoded query.
        login_url: Optional explicit login URL. Empty when the site only
            requires login at checkout.
        cart_url: Optional explicit cart URL.
        currency: ISO 4217 currency code typically displayed on the site.
        locale: BCP 47 locale string that best describes the default UI.
        product_card_selectors: CSS selectors that, in priority order, target
            search-result product cards on the search page.
        product_title_selectors: Selectors that target the product title text
            inside a card or on a product page.
        product_price_selectors: Selectors that target the visible price text.
        product_link_selectors: Selectors whose ``href`` points to the
            product detail page.
        add_to_cart_labels: Visible button labels (case-insensitive substring
            match) that add the current item to cart.
        cart_link_labels: Visible link labels for navigating to the cart.
        checkout_labels: Labels that take the user into the checkout funnel.
            These are allowed up to (but not through) the payment step.
        forbidden_action_labels: Labels for buttons the agent must never
            click without explicit human approval. These cover final order
            placement and payment submission.
        notes: Operator notes (e.g. anti-bot caveats, login quirks).
    """

    slug: str
    name: str
    domains: tuple[str, ...]
    home_url: str
    search_url_template: str
    regional_domains: tuple[str, ...] = ()
    login_url: str = ""
    cart_url: str = ""
    currency: str = "USD"
    locale: str = "en-US"
    product_card_selectors: tuple[str, ...] = ()
    product_title_selectors: tuple[str, ...] = ()
    product_price_selectors: tuple[str, ...] = ()
    product_link_selectors: tuple[str, ...] = ()
    add_to_cart_labels: tuple[str, ...] = ()
    cart_link_labels: tuple[str, ...] = ()
    checkout_labels: tuple[str, ...] = ()
    forbidden_action_labels: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def build_search_url(self, query: str) -> str:
        """Render the search URL for ``query``.

        URL-encodes the query so callers can pass raw user text without
        worrying about reserved characters.
        """
        if not query:
            raise ValueError("query must be non-empty")
        if "{query}" not in self.search_url_template:
            raise ValueError(
                f"search_url_template for {self.slug!r} is missing the '{{query}}' placeholder"
            )
        return self.search_url_template.replace("{query}", quote_plus(query))

    def matches_domain(self, url_or_host: str) -> bool:
        """Return True when ``url_or_host`` belongs to this profile."""
        host = _extract_host(url_or_host)
        if not host:
            return False
        for d in (*self.domains, *self.regional_domains):
            if host == d or host.endswith("." + d):
                return True
        return False


# ---------------------------------------------------------------------------
# Shared "never click" labels — applied on top of per-site forbidden labels
# ---------------------------------------------------------------------------

# These match the universal anti-purchase / anti-payment buttons we never
# auto-click. Per-site profiles can extend this list with localized variants.
COMMON_FORBIDDEN_LABELS: tuple[str, ...] = (
    "place order",
    "place your order",
    "submit order",
    "complete order",
    "confirm order",
    "confirm and pay",
    "confirm purchase",
    "pay now",
    "pay with",
    "buy now",
    "buy it now",
    "buy",
    "checkout now",
    "complete purchase",
    "complete payment",
    "authorize payment",
    "authorise payment",
    "place secure order",
    "submit payment",
)


def _extract_host(url_or_host: str) -> str:
    if not url_or_host:
        return ""
    if "://" in url_or_host:
        host = urlparse(url_or_host).hostname or ""
    else:
        host = url_or_host
    return host.lower().lstrip(".")


# ---------------------------------------------------------------------------
# Catalog — top 20 global e-commerce sites
# ---------------------------------------------------------------------------

_CATALOG: tuple[SiteProfile, ...] = (
    SiteProfile(
        slug="amazon",
        name="Amazon",
        domains=("amazon.com",),
        regional_domains=(
            "amazon.co.uk",
            "amazon.de",
            "amazon.fr",
            "amazon.it",
            "amazon.es",
            "amazon.co.jp",
            "amazon.ca",
            "amazon.com.au",
            "amazon.in",
            "amazon.com.br",
            "amazon.com.mx",
        ),
        home_url="https://www.amazon.com",
        search_url_template="https://www.amazon.com/s?k={query}",
        login_url="https://www.amazon.com/ap/signin",
        cart_url="https://www.amazon.com/gp/cart/view.html",
        currency="USD",
        product_card_selectors=(
            "div[data-component-type='s-search-result']",
            "div.s-result-item[data-asin]",
        ),
        product_title_selectors=("h2 a span", "span.a-text-normal"),
        product_price_selectors=(".a-price .a-offscreen", ".a-price"),
        product_link_selectors=("h2 a.a-link-normal", "a.a-link-normal"),
        add_to_cart_labels=("add to cart", "add to basket"),
        cart_link_labels=("cart", "basket"),
        checkout_labels=("proceed to checkout", "proceed to buy", "go to checkout"),
        forbidden_action_labels=(
            "place your order",
            "place order",
            "buy now",
            "1-click",
            "one-click",
        ),
        notes="Amazon presents 'Buy Now' on product pages — always forbidden.",
    ),
    SiteProfile(
        slug="ebay",
        name="eBay",
        domains=("ebay.com",),
        regional_domains=("ebay.co.uk", "ebay.de", "ebay.com.au", "ebay.ca"),
        home_url="https://www.ebay.com",
        search_url_template="https://www.ebay.com/sch/i.html?_nkw={query}",
        login_url="https://signin.ebay.com",
        cart_url="https://cart.ebay.com",
        currency="USD",
        product_card_selectors=("li.s-item", "div.s-item__wrapper"),
        product_title_selectors=(".s-item__title",),
        product_price_selectors=(".s-item__price",),
        product_link_selectors=("a.s-item__link",),
        add_to_cart_labels=("add to cart", "add to basket"),
        cart_link_labels=("cart", "basket"),
        checkout_labels=("go to checkout", "proceed to checkout", "review order"),
        forbidden_action_labels=("buy it now", "confirm and pay", "place order"),
    ),
    SiteProfile(
        slug="aliexpress",
        name="AliExpress",
        domains=("aliexpress.com", "aliexpress.us"),
        home_url="https://www.aliexpress.com",
        search_url_template="https://www.aliexpress.com/wholesale?SearchText={query}",
        login_url="https://login.aliexpress.com",
        cart_url="https://www.aliexpress.com/p/shoppingcart/index.html",
        currency="USD",
        product_card_selectors=(
            "a.search-card-item",
            "div.list--gallery--C2f2tvm a",
        ),
        product_title_selectors=("h3", ".multi--titleText--nXeOvyr"),
        product_price_selectors=(
            ".multi--price-sale--U-S0jtj",
            ".manhattan--price--WvaUgDY",
        ),
        product_link_selectors=("a.search-card-item",),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("proceed to checkout", "go to checkout"),
        forbidden_action_labels=("place order", "buy now", "pay now"),
        notes="Frequently shows aggressive captcha challenges — agent must pause.",
    ),
    SiteProfile(
        slug="walmart",
        name="Walmart",
        domains=("walmart.com",),
        regional_domains=("walmart.ca", "walmart.com.mx"),
        home_url="https://www.walmart.com",
        search_url_template="https://www.walmart.com/search?q={query}",
        login_url="https://www.walmart.com/account/login",
        cart_url="https://www.walmart.com/cart",
        currency="USD",
        product_card_selectors=("div[data-item-id]", "div[data-testid='item-stack']"),
        product_title_selectors=("span[data-automation-id='product-title']", "a span"),
        product_price_selectors=("div[data-automation-id='product-price']", "span.f6"),
        product_link_selectors=("a[link-identifier='linkTest']", "a"),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("continue to checkout", "checkout"),
        forbidden_action_labels=("place order", "place your order", "buy now"),
    ),
    SiteProfile(
        slug="etsy",
        name="Etsy",
        domains=("etsy.com",),
        home_url="https://www.etsy.com",
        search_url_template="https://www.etsy.com/search?q={query}",
        login_url="https://www.etsy.com/signin",
        cart_url="https://www.etsy.com/cart",
        currency="USD",
        product_card_selectors=("div.v2-listing-card", "li.wt-list-unstyled"),
        product_title_selectors=("h3.v2-listing-card__title", "h3"),
        product_price_selectors=("span.currency-value", "p.lc-price"),
        product_link_selectors=("a.listing-link", "a"),
        add_to_cart_labels=("add to cart", "add to basket"),
        cart_link_labels=("cart",),
        checkout_labels=("proceed to checkout",),
        forbidden_action_labels=("place your order", "place order", "pay now"),
    ),
    SiteProfile(
        slug="rakuten",
        name="Rakuten",
        domains=("rakuten.co.jp", "rakuten.com"),
        home_url="https://www.rakuten.co.jp",
        search_url_template="https://search.rakuten.co.jp/search/mall/{query}/",
        login_url="https://login.account.rakuten.com",
        cart_url="https://basket.step.rakuten.co.jp/rms/mall/bs/cartlist/",
        currency="JPY",
        locale="ja-JP",
        product_card_selectors=("div.searchresultitem", "div.dui-card"),
        product_title_selectors=("h2 a", "a.title"),
        product_price_selectors=(".price--3zUvK", "span.important"),
        product_link_selectors=("a.title", "h2 a"),
        add_to_cart_labels=("add to cart", "カートに追加", "買い物かごに追加"),
        cart_link_labels=("cart", "カート"),
        checkout_labels=("proceed to checkout", "ご購入手続きへ"),
        forbidden_action_labels=("place order", "注文を確定する", "購入を確定"),
        notes="Japanese UI — confirmation buttons are localized.",
    ),
    SiteProfile(
        slug="mercadolibre",
        name="Mercado Libre",
        domains=("mercadolibre.com",),
        regional_domains=(
            "mercadolibre.com.ar",
            "mercadolibre.com.mx",
            "mercadolivre.com.br",
            "mercadolibre.cl",
            "mercadolibre.com.co",
        ),
        home_url="https://www.mercadolibre.com",
        search_url_template="https://listado.mercadolibre.com.ar/{query}",
        login_url="https://www.mercadolibre.com/jms/mlb/lgz/login",
        cart_url="https://www.mercadolibre.com.ar/gz/cart",
        currency="ARS",
        locale="es-AR",
        product_card_selectors=("li.ui-search-layout__item", "div.andes-card"),
        product_title_selectors=("h2.ui-search-item__title", "h2"),
        product_price_selectors=("span.andes-money-amount", ".price-tag"),
        product_link_selectors=("a.ui-search-link",),
        add_to_cart_labels=("add to cart", "agregar al carrito", "adicionar ao carrinho"),
        cart_link_labels=("cart", "carrito", "carrinho"),
        checkout_labels=("continuar", "ir al pago", "ir para o pagamento"),
        forbidden_action_labels=(
            "comprar ahora",
            "comprar agora",
            "pagar",
            "confirmar compra",
            "finalizar compra",
        ),
    ),
    SiteProfile(
        slug="shopee",
        name="Shopee",
        domains=("shopee.com",),
        regional_domains=(
            "shopee.sg",
            "shopee.com.my",
            "shopee.co.id",
            "shopee.vn",
            "shopee.ph",
            "shopee.co.th",
            "shopee.tw",
            "shopee.com.br",
        ),
        home_url="https://shopee.sg",
        search_url_template="https://shopee.sg/search?keyword={query}",
        login_url="https://shopee.sg/buyer/login",
        cart_url="https://shopee.sg/cart",
        currency="SGD",
        locale="en-SG",
        product_card_selectors=("div.shopee-search-item-result__item", "li.col-xs-2-4"),
        product_title_selectors=("div._10Wbs- _5SSWfi UjjMrh", "div.ie3A+n"),
        product_price_selectors=("span.ZEgDH9",),
        product_link_selectors=("a[data-sqe='link']",),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("check out", "checkout"),
        forbidden_action_labels=("place order", "buy now", "confirm payment"),
        notes="Shopee uses dynamic class names; selectors are best-effort.",
    ),
    SiteProfile(
        slug="lazada",
        name="Lazada",
        domains=("lazada.com",),
        regional_domains=(
            "lazada.sg",
            "lazada.com.my",
            "lazada.co.id",
            "lazada.vn",
            "lazada.com.ph",
            "lazada.co.th",
        ),
        home_url="https://www.lazada.sg",
        search_url_template="https://www.lazada.sg/catalog/?q={query}",
        login_url="https://member.lazada.sg/user/login",
        cart_url="https://cart.lazada.sg/cart",
        currency="SGD",
        locale="en-SG",
        product_card_selectors=("div[data-qa-locator='product-item']", "div.Bm3ON"),
        product_title_selectors=("div.RfADt a", "a.RfADt"),
        product_price_selectors=("span.ooOxS", "div.aBrP0"),
        product_link_selectors=("div.RfADt a",),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("proceed to checkout",),
        forbidden_action_labels=("place order", "buy now", "pay now"),
    ),
    SiteProfile(
        slug="temu",
        name="Temu",
        domains=("temu.com",),
        home_url="https://www.temu.com",
        search_url_template="https://www.temu.com/search_result.html?search_key={query}",
        login_url="https://www.temu.com/login.html",
        cart_url="https://www.temu.com/cart.html",
        currency="USD",
        product_card_selectors=("div[data-mid]", "div.product-item"),
        product_title_selectors=("h2", "div._2qfL3i9R"),
        product_price_selectors=("div._2de9ERAH", "span._2OlnIWtA"),
        product_link_selectors=("a",),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("checkout", "proceed to checkout"),
        forbidden_action_labels=("place order", "pay now", "buy now"),
    ),
    SiteProfile(
        slug="shein",
        name="Shein",
        domains=("shein.com",),
        regional_domains=("us.shein.com", "uk.shein.com", "eur.shein.com"),
        home_url="https://us.shein.com",
        search_url_template="https://us.shein.com/pdsearch/{query}/",
        login_url="https://us.shein.com/user/auth/login",
        cart_url="https://us.shein.com/cart.html",
        currency="USD",
        product_card_selectors=("section.product-card", "div.S-product-item"),
        product_title_selectors=(".product-card__name", "a[role='link']"),
        product_price_selectors=(".product-card__price", "span.normal-price"),
        product_link_selectors=("a.S-product-item__link-target", "a"),
        add_to_cart_labels=("add to cart", "add to bag"),
        cart_link_labels=("cart", "bag"),
        checkout_labels=("check out", "checkout"),
        forbidden_action_labels=("place order", "pay now"),
    ),
    SiteProfile(
        slug="target",
        name="Target",
        domains=("target.com",),
        home_url="https://www.target.com",
        search_url_template="https://www.target.com/s?searchTerm={query}",
        login_url="https://www.target.com/login",
        cart_url="https://www.target.com/cart",
        currency="USD",
        product_card_selectors=(
            "div[data-test='product-card-default']",
            "div[data-test='@web/site-top-of-funnel/ProductCardWrapper']",
        ),
        product_title_selectors=("a[data-test='product-title']",),
        product_price_selectors=("span[data-test='current-price']",),
        product_link_selectors=("a[data-test='product-title']",),
        add_to_cart_labels=("add to cart", "add for shipping", "add for pickup"),
        cart_link_labels=("cart",),
        checkout_labels=("checkout",),
        forbidden_action_labels=("place your order", "place order", "pay now"),
    ),
    SiteProfile(
        slug="bestbuy",
        name="Best Buy",
        domains=("bestbuy.com",),
        regional_domains=("bestbuy.ca",),
        home_url="https://www.bestbuy.com",
        search_url_template="https://www.bestbuy.com/site/searchpage.jsp?st={query}",
        login_url="https://www.bestbuy.com/identity/signin",
        cart_url="https://www.bestbuy.com/cart",
        currency="USD",
        product_card_selectors=("li.sku-item", "div.product-list-item"),
        product_title_selectors=("h4.sku-title", "a.product-list-item__title"),
        product_price_selectors=("div.priceView-customer-price", "div.priceView-hero-price"),
        product_link_selectors=("h4.sku-title a", "a"),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("checkout",),
        forbidden_action_labels=("place your order", "place order", "buy now"),
    ),
    SiteProfile(
        slug="costco",
        name="Costco",
        domains=("costco.com",),
        regional_domains=("costco.ca", "costco.co.uk"),
        home_url="https://www.costco.com",
        search_url_template="https://www.costco.com/CatalogSearch?keyword={query}",
        login_url="https://www.costco.com/LogonForm",
        cart_url="https://www.costco.com/CheckoutCartView",
        currency="USD",
        product_card_selectors=("div.product", "div.product-tile-set"),
        product_title_selectors=("span.description a", "a.description-link"),
        product_price_selectors=("div.price",),
        product_link_selectors=("span.description a", "a"),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("checkout",),
        forbidden_action_labels=("place your order", "place order"),
    ),
    SiteProfile(
        slug="flipkart",
        name="Flipkart",
        domains=("flipkart.com",),
        home_url="https://www.flipkart.com",
        search_url_template="https://www.flipkart.com/search?q={query}",
        login_url="https://www.flipkart.com/account/login",
        cart_url="https://www.flipkart.com/viewcart",
        currency="INR",
        locale="en-IN",
        product_card_selectors=("div._1AtVbE", "div._4ddWXP", "div._13oc-S"),
        product_title_selectors=("a.IRpwTa", "div._4rR01T", "a.s1Q9rs"),
        product_price_selectors=("div._30jeq3",),
        product_link_selectors=("a.IRpwTa", "a._1fQZEK"),
        add_to_cart_labels=("add to cart",),
        cart_link_labels=("cart",),
        checkout_labels=("place order", "continue"),
        forbidden_action_labels=("place order", "pay", "buy now"),
        notes=(
            "Flipkart confusingly labels its final-checkout button 'Place Order' even on "
            "the address step; treat all 'Place Order' clicks as forbidden by default."
        ),
    ),
    SiteProfile(
        slug="jd",
        name="JD.com",
        domains=("jd.com",),
        regional_domains=("global.jd.com",),
        home_url="https://www.jd.com",
        search_url_template="https://search.jd.com/Search?keyword={query}",
        login_url="https://passport.jd.com/new/login.aspx",
        cart_url="https://cart.jd.com/cart_index",
        currency="CNY",
        locale="zh-CN",
        product_card_selectors=("li.gl-item", "div.gl-i-wrap"),
        product_title_selectors=("div.p-name a em", "div.p-name"),
        product_price_selectors=("div.p-price i", "div.p-price"),
        product_link_selectors=("div.p-name a",),
        add_to_cart_labels=("add to cart", "加入购物车"),
        cart_link_labels=("cart", "购物车"),
        checkout_labels=("checkout", "去结算", "结算"),
        forbidden_action_labels=("place order", "提交订单", "立即购买", "立即支付"),
    ),
    SiteProfile(
        slug="taobao",
        name="Taobao / Tmall",
        domains=("taobao.com", "tmall.com"),
        home_url="https://www.taobao.com",
        search_url_template="https://s.taobao.com/search?q={query}",
        login_url="https://login.taobao.com",
        cart_url="https://cart.taobao.com/cart.htm",
        currency="CNY",
        locale="zh-CN",
        product_card_selectors=("div.item", "div[data-spm]"),
        product_title_selectors=("div.title a", "a.J_ClickStat"),
        product_price_selectors=("strong.price", "span.price"),
        product_link_selectors=("a.J_ClickStat", "div.title a"),
        add_to_cart_labels=("add to cart", "加入购物车"),
        cart_link_labels=("cart", "购物车"),
        checkout_labels=("checkout", "结算", "去结算"),
        forbidden_action_labels=("place order", "提交订单", "立即购买"),
        notes="Taobao routinely blocks unauthenticated automation — expect captcha walls.",
    ),
    SiteProfile(
        slug="zalando",
        name="Zalando",
        domains=("zalando.com",),
        regional_domains=("zalando.de", "zalando.co.uk", "zalando.fr", "zalando.it"),
        home_url="https://www.zalando.com",
        search_url_template="https://www.zalando.com/catalog/?q={query}",
        login_url="https://www.zalando.com/login/",
        cart_url="https://www.zalando.com/cart/",
        currency="EUR",
        locale="en-EU",
        product_card_selectors=("article", "a[data-testid='product-card']"),
        product_title_selectors=("h3", "header"),
        product_price_selectors=("span[data-testid='price']", "p"),
        product_link_selectors=("a",),
        add_to_cart_labels=("add to bag", "add to cart"),
        cart_link_labels=("bag", "cart"),
        checkout_labels=("go to checkout", "checkout"),
        forbidden_action_labels=("place order", "buy now", "order and pay"),
    ),
    SiteProfile(
        slug="asos",
        name="ASOS",
        domains=("asos.com",),
        home_url="https://www.asos.com",
        search_url_template="https://www.asos.com/search/?q={query}",
        login_url="https://www.asos.com/identity/login",
        cart_url="https://www.asos.com/bag",
        currency="GBP",
        locale="en-GB",
        product_card_selectors=("article[data-auto-id='productTile']", "article"),
        product_title_selectors=("p[data-auto-id='productTileDescription']",),
        product_price_selectors=("span[data-auto-id='productTilePrice']",),
        product_link_selectors=("a",),
        add_to_cart_labels=("add to bag", "add to basket"),
        cart_link_labels=("bag", "basket"),
        checkout_labels=("checkout",),
        forbidden_action_labels=("place order", "pay now", "place your order"),
    ),
    SiteProfile(
        slug="coupang",
        name="Coupang",
        domains=("coupang.com",),
        home_url="https://www.coupang.com",
        search_url_template="https://www.coupang.com/np/search?q={query}",
        login_url="https://login.coupang.com/login/login.pang",
        cart_url="https://cart.coupang.com/cartView.pang",
        currency="KRW",
        locale="ko-KR",
        product_card_selectors=("li.search-product", "ul.search-product-list li"),
        product_title_selectors=("div.name",),
        product_price_selectors=("strong.price-value",),
        product_link_selectors=("a.search-product-link", "a"),
        add_to_cart_labels=("add to cart", "장바구니"),
        cart_link_labels=("cart", "장바구니"),
        checkout_labels=("checkout", "구매하기"),
        forbidden_action_labels=("place order", "결제하기", "주문하기"),
    ),
)


# Build lookup tables once at import time.
_BY_SLUG: dict[str, SiteProfile] = {p.slug: p for p in _CATALOG}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def all_profiles() -> tuple[SiteProfile, ...]:
    """Return every profile in the catalog in declaration order."""
    return _CATALOG


def all_slugs() -> tuple[str, ...]:
    """Return the slug of every profile in catalog order."""
    return tuple(p.slug for p in _CATALOG)


def get_profile(slug: str) -> SiteProfile:
    """Return the profile with the given slug.

    Raises:
        KeyError: When no profile matches ``slug``.
    """
    try:
        return _BY_SLUG[slug.lower().strip()]
    except KeyError as exc:
        raise KeyError(f"Unknown e-commerce site slug: {slug!r}") from exc


def find_profile_by_url(url: str) -> SiteProfile | None:
    """Return the profile whose domains match ``url``, or None."""
    host = _extract_host(url)
    if not host:
        return None
    for profile in _CATALOG:
        if profile.matches_domain(host):
            return profile
    return None


_LABEL_NORMALISER = re.compile(r"\s+")


def normalize_label(text: str) -> str:
    """Lowercase + collapse internal whitespace for label comparisons."""
    return _LABEL_NORMALISER.sub(" ", text or "").strip().lower()


def is_forbidden_label(text: str, profile: SiteProfile | None = None) -> bool:
    """Return True when ``text`` matches a known forbidden action label.

    Always checks ``COMMON_FORBIDDEN_LABELS``. When ``profile`` is provided,
    also checks the site's ``forbidden_action_labels``.

    Matching is case-insensitive whitespace-collapsed substring matching so
    "Place Your Order Now" matches "place order" and "Place your order".
    """
    needle = normalize_label(text)
    if not needle:
        return False
    labels: list[str] = [normalize_label(label) for label in COMMON_FORBIDDEN_LABELS]
    if profile is not None:
        labels.extend(normalize_label(label) for label in profile.forbidden_action_labels)
    for label in labels:
        if not label:
            continue
        if label in needle:
            return True
    return False


__all__ = [
    "COMMON_FORBIDDEN_LABELS",
    "SiteProfile",
    "all_profiles",
    "all_slugs",
    "find_profile_by_url",
    "get_profile",
    "is_forbidden_label",
    "normalize_label",
]
