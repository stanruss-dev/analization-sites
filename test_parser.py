# -*- coding: utf-8 -*-
import sys, requests, re
sys.stdout.reconfigure(encoding="utf-8")
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0"}
r = requests.get("https://rusalut.ru/catalog/batarei_salyutov/batarei_salyutov_bolshie/",
                  headers=headers, timeout=15)
soup = BeautifulSoup(r.text, "lxml")

# Берём первую карточку с price_value
card = None
for el in soup.find_all(class_="catalog-block-view__item"):
    if el.find(class_="price_value"):
        card = el
        break

if card:
    print("=== Полная карточка (1500 символов) ===")
    print(str(card)[:1500])
    print()
    print("=== Все классы внутри карточки ===")
    for child in card.find_all(True):
        cls = child.get("class", [])
        txt = child.get_text().strip()[:50]
        if cls and txt:
            print(f"  {child.name}.{' '.join(cls)[:60]} → {txt}")
