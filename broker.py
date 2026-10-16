#!/usr/bin/env python3.12
"""broker-finder: real-estate offices in one US county, as a clean lead table.

  OpenStreetMap (Overpass API, ODbL)  ->  polite homepage scrape (robots.txt first)
  ->  Claude classifies each office  ->  normalize + dedupe  ->  SQLite + CSV

  python3.12 broker.py "Wake County" "North Carolina" [--limit 40]

Python 3.12 stdlib only. Classification shells out to the local Claude Code CLI (`claude -p`).
"""
import csv, json, re, sqlite3, ssl, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request, urllib.robotparser

UA = "broker-finder/0.1 (one-off demo by McKeen Asma; respects robots.txt)"
# Public Overpass instances listed on the OSM wiki. The main one returns 502/504 when busy, so fall through to the next.
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
CATS = {"residential", "commercial", "property_management", "mortgage", "other"}
T0 = time.time()
if ssl.OPENSSL_VERSION.startswith("LibreSSL"):  # macOS /usr/bin/python3: overpass-api.de rejects its TLS handshake
    sys.exit(f"{ssl.OPENSSL_VERSION} can't talk to modern TLS servers; run with python3.12 (OpenSSL 3)")


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


# ---------- C. API: OpenStreetMap Overpass ----------
def overpass(county, state):
    q = f"""[out:json][timeout:90];
area["name"="{state}"]["admin_level"="4"]->.st;
area["name"="{county}"]["admin_level"="6"](area.st)->.a;
( nwr(area.a)["office"="estate_agent"];
  nwr(area.a)["shop"="estate_agent"];
  nwr(area.a)["office"="property_management"]; );
out center tags;"""
    body = urllib.parse.urlencode({"data": q}).encode()
    for attempt, url in enumerate(OVERPASS * 2):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            return json.load(urllib.request.urlopen(req, timeout=120))["elements"]
        except (urllib.error.URLError, TimeoutError) as e:  # HTTPError (502/504/429) is a URLError
            log(f"  {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'code', None) or type(e).__name__}, trying next server")
            time.sleep(5 * (attempt + 1))
    sys.exit("every Overpass server failed")


def from_osm(el):
    t = el.get("tags", {})
    c = el.get("center", el)  # ways/relations come back with a center point
    street = " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")]))
    return {
        "osm_id": f"{el['type']}/{el['id']}",
        "name": t.get("name") or t.get("brand") or "",
        "address": ", ".join(filter(None, [street, t.get("addr:city"), t.get("addr:postcode")])),
        "phone": norm_phone(t.get("phone") or t.get("contact:phone")),
        "website": norm_site(t.get("website") or t.get("contact:website")),
        "emails": [e for e in [t.get("email") or t.get("contact:email")] if valid_email(e)],
        "lat": c.get("lat"), "lon": c.get("lon"),
    }


# ---------- D. Data: normalize, validate, dedupe ----------
def norm_phone(p):
    """Any US format -> E.164 (+19195550142); None if it isn't a 10-digit US number."""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return "+1" + d if len(d) == 10 and d[0] not in "01" else None


def norm_site(u):
    """'HTTP://WWW.Foo.com/' -> 'foo.com'. Keeps path+query: brands share a domain across branches."""
    if not u:
        return None
    p = urllib.parse.urlsplit(u.strip() if "://" in u else "http://" + u.strip())
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") + ("?" + p.query if p.query else "")
    return host + path if host else None


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


JUNK_EMAIL = re.compile(r"^[0-9a-f]{32}@|@(sentry[\w.-]*|[\w.-]*wixpress\.com)$|\.(png|jpe?g|gif|svg|webp)$", re.I)


def valid_email(e):
    """Well-formed and a real contact: not a Sentry DSN key (hex@sentry.io) or an image name (logo@2x.png)."""
    return bool(e) and bool(EMAIL.fullmatch(e)) and not JUNK_EMAIL.search(e)


def dedupe(rows):
    """Same OSM id, website, phone, or same name within ~11 m = same office. First row wins, later rows fill its gaps."""
    seen, out = {}, []
    for r in rows:
        spot = r.get("lat") and f"n:{r['name'].strip().lower()}@{r['lat']:.4f},{r['lon']:.4f}"
        keys = [k for k in (r["osm_id"], r["website"] and "w:" + r["website"], r["phone"] and "p:" + r["phone"], spot) if k]
        kept = next((seen[k] for k in keys if k in seen), None)
        if kept is None:
            out.append(r)
            kept = r
        else:
            for f, v in r.items():
                if not kept.get(f) and v:
                    kept[f] = v
        for k in keys:
            seen[k] = kept
    return out


# ---------- B. Scraping: robots.txt first, homepage + /contact only ----------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(1_500_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def robots_allows(url):
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.parse(fetch(base + "/robots.txt").splitlines())
    except urllib.error.HTTPError as e:
        return e.code not in (401, 403)  # same rule as the stdlib: no robots.txt = allowed, forbidden = not
    return rp.can_fetch(UA, url)


