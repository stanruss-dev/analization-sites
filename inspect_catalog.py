# -*- coding: utf-8 -*-
import asyncio, sys, re
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        p = await b.new_page()
        await p.goto("https://rusalut.ru/catalog", wait_until="domcontentloaded", timeout=30000)
        await p.wait_for_timeout(2000)
        for _ in range(8):
            await p.evaluate("window.scrollBy(0, window.innerHeight)")
            await p.wait_for_timeout(400)
        await p.wait_for_timeout(1000)
        html = await p.content()
        soup = BeautifulSoup(html, "lxml")

        print("=== Элементы содержащие ₽ или руб ===")
        for el in soup.find_all(string=re.compile(r"руб|₽|\d{3,}")):
            par = el.parent
            gp  = par.parent if par.parent else par
            cls = par.get("class", [])
            if cls:
                print(f"  .{' .'.join(cls)} | {str(el).strip()[:60]}")

        print()
        print("=== Самые частые классы (x>2) ===")
        classes = Counter()
        for el in soup.find_all(["div","article","li","section"], class_=True):
            for c in el.get("class", []):
                classes[c] += 1
        for cls, cnt in classes.most_common(40):
            if cnt > 2:
                print(f"  .{cls} x{cnt}")

        print()
        print("=== Первые 5 элементов с 'item' или 'product' в классе ===")
        for el in soup.select("[class*='item'],[class*='product'],[class*='card']")[:5]:
            print(f"  {el.name}.{' '.join(el.get('class',[]))} → {el.get_text()[:80].strip()}")

        await b.close()

asyncio.run(main())
