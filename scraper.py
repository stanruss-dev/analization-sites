# -*- coding: utf-8 -*-
"""
Site Analyzer - полный сбор информации с сайта.
Архитектура:
  - Параллельный обход (пул из N воркеров)
  - Быстрый requests для статических страниц
  - Playwright только для JS-страниц (если requests вернул мало контента)
  - Перехват XHR/fetch (JSON API)
  - Автоскролл, пагинация, sitemap.xml, schema.org JSON-LD
"""

import asyncio
import json
import logging
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests as req_lib
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

try:
    from deep_translator import GoogleTranslator
    from langdetect import detect, LangDetectException
    _TRANSLATE_OK = True
except ImportError:
    _TRANSLATE_OK = False


def _translate_one(text: str) -> str:
    """Переводит одну строку EN→RU. Возвращает оригинал, если перевод не нужен/не удался."""
    if not _TRANSLATE_OK or not text or len(text.strip()) < 20:
        return text
    try:
        sample = text[:500].strip()
        if detect(sample) != "en":
            return text
        parts = [text[i:i+4500] for i in range(0, min(len(text), 27000), 4500)]
        return " ".join(GoogleTranslator(source="en", target="ru").translate(p) or p for p in parts)
    except Exception:
        return text


# Колонки для перевода: (таблица, ключ-колонка, текстовая-колонка)
_TRANSLATE_COLS = [
    ("pages",    "url",  "title"),
    ("pages",    "url",  "description"),
    ("pages",    "url",  "h1"),
    ("pages",    "url",  "raw_text"),
    ("products", "id",   "name"),
    ("products", "id",   "description"),
    ("services", "id",   "name"),
    ("services", "id",   "description"),
]


def translate_db(conn: sqlite3.Connection, delay: float = 0.3) -> None:
    """Пост-обработка: переводит EN→RU все собранные тексты пачкой после скрапа.

    Дедуплицирует одинаковые строки (переводим один раз), делает паузу между
    запросами к Google, чтобы не словить временный бан по IP.
    """
    if not _TRANSLATE_OK:
        log.warning("Перевод пропущен: deep_translator/langdetect не установлены")
        return

    # 1. Собираем уникальные тексты из всех целевых колонок
    uniq: set[str] = set()
    for table, _key, col in _TRANSLATE_COLS:
        for (val,) in conn.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"):
            uniq.add(val)
    # company_info — значения по ключам org_name/org_description
    for (val,) in conn.execute(
        "SELECT value FROM company_info WHERE key IN ('org_name','org_description') AND value != ''"):
        uniq.add(val)

    if not uniq:
        return

    # 2. Переводим каждую уникальную строку один раз
    cache: dict[str, str] = {}
    total = len(uniq)
    log.info("Перевод: %d уникальных строк", total)
    for i, src in enumerate(uniq, 1):
        dst = _translate_one(src)
        if dst != src:
            cache[src] = dst
            time.sleep(delay)  # пауза только после реального запроса к Google
        if i % 25 == 0:
            log.info("Перевод: %d/%d", i, total)

    if not cache:
        log.info("Перевод: англоязычного текста не найдено")
        return

    # 3. Пишем переводы обратно
    for table, key, col in _TRANSLATE_COLS:
        rows = conn.execute(
            f"SELECT {key}, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''").fetchall()
        for kval, src in rows:
            if src in cache:
                conn.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?", (cache[src], kval))
    for key in ("org_name", "org_description"):
        row = conn.execute("SELECT value FROM company_info WHERE key=?", (key,)).fetchone()
        if row and row[0] in cache:
            conn.execute("UPDATE company_info SET value=? WHERE key=?", (cache[row[0]], key))
    conn.commit()
    log.info("Перевод завершён: обновлено %d уникальных строк", len(cache))

sys.stdout.reconfigure(encoding="utf-8")

# ──────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(fmt)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt)
log = logging.getLogger("scraper")
log.setLevel(logging.INFO)
log.addHandler(fh)
log.addHandler(sh)

