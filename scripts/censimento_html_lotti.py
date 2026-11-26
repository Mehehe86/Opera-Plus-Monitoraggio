"""Censimento HTML a lotti, con checkpoint e riepilogo."""
import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from censimento_html import inspect, ROOT

def selected():
    with (ROOT / "fonti.csv").open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r["sito_candidato"].strip() and not (
            r["stato"] == "RSS collaudato" and r.get("valutazione_rss") == "Candidato operativo; copertura da verificare")]

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int)
    p.add_argument("--size", type=int, default=20)
    p.add_argument("--directory", type=Path, default=ROOT / "rapporti" / "html_lotti")
    p.add_argument("--merge", action="store_true")
    p.add_argument("--plan", action="store_true")
    a = p.parse_args()
    if a.size < 1:
        p.error("Dimensione lotto non valida")
    rows = selected()
    batches = (len(rows) + a.size - 1) // a.size
    if a.plan:
        print(json.dumps(list(range(batches))))
        return
    if a.merge:
        found = {}
        for path in sorted(a.directory.glob("lotto-*.json")):
            for r in json.loads(path.read_text(encoding="utf-8"))["risultati"]:
                found[r["fonte"]] = r
        results = [found[r["fonte"]] for r in rows if r["fonte"] in found]
        missing = [r["fonte"] for r in rows if r["fonte"] not in found]
        summary = dict(Counter(r["stato"] for r in results))
        report = {"data_utc": datetime.now(timezone.utc).isoformat(), "fonti_previste": len(rows), "fonti_esaminate": len(results), "non_esaminate": missing, "riepilogo": summary, "risultati": results}
        write_json(a.directory / "riepilogo.json", report)
        print(json.dumps({k:v for k,v in report.items() if k != "risultati"}, ensure_ascii=False))
        if missing:
            raise SystemExit(1)
        return
    if a.batch is None or not 0 <= a.batch < batches:
        p.error("Lotto non valido")
    results = []
    group = rows[a.batch*a.size:(a.batch+1)*a.size]
    for i, row in enumerate(group, 1):
        try:
            r = inspect(row, 10)
        except Exception as exc:
            r = {"fonte": row["fonte"], "stato": "Errore di analisi", "articoli": [], "errori": [{"errore": str(exc)[:200]}]}
        results.append(r)
        write_json(a.directory / f"lotto-{a.batch:03}.json", {"lotto": a.batch, "risultati": results})
        print(f'{i}/{len(group)} {row["fonte"]}: {r["stato"]} ({len(r["articoli"])} articoli)', flush=True)

if __name__ == "__main__":
    main()
