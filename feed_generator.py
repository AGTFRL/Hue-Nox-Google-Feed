import html
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

SITEMAP = "https://huenox.com/sitemap.xml"
OUTPUT = "google-feed.xml"
TIMEOUT = 30
MAX_ADDITIONAL_IMAGES = 10
RETRIES = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HueNoxProductFeed/1.2; +https://huenox.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

session = requests.Session()
session.headers.update(HEADERS)


def clean_url(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def fetch(url):
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(attempt * 2)
    raise last_error


def sitemap_products():
    soup = BeautifulSoup(fetch(SITEMAP), "xml")
    urls = [loc.get_text(strip=True) for loc in soup.find_all("loc")]
    return sorted(set(clean_url(u) for u in urls if "/catalogue/" in u))


def first_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    if isinstance(value, dict):
        return value.get("name") or value.get("value") or ""
    return str(value)


def product_jsonld(soup):
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue

            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)

            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(str(t).lower() == "product" for t in types):
                return item
    return None


def meta(soup, *, name=None, prop=None):
    tag = None
    if name:
        tag = soup.find("meta", attrs={"name": name})
    if not tag and prop:
        tag = soup.find("meta", attrs={"property": prop})
    return tag.get("content", "").strip() if tag else ""


def clean_title(raw_title, url):
    path_name = urlparse(url).path.split("/catalogue/", 1)[0].strip("/")
    if path_name:
        title = path_name.replace("-", " ")
        title = re.sub(r"^HUE NOX\s+", "", title, flags=re.I)
        title = re.sub(r"^Hue Nox\s+", "", title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip()
        return title[:150]

    title = re.sub(
        r"\s+Price in India\s*-\s*Buy.*$",
        "",
        raw_title or "",
        flags=re.I,
    ).strip()
    return title[:150]


def clean_description(text):
    text = BeautifulSoup(text or "", "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:5000]


def normalize_availability(value):
    value = str(value or "").lower()
    value = value.replace("http://schema.org/", "").replace("https://schema.org/", "")
    mapping = {
        "instock": "in_stock",
        "in stock": "in_stock",
        "outofstock": "out_of_stock",
        "out of stock": "out_of_stock",
        "preorder": "preorder",
        "backorder": "backorder",
    }
    return mapping.get(value, "in_stock")


def large_gallery_images(soup):
    images = []
    seen = set()

    for img in soup.find_all("img", src=True):
        src = img.get("src", "").strip()
        if not src.startswith("http"):
            continue
        if "NushopCatalogue" not in src:
            continue
        if "w-80" in src or "brand_logo" in src:
            continue
        if not (("w-600" in src) or ("cat_img" in src and "w-80" not in src)):
            continue

        src = src.replace(" ", "%20")
        if src not in seen:
            seen.add(src)
            images.append(src)

    return images[: MAX_ADDITIONAL_IMAGES + 1]


def fallback_product(soup, url):
    """Build a usable feed item when ShopDeck omits Product JSON-LD."""
    title = clean_title(meta(soup, prop="og:title") or (soup.title.get_text(" ", strip=True) if soup.title else ""), url)
    description = clean_description(
        meta(soup, prop="og:description") or meta(soup, name="description")
    )

    gallery = large_gallery_images(soup)
    main_image = gallery[0] if gallery else meta(soup, prop="og:image")
    additional = gallery[1:]

    # Best-effort price/availability extraction from visible page text.
    page_text = soup.get_text(" ", strip=True)
    price_match = re.search(r"(?:Rs\.?|₹)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", page_text)
    price = price_match.group(1).replace(",", "") if price_match else ""

    available = "in_stock"
    if re.search(r"\bout\s+of\s+stock\b", page_text, re.I):
        available = "out_of_stock"

    # Stable ID from the final catalogue path segment.
    sku = urlparse(url).path.rstrip("/").split("/")[-1]

    return {
        "id": sku,
        "title": title or sku,
        "description": description,
        "link": url,
        "image_link": main_image,
        "additional_image_links": additional,
        "price": price,
        "currency": "INR",
        "availability": available,
        "condition": "new",
        "brand": "HUE-NOX",
        "color": "",
        "parser": "fallback",
    }


def parse_product(url):
    soup = BeautifulSoup(fetch(url), "lxml")
    data = product_jsonld(soup)

    if not data:
        product = fallback_product(soup, url)
        if not product["image_link"]:
            raise ValueError("No Product JSON-LD and no usable product image found")
        return product

    offers = data.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    images = data.get("image")
    jsonld_image = ""
    if isinstance(images, list):
        jsonld_image = str(images[0]) if images else ""
    else:
        jsonld_image = str(images or "")

    gallery = large_gallery_images(soup)
    if gallery:
        main_image = gallery[0]
        additional = gallery[1:]
    else:
        main_image = jsonld_image
        additional = []

    sku = first_text(data.get("sku")) or first_text(data.get("productID"))

    return {
        "id": sku or urlparse(url).path.rstrip("/").split("/")[-1],
        "title": clean_title(first_text(data.get("name")), url),
        "description": clean_description(first_text(data.get("description"))),
        "link": url,
        "image_link": main_image,
        "additional_image_links": additional,
        "price": str(offers.get("price") or ""),
        "currency": str(offers.get("priceCurrency") or "INR").upper(),
        "availability": normalize_availability(offers.get("availability")),
        "condition": "new",
        "brand": first_text(data.get("brand")) or "HUE-NOX",
        "color": first_text(data.get("color")),
        "parser": "jsonld",
    }


def esc(value):
    return html.escape(str(value or ""), quote=True)


def build_feed(products):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
        "<channel>",
        "<title>HUE-NOX Product Feed</title>",
        "<link>https://huenox.com/</link>",
        "<description>HUE-NOX product catalogue</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]

    for p in products:
        lines.extend([
            "<item>",
            f"<g:id>{esc(p['id'])}</g:id>",
            f"<g:title>{esc(p['title'])}</g:title>",
            f"<g:description>{esc(p['description'])}</g:description>",
            f"<link>{esc(p['link'])}</link>",
            f"<g:image_link>{esc(p['image_link'])}</g:image_link>",
        ])

        if p["price"]:
            lines.append(f"<g:price>{esc(p['price'])} {esc(p['currency'])}</g:price>")

        lines.extend([
            f"<g:availability>{esc(p['availability'])}</g:availability>",
            f"<g:condition>{esc(p['condition'])}</g:condition>",
            f"<g:brand>{esc(p['brand'])}</g:brand>",
        ])

        for image in p["additional_image_links"][:MAX_ADDITIONAL_IMAGES]:
            lines.append(f"<g:additional_image_link>{esc(image)}</g:additional_image_link>")

        if p["color"]:
            lines.append(f"<g:color>{esc(p['color'])}</g:color>")

        lines.append("</item>")

    lines.extend(["</channel>", "</rss>"])
    return "\n".join(lines) + "\n"


def main():
    urls = sitemap_products()
    products = []
    failures = []
    jsonld_count = 0
    fallback_count = 0

    for url in urls:
        try:
            product = parse_product(url)
            products.append(product)
            if product.get("parser") == "jsonld":
                jsonld_count += 1
            else:
                fallback_count += 1
        except Exception as exc:
            failures.append((url, str(exc)))

    unique = {}
    for product in products:
        unique.setdefault(product["id"], product)

    products = sorted(unique.values(), key=lambda p: p["title"].lower())

    print(f"Products in sitemap: {len(urls)}")
    print(f"Products in feed: {len(products)}")
    print(f"JSON-LD parsed: {jsonld_count}")
    print(f"Fallback parsed: {fallback_count}")
    print(f"Failures: {len(failures)}")
    print(f"Total product images in feed: {sum(1 + len(p['additional_image_links']) for p in products)}")

    if failures:
        for url, error in failures[:10]:
            print(f"FAILED: {url} :: {error}")

    if not products:
        raise RuntimeError("No products were generated")

    with open(OUTPUT, "w", encoding="utf-8") as file:
        file.write(build_feed(products))

    print(f"Generated: {OUTPUT}")

    # A hard failure is reserved for cases where the feed is empty. Individual
    # product failures are reported, but one malformed ShopDeck page will not
    # take down the entire catalogue.


if __name__ == "__main__":
    main()