# ──────────────────────────────────────────────
# БД
# ──────────────────────────────────────────────
DB_PATH = Path("site_data.db")
_db_lock = asyncio.Lock()

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY, title TEXT, description TEXT,
            keywords TEXT, h1 TEXT, raw_text TEXT, scraped_at TEXT
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_url TEXT, type TEXT, value TEXT,
            UNIQUE(page_url, type, value)
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT, name TEXT NOT NULL, price TEXT, old_price TEXT,
            currency TEXT, sku TEXT, brand TEXT, category TEXT,
            description TEXT, image_url TEXT, product_url TEXT, in_stock TEXT,
            UNIQUE(name, source_url)
        );
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_url TEXT, name TEXT, description TEXT,
            UNIQUE(page_url, name)
        );
        CREATE TABLE IF NOT EXISTS company_info (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_url TEXT, to_url TEXT, text TEXT,
            UNIQUE(from_url, to_url)
        );
        CREATE TABLE IF NOT EXISTS api_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_url TEXT, api_url TEXT, body TEXT, scraped_at TEXT
        );
    """)
    conn.commit()
    return conn

# ──────────────────────────────────────────────
# Константы / паттерны
# ──────────────────────────────────────────────
RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
RE_PHONE = re.compile(r"(?:\+?[78][\s\-]?)?(?:\(?\d{3}\)?[\s\-]?)?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
RE_PRICE = re.compile(
    r"(?:[$€£¥₽₴₸]\s*\d[\d\s,\.]*|\d[\d\s,\.]*\s*(?:[$€£¥₽₴₸]|руб\.?|грн\.?|тг\.?|USD|EUR|RUB|UAH|KZT))",
    re.IGNORECASE
)
SKIP_EXT = re.compile(
    r"\.(pdf|docx?|xlsx?|zip|rar|7z|jpg|jpeg|png|gif|svg|webp|ico|mp4|mp3|wav|css|js|woff2?|ttf|eot)(\?.*)?$",
    re.IGNORECASE
)
SKIP_PATHS = re.compile(
    r"/(basket|cart|login|logout|register|personal|compare|favicon|captcha|tel:|mailto:)", re.I
)
SOCIAL_DOMAINS = {
    "facebook.com": "Facebook", "instagram.com": "Instagram",
    "twitter.com": "Twitter", "x.com": "X", "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube", "t.me": "Telegram",
    "vk.com": "VK", "tiktok.com": "TikTok", "ok.ru": "OK",
}
PAGINATION_SELECTORS = [
    "a[rel='next']", ".pagination a.next", "[class*='paginat'] a[href]",
    "a:has-text('Следующая')", "a:has-text('»')", "a:has-text('Далее')",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ──────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────

def clean(t): return re.sub(r"\s+", " ", t or "").strip()
def normalize(url):
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    # Срезаем trailing slash только если нет пути (корень сайта)
    # Для всех остальных путей - сохраняем как есть, чтобы не ломать Bitrix/etc.
    if parsed.path in ("/", ""):
        return url.rstrip("/")
    return url
def same_domain(u, base): return urlparse(u).netloc == urlparse(base).netloc

def should_skip(url: str, base: str) -> bool:
    if not same_domain(url, base): return True
    if SKIP_EXT.search(url):       return True
    if SKIP_PATHS.search(url):     return True
    return False

def is_phone(p): return 7 <= sum(c.isdigit() for c in p) <= 12

def price_and_currency(text: str):
    m = RE_PRICE.search(text or "")
    if not m: return "", ""
    raw = clean(m.group())
    for sym, name in [("₽","RUB"),("руб","RUB"),("$","USD"),("€","EUR"),
                      ("£","GBP"),("₴","UAH"),("₸","KZT"),("RUB","RUB"),("USD","USD"),("EUR","EUR")]:
        if sym.lower() in raw.lower(): return raw, name
    return raw, ""

# ──────────────────────────────────────────────
# Быстрая загрузка через requests
# ──────────────────────────────────────────────

def fetch_static(url: str) -> tuple[str | None, int]:
    """Возвращает (html, status). html=None при ошибке."""
    try:
        r = req_lib.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        return r.text, r.status_code
    except Exception as e:
        log.debug("requests error %s: %s", url, e)
        return None, 0

def needs_js(html: str, url: str = "") -> bool:
    """Playwright нужен только если страница явно SPA или совсем пустая."""
    if not html: return True
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text()).strip()
    if len(text) < 200: return True
    # SPA/React/Vue/Next - точно нужен JS
    if re.search(r'(ng-app|data-reactroot|__NEXT_DATA__|__vue|ReactDOM\.render)', html):
        return True
    # Пустые контейнеры под товары при наличии признаков каталога
    if re.search(r'(catalog|product-list|goods-list)', html, re.I):
        soup = BeautifulSoup(html, "lxml")
        for sel in [".product-list", ".goods-list", ".catalog__items", "#catalog-items"]:
            el = soup.select_one(sel)
            if el and len(el.get_text().strip()) < 30:
                return True
    return False

# ──────────────────────────────────────────────
# Парсинг HTML
# ──────────────────────────────────────────────

def _main_content(soup: BeautifulSoup) -> str:
    """Извлекает только основной контент страницы, без шапки/навигации/подвала."""
    # Удаляем явный мусор
    for tag in soup(["header", "footer", "nav", "aside",
                     "[class*='header']", "[class*='footer']", "[class*='menu']",
                     "[class*='sidebar']", "[class*='breadcrumb']", "[id*='header']",
                     "[id*='footer']", "[id*='nav']"]):
        tag.decompose()

    # Ищем основной контент по типичным контейнерам
    for sel in ["main", "article", "[role='main']", "#content", ".content",
                ".main-content", ".page-content", ".entry-content",
                ".article-content", ".post-content", ".text-content",
                "#main", ".inner-content", ".body-content"]:
        el = soup.select_one(sel)
        if el:
            txt = clean(el.get_text(" "))
            if len(txt) > 100:
                return txt[:30000]

    # Fallback: весь оставшийся текст
    return clean(soup.get_text(" "))[:30000]


def parse_html(url: str, html: str, conn: sqlite3.Connection, base: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script","style","noscript","svg"]): t.decompose()

    title  = clean(soup.title.string if soup.title else "")
    desc   = clean((soup.find("meta", attrs={"name": re.compile("^description$", re.I)}) or {}).get("content",""))
    kw     = clean((soup.find("meta", attrs={"name": re.compile("^keywords$", re.I)}) or {}).get("content",""))
    h1_el  = soup.find("h1")
    h1     = clean(h1_el.get_text()) if h1_el else ""
    # Сохраняем чистый контент без навигации
    raw    = _main_content(BeautifulSoup(html, "lxml"))
    now    = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT OR REPLACE INTO pages (url,title,description,keywords,h1,raw_text,scraped_at) VALUES (?,?,?,?,?,?,?)",
        (url, title, desc, kw, h1, raw, now))

    # Контакты
    full_html = str(soup)
    for e in {m.lower() for m in RE_EMAIL.findall(full_html)}:
        conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,"email",e))
    for p in {clean(p) for p in RE_PHONE.findall(raw) if is_phone(p)}:
        conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,"phone",p))
    for el in [soup.find("address"),
               soup.find(attrs={"itemprop":"address"}),
               soup.find(attrs={"class": re.compile(r"address|location",re.I)})]:
        if el:
            a = clean(el.get_text())
            if len(a) > 10:
                conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,"address",a))
    for a in soup.find_all("a", href=True):
        for dom, name in SOCIAL_DOMAINS.items():
            if dom in a["href"]:
                conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)",
                             (url, f"social:{name}", a["href"]))

    # JSON-LD schema.org
    raw_soup = BeautifulSoup(html, "lxml")
    for sc in raw_soup.find_all("script", type="application/ld+json"):
        try:
            _schema(json.loads(sc.string or ""), url, conn)
        except Exception: pass

    # HTML-карточки товаров
    _html_products(soup, url, conn)

    # Услуги
    for sec in soup.find_all(["section","div"], class_=re.compile(r"service|услуг|advantage",re.I)):
        h = sec.find(re.compile("^h[2-5]$"))
        if h:
            n = clean(h.get_text())
            if len(n) > 2:
                p = sec.find("p")
                conn.execute("INSERT OR IGNORE INTO services VALUES (NULL,?,?,?)",
                             (url, n, clean(p.get_text())[:400] if p else ""))

    # Ссылки
    links = []
    for a in soup.find_all("a", href=True):
        href = normalize(urljoin(url, a["href"]))
        if href.startswith("http"):
            conn.execute("INSERT OR IGNORE INTO links VALUES (NULL,?,?,?)",
                         (url, href, clean(a.get_text())[:150]))
            if not should_skip(href, base):
                links.append(href)

    conn.commit()
    return links


def _schema(data, url, conn):
    if isinstance(data, list):
        for i in data: _schema(i, url, conn)
        return
    if not isinstance(data, dict): return
    t = data.get("@type","")

    if t in ("Product","IndividualProduct"):
        name = clean(data.get("name",""))
        if not name: return
        price, cur, old = "", "", ""
        offers = data.get("offers")
        if isinstance(offers, dict):
            price = str(offers.get("price",""))
            cur   = offers.get("priceCurrency","")
        elif isinstance(offers, list) and offers:
            price = str(offers[0].get("price",""))
            cur   = offers[0].get("priceCurrency","")
        brand = ""
        b = data.get("brand")
        if isinstance(b, dict): brand = clean(b.get("name",""))
        elif isinstance(b, str): brand = clean(b)
        img = data.get("image","")
        if isinstance(img, list): img = img[0] if img else ""
        if isinstance(img, dict): img = img.get("url","")
        sku = str(data.get("sku", data.get("productID","")))
        desc = clean(data.get("description",""))[:400]
        conn.execute(
            "INSERT OR IGNORE INTO products VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url,name,price,old,cur,sku,brand,"",desc,img,"",""))

    elif t in ("Organization","LocalBusiness","Store","Company"):
        if data.get("name"):
            conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)",
                         ("org_name", clean(data["name"])))
        if data.get("description"):
            conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)",
                         ("org_description", clean(data["description"])[:800]))
        for p in ([data["telephone"]] if isinstance(data.get("telephone"),str) else data.get("telephone",[])):
            conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,"phone",clean(p)))
        if data.get("email"):
            conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,"email",clean(data["email"])))
        addr = data.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("streetAddress",""), addr.get("addressLocality",""),
                     addr.get("postalCode",""), addr.get("addressCountry","")]
            full = ", ".join(p for p in parts if p)
            if full: conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,"address",full))
        for lnk in ([data.get("sameAs",[])] if isinstance(data.get("sameAs"),str) else data.get("sameAs",[])):
            for dom, nm in SOCIAL_DOMAINS.items():
                if dom in lnk:
                    conn.execute("INSERT OR IGNORE INTO contacts VALUES (NULL,?,?,?)", (url,f"social:{nm}",lnk))

    for v in data.values():
        if isinstance(v, (dict,list)): _schema(v, url, conn)


def _html_products(soup, url, conn):
    # Bitrix-специфичные селекторы (aspro_max шаблон)
    selectors = [
        ".catalog-item", ".product-card", ".product-item",
        ".goods-item", ".item-card", "[class*='product-card']",
        "[class*='catalog-item']", "[itemtype*='schema.org/Product']",
    ]
    seen = set()
    found = 0

    # --- Bitrix aspro_max: .catalog-block-view__item ---
    for card in soup.select(".catalog-block-view__item, .catalog-block-view-item"):
        pid = card.get("data-id", "")
        # Название
        name_el = (card.select_one(".item-title") or
                   card.select_one("a.dark_link") or
                   card.find(class_=re.compile(r"\bitem.?title\b|\bname\b", re.I)))
        if not name_el: continue
        name = clean(name_el.get_text())
        if len(name) < 2 or name in seen: continue
        seen.add(name)
        # Цена (первый .price без .discount - актуальная)
        price, old, cur = "", "", "₽"
        price_blocks = card.select(".price")
        for pb in price_blocks:
            pv = pb.select_one(".price_value")
            if not pv: continue
            cur_el = pb.select_one(".price_currency")
            if cur_el: cur = clean(cur_el.get_text())
            if "discount" in " ".join(pb.get("class", [])):
                old = re.sub(r"\s+", "", pv.get_text()).strip()
            elif not price:
                price = re.sub(r"\s+", "", pv.get_text()).strip()
        # Остаток
        stock_el = card.select_one(".item-stock .value, .sa_block")
        stock = clean(stock_el.get_text()) if stock_el else ""
        # Изображение
        img_el = card.find("img")
        img = urljoin(url, img_el.get("data-src") or img_el.get("src", "")) if img_el else ""
        # Ссылка на товар
        a_el = card.select_one("a.dark_link") or card.find("a", href=True)
        purl = urljoin(url, a_el["href"]) if a_el else ""
        conn.execute(
            "INSERT OR IGNORE INTO products VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, name, price, old, cur, pid, "", "", "", img, purl, stock))
        found += 1

    # --- Универсальные селекторы для других сайтов ---
    if found == 0:
        for sel in selectors:
            try:
                for card in soup.select(sel):
                    name_el = (card.find(attrs={"itemprop": "name"}) or
                               card.find(re.compile("^h[2-5]$")) or
                               card.find(class_=re.compile(r"name|title", re.I)))
                    if not name_el: continue
                    name = clean(name_el.get_text())
                    if len(name) < 2 or name in seen: continue
                    seen.add(name)
                    price_el = (card.find(attrs={"itemprop": "price"}) or
                                card.find(class_=re.compile(r"price(?!.*old)|cost", re.I)))
                    price_raw = clean(price_el.get_text()) if price_el else ""
                    price, cur = price_and_currency(price_raw)
                    old_el = card.find(class_=re.compile(r"old.?price|price.?old|crossed", re.I))
                    old = clean(old_el.get_text()) if old_el else ""
                    img_el = card.find("img")
                    img = urljoin(url, img_el.get("data-src") or img_el.get("src", "")) if img_el else ""
                    a_el = card.find("a", href=True)
                    purl = urljoin(url, a_el["href"]) if a_el else ""
                    conn.execute(
                        "INSERT OR IGNORE INTO products VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (url, name, price, old, cur, "", "", "", "", img, purl, ""))
            except Exception: pass


# ──────────────────────────────────────────────
# Извлечение продуктов из JSON (API-ответы)
# ──────────────────────────────────────────────

def json_products(data, source_url, conn, _depth=0):
    if _depth > 8: return 0
    count = 0
    NAME_K  = ["name","title","product_name","label","наименование"]
    PRICE_K = ["price","cost","amount","цена","price_value"]
    SKU_K   = ["sku","article","artikul","code","id","product_id"]
    DESC_K  = ["description","desc","text","about"]
    IMG_K   = ["image","img","picture","photo","src","image_url","preview"]
    URL_K   = ["url","link","href","product_url","detail_url"]
    BRAND_K = ["brand","producer","manufacturer","vendor"]
    CAT_K   = ["category","section","group","type"]
    STOCK_K = ["in_stock","available","quantity","qty","stock"]

    if isinstance(data, list):
        for item in data: count += json_products(item, source_url, conn, _depth+1)
    elif isinstance(data, dict):
        name = next((clean(data[k]) for k in NAME_K if k in data and isinstance(data[k],str) and data[k].strip()), "")
        if name and len(name) > 1:
            price_raw = next((str(data[k]) for k in PRICE_K if k in data), "")
            price, cur = price_and_currency(price_raw) if price_raw else ("","")
            if not price and price_raw: price = price_raw
            old = next((str(data[k]) for k in ["old_price","price_old","compare_price"] if k in data), "")
            sku  = next((str(data[k]) for k in SKU_K if k in data and str(data[k]).strip()), "")
            desc = next((clean(data[k])[:400] for k in DESC_K if k in data and isinstance(data[k],str)), "")
            img  = next((data[k] for k in IMG_K if k in data and isinstance(data[k],str)), "")
            purl = next((data[k] for k in URL_K if k in data and isinstance(data[k],str)), "")
            brand= next((clean(data[k]) for k in BRAND_K if k in data and isinstance(data[k],str)), "")
            cat_v= next((data[k] for k in CAT_K if k in data), "")
            cat  = clean(cat_v) if isinstance(cat_v,str) else clean(cat_v.get("name","")) if isinstance(cat_v,dict) else ""
            stock= next((str(data[k]) for k in STOCK_K if k in data), "")
            conn.execute(
                "INSERT OR IGNORE INTO products VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_url,name,price,old,cur,sku,brand,cat,desc,img,purl,stock))
            count += 1
        for v in data.values():
            if isinstance(v,(dict,list)): count += json_products(v, source_url, conn, _depth+1)
    return count


# ──────────────────────────────────────────────
# Sitemap
# ──────────────────────────────────────────────

def sitemap_urls(base: str) -> list[str]:
    urls = []
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap"]:
        try:
            r = req_lib.get(base.rstrip("/")+path, headers=HEADERS, timeout=8)
            if r.status_code == 200 and "<url" in r.text:
                urls += _parse_sitemap(r.text, base)
                break
        except Exception: pass
    # robots.txt -> Sitemap:
    try:
        r = req_lib.get(base.rstrip("/")+"/robots.txt", headers=HEADERS, timeout=6)
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":",1)[1].strip()
                try:
                    sr = req_lib.get(sm, headers=HEADERS, timeout=8)
                    urls += _parse_sitemap(sr.text, base)
                except Exception: pass
    except Exception: pass
    result = list(dict.fromkeys(normalize(u) for u in urls if same_domain(u, base)))
    log.info("Sitemap: %d URL", len(result))
    return result

def _parse_sitemap(text, base):
    urls = []
    try:
        root = ET.fromstring(text)
        ns = {"s":"http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//s:sitemap/s:loc", ns):
            try:
                r = req_lib.get(loc.text.strip(), headers=HEADERS, timeout=8)
                urls += _parse_sitemap(r.text, base)
            except Exception: pass
        for loc in root.findall(".//s:url/s:loc", ns):
            u = normalize(loc.text.strip())
            if same_domain(u, base): urls.append(u)
    except ET.ParseError: pass
    return urls


# ──────────────────────────────────────────────
# Playwright-воркер
# ──────────────────────────────────────────────

async def pw_fetch(page, url: str, conn: sqlite3.Connection, wait_ms: int) -> str | None:
    """Загружает страницу Playwright, перехватывает XHR, скроллит."""

    async def on_response(resp):
        ct = resp.headers.get("content-type","")
        if "json" not in ct: return
        if any(s in resp.url for s in ["google","yandex","analytics","facebook","vk.com","metrika"]): return
        try:
            body = await resp.text()
            if len(body) < 10: return
            data = json.loads(body)
            c = json_products(data, url, conn)
            if c: log.info("  API %s → %d товаров", resp.url[:80], c)
            conn.execute("INSERT INTO api_responses VALUES (NULL,?,?,?,?)",
                         (url, resp.url, body[:80000], datetime.now(timezone.utc).isoformat()))
            conn.commit()
        except Exception: pass

    page.on("response", on_response)
    try:
        r = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        if r and r.status >= 400: return None
        await page.wait_for_timeout(wait_ms)
        try: await page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception: pass
        # Автоскролл
        for _ in range(8):
            prev = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            await page.wait_for_timeout(400)
            if await page.evaluate("document.body.scrollHeight") == prev: break
        return await page.content()
    except Exception as e:
        log.warning("PW error %s: %s", url, e)
        return None
    finally:
        page.remove_listener("response", on_response)


# ──────────────────────────────────────────────
# Пагинация
# ──────────────────────────────────────────────

def next_page_url(html: str, current_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for sel in ["a[rel='next']", ".pagination .next", "[class*='paginat'] .next"]:
        try:
            el = soup.select_one(sel)
            if el and el.get("href"):
                return normalize(urljoin(current_url, el["href"]))
        except Exception: pass
    # Ищем ссылку с текстом «следующая/»/далее
    for a in soup.find_all("a", href=True):
        txt = clean(a.get_text()).lower()
        if txt in ("»","›","следующая","далее","next","вперёд"):
            return normalize(urljoin(current_url, a["href"]))
    return None


# ──────────────────────────────────────────────
# Главный краулер
# ──────────────────────────────────────────────

async def crawl(start_url: str, max_pages: int = 100, workers: int = 5, wait_ms: int = 800):
    start_url = normalize(start_url)
    log.info("▶ %s  max=%d workers=%d wait=%dms", start_url, max_pages, workers, wait_ms)

    conn = init_db()

    # Очищаем данные предыдущего скрапа
    conn.executescript("""
        DELETE FROM pages;
        DELETE FROM contacts;
        DELETE FROM products;
        DELETE FROM services;
        DELETE FROM company_info;
        DELETE FROM links;
        DELETE FROM api_responses;
    """)
    conn.commit()

    visited: set[str] = set()
    queue: deque[str] = deque([start_url])

    # Добавляем URL из sitemap в очередь
    for u in sitemap_urls(start_url):
        if u not in visited:
            queue.append(u)

    sem = asyncio.Semaphore(workers)
    done = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # Пул страниц (по одной на воркер)
        pages = [await browser.new_page() for _ in range(workers)]
        for p in pages:
            await p.set_extra_http_headers(HEADERS)
            # Блокируем картинки/шрифты/медиа для скорости
            await p.route(
                re.compile(r"\.(png|jpg|jpeg|gif|webp|ico|woff2?|ttf|eot|mp4|mp3)(\?.*)?$", re.I),
                lambda route: route.abort()
            )
        page_pool: asyncio.Queue = asyncio.Queue()
        for p in pages:
            await page_pool.put(p)

        async def process(url: str):
            nonlocal done
            page = await page_pool.get()
            try:
                # 1. Быстрая попытка через requests
                html, status = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_static, url)
                if status >= 400:
                    log.debug("SKIP %d %s", status, url)
                    return
                # 2. Если страница требует JS - грузим через Playwright
                if needs_js(html or "", url):
                    html = await pw_fetch(page, url, conn, wait_ms)
                    if not html:
                        return
                    log.info("PW  %s", url)
                else:
                    log.info("REQ %s", url)

                new_links = parse_html(url, html, conn, start_url)
                done += 1

                # Пагинация
                nx = next_page_url(html, url)
                if nx and nx not in visited:
                    async with _db_lock:
                        if nx not in visited:
                            queue.appendleft(nx)

                # Добавляем новые ссылки
                for lnk in new_links:
                    if lnk not in visited:
                        queue.append(lnk)

            finally:
                await page_pool.put(page)

        tasks = set()

        while (queue or tasks) and done < max_pages:
            # Запускаем задачи пока есть очередь и свободные воркеры
            while queue and done + len(tasks) < max_pages:
                url = queue.popleft()
                url = normalize(url)
                if url in visited or should_skip(url, start_url):
                    continue
                visited.add(url)
                t = asyncio.create_task(process(url))
                tasks.add(t)
                t.add_done_callback(tasks.discard)

            if tasks:
                await asyncio.sleep(0.1)
            else:
                break

        # Ждём завершения всех задач
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await browser.close()

    # Перевод EN→RU отдельной фазой (браузер закрыт, краулинг не тормозит)
    translate_db(conn)
    _company_info(conn, start_url)
    conn.close()
    log.info("✓ Готово. Страниц: %d", done)


# ──────────────────────────────────────────────
# Сводка о компании
# ──────────────────────────────────────────────

def _company_info(conn, base_url):
    domain = urlparse(base_url).netloc
    row = conn.execute(
        "SELECT title,description,keywords,h1 FROM pages ORDER BY LENGTH(url) LIMIT 1").fetchone()
    if row:
        conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("domain", domain))
        conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("site_title",       row[0] or ""))
        conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("meta_description", row[1] or ""))
        conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("meta_keywords",    row[2] or ""))
        conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("main_h1",          row[3] or ""))
    emails  = [r[0] for r in conn.execute("SELECT DISTINCT value FROM contacts WHERE type='email'")]
    phones  = [r[0] for r in conn.execute("SELECT DISTINCT value FROM contacts WHERE type='phone'")]
    socials = {r[0].replace("social:",""):r[1] for r in
               conn.execute("SELECT type,value FROM contacts WHERE type LIKE 'social:%' GROUP BY type")}
    conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("emails",       json.dumps(emails,       ensure_ascii=False)))
    conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("phones",       json.dumps(phones,       ensure_ascii=False)))
    conn.execute("INSERT OR REPLACE INTO company_info VALUES (?,?)", ("social_links", json.dumps(socials,      ensure_ascii=False)))
    conn.commit()


# ──────────────────────────────────────────────
# Отчёт
# ──────────────────────────────────────────────

def print_report(output_json: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    info = dict(conn.execute("SELECT key,value FROM company_info").fetchall())
    pc = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    pd = conn.execute("SELECT COUNT(DISTINCT name) FROM products").fetchone()[0]
    sc = conn.execute("SELECT COUNT(DISTINCT name) FROM services").fetchone()[0]
    ac = conn.execute("SELECT COUNT(*) FROM api_responses").fetchone()[0]

    print("\n" + "="*70)
    print("САЙТ:", info.get("domain",""))
    print("="*70)
    for k,label in [("site_title","Title"),("meta_description","Description"),
                    ("meta_keywords","Keywords"),("main_h1","H1"),
                    ("org_name","Организация"),("org_description","О компании")]:
        v = info.get(k,"")
        if v: print(f"{label:14}: {v[:120]}")
    print(f"{'Страниц':14}: {pc}  |  API-перехватов: {ac}")

    emails  = json.loads(info.get("emails","[]"))
    phones  = json.loads(info.get("phones","[]"))
    socials = json.loads(info.get("social_links","{}"))
    if emails:  print("\nEMAIL:\n" + "\n".join(f"  {e}" for e in emails))
    if phones:  print("\nТЕЛЕФОНЫ:\n" + "\n".join(f"  {p}" for p in set(phones)))
    if socials: print("\nСОЦСЕТИ:\n" + "\n".join(f"  {n}: {h}" for n,h in socials.items()))
    addrs = conn.execute("SELECT DISTINCT value FROM contacts WHERE type='address'").fetchall()
    if addrs: print("\nАДРЕСА:\n" + "\n".join(f"  {a[0]}" for a in addrs))

    print(f"\n{'='*70}")
    print(f"ПРОДУКТЫ ({pd} уникальных):")
    rows = conn.execute(
        "SELECT name,price,old_price,currency,brand,category,description,product_url "
        "FROM products GROUP BY name ORDER BY name LIMIT 300").fetchall()
    for name,price,old,cur,brand,cat,desc,purl in rows:
        line = f"  • {name}"
        if price:  line += f"  | {price} {cur}".rstrip()
        if old and old != price: line += f"  (было {old})"
        if brand:  line += f"  | {brand}"
        if cat:    line += f"  | {cat}"
        print(line)
        if desc:  print(f"    {desc[:110]}")
        if purl:  print(f"    → {purl}")

    if sc:
        print(f"\n{'='*70}")
        print(f"УСЛУГИ ({sc}):")
        for n,d in conn.execute("SELECT name,description FROM services GROUP BY name LIMIT 50"):
            print(f"  • {n}")
            if d: print(f"    {d[:110]}")

    # Информационные страницы - текст без навигационного мусора
    INFO_SKIP = re.compile(r"/(catalog|basket|compare|personal|login|favicon|\?)", re.I)
    info_pages = [
        r for r in conn.execute(
            "SELECT url, title, h1, raw_text FROM pages "
            "WHERE raw_text IS NOT NULL AND LENGTH(raw_text) > 300 "
            "ORDER BY LENGTH(url)"
        ).fetchall()
        if not INFO_SKIP.search(r[0])
    ]
    if info_pages:
        print(f"\n{'='*70}")
        print(f"СОДЕРЖИМОЕ СТРАНИЦ ({len(info_pages)}):")
        for url, title, h1, raw in info_pages:
            domain_suffix = info.get("domain", "")
            heading = re.sub(rf"\s*[-–]\s*{re.escape(domain_suffix)}\s*$", "", title or "").strip() or h1 or url
            print(f"\n--- {heading} ---")
            print(f"URL: {url}")
            if raw:
                print(raw[:2000])
                if len(raw) > 2000:
                    print(f"  ... [ещё {len(raw)-2000} символов в БД]")

    print(f"\nБД  : {DB_PATH.resolve()}")
    print(f"Лог : {(LOG_DIR/'app.log').resolve()}")
    print("="*70)

    if output_json:
        report = {
            "company":  info,
            "pages":    [dict(zip(["url","title","h1","scraped_at"],r))
                         for r in conn.execute("SELECT url,title,h1,scraped_at FROM pages")],
            "contacts": [dict(zip(["page_url","type","value"],r))
                         for r in conn.execute("SELECT page_url,type,value FROM contacts")],
            "products": [dict(zip(["source_url","name","price","old_price","currency",
                                   "sku","brand","category","description","image_url","product_url","in_stock"],r))
                         for r in conn.execute(
                             "SELECT source_url,name,price,old_price,currency,sku,brand,"
                             "category,description,image_url,product_url,in_stock FROM products")],
            "services": [dict(zip(["page_url","name","description"],r))
                         for r in conn.execute("SELECT page_url,name,description FROM services")],
            "api_calls":[dict(zip(["trigger_url","api_url"],r))
                         for r in conn.execute("SELECT trigger_url,api_url FROM api_responses")],
        }
        Path(output_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {Path(output_json).resolve()}")

    # Markdown-отчёт
    _save_markdown(conn, info)

    conn.close()


def _clean_page_text(text: str) -> str:
    """Removes breadcrumbs, trailing navigation and pagination noise from page text."""
    # Remove breadcrumb at very start: "Word(s) Главная — Section — ... " up to ~250 chars
    text = re.sub(r'^.{0,50}Главная\s*[—\-]\s*.{0,200}(?=\s{2,}|\s[А-ЯA-Z][а-яa-z])', '', text, count=1).strip()
    # If still starts with "Главная", simpler cut: remove up to and including last " — Xxx "
    if text.startswith('Главная') or re.match(r'^\S+ Главная', text):
        text = re.sub(r'^[^\n]{0,300}Главная[^\n]{0,200}\s+', '', text, count=1).strip()
    # Remove trailing nav links like "Условия оплаты Условия доставки Возврат"
    text = re.sub(r'(\s*(Условия\s+оплаты|Условия\s+доставки|Возврат|Главная|Наверх)){2,}\s*$', '', text).strip()
    # Remove trailing site-nav chunks at end (short words without punctuation, 3+ items)
    text = re.sub(r'(\s+[А-ЯA-ZЁ№][а-яa-zА-ЯA-ZЁ\s«»"№\d]{1,30}){4,}\s*$', lambda m: '' if '.' not in m.group() else m.group(), text).strip()
    # Remove "Показать еще 1 2 3" pagination
    text = re.sub(r'Показать\s+еще\s*[\d\s]*', '', text)
    # Remove lone page number sequences at end "... 1 2 3 4"
    text = re.sub(r'(\s+\d+){3,}\s*$', '', text).strip()
    # Remove excessive repetitions of same word (e.g. "Подробнее Подробнее Подробнее")
    text = re.sub(r'\b(\w{4,})\s+(?:\1\s+){2,}', '', text)
    return text


def _save_markdown(conn: sqlite3.Connection, info: dict):
    domain = info.get("domain", "site")
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    md_path = reports_dir / f"report_{domain}.md"

    emails  = json.loads(info.get("emails", "[]"))
    phones  = list(set(json.loads(info.get("phones", "[]"))))
    socials = json.loads(info.get("social_links", "{}"))

    lines = []
    lines.append(f"# {info.get('site_title') or info.get('org_name') or domain}\n")

    # Общая информация
    lines.append("## Общая информация\n")
    if info.get("meta_description"): lines.append(f"{info['meta_description']}\n")
    if info.get("org_description"):  lines.append(f"\n{info['org_description']}\n")
    if info.get("org_name"):         lines.append(f"\n**Организация:** {info['org_name']}\n")

    # Контакты
    lines.append("\n## Контакты\n")
    if emails:
        lines.append("**Email:**")
        for e in emails: lines.append(f"- {e}")
    if phones:
        lines.append("\n**Телефоны:**")
        for p in phones: lines.append(f"- {p}")
    addrs = conn.execute("SELECT DISTINCT value FROM contacts WHERE type='address'").fetchall()
    if addrs:
        lines.append("\n**Адреса:**")
        for (a,) in addrs: lines.append(f"- {a}")
    if socials:
        lines.append("\n**Соцсети:**")
        for name, href in socials.items(): lines.append(f"- {name}: {href}")

    # Товары
    products = conn.execute(
        "SELECT name, price, old_price, currency, sku, description, product_url "
        "FROM products GROUP BY name ORDER BY name"
    ).fetchall()
    if products:
        lines.append(f"\n## Товары ({len(products)})\n")
        lines.append("| Название | Цена | Старая цена | Артикул |")
        lines.append("|---|---|---|---|")
        for name, price, old, cur, sku, desc, purl in products:
            price_str = f"{price} {cur}".strip() if price else ""
            old_str   = f"{old} {cur}".strip() if old else ""
            sku_str   = sku or ""
            lines.append(f"| {name} | {price_str} | {old_str} | {sku_str} |")

    # Услуги
    services = conn.execute("SELECT name, description FROM services GROUP BY name").fetchall()
    if services:
        lines.append(f"\n## Услуги ({len(services)})\n")
        for name, desc in services:
            lines.append(f"### {name}\n")
            if desc: lines.append(f"{desc}\n")

    # Содержимое информационных страниц
    INFO_SKIP = re.compile(r"/(catalog|basket|compare|personal|login|favicon|\?sort=)", re.I)
    info_pages = [
        r for r in conn.execute(
            "SELECT url, title, h1, raw_text FROM pages "
            "WHERE raw_text IS NOT NULL AND LENGTH(raw_text) > 300 "
            "ORDER BY LENGTH(url)"
        ).fetchall()
        if not INFO_SKIP.search(r[0])
    ]
    if info_pages:
        lines.append(f"\n## Содержимое страниц\n")
        for url, title, h1, raw in info_pages:
            heading = re.sub(rf"\s*[-–]\s*{re.escape(domain)}\s*$", "", title or "").strip() or (title or "").split(" - ")[0].split(" – ")[0].strip() or h1 or url
            lines.append(f"\n### {heading}\n")
            lines.append(f"**URL:** {url}\n")
            if raw:
                lines.append(_clean_page_text(raw))
                lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"MD  : {md_path.resolve()}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="Site Analyzer")
    p.add_argument("url")
    p.add_argument("--max-pages", type=int, default=100,  help="Макс. страниц (default 100)")
    p.add_argument("--workers",   type=int, default=5,    help="Параллельных воркеров (default 5)")
    p.add_argument("--wait",      type=int, default=800,  help="Ожидание JS мс (default 800)")
    p.add_argument("--json",      metavar="FILE")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()

    if args.report_only:
        print_report(args.json)
        return

    asyncio.run(crawl(args.url, max_pages=args.max_pages, workers=args.workers, wait_ms=args.wait))
    print_report(args.json)

if __name__ == "__main__":
    main()
