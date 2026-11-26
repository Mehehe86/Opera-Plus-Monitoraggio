#!/usr/bin/env python3
"""Collaudo HTML: scoperta delle sezioni notizie e verifica dei singoli articoli."""
import argparse
import csv
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.robotparser import RobotFileParser
from functools import lru_cache
from html.parser import HTMLParser
from censimento_rss import fetch, valid_url, ROOT, date_iso, UA

PILOT = ["Teatro alla Scala", "Teatro Regio Torino", "Opéra national de Paris", "Royal Opera", "Teatro di San Carlo"]
NEWS = re.compile(r"news|notizi[ea]|comunicat|ufficio.stampa|press|actualit|presse|nachrichten|aktuelles|noticias|prensa|nieuws|magazine|journal|novit", re.I)
SKIP = re.compile(r"\.(pdf|jpg|jpeg|png|gif|zip|mp4|webp)(?:$|\?)|/(login|account|cart|ticketshop)(?:/|$)", re.I)
ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "reportagenewsarticle", "pressrelease"}
META_DATES = ("article:published_time", "datepublished", "pubdate", "parsely-pub-date", "dc.date.issued")

def tidy(s):
    return " ".join((s or "").split())

MONTHS = (
    "gennaio janvier january januar enero", "febbraio fevrier february februar febrero",
    "marzo mars march marz marzo", "aprile avril april abril",
    "maggio mai may mayo", "giugno juin june juni junio",
    "luglio juillet july juli julio", "agosto aout august agosto",
    "settembre septembre september septiembre", "ottobre octobre october oktober octubre",
    "novembre november noviembre", "dicembre decembre december dezember diciembre"
)
MONTH_MAP = {word: i for i, words in enumerate(MONTHS, 1) for word in words.split()}
MONTH_PATTERN = "|".join(sorted(MONTH_MAP, key=len, reverse=True))

