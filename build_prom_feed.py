# -*- coding: utf-8 -*-
"""WebSklad -> Prom feed filter.
Downloads WebSklad universal YML, keeps chosen categories + in-stock,
drops hide_for_prom, applies tiered markup, writes Prom-ready YML.
"""
import sys, re, urllib.request, xml.etree.ElementTree as ET, datetime, os, shutil, json
from prom_category_map import prom_category

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

def keywords_bilingual(name_ua, name_ru, params):
    """Двуязычные пошукові запити: чередуем укр і рус запити (з укр- та рус-назви),
    прибираємо дублі, обрізаємо до KW_MAX. YML тримає один список <keyword> (макс 10),
    тож даємо змішаний укр+рус набір для покриття обох мов у межах ліміту."""
    ua = keywords_for(name_ua, params) if name_ua else []
    ru = keywords_for(name_ru, params) if name_ru else []
    out, seen = [], set()
    for i in range(max(len(ua), len(ru))):
        for src in (ua, ru):
            if i < len(src) and src[i].lower() not in seen:
                seen.add(src[i].lower()); out.append(src[i])
            if len(out) >= KW_MAX:
                return out[:KW_MAX]
    return out[:KW_MAX]

def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)

SRC = "https://www.websklad.biz.ua/wp-content/uploads/current-Universalnaya.xml"

# --- Стабильный каталог (постоянные карточки, переключаем только наличие) -
# Prom ранжирует карточку накопительно: показы, позиция в выдаче, отзывы,
# «возраст». Удаление/пересоздание = новый URL и обнуление всей истории, поэтому
# каталог держим ПОСТОЯННЫМ. Товар, однажды попавший в фид, остаётся в нём, пока
# существует у поставщика: если он в наличии — available="true"; если кончился —
# карточку НЕ выкидываем, а отдаём с available="false" (Prom просто гасит
# наличие). Полностью убираем карточку из фида только если товар пропал у
# поставщика совсем ИЛИ лежит без наличия дольше OOS_EVICT_DAYS. Членство в фиде
# фиксируется в файле-состоянии docs/feed_state.json (дата первого появления и
# дата последнего наличия по каждому Артикулу).
OOS_EVICT_DAYS = 30   # сколько дней держим карточку без наличия, потом выводим

# Приём новинок поставщика — раз в месяц (1-го числа) или вручную через
# REFRESH_NEW=1. В остальные дни фид только обновляет наличие/цены у уже
# заведённых карточек: состав каталога не меняется вообще.
NEW_INTAKE_DAY = 1

def new_intake_allowed(today):
    if os.environ.get("REFRESH_NEW") == "1":
        return True
    return datetime.date.fromisoformat(today).day == NEW_INTAKE_DAY

