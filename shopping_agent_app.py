"""
AI Shopping Agent -- Streamlit UI + Playwright + Bright Data Scraping Browser
Single-file, production-ready, hackathon-deployable.

Setup:
    pip install streamlit playwright anthropic python-dotenv
    playwright install chromium

Run:
    streamlit run shopping_agent_app.py

ENV VARS (set in .env or Streamlit secrets):
    BRD_CUSTOMER      e.g. brd-customer-HL_abc123
    BRD_ZONE          e.g. scraping_browser1
    BRD_PASSWORD      zone password
    ANTHROPIC_API_KEY optional -- enables Claude intent parsing
"""

# --- stdlib -------------------------------------------------------------------
import asyncio
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# --- third-party --------------------------------------------------------------
import streamlit as st
from dotenv import load_dotenv
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PWTimeout,
)

load_dotenv()

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False


# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------

@dataclass
class AgentConfig:
    # -- Bright Data -----------------------------------------------------------
    brd_customer: str  = field(default_factory=lambda: os.environ.get("BRD_CUSTOMER", ""))
    brd_zone: str      = field(default_factory=lambda: os.environ.get("BRD_ZONE", "scraping_browser1"))
    brd_password: str  = field(default_factory=lambda: os.environ.get("BRD_PASSWORD", ""))

    # -- Target store ----------------------------------------------------------
    base_url: str      = "https://books.toscrape.com"
    search_query: str  = field(default_factory=lambda: os.environ.get("SEARCH_QUERY", "mystery"))

    # -- Browser ---------------------------------------------------------------
    headless: bool     = field(default_factory=lambda: os.environ.get("HEADLESS", "true").lower() == "true")

    # -- Timeouts (ms) ---------------------------------------------------------
    nav_timeout: int      = 60_000
    element_timeout: int  = 30_000
    captcha_timeout: int  = 120_000

    # -- Human-like delay ranges (seconds) -------------------------------------
    delay_short:  tuple = (0.3, 0.9)
    delay_medium: tuple = (0.9, 2.2)
    delay_long:   tuple = (2.2, 4.5)

    # -- Mock checkout data ----------------------------------------------------
    checkout_email:    str = "demo.agent@example.com"
    checkout_fname:    str = "Alex"
    checkout_lname:    str = "Demo"
    checkout_address:  str = "742 Evergreen Terrace"
    checkout_address2: str = "Apt 1"
    checkout_city:     str = "Springfield"
    checkout_state:    str = "IL"
    checkout_postcode: str = "62701"
    checkout_country:  str = "US"
    checkout_phone:    str = "5555550100"

    card_number: str = "4111 1111 1111 1111"
    card_expiry: str = "12/26"
    card_cvc:    str = "123"
    card_name:   str = "Alex Demo"


# ------------------------------------------------------------------------------
# LOGGING -- dual output: Python logger + thread-safe queue for Streamlit
# ------------------------------------------------------------------------------

class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord):
        self.q.put(self.format(record))


def make_logger(log_queue: queue.Queue) -> logging.Logger:
    logger = logging.getLogger(f"agent-{id(log_queue)}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(fmt)
    logger.addHandler(stream_h)

    queue_h = QueueHandler(log_queue)
    queue_h.setFormatter(fmt)
    logger.addHandler(queue_h)

    return logger


# ------------------------------------------------------------------------------
# STEALTH HELPERS
# ------------------------------------------------------------------------------

async def human_delay(range_: tuple) -> None:
    await asyncio.sleep(random.uniform(*range_))


async def human_type(page: Page, selector: str, text: str) -> None:
    el = page.locator(selector).first
    await el.scroll_into_view_if_needed()
    await el.click()
    await asyncio.sleep(random.uniform(0.1, 0.3))
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.14))


async def safe_fill(
    page: Page,
    selectors: list[str],
    value: str,
    label: str,
    log: logging.Logger,
    frame=None,
) -> bool:
    """
    Try each selector in order on `frame` (or `page` if frame is None).
    Uses triple-click + type to handle pre-filled inputs correctly.
    Returns True on first success.
    """
    target = frame if frame else page
    for sel in selectors:
        try:
            el = target.locator(sel).first
            cnt = await el.count()
            if cnt == 0:
                continue
            await el.scroll_into_view_if_needed()
            await el.triple_click()
            await asyncio.sleep(0.05)
            for ch in value:
                await page.keyboard.type(ch)
                await asyncio.sleep(random.uniform(0.04, 0.13))
            log.info(f"    - {label}: {value}")
            return True
        except Exception as exc:
            log.debug(f"    selector '{sel}' failed for {label}: {exc}")
    log.warning(f"    - Could not fill '{label}' -- no selector matched")
    return False


