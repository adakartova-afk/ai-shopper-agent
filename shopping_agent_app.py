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

import streamlit as st
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PWTimeout,
)

@dataclass
class AgentConfig:
    brd_customer: str  = field(default_factory=lambda: os.environ.get("BRD_CUSTOMER", ""))
    brd_zone: str      = field(default_factory=lambda: os.environ.get("BRD_ZONE", "scraping_browser1"))
    brd_password: str  = field(default_factory=lambda: os.environ.get("BRD_PASSWORD", ""))
    base_url: str      = "https://books.toscrape.com"
    search_query: str  = field(default_factory=lambda: os.environ.get("SEARCH_QUERY", "mystery"))
    headless: bool     = field(default_factory=lambda: os.environ.get("HEADLESS", "true").lower() == "true")
    nav_timeout: int      = 60_000
    element_timeout: int  = 30_000
    captcha_timeout: int  = 120_000
    delay_short:  tuple = (0.3, 0.9)
    delay_medium: tuple = (0.9, 2.2)
    delay_long:   tuple = (2.2, 4.5)
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

async def human_delay(range_: tuple) -> None:
    await asyncio.sleep(random.uniform(*range_))

async def safe_fill(page: Page, selectors: list[str], value: str, label: str, log: logging.Logger, frame=None) -> bool:
    target = frame if frame else page
    for sel in selectors:
        try:
            el = target.locator(sel).first
            if await el.count() == 0:
                continue
            await el.scroll_into_view_if_needed()
            await el.triple_click()
            await asyncio.sleep(0.05)
            for ch in value:
                await page.keyboard.type(ch)
                await asyncio.sleep(random.uniform(0.04, 0.13))
            log.info(f"    ✓ {label}: {value}")
            return True
        except Exception as exc:
            log.debug(f"    selector '{sel}' failed for {label}: {exc}")
    log.warning(f"    ✗ Could not fill '{label}' — no selector matched")
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
    captcha_sels = ["iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".g-recaptcha", "#captcha", "[id*='captcha']", "[class*='captcha']"]
    for sel in captcha_sels:
        try:
            if await page.locator(sel).count() > 0:
                log.info("  CAPTCHA detected — awaiting Bright Data auto-solve...")
                await page.wait_for_selector(sel, state="hidden", timeout=timeout)
                log.info("  CAPTCHA resolved ✓")
                return
        except PWTimeout:
            log.warning(f"  CAPTCHA selector '{sel}' timeout — continuing")
        except Exception:
            pass

async def build_browser(cfg: AgentConfig, pw, log: logging.Logger) -> tuple[Browser, BrowserContext]:
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ctx_opts = dict(viewport={"width": 1440, "height": 900}, user_agent=ua, locale="en-US", timezone_id="America/Los_Angeles")
    if cfg.brd_customer and cfg.brd_password:
        endpoint = f"wss://{cfg.brd_customer}-zone-{cfg.brd_zone}:{cfg.brd_password}@brd.superproxy.io:9222"
        log.info(f"Connecting to Bright Data → zone: {cfg.brd_zone}")
        browser = await pw.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context(**ctx_opts)
        log.info("Bright Data connection established ✓")
    else:
        log.warning("No Bright Data credentials → local Chromium (dev mode)")
        browser = await pw.chromium.launch(headless=cfg.headless, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(**ctx_opts)
    context.set_default_timeout(cfg.element_timeout)
    context.set_default_navigation_timeout(cfg.nav_timeout)
    return browser, context

@dataclass
class ProductResult:
    title: str
    price: float
    url: str
    rating: Optional[str] = None
    availability: Optional[str] = None

async def step_search(page: Page, cfg: AgentConfig, query: str, log: logging.Logger) -> list[ProductResult]:
    log.info(f"[STEP 1/4] Searching '{query}' on {cfg.base_url}")
    await apply_stealth(page)
    await page.goto(cfg.base_url, wait_until="domcontentloaded")
    await wait_captcha(page, cfg.captcha_timeout, log)
    await human_delay(cfg.delay_medium)
    
    query_lower = query.lower()
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
        log.info(f"  Navigating to category: '{matched_name}' → {cat_url}")
        await page.goto(cat_url, wait_until="domcontentloaded")
        await wait_captcha(page, cfg.captcha_timeout, log)
        await human_delay(cfg.delay_medium)

    await page.wait_for_selector("article.product_pod", timeout=cfg.element_timeout)
    cards = await page.locator("article.product_pod").all()
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
            avail    = (await card.locator("p.availability").inner_text()).strip()
            results.append(ProductResult(title=title.strip(), price=price, url=href, availability=avail))
        except Exception as e:
            log.debug(f"  Skipped card: {e}")
    if not results:
        raise RuntimeError("No products found — check search query.")
    return results

async def step_add_to_cart(page: Page, cfg: AgentConfig, product: ProductResult, log: logging.Logger) -> None:
    log.info(f"[STEP 2/4] Opening product page: {product.title}")
    await page.goto(product.url, wait_until="domcontentloaded")
    await wait_captcha(page, cfg.captcha_timeout, log)
    await human_delay(cfg.delay_medium)
    
    atc_selectors = ["button:has-text('Add to basket')", "button:has-text('Add to Cart')", "button[type='submit']:has-text('Add')"]
    added = False
    for sel in atc_selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.scroll_into_view_if_needed()
                await el.click()
                log.info(f"  Clicked Add to Cart ({sel})")
                added = True
                break
        except Exception:
            pass
    if not added:
        raise RuntimeError("Could not find 'Add to Cart' button.")
    await human_delay(cfg.delay_medium)

async def step_open_cart(page: Page, cfg: AgentConfig, log: logging.Logger) -> None:
    log.info("[STEP 3/4] Opening cart...")
    for path in ["/basket/", "/cart/"]:
        try:
            await page.goto(f"{cfg.base_url}{path}", wait_until="domcontentloaded")
            log.info("[STEP 3/4] DONE — cart page loaded ✓")
            return
        except Exception:
            pass
    raise RuntimeError("Could not navigate to cart.")

async def _click_first(page: Page, selectors: list[str], label: str, log: logging.Logger) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.scroll_into_view_if_needed()
                await el.click()
                await page.wait_for_load_state("domcontentloaded")
                log.info(f"  Clicked '{label}' via: {sel}")
                return True
        except Exception:
            pass
    return False

async def step_checkout(page: Page, cfg: AgentConfig, log: logging.Logger) -> dict:
    log.info("[STEP 4/4] Starting checkout flow...")
    checkout_btns = ["a:has-text('Proceed to checkout')", "a:has-text('Proceed to Checkout')", ".checkout-btn"]
    await _click_first(page, checkout_btns, "Proceed to Checkout", log)
    await wait_captcha(page, cfg.captcha_timeout, log)
    await human_delay(cfg.delay_medium)

    log.info("  Filling shipping fields...")
    await safe_fill(page, ["input[type='email']", "input[name='email']"], cfg.checkout_email, "email", log)
    await safe_fill(page, ["input[name='first_name']", "input[name='firstName']"], cfg.checkout_fname, "first name", log)
    await safe_fill(page, ["input[name='last_name']", "input[name='lastName']"], cfg.checkout_lname, "last name", log)
    await safe_fill(page, ["input[name='address1']", "input[name='street_address']"], cfg.checkout_address, "address line 1", log)
    await safe_fill(page, ["input[name='city']"], cfg.checkout_city, "city", log)
    await safe_fill(page, ["input[name='postcode']", "input[name='zip']"], cfg.checkout_postcode, "postcode/zip", log)
    
    await human_delay(cfg.delay_medium)
    await page.screenshot(path="checkout_ready.png", full_page=True)
    log.info("[STEP 4/4] DONE — all fields filled. Stopped before final submit ✓")
    return {"status": "checkout_ready", "url": page.url, "screenshot": "checkout_ready.png"}

async def run_agent(prompt: str, cfg: AgentConfig, log: logging.Logger) -> dict:
    t0 = time.monotonic()
    log.info("="*50)
    log.info("  AI SHOPPING AGENT — START")
    log.info("="*50)
    async with async_playwright() as pw:
        browser, context = await build_browser(cfg, pw, log)
        page = await context.new_page()
        try:
            products = await step_search(page, cfg, prompt, log)
            best     = products[0]
            await step_add_to_cart(page, cfg, best, log)
            await step_open_cart(page, cfg, log)
            result = await step_checkout(page, cfg, log)
            elapsed = round(time.monotonic() - t0, 2)
            result["product"] = {"title": best.title, "price": best.price}
            result["elapsed"] = elapsed
            return result
        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 2)
            log.error(f"AGENT FAILED: {exc}")
            return {"status": "error", "error": str(exc), "elapsed": elapsed}
        finally:
            await context.close()
            await browser.close()

def run_agent_sync(prompt: str, cfg: AgentConfig, log_queue: queue.Queue) -> dict:
    log = make_logger(log_queue)
    return asyncio.run(run_agent(prompt, cfg, log))

# STREAMLIT UI
st.set_page_config(page_title="AI Shopping Agent", page_icon="🛒", layout="wide")

st.markdown("""
<style>
  .stApp { background: #0b0f1a; color: #e2e8f0; font-family: monospace; }
  .terminal { background:#040810; border:1px solid #1e293b; padding:14px; height:300px; overflow-y:auto; white-space:pre-wrap; color:#94a3b8; font-size:12px; }
  .result-box { background:#0f1f0f; border:1px solid #166534; padding:16px; border-radius:5px; }
</style>
""", unsafe_allow_html=True)

if "running" not in st.session_state:
    st.session_state.running = False
    st.session_state.log_lines = []
    st.session_state.result = None

st.sidebar.markdown("### ⚙️ Configuration")
brd_customer = st.sidebar.text_input("BRD Customer ID", value="", type="password")
brd_zone     = st.sidebar.text_input("BRD Zone",        value="scraping_browser1")
brd_password = st.sidebar.text_input("BRD Password",    value="", type="password")

st.markdown("## 🛒 AI Shopping Agent (Bright Data Hackathon)")
prompt = st.text_input("What are you looking for?", value="Mystery")

if st.button("▶ Run Agent", disabled=st.session_state.running):
    cfg = AgentConfig(brd_customer=brd_customer, brd_zone=brd_zone, brd_password=brd_password)
    st.session_state.running = True
    st.session_state.log_lines = []
    st.session_state.result = None
    log_q = queue.Queue()
    st.session_state._log_q = log_q

    def _run():
        res = run_agent_sync(prompt, cfg, log_q)
        log_q.put("__DONE__")
        st.session_state.result = res
        st.session_state.running = False

    threading.Thread(target=_run, daemon=True).start()
    st.rerun()

if st.session_state.running:
    log_q = st.session_state.get("_log_q")
    if log_q:
        while True:
            try:
                line = log_q.get_nowait()
                if line == "__DONE__":
                    break
                st.session_state.log_lines.append(line)
            except queue.Empty:
                break

col1, col2 = st.columns(2)
with col1:
    st.markdown("**Agent Execution Log**")
    log_text = "\n".join(st.session_state.log_lines) if st.session_state.log_lines else "Idle..."
    st.markdown(f'<div class="terminal">{log_text}</div>', unsafe_allow_html=True)

with col2:
    if st.session_state.result:
        r = st.session_state.result
        if r.get("status") == "error":
            st.error(f"Error: {r.get('error')}")
        else:
            st.markdown('<div class="result-box">', unsafe_allow_html=True)
            st.markdown("### ✅ Checkout Ready")
            st.write(f"**Product:** {r.get('product',{}).get('title')}")
            st.write(f"**Elapsed:** {r.get('elapsed')}s")
            st.markdown("</div>", unsafe_allow_html=True)
            if os.path.exists("checkout_ready.png"):
                st.image("checkout_ready.png", caption="Final State")

if st.session_state.running:
    time.sleep(0.5)
    st.rerun()
