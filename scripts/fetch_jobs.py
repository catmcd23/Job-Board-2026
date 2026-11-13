#!/usr/bin/env python3
"""Job Radar fetcher.

Reads companies.json, pulls open roles from each company's public
Greenhouse / Lever / Ashby job board, keeps sales/partnerships/GTM-ops
roles in NYC or US-remote, scores them, and writes data.json for the page.

Standard library only, so it runs on GitHub Actions with no installs.
"""
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "companies.json"
STATUS = ROOT / "status.json"
DATA = ROOT / "data.json"
SEEN = ROOT / "seen.json"

UA = {"User-Agent": "job-radar/1.0 (personal job search dashboard)"}
STALE_DAYS = 90

# ---------------------------------------------------------------- fetching

def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def plain(h):
    """HTML (possibly entity-escaped, as Greenhouse sends it) -> plain text."""
    if not h:
        return ""
    t = html.unescape(html.unescape(h))
    t = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<li[^>]*>", "\n• ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def from_greenhouse(slug):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not d or "jobs" not in d:
        return None
    out = []
    for j in d["jobs"]:
        out.append({
            "id": f"gh-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", "") or "",
            "remote_flag": False,
            "country": "",
            "workplace": "",
            "desc": plain(j.get("content")),
            "pay_hint": "",
            "posted": j.get("first_published") or j.get("updated_at"),
        })
    return out


def lever_pay(r):
    try:
        lo, hi, cur = r.get("min"), r.get("max"), r.get("currency") or "USD"
        if lo and hi and cur == "USD":
            return f"${int(lo)//1000}K–${int(hi)//1000}K"
    except Exception:
        pass
    return ""


def from_lever(slug):
    d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        out.append({
            "id": f"lv-{slug}-{j.get('id')}",
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": " / ".join(l for l in locs if l),
            "remote_flag": (j.get("workplaceType") == "remote"),
            "country": j.get("country") or "",
            "workplace": (j.get("workplaceType") or "").lower(),
            "desc": "\n".join([j.get("descriptionPlain") or plain(j.get("description"))]
                              + [f"{(l or {}).get('text','')}\n{plain((l or {}).get('content'))}" for l in (j.get("lists") or [])]
                              + [j.get("additionalPlain") or plain(j.get("additional"))]).strip(),
            "pay_hint": lever_pay(j.get("salaryRange")),
            "posted": ms_to_iso(j.get("createdAt")),
        })
    return out


def from_ashby(slug):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not d or "jobs" not in d:
        return None
    out = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        locs += [(s or {}).get("location", "") for s in (j.get("secondaryLocations") or [])]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out.append({
            "id": f"ab-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
            "location": " / ".join(l for l in locs if l),
            "remote_flag": bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            "country": addr.get("addressCountry") or "",
            "workplace": (j.get("workplaceType") or "").lower().replace("onsite", "on-site"),
            "desc": j.get("descriptionPlain") or plain(j.get("descriptionHtml")),
            "pay_hint": ((j.get("compensation") or {}).get("compensationTierSummary") or ""),
            "posted": j.get("publishedAt"),
        })
    return out


FETCHERS = {"greenhouse": from_greenhouse, "lever": from_lever, "ashby": from_ashby}
ATS_LABEL = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}


def fetch_company(c):
    """Try the listed ATS first, then the others. Returns (ats, jobs) or (None, None)."""
    order = [c["ats"]] if c.get("ats") in FETCHERS else []
    order += [a for a in FETCHERS if a not in order]
    for ats in order:
        jobs = FETCHERS[ats](c["slug"])
        # An empty board on the listed ATS still counts as "found";
        # an empty board on a guessed ATS is ignored (keeps looking).
        if jobs is not None and (jobs or ats == c.get("ats")):
            return ats, jobs
    return None, None

# ---------------------------------------------------------------- filtering

EXCLUDE = re.compile(
    r"\b(sdr|bdr|sales development|business development rep|intern|internship|"
    r"engineer|engineering|developer|scientist|designer|recruit|"
    r"vice president|vp|chief|svp|evp|counsel|accountant|marketing|"
    r"solutions consultant|sales consultant|support)\b", re.I)