async def apply_stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        window.chrome = { runtime: {} };
        Object.defineProperty(screen, 'width',  { get: () => 1920 });
        Object.defineProperty(screen, 'height', { get: () => 1080 });
    """)


async def wait_captcha(page: Page, timeout: int, log: logging.Logger) -> None:
    captcha_sels = [
        "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
        ".g-recaptcha", "#captcha", "[id*='captcha']", "[class*='captcha']",
    ]
    for sel in captcha_sels:
        try:
            if await page.locator(sel).count() > 0:
                log.info("  CAPTCHA detected -- awaiting Bright Data auto-solve...")
                await page.wait_for_selector(sel, state="hidden", timeout=timeout)
                log.info("  CAPTCHA resolved")
                return
        except PWTimeout:
            log.warning(f"  CAPTCHA selector '{sel}' timeout -- continuing")
        except Exception:
            pass


# ------------------------------------------------------------------------------
# BROWSER FACTORY
# ------------------------------------------------------------------------------

async def build_browser(
    cfg: AgentConfig, pw, log: logging.Logger
) -> tuple[Browser, BrowserContext]:
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    ctx_opts = dict(
        viewport={"width": 1440, "height": 900},
        user_agent=ua,
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    if cfg.brd_customer and cfg.brd_password:
        endpoint = (
            f"wss://{cfg.brd_customer}-zone-{cfg.brd_zone}:"
            f"{cfg.brd_password}@brd.superproxy.io:9222"
        )
        log.info(f"Connecting to Bright Data - brd.superproxy.io:9222 (zone: {cfg.brd_zone})")
        browser = await pw.chromium.connect_over_cdp(endpoint)
        context = (
            browser.contexts[0]
            if browser.contexts
            else await browser.new_context(**ctx_opts)
        )
        log.info("Bright Data connection established")
    else:
        log.warning("No Bright Data credentials - local Chromium (dev mode)")
        browser = await pw.chromium.launch(
            headless=cfg.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(**ctx_opts)

    context.set_default_timeout(cfg.element_timeout)
    context.set_default_navigation_timeout(cfg.nav_timeout)
    return browser, context


# ------------------------------------------------------------------------------
# INTENT PARSER
# ------------------------------------------------------------------------------

@dataclass
class ParsedIntent:
    query: str
    category: Optional[str]   = None
    max_price: Optional[float] = None
    color: Optional[str]      = None
    size: Optional[str]       = None


def _parse_local(prompt: str) -> ParsedIntent:
    price = re.search(r"under\s*[£$€]?([\d.]+)", prompt, re.I)
    size  = re.search(r"\bsize\s+([\w.]+)", prompt, re.I)
    color = re.search(
        r"\b(black|white|red|blue|green|grey|gray|brown|navy|beige|pink|purple)\b",
        prompt, re.I
    )
    q = re.sub(
        r"(under\s*[£$€]?[\d.]+|size\s+[\w.]+|add to cart|buy|find|get|please|,)",
        "", prompt, flags=re.I
    )
    q = re.sub(r"\s+", " ", q).strip()
    return ParsedIntent(
        query=q or prompt,
        max_price=float(price.group(1)) if price else None,
        color=color.group(1).lower() if color else None,
        size=size.group(1) if size else None,
    )


async def parse_intent(prompt: str, log: logging.Logger) -> ParsedIntent:
    if not _ANTHROPIC_AVAILABLE or not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("Using local regex intent parser.")
        return _parse_local(prompt)

    client = anthropic.Anthropic()
    system = (
        "Extract shopping intent. Return ONLY compact JSON with keys: "
        "query(str), category(str|null), max_price(num|null), "
        "color(str|null), size(str|null). No markdown."
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(msg.content[0].text.strip())
        log.info(f"Claude parsed intent: {data}")
        return ParsedIntent(
            query=data.get("query", prompt),
            category=data.get("category"),
            max_price=data.get("max_price"),
            color=data.get("color"),
            size=data.get("size"),
        )
    except Exception as e:
        log.warning(f"Claude intent parse failed ({e}) -- using regex fallback.")
        return _parse_local(prompt)


# ------------------------------------------------------------------------------
# STEP 1 -- SEARCH
# ------------------------------------------------------------------------------

@dataclass
class ProductResult:
    title: str
    price: float
    url: str
    rating: Optional[str]       = None
    availability: Optional[str] = None


async def step_search(
    page: Page, cfg: AgentConfig, intent: ParsedIntent, log: logging.Logger
) -> list[ProductResult]:
    log.info(f"[STEP 1/4] Searching '{intent.query}' on {cfg.base_url}")
    await apply_stealth(page)
    await page.goto(cfg.base_url, wait_until="domcontentloaded")
    await wait_captcha(page, cfg.captcha_timeout, log)
    await human_delay(cfg.delay_medium)

    query_lower = intent.query.lower()
    all_links = await page.locator("ul.nav-list li a").all()
    matched_href = None
    matched_name = None

    for link in all_links:
        text = (await link.inner_text()).strip().lower()
        if text in query_lower or any(w in text for w in query_lower.split() if len(w) > 3):
            matched_href = await link.get_attribute("href")
            matched_name = text.title()
            break

    if not matched_href and len(all_links) > 1:
        matched_href = await all_links[1].get_attribute("href")
        matched_name = (await all_links[1].inner_text()).strip()

    if matched_href:
        cat_url = f"{cfg.base_url}/{matched_href.lstrip('/')}"
        log.info(f"  Navigating to category: '{matched_name}' - {cat_url}")
        await page.goto(cat_url, wait_until="domcontentloaded")
        await wait_captcha(page, cfg.captcha_timeout, log)
        await human_delay(cfg.delay_medium)

    await page.wait_for_selector("article.product_pod", timeout=cfg.element_timeout)
    cards = await page.locator("article.product_pod").all()
    log.info(f"  Found {len(cards)} product cards")

    results: list[ProductResult] = []
    for card in cards[:12]:
        try:
            title_el = card.locator("h3 a")
            title    = await title_el.get_attribute("title") or await title_el.inner_text()
            price_t  = await card.locator("p.price_color").inner_text()
            price    = float(re.sub(r"[^\d.]", "", price_t))
            href     = await title_el.get_attribute("href")
            if href and not href.startswith("http"):
                href = f"{cfg.base_url}/catalogue/{href.replace('../', '')}"
            rating   = (await card.locator("p.star-rating").get_attribute("class") or "").replace("star-rating", "").strip()
            avail    = (await card.locator("p.availability").inner_text()).strip()
            results.append(ProductResult(
                title=title.strip(), price=price, url=href,
                rating=rating, availability=avail
            ))
        except Exception as e:
            log.debug(f"  Skipped card: {e}")

    if intent.max_price is not None:
        filtered = [r for r in results if r.price <= intent.max_price]
        if filtered:
            results = filtered
            log.info(f"  Filtered to {len(results)} items under {intent.max_price}")

    if not results:
        raise RuntimeError("No products found -- check search query or site availability.")

    log.info(f"[STEP 1/4] DONE -- top match: '{results[0].title}' @ {results[0].price:.2f}")
    return results


# ------------------------------------------------------------------------------
# STEP 2 -- SELECT PRODUCT & ADD TO CART
# ------------------------------------------------------------------------------

async def step_add_to_cart(
    page: Page, cfg: AgentConfig, product: ProductResult, log: logging.Logger
) -> None:
    log.info(f"[STEP 2/4] Opening product page: {product.title}")
    await page.goto(product.url, wait_until="domcontentloaded")
    await wait_captcha(page, cfg.captcha_timeout, log)
    await human_delay(cfg.delay_medium)

    for qsel in ["input#id_quantity", "input[name='quantity']", "input[type='number']"]:
        try:
            el = page.locator(qsel).first
            if await el.count():
                await el.triple_click()
                await el.type("1")
                log.info("  Set quantity - 1")
                break
        except Exception:
            pass

    for vsel in ["select[name*='option']", "select[name*='variant']", "select[id*='size']", "select[id*='color']"]:
        try:
            el = page.locator(vsel).first
            if await el.count():
                opts = await el.locator("option").all()
                for opt in opts:
                    val = await opt.get_attribute("value")
                    txt = await opt.inner_text()
                    if val and val not in ("", "0", "choose", "select", "Choose an option"):
                        await el.select_option(value=val)
                        log.info(f"  Selected variant: '{txt.strip()}'")
                        await human_delay(cfg.delay_short)
                        break
        except Exception:
            pass

    atc_selectors = [
        "button:has-text('Add to basket')",
        "button:has-text('Add to Basket')",
        "button:has-text('Add to cart')",
        "button:has-text('Add to Cart')",
        "input[value*='Add to basket']",
        "input[value*='Add to cart']",
        "input[value*='Add to Basket']",
        "button[data-test='add-to-cart']",
        "button[id*='add-to-cart']",
        ".btn-add-to-cart",
        "#add-to-basket",
        "#add-to-cart",
        "button[type='submit']:has-text('Add')",
    ]
    added = False
    for sel in atc_selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.scroll_into_view_if_needed()
                await human_delay(cfg.delay_short)
                await el.click()
                log.info(f"  Clicked Add to Cart ({sel})")
                added = True
                break
        except Exception:
            pass

    if not added:
        raise RuntimeError("Could not find 'Add to Cart' button.")

    await human_delay(cfg.delay_medium)

    for csel in [".alert-success", "p.alert", "#basket-mini .badge", ".flash.success", "[class*='cart-count']"]:
        try:
            el = page.locator(csel).first
            if await el.count():
                txt = (await el.inner_text()).strip()
                log.info(f"  Cart confirmation: '{txt[:80]}'")
                break
        except Exception:
            pass

    log.info("[STEP 2/4] DONE -- item in cart")


# ------------------------------------------------------------------------------
# STEP 3 -- NAVIGATE TO CART
# ------------------------------------------------------------------------------

async def step_open_cart(
    page: Page, cfg: AgentConfig, log: logging.Logger
) -> None:
    log.info("[STEP 3/4] Opening cart...")

    cart_selectors = [
        "a:has-text('View basket')",
        "a:has-text('View Basket')",
        "a:has-text('Cart')",
        "a[href*='basket']",
        "a[href*='cart']",
        ".basket-link",
    ]
    for sel in cart_selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.scroll_into_view_if_needed()
                await human_delay(cfg.delay_short)
                await el.click()
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"  Navigated via: {sel}")
                await human_delay(cfg.delay_medium)
                log.info("[STEP 3/4] DONE -- cart page loaded")
                return
        except Exception:
            pass

    for path in ["/basket/", "/cart/", "/checkout/basket/"]:
        try:
            await page.goto(f"{cfg.base_url}{path}", wait_until="domcontentloaded")
            log.info(f"  Navigated via direct URL: {path}")
            log.info("[STEP 3/4] DONE -- cart page loaded")
            return
        except Exception:
            pass

    raise RuntimeError("Could not navigate to cart.")


# ------------------------------------------------------------------------------
# STEP 4 -- CHECKOUT (full field population, stops before Place Order)
# ------------------------------------------------------------------------------

async def _click_first(page: Page, selectors: list[str], label: str, log: logging.Logger) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.scroll_into_view_if_needed()
                await human_delay((0.2, 0.6))
                await el.click()
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"  Clicked '{label}' via: {sel}")
                return True
        except Exception as e:
            log.debug(f"  click '{label}' selector '{sel}' failed: {e}")
    return False


async def _fill_stripe_iframe(page: Page, cfg: AgentConfig, log: logging.Logger) -> bool:
    card_filled = False
    for frame in page.frames:
        url  = frame.url or ""
        name = frame.name or ""
        if "stripe" not in url and "stripe" not in name and "privateStripe" not in name:
            continue

        log.info(f"  Found Stripe frame: {name or url[:60]}")

        try:
            for sel in ["input[name='cardnumber']", "input[placeholder*='1234']", "input[autocomplete='cc-number']"]:
                el = frame.locator(sel).first
                if await el.count():
                    await el.click()
                    await asyncio.sleep(0.2)
                    for ch in cfg.card_number.replace(" ", ""):
                        await page.keyboard.type(ch)
                        await asyncio.sleep(random.uniform(0.05, 0.12))
                    log.info(f"    - Stripe card number: {cfg.card_number}")
                    card_filled = True
                    break
        except Exception as e:
            log.debug(f"  Stripe card number error: {e}")

        try:
            for sel in ["input[name='exp-date']", "input[placeholder*='MM']", "input[autocomplete='cc-exp']"]:
                el = frame.locator(sel).first
                if await el.count():
                    await el.click()
                    await asyncio.sleep(0.15)
                    for ch in cfg.card_expiry:
                        await page.keyboard.type(ch)
                        await asyncio.sleep(random.uniform(0.05, 0.12))
                    log.info(f"    - Stripe expiry: {cfg.card_expiry}")
                    break
        except Exception as e:
            log.debug(f"  Stripe expiry error: {e}")

        try:
            for sel in ["input[name='cvc']", "input[placeholder*='CVC']", "input[placeholder*='CVV']", "input[autocomplete='cc-csc']"]:
                el = frame.locator(sel).first
                if await el.count():
                    await el.click()
                    await asyncio.sleep(0.15)
                    for ch in cfg.card_cvc:
                        await page.keyboard.type(ch)
                        await asyncio.sleep(random.uniform(0.05, 0.12))
                    log.info(f"    - Stripe CVC: {cfg.card_cvc}")
                    break
        except Exception as e:
            log.debug(f"  Stripe CVC error: {e}")

    return card_filled


async def step_checkout(
    page: Page, cfg: AgentConfig, log: logging.Logger
) -> dict:
    log.info("[STEP 4/4] Starting checkout flow...")

    checkout_btns = [
        "a:has-text('Proceed to checkout')",
        "button:has-text('Proceed to checkout')",
        "a:has-text('Proceed to Checkout')",
        "button:has-text('Proceed to Checkout')",
        "a:has-text('Checkout')",
        "input[value*='Checkout']",
        "button:has-text('Checkout')",
        ".checkout-btn",
        "#proceed-to-checkout",
        "a[href*='checkout']:not([href*='login'])",
        "button[data-test='checkout']",
    ]
    proceeded = await _click_first(page, checkout_btns, "Proceed to Checkout", log)
    if not proceeded:
        try:
            await page.goto(f"{cfg.base_url}/checkout/shipping-address/", wait_until="domcontentloaded")
            log.info("  Navigated to checkout via direct URL")
        except Exception:
            log.warning("  Could not find checkout button -- continuing on current page")

    await wait_captcha(page, cfg.captcha_timeout, log)
    await human_delay(cfg.delay_medium)

    for guest_sel in [
        "a:has-text('Continue as guest')",
        "button:has-text('Continue as Guest')",
        "a:has-text('Guest checkout')",
        "input[value*='guest' i]",
        "#guest-checkout",
    ]:
        try:
            el = page.locator(guest_sel).first
            if await el.count():
                await el.click()
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"  Chose guest checkout via: {guest_sel}")
                await human_delay(cfg.delay_medium)
                break
        except Exception:
            pass

    log.info("  Filling shipping fields...")

    await safe_fill(page, [
        "input[type='email']", "#id_email", "input[name='email']",
        "input[placeholder*='email' i]", "input[autocomplete='email']",
    ], cfg.checkout_email, "email", log)

    await safe_fill(page, [
        "#id_first_name", "input[name='first_name']", "input[name='firstName']",
        "input[name='billing_first_name']", "input[placeholder*='first' i]",
        "input[autocomplete='given-name']",
    ], cfg.checkout_fname, "first name", log)

    await safe_fill(page, [
        "#id_last_name", "input[name='last_name']", "input[name='lastName']",
        "input[name='billing_last_name']", "input[placeholder*='last' i]",
        "input[autocomplete='family-name']",
    ], cfg.checkout_lname, "last name", log)

    await safe_fill(page, [
        "#id_line1", "input[name='address1']", "input[name='address_1']",
        "input[name='street_address']", "input[name='billing_address_1']",
        "input[placeholder*='address' i]", "input[autocomplete='address-line1']",
    ], cfg.checkout_address, "address line 1", log)

    await safe_fill(page, [
        "#id_line2", "input[name='address2']", "input[name='address_2']",
        "input[name='billing_address_2']", "input[placeholder*='address 2' i]",
        "input[autocomplete='address-line2']",
    ], cfg.checkout_address2, "address line 2", log)

    await safe_fill(page, [
        "#id_line4", "input[name='city']", "input[name='town']",
        "input[name='billing_city']", "input[placeholder*='city' i]",
        "input[placeholder*='town' i]", "input[autocomplete='address-level2']",
    ], cfg.checkout_city, "city", log)

    await safe_fill(page, [
        "#id_state", "input[name='state']", "input[name='province']",
        "input[name='billing_state']", "input[placeholder*='state' i]",
        "input[autocomplete='address-level1']",
    ], cfg.checkout_state, "state", log)

    await safe_fill(page, [
        "#id_postcode", "input[name='postcode']", "input[name='zip']",
        "input[name='postal_code']", "input[name='billing_postcode']",
        "input[placeholder*='zip' i]", "input[placeholder*='post' i]",
        "input[autocomplete='postal-code']",
    ], cfg.checkout_postcode, "postcode/zip", log)

    await safe_fill(page, [
        "#id_phone", "input[name='phone']", "input[name='phone_number']",
        "input[type='tel']", "input[placeholder*='phone' i]",
        "input[autocomplete='tel']",
    ], cfg.checkout_phone, "phone", log)

    for csel in [
        "#id_country", "select[name='country']", "select[name='billing_country']",
        "select[id*='country']", "select[autocomplete='country']",
    ]:
        try:
            el = page.locator(csel).first
            if await el.count():
                try:
                    await el.select_option(value=cfg.checkout_country)
                except Exception:
                    await el.select_option(label="United States")
                log.info(f"    - country: {cfg.checkout_country}")
                break
        except Exception:
            pass

    for ssel in [
        "select[name='state']", "select[name='province']",
        "select[id*='state']", "select[name='billing_state']",
    ]:
        try:
            el = page.locator(ssel).first
            if await el.count():
                try:
                    await el.select_option(value=cfg.checkout_state)
                except Exception:
                    try:
                        await el.select_option(label="Illinois")
                    except Exception:
                        pass
                log.info(f"    - state (dropdown): {cfg.checkout_state}")
                break
        except Exception:
            pass

    await human_delay(cfg.delay_medium)

    continue_btns = [
        "button:has-text('Continue')",
        "input[value*='Continue']",
        "button:has-text('Next')",
        "button:has-text('Proceed')",
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Shipping')",
    ]
    advanced = await _click_first(page, continue_btns, "Continue to payment", log)
    if advanced:
        await wait_captcha(page, cfg.captcha_timeout, log)
        await human_delay(cfg.delay_medium)

    log.info("  Filling payment fields...")
    card_in_iframe = await _fill_stripe_iframe(page, cfg, log)

    if not card_in_iframe:
        await safe_fill(page, [
            "input[name='card_number']", "input[name='cardNumber']",
            "input[id*='card-number']", "input[id*='card_number']",
            "input[placeholder*='card number' i]", "input[placeholder*='1234' i]",
            "input[autocomplete='cc-number']", "#id_card_number",
        ], cfg.card_number, "card number", log)

        await safe_fill(page, [
            "input[name='expiry']", "input[name='exp_date']", "input[name='exp-date']",
            "input[name='cardExpiry']", "input[placeholder*='MM / YY' i]",
            "input[placeholder*='MM/YY' i]", "input[placeholder*='expir' i]",
            "input[autocomplete='cc-exp']", "#id_expiry",
        ], cfg.card_expiry, "expiry", log)

        await safe_fill(page, [
            "input[name='cvc']", "input[name='cvv']", "input[name='csc']",
            "input[name='cardCvc']", "input[placeholder*='CVC' i]",
            "input[placeholder*='CVV' i]", "input[placeholder*='security' i]",
            "input[autocomplete='cc-csc']", "#id_cvc",
        ], cfg.card_cvc, "CVC", log)

        await safe_fill(page, [
            "input[name='name_on_card']", "input[name='card_name']",
            "input[name='cardName']", "input[placeholder*='name on card' i]",
            "input[placeholder*='cardholder' i]", "input[autocomplete='cc-name']",
        ], cfg.card_name, "cardholder name", log)

    await human_delay(cfg.delay_medium)
    await page.screenshot(path="checkout_ready.png", full_page=True)

    place_order_text = "NOT CLICKED (demo stop)"
    for sel in [
        "button:has-text('Place order')",
        "button:has-text('Place Order')",
        "button:has-text('Submit order')",
        "button:has-text('Confirm payment')",
        "button:has-text('Pay now')",
        "input[value*='Place order' i]",
        "input[value*='Submit' i]",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count():
                btn_text = await el.inner_text()
                place_order_text = f"FOUND but NOT clicked: '{btn_text}'"
                log.info(f"  STOP -- '{btn_text}' located, not clicked (demo mode)")
                break
        except Exception:
            pass

    log.info("[STEP 4/4] DONE -- all fields filled. Stopped before final submit")
    log.info("           Screenshot saved - checkout_ready.png")

    return {
        "status": "checkout_ready",
        "url": page.url,
        "title": await page.title(),
        "place_order_button": place_order_text,
        "stripe_detected": card_in_iframe,
        "screenshot": "checkout_ready.png",
    }


# ------------------------------------------------------------------------------
# ORCHESTRATOR
# ------------------------------------------------------------------------------

async def run_agent(
    prompt: str,
    cfg: AgentConfig,
    log: logging.Logger,
) -> dict:
    t0 = time.monotonic()
    log.info("-" * 56)
    log.info("  AI SHOPPING AGENT -- START")
    log.info(f"  Prompt: {prompt!r}")
    log.info("-" * 56)

    intent = await parse_intent(prompt, log)
    log.info(
        f"Intent - query={intent.query!r}  max_price={intent.max_price}  "
        f"color={intent.color}  size={intent.size}"
    )

    async with async_playwright() as pw:
        browser, context = await build_browser(cfg, pw, log)
        page = await context.new_page()

        try:
            products = await step_search(page, cfg, intent, log)
            best     = products[0]
            log.info(f"\n  Best match: {best.title}  {best.price:.2f}\n")

            await step_add_to_cart(page, cfg, best, log)
            await step_open_cart(page, cfg, log)
            result = await step_checkout(page, cfg, log)

            elapsed = round(time.monotonic() - t0, 2)
            result["product"] = {"title": best.title, "price": best.price, "url": best.url}
            result["intent"]  = {"query": intent.query, "max_price": intent.max_price}
            result["elapsed"] = elapsed

            log.info("-" * 56)
            log.info(f"  AGENT COMPLETE  ({elapsed}s)")
            log.info(f"  Product : {best.title}")
            log.info(f"  Price   : {best.price:.2f}")
            log.info(f"  Status  : {result['status']}")
            log.info("-" * 56)
            return result

        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 2)
            log.error(f"AGENT FAILED ({elapsed}s): {exc}", exc_info=True)
            try:
                await page.screenshot(path="error_state.png", full_page=True)
                log.info("Error screenshot - error_state.png")
            except Exception:
                pass
            return {"status": "error", "error": str(exc), "elapsed": elapsed}

        finally:
            await context.close()
            await browser.close()
            log.info("Browser closed.")


def run_agent_sync(prompt: str, cfg: AgentConfig, log_queue: queue.Queue) -> dict:
    """Thread-safe wrapper: runs the async agent in a fresh event loop."""
    log = make_logger(log_queue)
    return asyncio.run(run_agent(prompt, cfg, log))


# ------------------------------------------------------------------------------
# STREAMLIT UI
# ------------------------------------------------------------------------------

st.set_page_config(
    page_title="AI Shopping Agent",
    page_icon="-",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&display=swap');
  html, body, [class*='css'] { font-family: 'DM Mono', monospace; }
  .stApp { background: #0b0f1a; color: #e2e8f0; }
  section[data-testid='stSidebar'] { background: #0d1220 !important; border-right: 1px solid #1e2a3a; }
  section[data-testid='stSidebar'] * { color: #94a3b8 !important; }
  .agent-card {
    background: #111827; border: 1px solid #1e293b;
    border-radius: 10px; padding: 18px 22px; margin-bottom: 14px;
  }
  .step-row { display:flex; align-items:center; gap:12px; padding:6px 0; }
  .step-dot {
    width:24px; height:24px; border-radius:50%; border:1px solid #334155;
    display:flex; align-items:center; justify-content:center;
    font-size:11px; color:#64748b; flex-shrink:0;
  }
  .step-dot.active { background:#1d4ed8; border-color:#3b82f6; color:#fff; }
  .step-dot.done   { background:#065f46; border-color:#10b981; color:#10b981; }
  .terminal {
    background:#040810; border:1px solid #1e293b; border-radius:8px;
    padding:14px; font-size:11px; line-height:1.75;
    height:380px; overflow-y:auto; white-space:pre-wrap; color:#94a3b8;
  }
  .badge {
    display:inline-block; padding:3px 10px; border-radius:20px; font-size:10px;
    background:#1e293b; border:1px solid #334155; color:#64748b; margin:3px;
  }
  .result-box {
    background:#0f1f0f; border:1px solid #166534; border-radius:8px; padding:16px;
  }
  div[data-testid='stTextArea'] textarea { background:#0d1220; color:#e2e8f0; border:1px solid #1e293b; }
  div[data-testid='stTextInput'] input   { background:#0d1220; color:#e2e8f0; border:1px solid #1e293b; }
  .stButton>button {
    background: linear-gradient(135deg,#1d4ed8,#1e40af);
    color:#fff; border:none; border-radius:8px; padding:10px 28px;
    font-family:'DM Mono',monospace; font-weight:600; font-size:13px;
    width:100%; cursor:pointer; transition:opacity 0.2s;
  }
  .stButton>button:hover { opacity:0.85; }
  .stButton>button:disabled { opacity:0.4; cursor:not-allowed; }
</style>
""", unsafe_allow_html=True)