def human_date(value):
    value = tidy(value)
    iso = date_iso(value)
    if iso:
        return iso
    v = "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))
    m = re.fullmatch(r"(\d{1,2})(?:er|st|nd|rd|th)?[.\s]+(" + MONTH_PATTERN + r")[,\s]+(\d{4})", v)
    if m:
        day, month, year = m.groups()
    else:
        m = re.fullmatch(r"(" + MONTH_PATTERN + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", v)
        if not m:
            return ""
        month, day, year = m.groups()
    try:
        return datetime(int(year), MONTH_MAP[month], int(day)).date().isoformat()
    except ValueError:
        return ""

def host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")

def same_site(url, base):
    return valid_url(url) and host(url) == host(base)

@lru_cache(maxsize=256)
def robot_policy(origin):
    _, body, err = fetch(urljoin(origin, "/robots.txt"))
    if err:
        return (None, err == "HTTP 404" or err == "HTTP 410")
    rp = RobotFileParser()
    rp.parse(body.decode("utf-8", errors="replace").splitlines())
    return rp, True

def get_page(url):
    if not valid_url(url):
        return url, b"", "URL non valido"
    rp, allowed = robot_policy(urljoin(url, "/"))
    if not allowed or (rp and not rp.can_fetch("OperaPlus-Monitoraggio", url)):
        return url, b"", "Accesso non autorizzato o robots.txt non verificabile"
    return fetch(url)

class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.meta = {}
        self.jsonld = []
        self.anchor = None
        self.script = None
        self.heading = None
        self.headings = []
        self.title = None
        self.canonical = ""
        self.published_times = []
        self.time_tag = None
        self.visible_text = []
        self.tokens = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "time":
            marker = " ".join((a.get(k) or "") for k in ("class", "itemprop", "aria-label")).lower()
            if re.search(r"datepublished|published|publication|pubdate|date-publication|data-pubblicazione", marker):
                self.time_tag = [a.get("datetime") or "", ""]
        if tag == "a" and a.get("href"):
            self.anchor = [a["href"], a.get("aria-label") or a.get("title") or ""]
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta[key] = a["content"]
        if tag == "link" and "canonical" in (a.get("rel") or "").lower().split():
            self.canonical = a.get("href") or ""
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.script = ""
        if tag in ("h1", "title"):
            self.heading = [tag, ""]
    def handle_data(self, data):
        if not self.hidden:
            self.visible_text.append(tidy(data))
            if tidy(data) and self.anchor is None:
                self.tokens.append(("text", tidy(data)))
        if self.time_tag is not None:
            self.time_tag[1] += data
        if self.anchor is not None:
            self.anchor[1] += " " + data
        if self.script is not None:
            self.script += data
        if self.heading is not None:
            self.heading[1] += data
    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "time" and self.time_tag is not None:
            self.published_times.append(self.time_tag[0] or self.time_tag[1])
            self.time_tag = None
        if tag == "a" and self.anchor is not None:
            self.links.append((self.anchor[0], tidy(self.anchor[1])))
            self.tokens.append(("link", (self.anchor[0], tidy(self.anchor[1]))))
            self.anchor = None
        if tag == "script" and self.script is not None:
            try:
                self.jsonld.append(json.loads(self.script))
            except (ValueError, TypeError):
                pass
            self.script = None
        if self.heading is not None and tag == self.heading[0]:
            if tag == "title":
                self.title = tidy(self.heading[1])
            else:
                self.headings.append(tidy(self.heading[1]))
            self.heading = None

def parse(data):
    p = Page()
    p.feed(data.decode("utf-8", errors="replace"))
    return p

def nodes(value):
    if isinstance(value, list):
        for child in value:
            yield from nodes(child)
    elif isinstance(value, dict):
        yield value
        if "@graph" in value:
            yield from nodes(value["@graph"])

def links(page, base):
    for href, title in page.links:
        url = urldefrag(urljoin(base, href))[0]
        if same_site(url, base) and not SKIP.search(url):
            yield url, title

def article(page, url, anchor):
    for item in nodes(page.jsonld):
        types = item.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if not any(str(t).rsplit("/", 1)[-1].lower() in ARTICLE_TYPES for t in types):
            continue
        raw_title = item.get("headline") or item.get("name") or ""
        title = tidy(raw_title) if isinstance(raw_title, str) else ""
        raw_date = item.get("datePublished", "")
        date = date_iso(raw_date) if isinstance(raw_date, str) else ""
        if title and date:
            return {"titolo": title, "url": url, "data_pubblicazione": date, "prova": "JSON-LD Article/datePublished"}
    meta = page.meta
    title = tidy(meta.get("og:title") or (page.headings[0] if page.headings else "") or page.title)
    date = next((d for key in META_DATES if (d := date_iso(meta.get(key, "")))), "")
    if title and date and (meta.get("og:type", "").lower() == "article" or NEWS.search(url)):
        return {"titolo": title, "url": url, "data_pubblicazione": date, "prova": "metadati di pubblicazione"}
    if title and NEWS.search(urlsplit(url).path):
        dates = {d for raw in page.published_times if (d := human_date(raw))}
        # Only explicit publication labels; never infer from performance dates.
        for line in page.visible_text:
            m = re.fullmatch(r"(?:pubblicat[oa](?: il)?|publi[eé] le|published(?: on)?|veröffentlicht am|publicado el)\s*:?\s*(.+)", line, re.I)
            if m and (d := human_date(m.group(1))):
                dates.add(d)
        if len(dates) == 1:
            return {"titolo": title, "url": url, "data_pubblicazione": dates.pop(), "prova": "etichetta esplicita di pubblicazione"}
    return None

@lru_cache(maxsize=1)
def listing_config():
    return json.loads((ROOT / "config" / "html.json").read_text(encoding="utf-8"))

def listing_articles(page, base, source):
    """Configured listing layouts; no generic date inference."""
    rule = listing_config().get(source, {})
    found = {}
    if rule.get("model") == "date_then_title":
        pending = ""
        for kind, value in page.tokens:
            if kind == "text":
                pending = human_date(value)
                continue
            href, title = value
            url = urldefrag(urljoin(base, href))[0]
            if not same_site(url, base) or not re.fullmatch(rule["path_pattern"], urlsplit(url).path) or urlsplit(url).path.rstrip("/") in rule.get("exclude_paths", []):
                pending = ""
                continue
            m = re.match(r"^(\d{1,2}(?:er)?\s+[^\W\d_]+\s+\d{4})\s+(.+)$", title, re.I)
            date = human_date(m.group(1)) if m else pending
            if m:
                title = tidy(m.group(2))
            if date and len(title) >= 8:
                found[url] = {"titolo": title, "url": url, "data_pubblicazione": date, "prova": "data associata al titolo nella lista ufficiale dei comunicati", "pagina_prova": base}
            pending = ""
    elif rule.get("model") == "marked_title_then_date":
        pending = None
        marked = False
        for kind, value in page.tokens:
            if kind == "text":
                if value.casefold() == rule["marker"].casefold():
                    marked, pending = True, None
                elif pending:
                    date = human_date(value)
                    if date:
                        url, title = pending
                        found[url] = {"titolo": title, "url": url, "data_pubblicazione": date, "prova": "data dopo il titolo nel blocco comunicato stampa", "pagina_prova": base}
                    # Only the immediately following nonempty text token.
                    pending, marked = None, False
            elif kind == "link":
                href, title = value
                url = urldefrag(urljoin(base, href))[0]
                if marked and same_site(url, base) and re.fullmatch(rule["path_pattern"], urlsplit(url).path) and title.casefold() not in rule.get("exclude_titles", []) and len(title) >= 8:
                    pending = (url, title)
                    marked = False
                else:
                    pending = None
    return list(found.values())

def blocked(page):
    title = (page.title or "").lower()
    text = " ".join(page.visible_text[:30]).lower()
    return any(x in title or x in text for x in ("just a moment", "checking your browser", "verify you are human", "access denied", "attention required", "checking your connection", "verifica di sicurezza", "security check required"))

def inspect(row, max_articles):
    site = row["sito_candidato"].strip()
    result = {"fonte": row["fonte"], "sito": site, "sezioni": [], "articoli": [], "candidati_non_confermati": [], "errori": [], "stato": ""}
    if not site:
        result["stato"] = "URL da individuare"
        return result
    base, body, err = get_page(site)
    if err:
        result["errori"].append({"url": site, "errore": err})
        result["stato"] = "Accesso fallito"
        return result
    home = parse(body)
    if blocked(home):
        result["errori"].append({"url": base, "errore": "Schermata di sicurezza; nessun tentativo di aggiramento"})
        result["stato"] = "Accesso fallito"
        return result
    sections = {}
    explicit = row.get("pagina_notizie", "").strip()
    if explicit:
        u = urljoin(base, explicit)
        if same_site(u, base):
            sections[u] = "pagina registrata"
    for url, title in links(home, base):
        if NEWS.search(title) or NEWS.search(urlsplit(url).path):
            sections.setdefault(url, title)
    for path in ("news", "notizie", "comunicati-stampa", "it/news", "it/notizie", "en/news", "fr/actualites", "actualites", "press", "presse"):
        sections.setdefault(urljoin(base, "/" + path), "tentativo")
    candidates = {}
    rule = listing_config().get(row["fonte"], {})
    if rule and explicit:
        primary = urljoin(base, explicit)
        sections = {primary: "pagina registrata", **{u: label for u, label in sections.items() if u != primary}}
    for url, label in list(sections.items())[:12]:
        final, body, err = get_page(url)
        if err:
            result["errori"].append({"url": url, "errore": err})
            continue
        if not same_site(final, base) or final.rstrip("/") == base.rstrip("/"):
            continue
        page = parse(body)
        if blocked(page):
            result["errori"].append({"url": final, "errore": "Schermata di sicurezza"})
            continue
        result["sezioni"].append({"url": final, "etichetta": label, "titolo": page.title})
        if label == "pagina registrata":
            result["articoli"].extend(listing_articles(page, final, row["fonte"]))
        if rule and label == "pagina registrata" and len(result["articoli"]) >= rule.get("minimum_expected", 1):
            break
        for u, title in links(page, final):
            if u.rstrip("/") == final.rstrip("/") or len(title) < 18:
                continue
            path = urlsplit(u).path
            if NEWS.search(path) or path.startswith(urlsplit(final).path.rstrip("/") + "/"):
                candidates.setdefault(u, title)
    for url, title in list(candidates.items())[:max_articles]:
        if any(a["url"] == url for a in result["articoli"]):
            continue
        final, body, err = get_page(url)
        if err:
            result["errori"].append({"url": url, "errore": err})
            continue
        if not same_site(final, base):
            continue
        page = parse(body)
        if blocked(page):
            result["errori"].append({"url": final, "errore": "Schermata di sicurezza"})
            continue
        a = article(page, final, title)
        if a:
            if not any(x["url"] == a["url"] for x in result["articoli"]):
                result["articoli"].append(a)
        else:
            result["candidati_non_confermati"].append({"url": final, "titolo_link": title})
    result["diagnostica"] = {"modello": rule.get("model", "generico"), "estrazione_riuscita": bool(result["articoli"]), "copertura_completa_verificata": False, "richiede_verifica_alternativa": not bool(result["articoli"])}
    result["stato"] = "Articoli con data confermata" if result["articoli"] else "Sezioni candidate senza articoli confermati" if result["sezioni"] else "Sezione non individuata"
    return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", action="append", default=[], help="Nome esatto della fonte; ripetibile")
    p.add_argument("--all", action="store_true", help="Tutte le fonti anziché il gruppo pilota")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-articles", type=int, default=15)
    p.add_argument("--output", type=Path, default=ROOT / "rapporti" / "censimento_html.json")
    args = p.parse_args()
    if not 1 <= args.max_articles <= 50 or args.limit < 0:
        p.error("Limiti non validi")
    with (ROOT / "fonti.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if args.source:
        rows = [r for r in rows if r["fonte"] in args.source]
        if len(rows) != len(set(args.source)):
            raise ValueError("Fonte richiesta non trovata")
    elif not args.all:
        rows = [r for r in rows if r["fonte"] in PILOT]
        if len(rows) != len(PILOT):
            raise ValueError("Gruppo pilota incompleto")
    if args.limit:
        rows = rows[:args.limit]
    results = []
    for i, row in enumerate(rows, 1):
        try:
            r = inspect(row, args.max_articles)
        except Exception as exc:
            r = {"fonte": row["fonte"], "stato": "Errore di analisi", "articoli": [], "errori": [{"errore": str(exc)[:200]}]}
        results.append(r)
        print(f'{i}/{len(rows)} {r["fonte"]}: {r["stato"]} ({len(r["articoli"])} articoli)', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"data_utc": datetime.now(timezone.utc).isoformat(), "fonti_esaminate": len(results), "risultati": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
