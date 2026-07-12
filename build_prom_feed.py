# -*- coding: utf-8 -*-
"""WebSklad -> Prom feed filter.
Downloads WebSklad universal YML, keeps chosen categories + in-stock,
drops hide_for_prom, applies tiered markup, writes Prom-ready YML.
"""
import sys, re, urllib.request, xml.etree.ElementTree as ET, datetime, os, shutil

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# --- Prom search queries (пошукові запити) -------------------------------
# Prom lets each offer carry up to 10 search queries as <keyword> tags; the
# source feed has none, so we derive them. We aim for the full 10 per offer,
# best signal first: the supplier already writes several comma/sentence SEO
# phrases into the name, so we split those out, add progressively shorter
# "head" queries, the brand, and finally head + attribute (param) variants.
KW_MAX = 10           # Prom hard cap: no more than 10 queries per offer
KW_MAXLEN, KW_MINLEN = 50, 5
_LABEL_HEADS = {"колір", "цвет", "модель", "розмір", "размер", "size", "арт", "артикул", "код"}
_STOP_BRANDS = {"", "-", "нет", "немає", "без бренда", "без бренду", "no name", "noname", "brand"}
_STOP_TAIL = {"для", "від", "з", "зі", "із", "на", "та", "і", "й", "в", "у", "по", "до",
              "the", "with", "for", "and", "of", "a"}
# param names whose values are noise/numeric/logistics — never used as queries
_BAD_PARAMS = {"Состояние", "Страна производитель", "Производитель", "Гарантия",
    "Потребляемая мощность", "Мощность фена", "Вес", "Вес в упаковке", "Объем котла (бака для воды)",
    "Время подготовки", "Диапазон измерения", "Рабочая температура", "Точность измерения",
    "Единицы измерения", "Тип батареи", "Минимальная длина стрижки", "Максимальная длина стрижки",
    "Количество насадок", "Количество режимов работы", "Количество скоростей", "Тип управления",
    "Тип аккумулятора", "Тип питания", "Регулировка силы пара", "Способ продувания", "Датчик", "Дизайн"}
# param values that carry no search meaning ("нет", "стандартный", ...)
_BAD_VALUES = {"нет", "немає", "да", "так", "yes", "no", "none", "-", "без", "новое", "новый",
    "нове", "б/у", "стандартный", "стандартний", "обычный", "звичайний", "универсальный",
    "універсальний", "есть", "в наличии", "розпродаж", "отсутствует", "відсутній"}
# preferred order of the params we DO turn into queries (most search-worthy first)
_PREF_PARAMS = ["Тип", "Тип прибора", "Вид", "Назначение", "Зона использования", "Модель сумки",
    "Стиль", "Форма", "Пол", "Действие", "Материал", "Материал лезвий", "Цвет", "Цвет корпуса",
    "Комплектация", "Особенности", "Тип изделия", "Вид педикюрного инструмента"]
# a lone first word is a useful broad query only if it is a noun, not an adjective
_ADJ_END = ("ий", "ій", "ый", "ой", "ая", "яя", "ое", "ее", "ний", "ній", "ська", "ський",
            "цький", "ова", "ове", "на", "не", "ні", "ча", "ще")

def _kw_clean(s):
    return re.sub(r"\s+", " ", (s or "").strip()).strip(" ,.;:/\\-")

def _kw_trim_tail(s):
    w = s.split()
    while w and w[-1].lower().strip(":.") in _STOP_TAIL:
        w.pop()
    return " ".join(w)

def _kw_tidy(k):
    """Cut to <=KW_MAXLEN on a word boundary and drop dangling
    prepositions / lone letters / bare numbers left at the tail."""
    if len(k) > KW_MAXLEN:
        k = k[:KW_MAXLEN].rsplit(" ", 1)[0]
    w = k.split()
    while w and (w[-1].lower().strip(":.") in _STOP_TAIL
                 or len(re.sub(r"[^\wА-Яа-яЇїІіЄєҐґ]", "", w[-1])) <= 1
                 or re.fullmatch(r"[\d.,x×]+", w[-1])):
        w.pop()
    return " ".join(w)

