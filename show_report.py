# -*- coding: utf-8 -*-
import json, sys

sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("report.json", encoding="utf-8"))
info = data["company"]

print("=" * 70)
print("=== КОМПАНИЯ ===")
print(f"Домен       : {info.get('domain','')}")
print(f"Title       : {info.get('site_title','')}")
print(f"Description : {info.get('meta_description','')}")
print(f"H1          : {info.get('main_h1','')}")

emails = json.loads(info.get("emails", "[]"))
phones = json.loads(info.get("phones", "[]"))
socials = json.loads(info.get("social_links", "{}"))

print(f"\nEMAIL:")
for e in emails:
    print(f"  {e}")

print(f"\nТЕЛЕФОНЫ (отфильтрованные):")
real_phones = [p for p in phones if sum(c.isdigit() for c in p) >= 7 and not any(y in p for y in ["2016","2017","2026","322774"])]
for p in real_phones:
    print(f"  {p}")

print(f"\nСОЦСЕТИ:")
for name, href in socials.items():
    print(f"  {name}: {href}")

# Адреса
ct = {}
for c in data["contacts"]:
    ct.setdefault(c["type"], set()).add(c["value"])

if "address" in ct:
    print(f"\nАДРЕСА:")
    for a in ct["address"]:
        print(f"  {a}")

# Продукты
products = data["products"]
seen = set()
unique = []
for p in products:
    if p["name"] not in seen:
        seen.add(p["name"])
        unique.append(p)

print(f"\n{'='*70}")
print(f"=== ПРОДУКТЫ ({len(unique)} уникальных) ===")
for p in unique[:60]:
    price = f"  | {p['price']}" if p["price"] else ""
    print(f"  • {p['name']}{price}")
    if p["description"]:
        print(f"    {p['description'][:110]}")

# Услуги
services = data["services"]
seen2 = set()
unique_s = []
for s in services:
    if s["name"] not in seen2:
        seen2.add(s["name"])
        unique_s.append(s)

print(f"\n{'='*70}")
print(f"=== УСЛУГИ ({len(unique_s)} уникальных) ===")
for s in unique_s[:40]:
    print(f"  • {s['name']}")
    if s["description"]:
        print(f"    {s['description'][:110]}")

print(f"\n{'='*70}")
print(f"Страниц обработано : {len(data['pages'])}")
print(f"Всего контактов    : {len(data['contacts'])}")