for k, v in {
    "running": False,
    "log_lines": [],
    "result": None,
    "step_idx": -1,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

STEPS = [
    ("-", "Parse Intent"),
    ("-", "Connect Bright Data"),
    ("-", "Search Products"),
    ("-", "Add to Cart"),
    ("-", "Checkout"),
    ("-", "Complete"),
]

with st.sidebar:
    st.markdown("### Configuration")

    brd_customer = st.text_input("BRD Customer ID", value=os.environ.get("BRD_CUSTOMER", ""), type="password")
    brd_zone     = st.text_input("BRD Zone",        value=os.environ.get("BRD_ZONE", "scraping_browser1"))
    brd_password = st.text_input("BRD Password",    value=os.environ.get("BRD_PASSWORD", ""), type="password")
    headless     = st.checkbox("Headless browser", value=True)

    st.markdown("---")
    st.markdown("### Checkout Details")
    email    = st.text_input("Email",   value="demo.agent@example.com")
    fname    = st.text_input("First",   value="Alex")
    lname    = st.text_input("Last",    value="Demo")
    address  = st.text_input("Address", value="742 Evergreen Terrace")
    city     = st.text_input("City",    value="Springfield")
    state    = st.text_input("State",   value="IL")
    postcode = st.text_input("Zip",     value="62701")
    phone    = st.text_input("Phone",   value="5555550100")

    st.markdown("---")
    st.markdown("### Card (Test)")
    card_number = st.text_input("Card Number", value="4111 1111 1111 1111")
    card_expiry = st.text_input("Expiry",      value="12/26")
    card_cvc    = st.text_input("CVC",         value="123")

    st.markdown("---")
    for b in ["Playwright", "Bright Data", "Claude API", "Stripe Test"]:
        st.markdown(f'<span class="badge">{b}</span>', unsafe_allow_html=True)


st.markdown("## AI Shopping Agent")
st.markdown("Autonomous agent: search - cart - checkout -- powered by Bright Data + Claude")

col_left, col_right = st.columns([1, 1], gap="large")

with col_left:
    st.markdown('<div class="agent-card">', unsafe_allow_html=True)
    prompt = st.text_area(
        "User Prompt",
        value="Find a mystery book under 15",
        height=90,
        placeholder='e.g. "Find black Nike shoes size 10 under $100"',
        label_visibility="collapsed",
    )

    if st.button("Run Agent", disabled=st.session_state.running):
        cfg = AgentConfig(
            brd_customer=brd_customer, brd_zone=brd_zone, brd_password=brd_password,
            headless=headless,
            checkout_email=email, checkout_fname=fname, checkout_lname=lname,
            checkout_address=address, checkout_city=city, checkout_state=state,
            checkout_postcode=postcode, checkout_phone=phone,
            card_number=card_number, card_expiry=card_expiry, card_cvc=card_cvc,
            card_name=f"{fname} {lname}",
        )
        st.session_state.running   = True
        st.session_state.log_lines = []
        st.session_state.result    = None
        st.session_state.step_idx  = 0
        st.session_state._cfg      = cfg
        st.session_state._prompt   = prompt
        log_q: queue.Queue = queue.Queue()
        st.session_state._log_q    = log_q

        def _run():
            result = run_agent_sync(prompt, cfg, log_q)
            log_q.put("__DONE__")
            st.session_state.result  = result
            st.session_state.running = False

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        st.session_state._thread = t
        st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="agent-card">', unsafe_allow_html=True)
    st.markdown("**Agent Pipeline**")

    step_keywords = ["Parsing", "Connecting", "Search", "Add to Cart", "checkout", "COMPLETE"]

    for i, (icon, label) in enumerate(STEPS):
        logs_text = "\n".join(st.session_state.log_lines).lower()
        done   = any(kw.lower() in logs_text for kw in step_keywords[:i+1]) and i < len(STEPS) - 1
        active = st.session_state.running and not done and i == st.session_state.step_idx

        dot_class = "done" if done else ("active" if active else "")
        marker    = "-" if done else (icon if active else str(i+1))
        opacity   = "1" if done or active else "0.35"

        st.markdown(f"""
        <div class="step-row" style="opacity:{opacity}">
          <div class="step-dot {dot_class}">{marker}</div>
          <span style="font-size:13px">{label}</span>
        </div>""", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


with col_right:
    if st.session_state.running:
        log_q = st.session_state.get("_log_q")
        if log_q:
            new_lines = []
            while True:
                try:
                    line = log_q.get_nowait()
                    if line == "__DONE__":
                        st.session_state.running = False
                        break
                    new_lines.append(line)
                except queue.Empty:
                    break
            if new_lines:
                st.session_state.log_lines.extend(new_lines)

    st.markdown('<div class="agent-card">', unsafe_allow_html=True)
    st.markdown("**Agent Log**")
    log_text = "\n".join(st.session_state.log_lines) if st.session_state.log_lines else "Waiting for agent run..."
    st.markdown(f'<div class="terminal">{log_text}</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.result:
        r = st.session_state.result
        if r.get("status") == "error":
            st.error(f"Agent failed: {r.get('error', 'unknown error')}")
        else:
            st.markdown('<div class="result-box">', unsafe_allow_html=True)
            st.markdown("### Agent Complete")
            prod = r.get("product", {})
            st.markdown(f"**Product:** {prod.get('title', '--')}")
            st.markdown(f"**Price:** {prod.get('price', 0):.2f}")
            st.markdown(f"**Status:** `{r.get('status', '--')}`")
            st.markdown(f"**Elapsed:** {r.get('elapsed', '--')}s")
            st.markdown(f"**Checkout URL:** `{r.get('url', '--')[:80]}`")
            st.markdown(f"**Place Order button:** `{r.get('place_order_button', '--')}`")
            st.markdown(f"**Stripe detected:** `{r.get('stripe_detected', False)}`")
            if os.path.exists("checkout_ready.png"):
                st.image("checkout_ready.png", caption="Final checkout state (before Place Order)", use_container_width=True)
            st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.running:
        time.sleep(0.8)
        st.rerun()


st.markdown("---")
st.markdown(
    '<p style="color:#334155;font-size:11px;text-align:center">'
    'AI Shopping Agent - Bright Data Hackathon - Playwright + Claude + Bright Data Scraping Browser'
    '</p>',
    unsafe_allow_html=True,
)