CATEGORIES = [
    # (label, regex, base points)
    ("Deal Desk / GTM Ops", r"deal desk|revenue operations|rev ?ops|sales operations|sales ops|"
                           r"gtm (operations|ops|strategy|strategy & operations)|go[- ]to[- ]market (operations|strategy)|"
                           r"business operations|biz ?ops|strategy (&|and) operations|enablement|"
                           r"field productivity|commercial operations|sales strategy", 50),
    ("Partnerships / BD", r"partner|alliance|channel|business development|\bbd\b", 50),
    ("Founding / SMB AE", r"founding|\bsmb\b|small business|startup|emerging", 50),
    ("Mid-Market AE", r"mid[- ]?market|commercial", 50),
    ("Enterprise AE", r"enterprise|strategic|major accounts|named accounts|global accounts", 50),
]
AE = re.compile(r"account executive|\bae\b|sales executive|account director|"
                r"account manager|upsell|expansion|sales lead|sales representative|seller", re.I)


def categorize(title):
    t = title.lower()
    if EXCLUDE.search(t):
        return None, 0
    # Ops and partnerships roles don't need "account executive" in the title.
    for label, rx, pts in CATEGORIES[:2]:
        if re.search(rx, t):
            return label, pts
    if not AE.search(t):
        return None, 0
    for label, rx, pts in CATEGORIES[2:]:
        if re.search(rx, t):
            return label, pts
    if re.search(r"account manager|upsell|expansion", t):
        return "Account Manager / Expansion", 35
    return "Account Executive", 40


NYC = re.compile(r"new york|\bnyc\b|brooklyn|manhattan|jersey city|hoboken", re.I)
REMOTE = re.compile(r"remote|anywhere|distributed", re.I)
NON_US = re.compile(r"\b(uk|united kingdom|london|ireland|dublin|germany|berlin|munich|france|paris|"
                    r"netherlands|amsterdam|spain|madrid|barcelona|portugal|lisbon|poland|sweden|stockholm|"
                    r"emea|apac|latam|india|bangalore|singapore|japan|tokyo|australia|sydney|"
                    r"canada|toronto|vancouver|brazil|mexico|israel|tel aviv|europe)\b", re.I)
US = re.compile(r"\b(us|u\.s\.|usa|united states|north america|americas|east coast|eastern)\b", re.I)


def location_tag(job):
    loc = job["location"]
    if NYC.search(loc):
        return "NYC"
    remote = job["remote_flag"] or REMOTE.search(loc)
    if not remote:
        return None
    country = (job["country"] or "").lower()
    if country and country not in ("us", "usa", "united states", "united states of america"):
        return None
    if NON_US.search(loc) and not US.search(loc):
        return None
    return "Remote"


def days_old(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def score(cat_pts, tag, age):
    s = cat_pts
    s += 30 if tag == "NYC" else 20
    if age is not None:
        if age <= 7:
            s += 20
        elif age <= 30:
            s += 10
        elif age > STALE_DAYS:
            s -= 30
    return max(0, min(100, s))

# ---------------------------------------------------------------- office days & pay

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
N = r"(\d|one|two|three|four|five)"
OFFICE_WORDS = re.compile(r"office|on[- ]?site|in[- ]person|hybrid|in the hq|at hq|headquarters|onsite", re.I)
DAYS_PATTERNS = [
    re.compile(N + r"\s*(?:\+|or more)?\s*(?:(?:-|–|to)\s*(\d|two|three|four|five))?\s*days?\s*(?:a|per|/|each|every)?\s*(?:week|wk)", re.I),
    re.compile(N + r"\s*(?:\+\s*)?(?:(?:-|–|to)\s*(\d|two|three|four|five))?\s*days?\s*(?:in|at|from)\s*(?:the|our)?\s*(?:office|hq|nyc|on[- ]?site)", re.I),
    re.compile(r"(?:in[- ]office|on[- ]?site|in[- ]person)\s*(?:at least|minimum of|for)?\s*" + N + r"\s*(?:\+\s*)?days?", re.I),
    re.compile(N + r"x\s*(?:a|per|/)?\s*week", re.I),
]
FULL_OFFICE = re.compile(r"(fully|100%|full[- ]time)\s*(in[- ]person|in[- ]office|on[- ]?site)|"
                         r"in[- ]office\s*(full[- ]time|every day|daily)|"
                         r"(five|5) days (a|per) week|monday (through|thru|to|-|–) friday", re.I)
REMOTE_FIRST = re.compile(r"remote[- ]first|fully remote|100% remote|work from anywhere|distributed[- ]first", re.I)
WEEKDAYS = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|mon|tues?|wed|thurs?|fri)s?\b", re.I)


