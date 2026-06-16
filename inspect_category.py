# -*- coding: utf-8 -*-
import asyncio, sys, re
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ))
        p = await ctx.new_page()
        # Блокируем тяжёлые ресурсы
        import re as re_mod
        await p.route(
            re_mod.compile(r"\.(png|jpg|jpeg|gif|webp|ico|woff2?|ttf|eot|mp4|mp3|css)(\?.*)?$", re_mod.I),
            lambda route: route.abort()
        )

        # Сначала заходим на главную - получаем куки/сессию
        try:
            await p.goto("https://rusalut.ru", wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print(f"Главная: {e} (продолжаем)")
        await p.wait_for_timeout(1000)

        # Теперь страница категории
        resp = await p.goto("https://rusalut.ru/catalog/batarei_salyutov",
                            wait_until="domcontentloaded", timeout=40000)
        print(f"HTTP статус: {resp.status}")
        await p.wait_for_timeout(2000)
        for _ in range(5):
            await p.evaluate("window.scrollBy(0, window.innerHeight)")
            await p.wait_for_timeout(400)

        html = await p.content()
        soup = BeautifulSoup(html, "lxml")

        # Ищем элементы с ценами
        print("\n=== Элементы с ценами/руб/₽ ===")
        for el in soup.find_all(string=re.compile(r"₽|руб|\d{3,}\s*(р|₽)")):
            par = el.parent
            cls = " ".join(par.get("class", []))
            print(f"  [{par.name}.{cls[:50]}] {str(el).strip()[:60]}")

        # Топ классы
        print("\n=== Частые классы (x>3) ===")
        classes = Counter()
        for el in soup.find_all(True, class_=True):
            for c in el.get("class", []):
                classes[c] += 1
        for cls, cnt in classes.most_common(30):
            if cnt > 3:
                print(f"  .{cls} x{cnt}")

        # Ищем карточки товаров
        print("\n=== Первые карточки товаров ===")
        for sel in [".item", ".product", "[class*='item']", "article"]:
            els = soup.select(sel)[:3]
            for el in els:
                txt = el.get_text()[:100].strip().replace("\n"," ")
                cls = " ".join(el.get("class",[]))[:50]
                if txt:
                    print(f"  {el.name}.{cls} → {txt}")

        # Сохраним HTML для анализа
        with open("catalog_html.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("\nHTML сохранён в catalog_html.html")

        await b.close()

asyncio.run(main())
