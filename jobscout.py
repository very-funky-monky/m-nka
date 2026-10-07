#!/usr/bin/env python3
"""
Zeneipari állásfigyelő (Magyarország)
=====================================
Futtatás:   python3 jobscout.py        (Windowson: python jobscout.py)
Eredmény:   jobscout_report.html nyílik meg a böngészőben.

Nem kell hozzá telepíteni semmit (csak Python 3.8+).
Az "új" jelölés a jobscout_seen.json alapján működik, ami a szkript mellé
kerül mentésre. Töröld a fájlt, ha mindent újként akarsz látni.

FONTOS: a portálok és cégoldalak felépítése változik, és a lenti URL-mintákat
nem tudtam élesben letesztelni. A jelentés "Források állapota" része mutatja,
hogy melyik működött. Ami nem, azt a konfigurációban egyszerűen át lehet írni.
"""

import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

# ----------------------------------------------------------------------------
# KONFIGURÁCIÓ: ezt nyugodtan szerkeszd
# ----------------------------------------------------------------------------

# Kulcsszavak a portálos kereséshez
KEYWORDS = [
    "zene", "fesztivál", "booking", "lemezkiadó", "koncert",
    "A&R", "zenei PR", "zenei marketing", "label manager", "produkciós asszisztens koncert",
]

# Álláshirdetési portálok. {kw} helyére a kulcsszó kerül (URL-kódolva).
# link_contains: csak az ilyen szöveget tartalmazó linkeket tekintjük hirdetésnek.
PORTALS = [
    {
        "name": "Profession.hu",
        "url": "https://www.profession.hu/allasok/1,0,0,{kw}",
        "link_contains": ["/allas/"],
    },
    {
        "name": "Jobline",
        "url": "https://www.jobline.hu/allasok/{kw}",
        "link_contains": ["/allas/"],
    },
    {
        "name": "Indeed (gyakran blokkolja a robotokat)",
        "url": "https://hu.indeed.com/jobs?q={kw}&l=Budapest",
        "link_contains": ["/viewjob", "/rc/clk", "/pagead"],
    },
]

# Konkrét szereplők honlapjai. A szkript megkeresi rajtuk az állás/karrier
# linkeket, megnézi az oldalt, és jelzi, ha változott a legutóbbi futtatás óta.
WATCHLIST = [
    ("Sziget", "https://szigetfestival.com"),
    ("VOLT Fesztivál", "https://voltfestival.hu"),
    ("Balaton Sound", "https://balatonsound.com"),
    ("Budapest Park", "https://www.budapestpark.hu"),
    ("A38", "https://www.a38.hu"),
    ("Müpa", "https://www.mupa.hu"),
    ("Petőfi Csarnok", "https://www.petoficsarnok.hu"),
    ("Akvárium Klub", "https://akvarium.net"),
    ("Hangfoglaló", "https://hangfoglalo.hu"),
    ("Mahasz", "https://www.mahasz.hu"),
    ("Artisjus", "https://www.artisjus.hu"),
    ("Universal Music Hungary", "https://www.universalmusic.hu"),
    ("Sony Music Hungary", "https://www.sonymusic.hu"),
    ("Warner Music Hungary", "https://www.warnermusic.hu"),
    ("Hungaroton", "https://www.hungaroton.hu"),
    ("Tilos Rádió", "https://tilos.hu"),
]

# Ezek a szavak jelzik egy linken, hogy állás/karrier oldalról van szó
CAREER_WORDS = ["állás", "allas", "karrier", "career", "jobs", "job ", "csatlakozz",
                "munkatárs", "pályázat", "join us", "work with us", "dolgozz"]

# Ezek a szavak jelzik egy cégoldali linkszövegben, hogy konkrét pozíció
ROLE_WORDS = ["manager", "menedzser", "koordinátor", "asszisztens", "specialist",
              "szakértő", "producer", "booking", "pr ", "marketing", "social",
              "tartalom", "content", "munkatárs", "gyakornok", "intern", "szerkesztő",
              "a&r", "label", "produkció", "fesztivál", "festival", "zene"]

# A találatok rendezésekor ezek a szavak adnak pluszpontot a címben
MUSIC_WORDS = ["zene", "music", "fesztivál", "festival", "koncert", "concert",
               "booking", "kiadó", "label", "a&r", "rádió", "radio", "hang"]

MAX_CAREER_PAGES_PER_SITE = 3
REQUEST_TIMEOUT = 15
PAUSE_BETWEEN_REQUESTS = 0.6  # másodperc, hogy ne terheljük a szervereket

