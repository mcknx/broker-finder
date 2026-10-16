"""Self-check for the data rules. python3 check.py -> prints 'ok' or fails on the first broken rule."""
import json, pathlib
from broker import dedupe, from_osm, norm_phone, norm_site, parse_labels, valid_email

assert norm_phone("(919) 555-0142") == norm_phone("+1-919-555-0142") == norm_phone("1 919 555 0142") == "+19195550142"
assert norm_phone("555-0142") is None and norm_phone("") is None

assert norm_site("HTTP://WWW.OakRidgeRealty.example/") == "oakridgerealty.example"
assert norm_site("pinecrestrealty.example") == "pinecrestrealty.example"
assert norm_site("https://www.bigbrand.example/office?id=3778") == "bigbrand.example/office?id=3778"

assert valid_email("info@oakridgerealty.example")
assert not valid_email("not-an-email") and not valid_email("")
# the shape of real junk from a real run (keys replaced): Wix pages embed Sentry DSNs that look like emails
assert not valid_email("0123456789abcdef0123456789abcdef@sentry-next.wixpress.com")
assert not valid_email("fedcba9876543210fedcba9876543210@sentry.io") and not valid_email("logo@2x.png")

a = {"osm_id": "node/1", "name": "Oak Ridge Realty", "website": "oakridge.example", "phone": None, "emails": []}
b = {"osm_id": "way/9", "name": "Oak Ridge Realty Inc", "website": "oakridge.example", "phone": "+19190000000", "emails": []}
c = {"osm_id": "node/2", "name": "Other", "website": None, "phone": "+19190000000", "emails": []}
d = {"osm_id": "node/3", "name": "Fresh", "website": "fresh.example", "phone": None, "emails": []}
# the shape of a real duplicate from a run: two OSM nodes, same name (different case), same spot, no website or phone
k1 = {"osm_id": "node/9000000001", "name": "Maple Street realty", "website": None, "phone": None, "emails": [], "lat": 35.7000001, "lon": -78.8000001}
k2 = dict(k1, osm_id="node/9000000002", name="Maple Street Realty ")
assert len(dedupe([k1, k2])) == 1
out = dedupe([a, b, c, d])
assert [r["osm_id"] for r in out] == ["node/1", "node/3"], out  # b matches a by website, c matches a by b's phone
assert out[0]["phone"] == "+19190000000"  # gap filled from the duplicate

ids = ["node/1", "node/2"]
assert parse_labels('[{"id":"node/1","category":"mortgage"},{"id":"node/2","category":"banana"}]', ids) == {"node/1": "mortgage", "node/2": "unknown"}
assert parse_labels("Sure! Here you go: ...", ids) == {"node/1": "unknown", "node/2": "unknown"}
# the fictional sample element maps to one clean row
row = from_osm(json.load(open(pathlib.Path(__file__).parent / "sample/overpass-element.sample.json")))
assert (row["name"], row["phone"], row["website"]) == ("Maple Street Realty", "+19195550142", "maplestreetrealty.example"), row
print("ok")