def _load_state(path):
    """Файл-состояние: {vendorCode: {first_seen, last_in_stock}} — членство в фиде."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def _seed_state_from_feed(path, today):
    """Первый запуск без state: текущий живой фид становится стабильным ядром,
    чтобы при переходе на новую логику не перетасовать каталог лишний раз."""
    state = {}
    try:
        tree = ET.parse(path)
    except (FileNotFoundError, ET.ParseError):
        return state
    for off in tree.iter("offer"):
        vc = off.findtext("vendorCode") or off.get("id")
        if vc:
            state[vc] = {"first_seen": today, "last_in_stock": today}
    return state

KEEP_CATS = None  # None = whole catalog; or a set like {"342","339"} to restrict
# Bags are the user's OWN warehouse stock, managed manually on Prom — the dropship
# feed must never carry a bag, or the import could collide with those cards by
# id/vendorCode. Cat 339 = "Сумки, клатчи, кошельки, очки". Keep it out entirely.
EXCLUDE_CATS = {"339"}
MAX_PICS = 10  # Prom hard cap: no more than 10 photos per product
MAX_OFFERS = 700  # цель: 900 товаров на Prom всего (план 1000, запас 100).
# 700 дропшип + свои: ~147 сумок + 20 новых (серпень 2026) + 31 куля = ~898.
# (свои сумки — ОТДЕЛЬНЫЙ каталог, ведётся вручную, сюда никогда не попадает)

# Card-quality gate: the source has no sales data, so as a first pass we keep
# only well-merchandised offers (proven-enough to sell) and, when more pass
# than we have Prom slots, keep the best-scored ones. Real best-sellers get
# picked later from Prom's own analytics once the feed is live.
REQ_VIDEO = True   # must have a product video
MIN_PICS = 5       # at least this many photos
MIN_DESC = 300     # description at least this many chars

def quality_score(has_video, npics, desc_len, has_brand):
    """Higher = better-merchandised card; part of the ranking below."""
    return (3 if has_video else 0) + min(npics, 10) + min(desc_len // 300, 6) + (1 if has_brand else 0)

# Demand tier by WebSklad sub_category, calibrated from Prom/Rozetka listing
# volume (proxy for Ukrainian demand) + market knowledge. There is no per-SKU
# sales data, so demand is applied at the niche level. Unlisted niches = medium.
# Seasonal-off niches (winter boots in summer) are deliberately demoted.
DEMAND_HIGH = {
    "Машинки для стрижки", "Фени", "Чайники", "Бритви / епілятори", "Блендери", "Міксери",
    "Бутербродниці", "Мясорубки", "Праски", "Тостери", "Відпарювачі", "Портативні колонки",
    "Ліхтарі ручні", "Ліхтарі налобні", "Ліхтарі кемпінг", "Велофари", "Пилососи",
    "Мультиварки", "Фритюрниці", "Соковитискачі", "Кавомолки", "Кавоварки", "Ваги кухонні",
    "Годинники SKMEI", "Проектори / нічники", "Масажери", "Набори ножів", "Зубні щітки",
    "Сушилки для нігтів", "Фрезери", "Кільцеві лампи", "Безпровідні навушники",
    "Смарт-годинники", "Павербанки", "Турки", "Вакууматори", "Печі", "Електроплити",
}
DEMAND_LOW = {
    "Чоловіче зимове взуття", "Жіноче зимове взуття", "Ваги ювелірні", "Рахункові машинки",
    "Скарбнички", "Гаманці", "Дошки", "Літаючі іграшки", "Попкорн", "Від катишків",
    "Перекачка топлива", "Пульсоксиметри", "Термометри", "Гірлянди", "(none)",
}

def demand_bonus(sub):
    if sub in DEMAND_HIGH: return 8
    if sub in DEMAND_LOW:  return 0
    return 4  # medium (default for the long tail of niches)

def markup(price):
    if price < 500:   f = 1.20
    elif price < 1000: f = 1.15
    elif price < 3000: f = 1.12
    elif price < 7000: f = 1.10
    else:             f = 1.07
    p = price * f
    return int(round(p / 10.0) * 10)  # round to nearest 10 грн

def build(src_path, out_path):
    today = datetime.date.today().isoformat()
    state_path = os.path.join(os.path.dirname(out_path) or ".", "feed_state.json")
    # состояние = членство в постоянном каталоге; на первом запуске засеиваем
    # его текущим живым фидом, чтобы не перетасовать каталог при переходе
    state = _load_state(state_path) or _seed_state_from_feed(out_path, today)

    ctx = ET.iterparse(src_path, events=("end",))
    cats = {}
    candidates = []       # все валидные офферы источника (и в наличии, и нет)
    seen_keys = set()     # ключи, реально присутствующие у поставщика сейчас
    skipped_hide = skipped_quality = 0
    for ev, el in ctx:
        if el.tag == "category":
            cats[el.get("id")] = el.text
            el.clear()
        elif el.tag == "offer":
            cid = el.findtext("categoryId")
            avail = (el.get("available") or "").lower() == "true"
            hide = (el.findtext("hide_for_prom") or "").lower() in ("1", "true", "yes")
            in_cat = (KEEP_CATS is None or cid in KEEP_CATS) and cid not in EXCLUDE_CATS
            if not in_cat or hide:
                if hide: skipped_hide += 1
                el.clear(); continue
            try:
                price = float(el.findtext("price"))
            except (TypeError, ValueError):
                el.clear(); continue
            name = el.findtext("name_ua") or el.findtext("name") or ""
            name_ru = el.findtext("name") or ""  # рус-назва для двомовних запитів
            desc = el.findtext("description_ua") or el.findtext("description") or ""
            all_pics = [p.text for p in el.findall("picture") if p.text]
            has_video = bool((el.findtext("video_link") or "").strip())
            brand = (el.findtext("brand") or "").strip()
            has_brand = bool(brand) and brand.lower() not in _STOP_BRANDS
            vc = el.findtext("vendorCode") or ""
            key = vc or el.get("id")          # чем Prom матчит карточку (Артикул)
            incumbent = key in state          # уже в постоянном каталоге?
            # фильтр качества применяем ТОЛЬКО к новым кандидатам; товар, уже
            # стоящий в каталоге, из-за качества не выкидываем — важнее история
            low_quality = (REQ_VIDEO and not has_video) or len(all_pics) < MIN_PICS or len(desc) < MIN_DESC
            if low_quality and not incumbent:
                skipped_quality += 1
                el.clear(); continue
            params = [(p.get("name"), p.text) for p in el.findall("param") if p.text]
            sub = el.findtext("sub_category_ua") or el.findtext("sub_category") or "(none)"
            # рубрика Prom по нише WebSklad: кладём ID рубрики Prom в <categoryId>,
            # чтобы маркетплейс сам развёл товар в правильную рубрику (иначе Prom
            # угадывает по широкой категории → неверная рубрика / «invalid data»)
            prom_cid, prom_cname = prom_category(sub, cid)
            seen_keys.add(key)
            candidates.append({
                "id": el.get("id"),
                "cid": prom_cid,
                "cname": prom_cname,
                "price": markup(price),
                "name": name.strip(),
                "desc": desc.strip(),
                "pics": all_pics[:MAX_PICS],
                "vendorCode": vc,
                "key": key,
                "avail": avail,
                "incumbent": incumbent,
                "qty": el.findtext("quantity_in_stock") or "",
                "params": params,
                "keywords": keywords_bilingual(name.strip(), name_ru.strip(), params),
                # ранг = спрос ниши (прокси) + качество карточки; бонуса новинок
                # больше НЕТ — каталог стабильный, новинки не выдавливают устоявшиеся
                "rank": demand_bonus(sub) + quality_score(has_video, len(all_pics), len(desc), has_brand),
            })
            el.clear()

    # --- отбор: стабильное ядро + добор новинок на свободные слоты ----------
    kept = []
    added_new = evicted = oos_in_feed = 0

    # 1) инкумбенты, которые всё ещё есть у поставщика: остаются в фиде
    for c in candidates:
        if not c["incumbent"]:
            continue
        rec = state.get(c["key"]) or {"first_seen": today, "last_in_stock": today}
        if c["avail"]:
            rec["last_in_stock"] = today
            state[c["key"]] = rec
            kept.append(c)
        else:
            days_out = (datetime.date.fromisoformat(today)
                        - datetime.date.fromisoformat(rec.get("last_in_stock", today))).days
            if days_out <= OOS_EVICT_DAYS:
                state[c["key"]] = rec
                kept.append(c)          # карточка остаётся, но с available="false"
                oos_in_feed += 1
            else:
                state.pop(c["key"], None)   # слишком долго без наличия — выводим
                evicted += 1

    # 2) инкумбенты, пропавшие у поставщика совсем (сняты) — чистим состояние
    for k in list(state.keys()):
        if k not in seen_keys:
            state.pop(k, None)
            evicted += 1

    # 3) если ядро переполнило лимит — режем сначала «нет в наличии», затем слабых по рангу
    if len(kept) > MAX_OFFERS:
        kept.sort(key=lambda c: (c["avail"], c["rank"]))  # OOS и низкий ранг — первыми на вылет
        for c in kept[:len(kept) - MAX_OFFERS]:
            state.pop(c["key"], None)
            evicted += 1
        kept = kept[len(kept) - MAX_OFFERS:]

    # 4) добор новинок (в наличии, прошли фильтр качества) на свободные слоты, по рангу
    free = MAX_OFFERS - len(kept)
    if free > 0 and not new_intake_allowed(today):
        free = 0
    if free > 0:
        newbies = sorted((c for c in candidates if not c["incumbent"] and c["avail"]),
                         key=lambda c: c["rank"], reverse=True)[:free]
        for c in newbies:
            state[c["key"]] = {"first_seen": today, "last_in_stock": today}
            kept.append(c)
            added_new += 1

    offers_out = sorted(kept, key=lambda c: c["rank"], reverse=True)

    # write YML
    # категории = рубрики Prom, реально использованные в фиде (id → название)
    promcats = {o["cid"]: o["cname"] for o in offers_out}
    used_cats = sorted(promcats)
    def esc(s):
        s = (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = s.replace('"', "&quot;").replace("'", "&apos;")
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)  # drop XML-invalid control chars
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    lines.append('<yml_catalog date="%s">' % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    lines.append('  <shop>')
    lines.append('    <name>Top Sale Ukraine</name>')
    lines.append('    <company>Top Sale Ukraine</company>')
    lines.append('    <url>https://prom.ua/</url>')
    lines.append('    <currencies><currency id="UAH" rate="1"/></currencies>')
    lines.append('    <categories>')
    for c in used_cats:
        lines.append('      <category id="%s">%s</category>' % (c, esc(promcats[c])))
    lines.append('    </categories>')
    lines.append('    <offers>')
    for o in offers_out:
        lines.append('      <offer id="%s" available="%s">' % (esc(o["id"]), "true" if o["avail"] else "false"))
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
        safe_desc = (o["desc"] or "").replace("]]>", "]]&gt;")  # never break out of CDATA
        safe_desc = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", safe_desc)
        lines.append('        <description><![CDATA[%s]]></description>' % safe_desc)
        # ЭКСПЕРИМЕНТ: даём запити двумя форматами — множинні <keyword>
        # і єдиний <keywords> через кому — щоб перевірити, який читає Prom
        for kw in o["keywords"]:
            lines.append('        <keyword>%s</keyword>' % esc(kw))
        if o["keywords"]:
            lines.append('        <keywords>%s</keywords>' % esc(", ".join(o["keywords"])))
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
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, sort_keys=True, indent=0)
    return len(offers_out), len(used_cats), [o["price"] for o in offers_out], added_new, oos_in_feed, evicted

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "websklad.xml"
    out = sys.argv[2] if len(sys.argv) > 2 else "prom_feed.xml"
    if src == "download":
        src = "websklad_dl.xml"
        download(SRC, src)
    kept, ncats, prices, added_new, oos_in_feed, evicted = build(src, out)
    prices.sort()
    print("offers kept:", kept, "| categories:", ncats)
    print("добавлено новых:", added_new,
          "(приём новинок:", "ДА" if new_intake_allowed(datetime.date.today().isoformat()) else "нет, только 1-го числа",
          ") | без наличия (карточка держится):", oos_in_feed, "| выведено:", evicted)
    print("price min/median/max:", prices[0], prices[len(prices)//2], prices[-1])
    print("output:", out, "size:", os.path.getsize(out), "bytes")