# Kézi keresési linkek (ezeket nem scrapeljük, csak megnyitható linkeket adunk)
MANUAL_SEARCHES = [
    ("LinkedIn: zenei állások Magyarországon",
     "https://www.linkedin.com/jobs/search/?keywords={kw}&location=Hungary"),
    ("Indeed Budapest",
     "https://hu.indeed.com/jobs?q={kw}&l=Budapest"),
    ("Google: állás zeneipar",
     "https://www.google.com/search?q={kw}+%C3%A1ll%C3%A1s+Budapest"),
    ("Facebook-csoportok és oldalak",
     "https://www.facebook.com/search/posts?q={kw}%20%C3%A1ll%C3%A1s"),
]
MANUAL_KEYWORDS = ["zeneipar", "fesztivál booking", "lemezkiadó", "music industry"]

# ----------------------------------------------------------------------------
# Technikai rész
# ----------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "jobscout_seen.json"
REPORT_FILE = HERE / "jobscout_report.html"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class LinkParser(HTMLParser):
    """Összegyűjti az <a> linkeket a szövegükkel együtt."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None
            self._text = []


def fetch(url):
    """Letölti az oldalt, és szövegként visszaadja."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def extract_links(page_html, base_url):
    parser = LinkParser()
    parser.feed(page_html)
    out = []
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        out.append((urllib.parse.urljoin(base_url, href), text))
    return out


def score(title):
    t = title.lower()
    return sum(1 for w in MUSIC_WORDS if w in t)


def scan_portals(results, status):
    for portal in PORTALS:
        found_total = 0
        error = None
        for kw in KEYWORDS:
            url = portal["url"].format(kw=urllib.parse.quote_plus(kw))
            try:
                page = fetch(url)
                for link, text in extract_links(page, url):
                    if len(text) < 6:
                        continue
                    if not any(p in link for p in portal["link_contains"]):
                        continue
                    if link not in results:
                        results[link] = {
                            "title": text, "source": portal["name"],
                            "kind": "portál", "keyword": kw,
                        }
                        found_total += 1
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                break  # ha az első lekérés elhasal, nincs értelme tovább próbálni
            time.sleep(PAUSE_BETWEEN_REQUESTS)
        if error:
            status.append((portal["name"], "hiba", error))
        elif found_total == 0:
            status.append((portal["name"], "0 találat",
                           "Lehet, hogy az URL-minta vagy a link_contains nem stimmel, vagy blokkolt."))
        else:
            status.append((portal["name"], "ok", f"{found_total} találat"))


def find_career_links(home_html, home_url):
    """A főoldalon megkeresi az állás/karrier linkeket."""
    careers = []
    for link, text in extract_links(home_html, home_url):
        hay = (text + " " + link).lower()
        if any(w in hay for w in CAREER_WORDS) and link not in careers:
            careers.append(link)
    return careers[:MAX_CAREER_PAGES_PER_SITE]


