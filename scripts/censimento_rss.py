#!/usr/bin/env python3
"""Censimento tecnico RSS/Atom. Non modifica fonti.csv."""
import argparse
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000
UA = "OperaPlus-Monitoraggio/0.1 (public RSS discovery; github.com/Mehehe86/Opera-Plus-Monitoraggio)"

class Discovery(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and "alternate" in (a.get("rel") or "").lower().split() and (a.get("type") or "").lower().split(";")[0] in ("application/rss+xml", "application/atom+xml", "application/rdf+xml") and a.get("href"):
            self.feeds.append(a["href"])

def valid_url(url):
    p = urlsplit(url)
    return p.scheme in ("http", "https") and bool(p.hostname) and not p.username and not p.password

@lru_cache(maxsize=2048)
def fetch(url):
    if not valid_url(url):
        return "", b"", "URL non valido"
    try:
        time.sleep(0.35)
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html, */*;q=0.5"})
        with urlopen(req, timeout=12) as response:
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return response.geturl(), b"", "Risposta oltre limite"
            return response.geturl(), data, ""
    except HTTPError as exc:
        return url, b"", f"HTTP {exc.code}"
    except Exception as exc:
        return url, b"", f"{type(exc).__name__}: {str(exc)[:160]}"

def local(tag):
    return tag.rsplit("}", 1)[-1].lower()

def value(node, *names):
    for child in node:
        if local(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return ""

def date_iso(s):
    if not s:
        return ""
    try:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(s)
        return dt.isoformat() if dt else ""
    except (ValueError, TypeError, OverflowError):
        return ""

def parse_feed(data, base):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    kind = local(root.tag)
    if kind not in ("rss", "feed", "rdf"):
        return None
    entries = []
    for item in root.iter():
        if local(item.tag) not in ("item", "entry"):
            continue
        title = value(item, "title")
        link = value(item, "link")
        if not link:
            link = next((c.get("href", "") for c in item if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate"), "")
        link = urljoin(base, link) if link else ""
        if not valid_url(link):
            link = ""
        published = value(item, "pubdate", "published", "date", "issued")
        updated = value(item, "updated", "modified")
        entries.append({"titolo": title, "link": link, "pubblicazione": date_iso(published), "aggiornamento": date_iso(updated)})
    return entries

def inspect(row):
    site = row["sito_candidato"].strip()
    explicit = row["feed_candidato"].strip()
    result = {"fonte": row["fonte"], "categoria": row["categoria"], "sito_candidato": site, "sito_raggiunto": "", "errore_sito": "", "feed": [], "tentativi": [], "stato": ""}
    if not site and not explicit:
        result["stato"] = "URL da individuare"
        return result
    candidates = [explicit] if explicit else []
    if site:
        final, data, error = fetch(site)
        result["sito_raggiunto"] = final if not error else ""
        result["errore_sito"] = error
        if not error:
            parser = Discovery()
            parser.feed(data.decode("utf-8", errors="replace"))
            candidates.extend(urljoin(final, href) for href in parser.feeds)
            if parse_feed(data, final) is not None:
                candidates.append(final)
            origin = urljoin(final, "/")
            candidates.extend(urljoin(origin, p) for p in ("feed/", "rss.xml", "rss", "atom.xml", "index.xml"))
        else:
            origin = urljoin(site, "/")
            candidates.extend(urljoin(origin, p) for p in ("feed/", "rss.xml", "rss"))
    for url in list(dict.fromkeys(candidates))[:15]:
        final, data, error = fetch(url)
        if error:
            result["tentativi"].append({"url": url, "esito": error})
            continue
        entries = parse_feed(data, final)
        if entries is None:
            result["tentativi"].append({"url": url, "esito": "Non RSS/Atom valido"})
            continue
        usable = [e for e in entries if e["titolo"] and e["link"]]
        dated = [e for e in usable if e["pubblicazione"]]
        result["tentativi"].append({"url": url, "esito": "Feed leggibile"})
        if not any(f["url"] == final for f in result["feed"]):
            result["feed"].append({"url": final, "elementi": len(entries), "elementi_utilizzabili": len(usable), "con_data_pubblicazione": len(dated), "esempio": (dated or usable or [{}])[0]})
    if any(f["con_data_pubblicazione"] for f in result["feed"]):
        result["stato"] = "RSS collaudato"
    elif result["feed"]:
        result["stato"] = "RSS leggibile ma dati incompleti"
    elif result["errore_sito"]:
        result["stato"] = "Accesso impedito o fallito"
    else:
        result["stato"] = "RSS non individuato"
    return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=0, help="0 = tutte le fonti")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    p.add_argument("--category", default="")
    p.add_argument("--output", type=Path, default=ROOT / "rapporti" / "censimento.json")
    args = p.parse_args()
    if args.limit < 0 or args.offset < 0:
        p.error("limit e offset devono essere non negativi")
    with (ROOT / "fonti.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if args.category:
        rows = [r for r in rows if r["categoria"] == args.category]
    rows = rows[args.offset:]
    if args.limit:
        rows = rows[:args.limit]
    started = datetime.now(timezone.utc).isoformat()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row, result in zip(rows, pool.map(inspect, rows)):
            results.append(result)
            print(f'{len(results)}/{len(rows)} {row["fonte"]}: {result["stato"]}', flush=True)
    summary = {}
    for r in results:
        summary[r["stato"]] = summary.get(r["stato"], 0) + 1
    report = {"inizio_utc": started, "fine_utc": datetime.now(timezone.utc).isoformat(), "fonti_esaminate": len(results), "riepilogo": summary, "risultati": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
