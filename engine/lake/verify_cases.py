"""Check every key-paper DOI in a cases file against Europe PMC before building lakes.
A title-only match can return a letter, erratum or comment that carries the original's title,
so the DOI is what is checked. Run: python3 engine/lake/verify_cases.py packs/deep-research/cases-shapes.json"""
import json, sys, time, urllib.parse, urllib.request
bad = 0
for c in json.load(open(sys.argv[1]))["cases"]:
    print("==", c["id"])
    for kp in c["key_papers"]:
        q = urllib.parse.quote(f'DOI:"{kp["doi"]}"')
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json&resultType=lite"
        try:
            r = json.loads(urllib.request.urlopen(url, timeout=30).read())["resultList"]["result"]
        except Exception as ex:
            print(f"   ERROR {kp['doi']}: {ex}"); bad += 1; continue
        if not r:
            print(f"   MISSING {kp['doi']}  ({kp['label']})"); bad += 1; continue
        t = r[0].get("title", "")
        ok = kp["title"].lower()[:30] in t.lower()
        bad += not ok
        print(f"   {'ok  ' if ok else 'TITLE?'} {kp['doi']:32} pmid {r[0].get('pmid','-'):9} {t[:70]}")
        time.sleep(0.4)
print("ALL VERIFIED" if not bad else f"{bad} PROBLEMS - fix before building")
sys.exit(1 if bad else 0)
