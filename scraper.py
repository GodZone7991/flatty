#!/usr/bin/env python3
"""
Property Alert Scraper
Scrapes pisos.com, Fotocasa, and Idealista for residential properties.
- pisos.com: requests + BeautifulSoup (server-rendered)
- Fotocasa: Playwright headless browser (JS-rendered)
- Idealista: Official API only (DataDome blocks headless browsers)
Hard filters: price, size, rooms. Dedup by URL hash + cross-source by address+price+size.
"""

import json
import hashlib
import os
import re
import time
import base64
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from profile import SEARCH_CONFIGS, PRICE_MIN, PRICE_MAX, SIZE_MIN_M2, ROOMS_MIN

# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

SEEN_FILE = Path("seen_properties.json")
HISTORY_FILE = Path("properties_history.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE, "r") as f:
                data = json.load(f)
                return set(data.get("property_ids", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def save_seen(ids: set):
    with open(SEEN_FILE, "w") as f:
        json.dump({
            "property_ids": list(ids),
            "last_updated": datetime.now().isoformat(),
        }, f, indent=2)


def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return []
    return []


def save_history(new_properties: list):
    history = load_history()
    existing_ids = {p.get("id") for p in history}
    timestamp = datetime.now().isoformat()
    to_add = []
    for prop in new_properties:
        if prop["id"] not in existing_ids:
            prop["scraped_at"] = timestamp
            to_add.append(prop)
    history = to_add + history
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Dedup & parsing helpers
# ---------------------------------------------------------------------------

def generate_property_id(url: str, source: str) -> str:
    return hashlib.md5(f"{source}:{url}".encode()).hexdigest()


def cross_source_key(prop: dict) -> str:
    addr = re.sub(r"\s+", " ", prop.get("address", "").lower().strip())
    price = prop.get("price", 0)
    size = prop.get("size_m2", 0)
    return f"{addr}|{int(price / 1000)}|{int(size)}"


def passes_hard_filters(prop: dict) -> bool:
    price = prop.get("price", 0)
    size = prop.get("size_m2", 0)
    rooms = prop.get("rooms", 0)
    if price < PRICE_MIN or price > PRICE_MAX:
        return False
    if size > 0 and size < SIZE_MIN_M2:
        return False
    if rooms > 0 and rooms < ROOMS_MIN:
        return False
    return True


def parse_price(text: str) -> int:
    if not text:
        return 0
    cleaned = re.sub(r"[^\d]", "", text)
    if cleaned:
        try:
            val = int(cleaned)
            if 10_000 <= val <= 10_000_000:
                return val
        except ValueError:
            pass
    return 0


def parse_int(text: str) -> int:
    if not text:
        return 0
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


def parse_size(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d+[.,]?\d*)\s*m", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return 0.0


# ---------------------------------------------------------------------------
# Playwright browser helpers
# ---------------------------------------------------------------------------

_browser_instance = None


def _get_browser():
    """Lazy-init a shared Playwright browser instance."""
    global _browser_instance
    if _browser_instance is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        _browser_instance = pw.chromium.launch(headless=True)
    return _browser_instance


def _new_page():
    """Create a new page with stealth-ish settings."""
    browser = _get_browser()
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="es-ES",
        viewport={"width": 1920, "height": 1080},
        java_script_enabled=True,
    )
    page = context.new_page()
    return page, context


def close_browser():
    """Clean up browser on exit."""
    global _browser_instance
    if _browser_instance:
        _browser_instance.close()
        _browser_instance = None


# ---------------------------------------------------------------------------
# pisos.com scraper (requests — works fine without JS)
# ---------------------------------------------------------------------------

def scrape_pisos_com(config: dict) -> list:
    properties = []
    pisos_cfg = config["pisos_com"]
    city = config["city"]

    base = pisos_cfg["base_url"]
    params = pisos_cfg["params"]
    url = f"{base}?{urlencode(params)}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        cards = soup.select(".ad-preview")
        if not cards:
            cards = soup.select("[class*='listing']") or soup.select("article")

        for card in cards:
            try:
                prop = _parse_pisos_card(card, city)
                if prop and passes_hard_filters(prop):
                    properties.append(prop)
            except Exception as e:
                print(f"  Error parsing pisos.com card: {e}")
    except requests.RequestException as e:
        print(f"  Error fetching pisos.com ({city}): {e}")

    return properties


def _parse_pisos_card(card, city: str) -> dict | None:
    link = card.select_one("a[href*='/venta/']") or card.select_one("a")
    if not link:
        return None

    href = link.get("href", "")
    if not href.startswith("http"):
        href = f"https://www.pisos.com{href}"

    title = link.get_text(strip=True) or ""

    price_el = card.select_one("[class*='price']") or card.select_one(".ad-preview__price")
    price = parse_price(price_el.get_text() if price_el else "")

    size_el = card.select_one("[class*='surface']") or card.select_one("[class*='area']")
    size = parse_size(size_el.get_text() if size_el else "")
    if size == 0:
        size = parse_size(card.get_text())

    rooms_el = card.select_one("[class*='room']") or card.select_one("[class*='hab']")
    rooms = parse_int(rooms_el.get_text() if rooms_el else "")
    if rooms == 0:
        rooms_match = re.search(r"(\d+)\s*hab", card.get_text(), re.IGNORECASE)
        rooms = int(rooms_match.group(1)) if rooms_match else 0

    addr_el = card.select_one("[class*='location']") or card.select_one("[class*='address']")
    address = addr_el.get_text(strip=True) if addr_el else ""

    img_urls = []
    for img in card.select("img[src*='http']")[:4]:
        src = img.get("src") or img.get("data-src") or ""
        if src and "placeholder" not in src:
            img_urls.append(src)

    prop = {
        "title": title,
        "url": href,
        "price": price,
        "size_m2": size,
        "rooms": rooms,
        "address": address,
        "city": city,
        "source": "pisos.com",
        "image_urls": img_urls,
        "raw_description": title,
    }
    prop["id"] = generate_property_id(href, "pisos.com")
    return prop


# ---------------------------------------------------------------------------
# Idealista — DataDome blocks headless browsers, API-only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Idealista API scraper (fallback when API keys available)
# ---------------------------------------------------------------------------

def _get_idealista_token() -> str | None:
    api_key = os.environ.get("IDEALISTA_API_KEY", "")
    api_secret = os.environ.get("IDEALISTA_API_SECRET", "")
    if not api_key or not api_secret:
        return None

    credentials = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    try:
        response = requests.post(
            "https://api.idealista.com/oauth/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("access_token")
    except requests.RequestException as e:
        print(f"  Error getting Idealista token: {e}")
        return None


def scrape_idealista_api(config: dict) -> list:
    """Scrape Idealista via official API (if credentials available)."""
    properties = []
    city = config["city"]
    id_cfg = config["idealista"]

    token = _get_idealista_token()
    if not token:
        return []

    params = {
        "center": id_cfg["center"],
        "distance": id_cfg["distance"],
        "country": id_cfg["country"],
        "operation": id_cfg["operation"],
        "propertyType": id_cfg["propertyType"],
        "locale": id_cfg["locale"],
        "minPrice": PRICE_MIN,
        "maxPrice": PRICE_MAX,
        "minSize": SIZE_MIN_M2,
        "minRooms": ROOMS_MIN,
        "maxItems": 50,
        "order": "publicationDate",
        "sort": "desc",
    }

    try:
        response = requests.post(
            "https://api.idealista.com/3.5/es/search",
            headers={"Authorization": f"Bearer {token}"},
            data=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        for item in data.get("elementList", []):
            prop = _parse_idealista_api_item(item, city)
            if prop and passes_hard_filters(prop):
                properties.append(prop)
    except requests.RequestException as e:
        print(f"  Error fetching Idealista API ({city}): {e}")

    return properties


def _parse_idealista_api_item(item: dict, city: str) -> dict | None:
    url = item.get("url", "")
    if not url:
        return None
    if not url.startswith("http"):
        url = f"https://www.idealista.com{url}"

    price = item.get("price", 0)
    size = item.get("size", 0)
    rooms = item.get("rooms", 0)

    district = item.get("district", "")
    neighborhood = item.get("neighborhood", "")
    address = item.get("address", "")
    location_parts = [p for p in [address, neighborhood, district] if p]
    address_str = ", ".join(location_parts)

    title = f"{rooms} hab, {int(size)} m², {int(price):,} € - {address_str}".replace(",", ".")

    img_urls = []
    for photo in item.get("multimedia", {}).get("images", [])[:4]:
        img_url = photo.get("url", "")
        if img_url:
            if not img_url.startswith("http"):
                img_url = f"https:{img_url}"
            img_urls.append(img_url)

    prop = {
        "title": title,
        "url": url,
        "price": int(price),
        "size_m2": float(size),
        "rooms": int(rooms),
        "address": address_str,
        "city": city,
        "source": "idealista",
        "image_urls": img_urls,
        "raw_description": item.get("description", "")[:500],
        "floor": item.get("floor", ""),
        "has_elevator": item.get("hasLift"),
        "energy_certificate": item.get("energyCertification", {}).get("rating", ""),
        "price_per_m2": round(price / size, 1) if size > 0 else 0,
        "bathrooms": item.get("bathrooms", 0),
    }
    prop["id"] = generate_property_id(url, "idealista")
    return prop


def scrape_idealista(config: dict) -> list:
    """Scrape Idealista via official API. DataDome blocks headless browsers."""
    city = config["city"]
    results = scrape_idealista_api(config)
    if not results:
        print(f"  Idealista: skipping {city} (no API credentials — apply at developers.idealista.com)")
    return results


# ---------------------------------------------------------------------------
# Fotocasa scraper (Playwright browser)
# ---------------------------------------------------------------------------

def _build_fotocasa_url(config: dict) -> str:
    """Build Fotocasa search URL with filters."""
    fc_cfg = config["fotocasa"]
    base = fc_cfg["base_url"]
    params = fc_cfg["params"]
    return f"{base}?{urlencode(params)}"


def scrape_fotocasa_browser(config: dict) -> list:
    """Scrape Fotocasa search results using Playwright."""
    properties = []
    city = config["city"]
    url = _build_fotocasa_url(config)

    try:
        page, context = _new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Accept cookies if prompted
            try:
                cookie_btn = page.query_selector("#didomi-notice-agree-button")
                if not cookie_btn:
                    cookie_btn = page.query_selector("button[aria-label*='Aceptar']")
                if cookie_btn:
                    cookie_btn.click()
                    time.sleep(1)
            except Exception:
                pass

            # Wait for listing cards
            try:
                page.wait_for_selector("[class*='re-Card']", timeout=15000)
            except Exception:
                # Try scrolling to trigger lazy load
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                time.sleep(2)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)

            # Scroll down to load more cards
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 800)")
                time.sleep(1)

            cards = page.query_selector_all("article[class*='re-Card'], [class*='re-CardPackPremium'], [class*='re-CardPackBasic'], article[data-type='ad']")
            if not cards:
                # Broader fallback
                cards = page.query_selector_all("article")

            print(f"  Fotocasa browser: found {len(cards)} cards")

            for card in cards:
                try:
                    prop = _parse_fotocasa_card(card, city)
                    if prop and passes_hard_filters(prop):
                        properties.append(prop)
                except Exception as e:
                    print(f"  Error parsing Fotocasa card: {e}")

        finally:
            context.close()

    except Exception as e:
        print(f"  Fotocasa browser error ({city}): {e}")

    return properties


def _parse_fotocasa_card(card, city: str) -> dict | None:
    """Parse a Fotocasa card by extracting inner_text() and parsing the structured lines.

    Typical card text:
        Líder de zona • La Casa Agency ...
        1/31
        299.000 €
        Ha bajado 6.000 €
        Hace 64 días
        Piso con calefacción en Carrer de Salvà, El Poble Sec - Parc de Montjuïc
        4 habs·3 baños·106 m²·Calefacción
        Más detalles
        Llamar
        Contactar
    """
    # Get link first
    link_el = card.query_selector("a[href*='/comprar/'], a[href*='/vivienda/']")
    if not link_el:
        link_el = card.query_selector("a")
    if not link_el:
        return None

    href = link_el.get_attribute("href") or ""
    if not href.startswith("http"):
        href = f"https://www.fotocasa.es{href}"

    # Parse the full text content
    card_text = card.inner_text()
    lines = [l.strip() for l in card_text.split("\n") if l.strip()]

    # Extract price: look for line matching "XXX.XXX €"
    price = 0
    for line in lines:
        if "€" in line and not line.startswith("Ha bajado"):
            price = parse_price(line)
            if price > 0:
                break

    # Extract title: line that starts with "Piso", "Ático", "Dúplex", "Casa", "Apartamento", "Estudio"
    title = ""
    title_prefixes = ("piso", "ático", "àtic", "dúplex", "duplex", "casa", "apartamento", "estudio", "planta", "vivienda")
    for line in lines:
        if line.lower().startswith(title_prefixes):
            title = line
            break

    # Extract address from title: "Piso con ... en STREET, NEIGHBORHOOD"
    address = ""
    if title:
        en_match = re.search(r"\ben\s+(.+)", title, re.IGNORECASE)
        if en_match:
            address = en_match.group(1).strip()

    # Extract features: line with "·" separator like "4 habs·3 baños·106 m²·Calefacción"
    # Must contain a property keyword to avoid matching agency names with "·"
    rooms = 0
    size = 0.0
    floor = ""
    for line in lines:
        ll = line.lower()
        is_feature_line = "·" in line and ("hab" in ll or "m²" in ll or "m2" in ll or "baño" in ll)
        if is_feature_line:
            parts = re.split(r"[·]", line)
            for part in parts:
                pt = part.strip().lower()
                if "hab" in pt:
                    rooms = parse_int(pt)
                elif "m²" in pt or "m2" in pt:
                    size = parse_size(pt)
                elif "planta" in pt:
                    floor = part.strip()
            break

    # Images
    img_urls = []
    for img_el in card.query_selector_all("img")[:4]:
        src = img_el.get_attribute("src") or img_el.get_attribute("data-src") or ""
        if src and src.startswith("http") and "placeholder" not in src and "data:" not in src:
            img_urls.append(src)

    if not title:
        title = f"{rooms} hab, {size} m² - {address}" if address else ""

    if not title and price == 0:
        return None

    prop = {
        "title": title[:200],
        "url": href,
        "price": price,
        "size_m2": size,
        "rooms": rooms,
        "address": address,
        "city": city,
        "source": "fotocasa",
        "image_urls": img_urls,
        "raw_description": title[:200],
        "floor": floor,
    }
    prop["id"] = generate_property_id(href, "fotocasa")
    return prop


def scrape_fotocasa(config: dict) -> list:
    """Scrape Fotocasa using Playwright browser."""
    return scrape_fotocasa_browser(config)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Starting property search at {datetime.now().isoformat()}")

    seen = load_seen()
    print(f"Loaded {len(seen)} previously seen properties")

    all_new = []
    cross_keys = set()
    filtered_count = 0

    try:
        for config in SEARCH_CONFIGS:
            city = config["label"]

            # --- pisos.com ---
            print(f"\n=== pisos.com - {city} ===")
            pisos = scrape_pisos_com(config)
            print(f"  Found {len(pisos)} listings")
            new_pisos = [p for p in pisos if p["id"] not in seen]
            print(f"  {len(new_pisos)} are new")

            for p in new_pisos:
                ck = cross_source_key(p)
                if ck not in cross_keys:
                    cross_keys.add(ck)
                    all_new.append(p)
                    seen.add(p["id"])
                else:
                    filtered_count += 1

            time.sleep(2)

            # --- Fotocasa ---
            print(f"\n=== Fotocasa - {city} ===")
            fotocasa = scrape_fotocasa(config)
            print(f"  Found {len(fotocasa)} listings")
            new_fc = [p for p in fotocasa if p["id"] not in seen]
            print(f"  {len(new_fc)} are new")

            for p in new_fc:
                ck = cross_source_key(p)
                if ck not in cross_keys:
                    cross_keys.add(ck)
                    all_new.append(p)
                    seen.add(p["id"])
                else:
                    filtered_count += 1

            time.sleep(2)

            # --- Idealista ---
            print(f"\n=== Idealista - {city} ===")
            idealista = scrape_idealista(config)
            print(f"  Found {len(idealista)} listings")
            new_id = [p for p in idealista if p["id"] not in seen]
            print(f"  {len(new_id)} are new")

            for p in new_id:
                ck = cross_source_key(p)
                if ck not in cross_keys:
                    cross_keys.add(ck)
                    all_new.append(p)
                    seen.add(p["id"])
                else:
                    filtered_count += 1

            time.sleep(2)

    finally:
        close_browser()

    # --- Save state ---
    save_seen(seen)

    if all_new:
        save_history(all_new)

    print(f"\nSaved {len(all_new)} new properties to {HISTORY_FILE}")
    if filtered_count:
        print(f"Filtered {filtered_count} cross-source duplicates")
    print(f"Run analyzer.py next to evaluate with AI agents.")
    print(f"\nCompleted at {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
