# -*- coding: utf-8 -*-
"""Перехватывает ВСЕ сетевые запросы при загрузке каталога."""
import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ))
        p = await ctx.new_page()

        requests_log = []

        async def on_request(req):
            requests_log.append((req.method, req.url, req.resource_type))

        async def on_response(resp):
            ct = resp.headers.get("content-type", "")
            # Показываем все XHR/fetch и JSON
            if resp.request.resource_type in ("xhr", "fetch") or "json" in ct:
                try:
                    body = await resp.text()
                    print(f"\n[{resp.status}] {resp.url[:100]}")
                    print(f"  CT: {ct[:60]}")
                    print(f"  Body preview: {body[:200]}")
                except Exception as e:
                    print(f"  [body error: {e}]")

        p.on("response", on_response)

        # Пробуем разные страницы каталога
        for url in [
            "https://rusalut.ru/catalog",
            "https://rusalut.ru/catalog/batarei_salyutov",
        ]:
            print(f"\n{'='*60}")
            print(f"Загружаю: {url}")
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=20000)
                await p.wait_for_timeout(3000)
                # Скролл
                for _ in range(5):
                    await p.evaluate("window.scrollBy(0, window.innerHeight)")
                    await p.wait_for_timeout(500)
                await p.wait_for_timeout(1000)
            except Exception as e:
                print(f"Error: {e}")

        await b.close()

asyncio.run(main())