def office_days(desc, workplace, tag):
    """Returns (days or None, label, evidence sentence)."""
    sentences = re.split(r"(?<=[.!?])\s+|\n", desc or "")
    for snt in sentences:
        if not OFFICE_WORDS.search(snt):
            continue
        for rx in DAYS_PATTERNS:
            m = rx.search(snt)
            if m:
                n = m.group(1).lower()
                n = NUM.get(n, int(n) if n.isdigit() else 0)
                hi = (m.group(2) or "").lower() if m.re.groups > 1 else ""
                hi = NUM.get(hi, int(hi) if hi.isdigit() else 0)
                if 1 <= n <= 5:
                    label = f"{n}–{hi} days" if n < hi <= 5 else f"{n} days"
                    return n, label, snt.strip()[:220]
        if FULL_OFFICE.search(snt):
            return 5, "5 days", snt.strip()[:220]
        days = {d.group(1).lower()[:3] for d in WEEKDAYS.finditer(snt)}
        if len(days) >= 2 and re.search(r"office|on[- ]?site|in[- ]person", snt, re.I):
            return len(days), f"{len(days)} days", snt.strip()[:220]
    for snt in sentences:
        if REMOTE_FIRST.search(snt):
            return 0, "Remote", snt.strip()[:220]
    if workplace in ("on-site", "onsite"):
        return None, "On-site (days not stated)", ""
    if workplace == "hybrid":
        return None, "Hybrid (days not stated)", ""
    if workplace == "remote" or tag == "Remote":
        return 0, "Remote", ""
    if re.search(r"\bhybrid\b", desc or "", re.I):
        return None, "Hybrid (days not stated)", ""
    return None, "Not stated", ""


PAY = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])\s*(?:USD)?\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])")


def money(x):
    x = x.replace(",", "").replace(" ", "").lower()
    return int(float(x[:-1]) * 1000) if x.endswith("k") else int(x)


def pay_range(desc, hint):
    if hint:
        return hint
    best = None
    for m in PAY.finditer(desc or ""):
        lo, hi = money(m.group(1)), money(m.group(2))
        if 30000 <= lo < hi <= 1500000 and (best is None or hi > best[1]):
            best = (lo, hi)  # widest/highest range is usually OTE
    return f"${best[0]//1000}K–${best[1]//1000}K" if best else ""

# ---------------------------------------------------------------- main

def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    companies = load(COMPANIES, {}).get("companies", [])
    status = {k: v for k, v in load(STATUS, {}).items() if not k.startswith("_")}
    seen = set(load(SEEN, []))
    first_run = not seen

    postings, coverage, missing = [], [], []
    for c in companies:
        ats, jobs = fetch_company(c)
        if ats is None:
            missing.append(c["name"])
            print(f"  not found: {c['name']} ({c['slug']})", file=sys.stderr)
            continue
        coverage.append({"company": c["name"], "ats": ATS_LABEL[ats], "slug": c["slug"], "open_roles": len(jobs)})
        for j in jobs:
            cat, pts = categorize(j["title"])
            if not cat:
                continue
            tag = location_tag(j)
            if not tag:
                continue
            age = days_old(j["posted"])
            n_days, office_label, evidence = office_days(j["desc"], j["workplace"], tag)
            postings.append({
                "id": j["id"],
                "company": c["name"],
                "status": status.get(c["name"], ""),
                "title": j["title"].strip(),
                "url": j["url"],
                "category": cat,
                "location": j["location"],
                "tag": tag,
                "office_days": n_days,
                "office": office_label,
                "office_quote": evidence,
                "pay": pay_range(j["desc"], j["pay_hint"]),
                "posted": (j["posted"] or "")[:10],
                "age_days": age,
                "stale": age is not None and age > STALE_DAYS,
                "source": ATS_LABEL[ats],
                "score": score(pts, tag, age),
                "new": (not first_run) and j["id"] not in seen,
            })
        time.sleep(0.2)  # be polite to the job boards

    postings.sort(key=lambda p: (-p["score"], p["age_days"] if p["age_days"] is not None else 9999))
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "postings": postings,
        "coverage": sorted(coverage, key=lambda x: x["company"].lower()),
        "missing": sorted(missing, key=str.lower),
    }
    DATA.write_text(json.dumps(out, indent=1))
    SEEN.write_text(json.dumps(sorted(seen | {p["id"] for p in postings})))
    print(f"{len(postings)} matching roles across {len(coverage)} boards; "
          f"{sum(p['new'] for p in postings)} new; {len(missing)} companies not found")


if __name__ == "__main__":
    main()
