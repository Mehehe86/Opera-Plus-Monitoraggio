#!/usr/bin/env python3
"""Collaudo HTML: scoperta delle sezioni notizie e verifica dei singoli articoli."""
import argparse
import csv
import json
import re
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
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
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
        if self.anchor is not None:
            self.anchor[1] += " " + data
        if self.script is not None:
            self.script += data
        if self.heading is not None:
            self.heading[1] += data
    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append((self.anchor[0], tidy(self.anchor[1])))
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
    return None

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
    for url, label in list(sections.items())[:12]:
        final, body, err = get_page(url)
        if err:
            result["errori"].append({"url": url, "errore": err})
            continue
        if not same_site(final, base) or final.rstrip("/") == base.rstrip("/"):
            continue
        page = parse(body)
        result["sezioni"].append({"url": final, "etichetta": label, "titolo": page.title})
        for u, title in links(page, final):
            if u.rstrip("/") == final.rstrip("/") or len(title) < 18:
                continue
            path = urlsplit(u).path
            if NEWS.search(path) or path.startswith(urlsplit(final).path.rstrip("/") + "/"):
                candidates.setdefault(u, title)
        a = article(page, final, label)
        if a and a not in result["articoli"]:
            result["articoli"].append(a)
    for url, title in list(candidates.items())[:max_articles]:
        final, body, err = get_page(url)
        if err:
            result["errori"].append({"url": url, "errore": err})
            continue
        if not same_site(final, base):
            continue
        a = article(parse(body), final, title)
        if a:
            if not any(x["url"] == a["url"] for x in result["articoli"]):
                result["articoli"].append(a)
        else:
            result["candidati_non_confermati"].append({"url": final, "titolo_link": title})
    result["stato"] = "Articoli con data confermata" if result["articoli"] else "Sezioni candidate senza articoli confermati" if result["sezioni"] else "Sezione non individuata"
    return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--all", action="store_true", help="Tutte le fonti anziché il gruppo pilota")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-articles", type=int, default=15)
    p.add_argument("--output", type=Path, default=ROOT / "rapporti" / "censimento_html.json")
    args = p.parse_args()
    if not 1 <= args.max_articles <= 50 or args.limit < 0:
        p.error("Limiti non validi")
    with (ROOT / "fonti.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not args.all:
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
