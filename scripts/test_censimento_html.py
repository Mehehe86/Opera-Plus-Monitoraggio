import unittest
from censimento_html import parse, listing_articles, human_date, blocked

class ListingTests(unittest.TestCase):
    def test_torino(self):
        p = parse(b'<div>Comunicato stampa</div><a href="/area-stampa/comunicato-stampa/prova">Titolo comunicato di prova</a><div>20 novembre 2026</div>')
        a = listing_articles(p, "https://www.teatroregio.torino.it/area-stampa", "Teatro Regio Torino")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["data_pubblicazione"], "2026-11-20")
    def test_parigi(self):
        p = parse(b'<div>20 novembre 2026</div><a href="/actualites/prova">Titre du communique</a>')
        a = listing_articles(p, "https://www.operadeparis.fr/actualites/communiques", "Opéra national de Paris")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["data_pubblicazione"], "2026-11-20")
    def test_parigi_date_in_link(self):
        p = parse(b'<a href="/actualites/prova">20 novembre 2026 Titre du communique</a>')
        self.assertEqual(len(listing_articles(p, "https://www.operadeparis.fr/actualites/communiques", "Opéra national de Paris")), 1)
    def test_missing_date(self):
        p = parse(b'<a href="/actualites/prova">Titre du communique</a>')
        self.assertEqual(listing_articles(p, "https://www.operadeparis.fr/actualites/communiques", "Opéra national de Paris"), [])
    def test_invalid_date(self):
        self.assertEqual(human_date("31 febbraio 2026"), "")
    def test_security(self):
        self.assertTrue(blocked(parse(b"<title>Security check required</title>")))

if __name__ == "__main__":
    unittest.main()