def scan_watchlist(results, status, page_hashes, new_hashes):
    for name, home in WATCHLIST:
        try:
            home_html = fetch(home)
        except Exception as exc:  # noqa: BLE001
            status.append((name, "hiba", f"{type(exc).__name__}: {exc}"))
            continue
        time.sleep(PAUSE_BETWEEN_REQUESTS)

        careers = find_career_links(home_html, home)
        if not careers:
            status.append((name, "nincs karrier link",
                           "A főoldalon nem találtam állás/karrier linket. Érdemes kézzel megnézni."))
            continue

        notes = []
        for cpage in careers:
            try:
                page = fetch(cpage)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{cpage} → {type(exc).__name__}")
                continue
            time.sleep(PAUSE_BETWEEN_REQUESTS)

            # Változásfigyelés: a látható szöveg hash-e
            body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
            h = hashlib.sha1(body.encode("utf-8", errors="ignore")).hexdigest()
            new_hashes[cpage] = h
            changed = page_hashes.get(cpage) not in (None, h)
            first_time = cpage not in page_hashes

            # A karrieroldal maga is bekerül találatként
            results[cpage] = {
                "title": f"{name}: karrieroldal"
                         + (" (VÁLTOZOTT az utolsó futtatás óta)" if changed else "")
                         + (" (első alkalom, még nincs összehasonlítás)" if first_time else ""),
                "source": name, "kind": "cégoldal", "changed": changed,
            }
            # Az oldalon talált, pozíciónak tűnő linkek
            for link, text in extract_links(page, cpage):
                tl = text.lower()
                if 6 <= len(text) <= 120 and any(w in tl for w in ROLE_WORDS) and link != cpage:
                    results.setdefault(link, {
                        "title": text, "source": name, "kind": "cégoldal",
                    })
        status.append((name, "ok", f"{len(careers)} karrieroldal ellenőrizve"
                       + (f"; gondok: {'; '.join(notes)}" if notes else "")))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def render_report(results, seen_before, status, today):
    rows_new, rows_old = [], []
    for url, info in results.items():
        is_new = url not in seen_before
        info["is_new"] = is_new
        info["score"] = score(info["title"]) + (5 if info.get("changed") else 0)
        (rows_new if is_new else rows_old).append((url, info))

    def key(item):
        return (-item[1]["score"], item[1]["source"], item[1]["title"])
    rows_new.sort(key=key)
    rows_old.sort(key=key)

    def row(url, info):
        badge = '<span class="new">ÚJ</span>' if info["is_new"] else ""
        return (f'<tr><td>{badge}</td>'
                f'<td><a href="{html.escape(url)}" target="_blank" rel="noopener">'
                f'{html.escape(info["title"])}</a></td>'
                f'<td>{html.escape(info["source"])}</td>'
                f'<td>{html.escape(info["kind"])}</td></tr>')

    def table(rows):
        if not rows:
            return "<p class='muted'>Nincs.</p>"
        return ("<table><tr><th></th><th>Cím</th><th>Forrás</th><th>Típus</th></tr>"
                + "".join(row(u, i) for u, i in rows) + "</table>")

    status_rows = "".join(
        f"<tr><td>{html.escape(n)}</td><td class='{'ok' if s == 'ok' else 'bad'}'>{html.escape(s)}</td>"
        f"<td>{html.escape(d)}</td></tr>" for n, s, d in status)

    manual = []
    for label, tpl in MANUAL_SEARCHES:
        links = " · ".join(
            f'<a href="{html.escape(tpl.format(kw=urllib.parse.quote_plus(k)))}" '
            f'target="_blank" rel="noopener">{html.escape(k)}</a>' for k in MANUAL_KEYWORDS)
        manual.append(f"<li><b>{html.escape(label)}:</b> {links}</li>")

    return f"""<!doctype html>
<html lang="hu"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zeneipari állások, {today}</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;line-height:1.45;color:#1b1b1b}}
 h1{{margin-bottom:.2rem}} h2{{margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}}
 table{{border-collapse:collapse;width:100%}} td,th{{padding:.4rem .5rem;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
 .new{{background:#d9480f;color:#fff;border-radius:4px;padding:1px 6px;font-size:.75rem;font-weight:700}}
 .muted{{color:#777}} .ok{{color:#2b8a3e}} .bad{{color:#c92a2a}}
 @media (prefers-color-scheme: dark){{body{{background:#161616;color:#e8e8e8}} td,th{{border-color:#333}} h2{{border-color:#333}} a{{color:#74c0fc}}}}
</style></head><body>
<h1>Zeneipari állások, Magyarország</h1>
<p class="muted">Futtatva: {today} · {len(rows_new)} új, {len(rows_old)} korábban már látott</p>
<h2>Új találatok</h2>{table(rows_new)}
<h2>Korábban látott találatok</h2>{table(rows_old)}
<h2>Kézi keresési linkek (Facebook, LinkedIn, Google)</h2>
<p class="muted">Ezeket a platformok tiltják vagy blokkolják a robotos lekérdezést, ezért itt csak megnyitható keresések vannak.</p>
<ul>{''.join(manual)}</ul>
<h2>Források állapota</h2>
<table><tr><th>Forrás</th><th>Állapot</th><th>Részlet</th></tr>{status_rows}</table>
</body></html>"""


def main():
    today = date.today().isoformat()
    seen = load_json(SEEN_FILE, {"jobs": {}, "pages": {}})
    seen_jobs = seen.get("jobs", {})
    page_hashes = seen.get("pages", {})

    results, status, new_hashes = {}, [], {}
    print("Portálok keresése…")
    scan_portals(results, status)
    print("Cégoldalak ellenőrzése…")
    scan_watchlist(results, status, page_hashes, new_hashes)

    REPORT_FILE.write_text(render_report(results, seen_jobs, status, today), encoding="utf-8")

    for url in results:
        seen_jobs.setdefault(url, today)
    page_hashes.update(new_hashes)
    SEEN_FILE.write_text(json.dumps({"jobs": seen_jobs, "pages": page_hashes},
                                    ensure_ascii=False, indent=1), encoding="utf-8")

    n_new = sum(1 for u in results if results[u].get("is_new"))
    print(f"Kész: {len(results)} találat, ebből {n_new} új. Jelentés: {REPORT_FILE}")
    if "--no-open" not in sys.argv:
        webbrowser.open(REPORT_FILE.as_uri())


if __name__ == "__main__":
    main()