def keywords_for(name, params):
    """Return up to KW_MAX de-duplicated Prom search queries for one offer."""
    p = {}
    for k, v in params:
        if k and v and k not in p:
            p[k] = _kw_clean(v)
    out, seen = [], set()

    def add(k, force=False):
        k = _kw_tidy(_kw_trim_tail(_kw_clean(k)))
        if not k:
            return False
        low = k.lower()
        if low in seen or len(k) < KW_MINLEN:
            return False
        if not force and low.split()[0].strip(":") in _LABEL_HEADS:
            return False
        seen.add(low)
        out.append(k)
        return True

    phrases = [_kw_clean(x) for x in re.split(r"[,.]\s+|,", name) if _kw_clean(x)]
    base = phrases[0] if phrases else _kw_clean(name)
    words = base.split()
    h3 = _kw_trim_tail(" ".join(words[:3]))
    h2 = _kw_trim_tail(" ".join(words[:2]))
    h4 = _kw_trim_tail(" ".join(words[:4]))
    h5 = _kw_trim_tail(" ".join(words[:5]))
    brand = p.get("Производитель", "")

    add(h3)
    for ph in phrases:            # supplier's own SEO phrases
        add(ph)
    add(h4); add(h5); add(h2)     # progressively shorter head queries
    if brand and brand.lower() not in _STOP_BRANDS:
        add(brand)
        if brand.lower() not in h3.lower():
            add(f"{h3} {brand}")
    if words and not words[0].lower().endswith(_ADJ_END):
        add(words[0])             # broad single-noun query
    # fill toward KW_MAX with head + attribute values (curated params first)
    ordered = ([pn for pn in _PREF_PARAMS if pn in p]
               + [pn for pn in p if pn not in _PREF_PARAMS and pn not in _BAD_PARAMS])
    for pname in ordered:
        if len(out) >= KW_MAX:
            break
        val = _kw_clean(re.split(r"[;,]", p[pname])[0]).lower()
        if (len(val) < 3 or val in _LABEL_HEADS or val in _BAD_VALUES
                or val in h3.lower() or not re.search(r"[а-яa-zА-ЯA-ZЇїІіЄєҐґ]{3}", val)):
            continue
        add(f"{h3} {val}")
        if len(out) < KW_MAX:
            add(f"{h2} {val}")
    if not out:  # never leave an offer without at least one query
        add(base, force=True) or add(" ".join(words[:2]), force=True)
    return out[:KW_MAX]

def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)

SRC = "https://www.websklad.biz.ua/wp-content/uploads/current-Universalnaya.xml"
KEEP_CATS = {"342", "336", "339", "351"}  # Красота, Дом и сад, Сумки, Моб.аксессуары
MAX_PICS = 10  # Prom hard cap: no more than 10 photos per product
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
                params = [(p.get("name"), p.text) for p in el.findall("param") if p.text]
                offers_out.append({
                    "id": el.get("id"),
                    "cid": cid,
                    "price": markup(price),
                    "name": name.strip(),
                    "desc": desc.strip(),
                    "pics": pics,
                    "vendorCode": el.findtext("vendorCode") or "",
                    "qty": el.findtext("quantity_in_stock") or "",
                    "params": params,
                    "keywords": keywords_for(name.strip(), params),
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
        for kw in o["keywords"]:
            lines.append('        <keyword>%s</keyword>' % esc(kw))
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
        download(SRC, src)
    kept, ncats, prices = build(src, out)
    prices.sort()
    print("offers kept:", kept, "| categories:", ncats)
    print("price min/median/max:", prices[0], prices[len(prices)//2], prices[-1])
    print("output:", out, "size:", os.path.getsize(out), "bytes")