def extract(html):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", html)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return {
        "title": re.sub(r"\s+", " ", title.group(1)).strip()[:120] if title else "",
        "snippet": re.sub(r"\s+", " ", text).strip()[:300],
        "emails": sorted({e for e in EMAIL.findall(html) if valid_email(e)}),
        "phones": sorted({p for p in map(norm_phone, re.findall(r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", text)) if p}),
    }


def scrape(row, stats):
    home = "http://" + row["website"]
    pages = [home]
    try:
        if not robots_allows(home):
            stats["robots_skipped"] += 1
            return log(f"robots.txt disallows  {row['website'][:50]}  -> skipped")
        html = fetch(home)
        link = re.search(r'href="([^"]*contact[^"]*)"', html, re.I)
        if link:
            contact = urllib.parse.urljoin(home, link.group(1))
            if urllib.parse.urlsplit(contact).netloc == urllib.parse.urlsplit(home).netloc and robots_allows(contact):
                time.sleep(1)
                html += fetch(contact)
                pages.append(contact)
    except Exception as e:  # dead sites, TLS errors, timeouts: log and move on
        stats["fetch_failed"] += 1
        return log(f"fetch failed  {row['website'][:50]}  ({type(e).__name__})")
    x = extract(html)
    row.update(title=x["title"], snippet=x["snippet"], source_url=" ".join(pages))
    row["emails"] = sorted(set(row["emails"]) | set(x["emails"]))
    row["phone"] = row["phone"] or (x["phones"][0] if x["phones"] else None)
    stats["scraped"] += 1
    log(f"scraped  {row['website'][:45]:45}  {len(pages)} page(s)  {len(x['emails'])} email  {len(x['phones'])} phone")


# ---------- A. Claude: classify from name + homepage title/text ----------
PROMPT = """Classify each US real-estate business by its name and homepage text.
Categories: residential, commercial, property_management, mortgage, other.
Return ONLY a JSON array, no prose: [{"id": "<id>", "category": "<category>"}]

"""


def parse_labels(raw, ids):
    """Strict: a JSON array of {id, category}, ids from this batch, categories from CATS. Anything else -> unknown."""
    out = dict.fromkeys(ids, "unknown")
    try:
        for item in json.loads(raw):
            if item.get("id") in out and item.get("category") in CATS:
                out[item["id"]] = item["category"]
    except (ValueError, TypeError, AttributeError):
        pass
    return out


def ask_claude(items):
    """One `claude -p` call to the local Claude Code CLI (Opus). Returns the model's raw text, '' if the CLI fails."""
    res = subprocess.run(
        ["claude", "-p", "--model", "opus", "--output-format", "json", "--tools", "", "--setting-sources", "",
         "--no-session-persistence", "--system-prompt", "You label businesses. Output only JSON."],
        input=PROMPT + json.dumps(items), capture_output=True, text=True, timeout=180)
    try:
        return json.loads(res.stdout).get("result", "")
    except ValueError:
        return ""


def classify(rows, batch=20):
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        items = [{"id": r["osm_id"], "name": r["name"], "title": r.get("title", ""), "text": r.get("snippet", "")[:200]} for r in chunk]
        labels = parse_labels(ask_claude(items), [r["osm_id"] for r in chunk])
        for r in chunk:
            r["category"] = labels[r["osm_id"]]
        log(f"claude -p  batch {i // batch + 1}: {sum(v != 'unknown' for v in labels.values())}/{len(chunk)} labelled")


# ---------- store ----------
COLS = ["osm_id", "name", "category", "address", "phone", "website", "emails", "lat", "lon", "title", "source_url", "county"]


def save(rows, county, db="offices.db", out_csv="offices.csv"):
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE IF NOT EXISTS offices ({', '.join(c + (' TEXT PRIMARY KEY' if c == 'osm_id' else '') for c in COLS)})")
    con.executemany(  # upsert: re-running a county updates rows instead of duplicating them
        f"INSERT INTO offices VALUES ({','.join('?' * len(COLS))}) ON CONFLICT(osm_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in COLS[1:]),
        [[county if c == "county" else ";".join(r[c]) if c == "emails" else r.get(c) for c in COLS] for r in rows])
    con.commit()
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(con.execute(f"SELECT {','.join(COLS)} FROM offices ORDER BY category, name"))
    return con.execute("SELECT count(*) FROM offices").fetchone()[0]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    county, state = (args + ["Wake County", "North Carolina"][len(args):])[:2]
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 40
    stats = dict.fromkeys(["fetched", "deduped", "scraped", "robots_skipped", "fetch_failed", "classified", "unknown"], 0)

    log(f"Overpass API: real-estate offices in {county}, {state}")
    rows = [from_osm(e) for e in overpass(county, state)]
    rows = [r for r in rows if r["name"]]  # an office with no name is not a lead
    stats["fetched"] = len(rows)
    log(f"Overpass returned {len(rows)} named offices")
    rows = dedupe(rows)
    stats["deduped"] = stats["fetched"] - len(rows)
    log(f"dedupe: {len(rows)} unique ({stats['deduped']} duplicates merged)")

    for r in [r for r in rows if r["website"]][:limit]:
        scrape(r, stats)
        time.sleep(1)  # at least 1 s between sites
    classify(rows)
    stats["classified"] = sum(r["category"] != "unknown" for r in rows)
    stats["unknown"] = len(rows) - stats["classified"]
    stats["in_db"] = save(rows, county)
    stats["seconds"] = round(time.time() - T0, 1)
    log("done " + json.dumps(stats))
    json.dump(stats, open("run.json", "w"), indent=2)


if __name__ == "__main__":
    main()
