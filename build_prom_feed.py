# -*- coding: utf-8 -*-
"""WebSklad -> Prom feed filter.
Downloads WebSklad universal YML, keeps chosen categories + in-stock,
drops hide_for_prom, applies tiered markup, writes Prom-ready YML.
"""
import sys, urllib.request, xml.etree.ElementTree as ET, datetime, os

SRC = "https://www.websklad.biz.ua/wp-content/uploads/current-Universalnaya.xml"
KEEP_CATS = {"342", "336", "339", "351"}  # Красота, Дом и сад, Сумки, Моб.аксессуары
MAX_PICS = 15
MAX_OFFERS = 900  # never exceed free Prom slots (100 bags + 900 = 1000 plan)

def markup(price):
    if price < 500:   f = 1.20
    elif price < 1000: f = 1.15
    elif price < 3000: f = 1.12
    elif price < 7000: f = 1.10
    else:             f = 1.07
    p = price * f
    return int(round(p / 10.0) * 10)  # round to nearest 10 грн

def build(src_path, out_path):
    ctx = ET.iterparse(src_path, events=("end",))
    cats = {}
    offers_out = []
    kept = skipped_hide = skipped_stock = 0
    for ev, el in ctx:
        if el.tag == "category":
            cats[el.get("id")] = el.text
        elif el.tag == "offer":
            cid = el.findtext("categoryId")
            avail = (el.get("available") or "").lower()
            hide = (el.findtext("hide_for_prom") or "").lower()
            if cid in KEEP_CATS and avail == "true" and hide not in ("1", "true", "yes"):
                try:
                    price = float(el.findtext("price"))
                except (TypeError, ValueError):
                    el.clear(); continue
                name = el.findtext("name_ua") or el.findtext("name") or ""
                desc = el.findtext("description_ua") or el.findtext("description") or ""
                pics = [p.text for p in el.findall("picture") if p.text][:MAX_PICS]
                offers_out.append({
                    "id": el.get("id"),
                    "cid": cid,
                    "price": markup(price),
                    "name": name.strip(),
                    "desc": desc.strip(),
                    "pics": pics,
                    "vendorCode": el.findtext("vendorCode") or "",
                    "qty": el.findtext("quantity_in_stock") or "",
                    "params": [(p.get("name"), p.text) for p in el.findall("param") if p.text],
                })
                kept += 1
            else:
                if cid in KEEP_CATS and avail != "true": skipped_stock += 1
                if hide in ("1", "true", "yes"): skipped_hide += 1
            el.clear()

    if len(offers_out) > MAX_OFFERS:
        offers_out = offers_out[:MAX_OFFERS]
        kept = len(offers_out)

    # write YML
    used_cats = sorted({o["cid"] for o in offers_out})
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    lines.append('<yml_catalog date="%s">' % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    lines.append('  <shop>')
    lines.append('    <name>Top Sale Ukraine</name>')
    lines.append('    <company>Top Sale Ukraine</company>')
    lines.append('    <url>https://prom.ua/</url>')
    lines.append('    <currencies><currency id="UAH" rate="1"/></currencies>')
    lines.append('    <categories>')
    for c in used_cats:
        lines.append('      <category id="%s">%s</category>' % (c, esc(cats.get(c, ""))))
    lines.append('    </categories>')
    lines.append('    <offers>')
    for o in offers_out:
        lines.append('      <offer id="%s" available="true">' % esc(o["id"]))
        lines.append('        <name>%s</name>' % esc(o["name"]))
        lines.append('        <categoryId>%s</categoryId>' % o["cid"])
        lines.append('        <price>%d</price>' % o["price"])
        lines.append('        <currencyId>UAH</currencyId>')
        if o["vendorCode"]:
            lines.append('        <vendorCode>%s</vendorCode>' % esc(o["vendorCode"]))
        for pic in o["pics"]:
            lines.append('        <picture>%s</picture>' % esc(pic))
        if o["qty"]:
            lines.append('        <quantity_in_stock>%s</quantity_in_stock>' % esc(o["qty"]))
        lines.append('        <description><![CDATA[%s]]></description>' % (o["desc"] or ""))
        for pn, pv in o["params"]:
            if pn and pv:
                lines.append('        <param name="%s">%s</param>' % (esc(pn), esc(pv)))
        lines.append('      </offer>')
    lines.append('    </offers>')
    lines.append('  </shop>')
    lines.append('</yml_catalog>')
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return kept, len(used_cats), [markup and o["price"] for o in offers_out]

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "websklad.xml"
    out = sys.argv[2] if len(sys.argv) > 2 else "prom_feed.xml"
    if src == "download":
        src = "websklad_dl.xml"
        urllib.request.urlretrieve(SRC, src)
    kept, ncats, prices = build(src, out)
    prices.sort()
    print("offers kept:", kept, "| categories:", ncats)
    print("price min/median/max:", prices[0], prices[len(prices)//2], prices[-1])
    print("output:", out, "size:", os.path.getsize(out), "bytes")
