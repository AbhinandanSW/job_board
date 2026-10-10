#!/usr/bin/env python3
"""Job opening tracker.

Pulls openings from public, API-based job feeds (company ATS boards plus two
remote-job APIs), filters by title / skills / location and writes JOBS.md.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = yaml.safe_load((HERE / "config.yml").read_text())
SEEN_PATH = HERE / "seen.json"
REPORT_PATH = ROOT / "JOBS.md"
CACHE_PATH = HERE / "apify_cache.json"
APIFY = CFG.get("apify") or {}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = CFG.get("user_agent", "job-tracker/1.0")


def warn(msg):
    print(f"::warning::{msg}", file=sys.stderr)


def get_json(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------- fetchers ------------------------------------
def greenhouse(token):
    data = get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        params={"content": "true"},
    )
    for j in data.get("jobs", []):
        yield dict(
            id=f"gh:{token}:{j['id']}",
            title=j["title"],
            company=j.get("company_name") or token,
            location=(j.get("location") or {}).get("name", ""),
            url=j["absolute_url"],
            desc=clean(j.get("content")),
            remote=False,
            posted=j.get("first_published"),
        )


def lever(slug):
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}):
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        extra = " ".join(
            clean(f"{x.get('text', '')} {x.get('content', '')}") for x in j.get("lists", [])
        )
        yield dict(
            id=f"lever:{slug}:{j['id']}",
            title=j["text"],
            company=slug,
            location=" / ".join(locs),
            url=j["hostedUrl"],
            desc=f"{j.get('descriptionPlain', '')} {extra}",
            remote=j.get("workplaceType") == "remote",
            posted=j.get("createdAt"),
        )


def ashby(name):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""]
        for s in j.get("secondaryLocations") or []:
            locs.append(s.get("location", "") if isinstance(s, dict) else str(s))
        yield dict(
            id=f"ashby:{name}:{j['id']}",
            title=j["title"],
            company=name,
            location=" / ".join(l for l in locs if l),
            url=j.get("jobUrl", ""),
            desc=j.get("descriptionPlain") or clean(j.get("descriptionHtml")),
            remote=bool(j.get("isRemote")),
            posted=j.get("publishedAt"),
        )


def remotive(query):
    data = get_json(
        "https://remotive.com/api/remote-jobs",
        params={"category": "software-dev", "search": query},
    )
    for j in data.get("jobs", []):
        yield dict(
            id=f"remotive:{j['id']}",
            title=j["title"],
            company=j["company_name"],
            location=j.get("candidate_required_location") or "Remote",
            url=j["url"],
            desc=clean(j.get("description")),
            remote=True,
            posted=j.get("publication_date"),
        )


def remoteok(_=None):
    for j in get_json("https://remoteok.com/api")[1:]:  # first item is a legal notice
        yield dict(
            id=f"remoteok:{j['id']}",
            title=j.get("position", ""),
            company=j.get("company", ""),
            location=j.get("location") or "Remote",
            url=j.get("url", ""),
            desc=f"{clean(j.get('description'))} {' '.join(j.get('tags') or [])}",
            remote=True,
            posted=j.get("date"),
        )


def collect():
    src = CFG["sources"]
    plan = [
        ("greenhouse", greenhouse, src.get("greenhouse", [])),
        ("lever", lever, src.get("lever", [])),
        ("ashby", ashby, src.get("ashby", [])),
        ("remotive", remotive, src.get("remotive_queries") or CFG.get("search_terms", [])[:3]),
        ("remoteok", remoteok, [None] if src.get("remoteok") else []),
    ]
    jobs = []
    for name, fn, args in plan:
        for a in args:
            try:
                jobs.extend(fn(a))
            except Exception as e:  # one bad source must not kill the run
                warn(f"{name} {a or ''} failed: {e}")
    return jobs


# ----------------------------- filters -------------------------------------
TITLE_INC = [re.compile(p, re.I) for p in CFG["title_include"]]
TITLE_EXC = [re.compile(p, re.I) for p in CFG["title_exclude"]]
SKILLS = {
    s: re.compile(rf"(?<!\w){re.escape(s)}(?!\w)", re.I) for s in CFG["skills"]
}
LOCS = [l.lower() for l in CFG["locations"]]
BLOCKED_REGION = re.compile(
    r"\b(us|usa|u\.s\.|united states|uk|canada|emea|europe|latam|north america|germany|brazil)\b", re.I
)
OPEN_REGION = re.compile(r"\b(india|anywhere|worldwide|global|apac|asia)\b", re.I)


def title_ok(title):
    return any(p.search(title) for p in TITLE_INC) and not any(p.search(title) for p in TITLE_EXC)


def location_ok(job):
    if CFG.get("location_mode", "filter") == "any":
        return True
    loc = job["location"].lower()
    if any(k in loc for k in LOCS):
        return True
    if CFG.get("remote_ok") and (job["remote"] or "remote" in loc):
        if BLOCKED_REGION.search(loc) and not OPEN_REGION.search(loc):
            return False
        return True
    return False


EXP_CFG = CFG.get("experience") or {}
EXP_MIN, EXP_MAX = EXP_CFG.get("min", 3), EXP_CFG.get("max", 4)
EXP_RE = re.compile(r"(\d{1,2})\s*(?:(?:-|–|—|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b", re.I)
MUST = {s.lower() for s in CFG.get("must_have_any_skill", [])}


def parse_experience(text, trusted=False):
    """Return (lo, hi) years required (hi None = open-ended) or None.

    Untrusted free text must mention 'experience' near the number, so things like
    '10 years in business' are ignored. First qualifying mention wins.
    """
    text = text or ""
    for m in EXP_RE.finditer(text):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if hi is not None and hi < lo:
            continue
        if not trusted:
            ctx = text[max(0, m.start() - 60): m.end() + 60]
            if not re.search(r"experienc|\bexp\b", ctx, re.I):
                continue
        return lo, hi
    return None


def experience_ok(job):
    exp = parse_experience(job.get("exp_text"), trusted=True) or parse_experience(
        f"{job['title']} {job['desc']}"
    )
    if not exp:
        job["exp_label"] = ""
        return EXP_CFG.get("include_unspecified", True)
    lo, hi = exp
    job["exp_label"] = f"{lo}-{hi} yrs" if hi else f"{lo}+ yrs"
    return lo <= EXP_MAX and (hi is None or hi >= EXP_MIN)


def matched_skills(job):
    text = f"{job['title']} {job['desc']}"
    return [s for s, rx in SKILLS.items() if rx.search(text)]


def filter_jobs(jobs):
    out, seen_ids = [], set()
    for j in jobs:
        if j["id"] in seen_ids or not title_ok(j["title"]) or not location_ok(j):
            continue
        seen_ids.add(j["id"])
        if not experience_ok(j):
            continue
        skills = matched_skills(j)
        if MUST and not MUST.intersection(x.lower() for x in skills):
            continue
        if len(skills) < j.get("min_skills", CFG.get("min_skill_matches", 1)):
            continue
        j["skills"] = skills
        out.append(j)
    return out


# ----------------------------- apify ---------------------------------------
def apify_run(actor, token):
    """Start an Actor run, poll until it finishes, return its dataset items."""
    slug = actor["id"].replace("/", "~")
    headers = {"Authorization": f"Bearer {token}"}
    timeout_s = APIFY.get("timeout_s", 900)
    params = {
        "maxTotalChargeUsd": APIFY.get("max_charge_usd", 1.0),  # hard spend cap
        "timeout": timeout_s,
    }
    r = SESSION.post(
        f"https://api.apify.com/v2/acts/{slug}/runs",
        headers=headers, params=params, json={"searchTerms": CFG.get("search_terms", []), **actor["input"]}, timeout=60,
    )
    r.raise_for_status()
    run = r.json()["data"]
    deadline = time.time() + timeout_s + 60
    while run["status"] in ("READY", "RUNNING", "TIMING-OUT", "ABORTING"):
        if time.time() > deadline:
            raise TimeoutError(f"{actor['id']} still running after {timeout_s}s")
        time.sleep(10)
        r = SESSION.get(f"https://api.apify.com/v2/actor-runs/{run['id']}", headers=headers, timeout=30)
        r.raise_for_status()
        run = r.json()["data"]
    if run["status"] != "SUCCEEDED":  # results stream in, so partial data may exist
        warn(f"{actor['id']} finished as {run['status']}; using any partial results")
    r = SESSION.get(
        f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items",
        headers=headers, params={"clean": "true", "format": "json"}, timeout=120,
    )
    r.raise_for_status()
    return r.json()


def _first(item, keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", []):
            return v
    return ""


def _text(v):
    return " ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))


def map_item(item, m):
    """Map one Actor output record to the tracker's job dict using config field names."""
    site = _text(_first(item, m.get("source", ["site"]))) or "apify"
    raw_id = _text(_first(item, m["id"]))
    remote_keys = m.get("remote", [])
    return dict(
        id=f"{site}:{raw_id}",
        title=_text(_first(item, m["title"])),
        company=_text(_first(item, m["company"])),
        location=_text(_first(item, m["location"])),
        url=_text(_first(item, m["url"])),
        desc=" ".join(_text(item.get(k)) for k in m["desc"]),
        exp_text=_text(_first(item, m.get("experience", []))),
        posted=_text(_first(item, m.get("posted", ["date_posted"]))),
        remote=bool(_first(item, remote_keys)) if remote_keys else False,
        has_id=bool(raw_id),
    )


def apify_jobs(today):
    """Run due Apify Actors, cache matches for retention_days, return cached matches."""
    if not APIFY.get("enabled"):
        return []
    cache = {"last_run": None, "jobs": {}}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
    last = cache.get("last_run")
    gap = (today - dt.date.fromisoformat(last)).days if last else None
    due = gap is None or gap >= APIFY.get("min_interval_days", 2)
    token = os.environ.get("APIFY_TOKEN")

    if due and not token:
        print("::notice::APIFY_TOKEN not set; skipping Apify sources (optional)")
    elif due:
        fresh, ran = [], False
        for actor in APIFY.get("actors", []):
            try:
                items = apify_run(actor, token)
            except Exception as e:
                warn(f"apify {actor['id']} failed: {e}")
                continue
            ran = True
            for it in items:
                j = map_item(it, actor["map"])
                if j.pop("has_id") and j["title"] and j["url"]:
                    j["min_skills"] = actor.get("min_skill_matches", CFG.get("min_skill_matches", 1))
                    fresh.append(j)
        for j in filter_jobs(fresh):
            j.pop("desc", None)
            cache["jobs"][j["id"]] = {**j, "last_seen": today.isoformat()}
        if ran:
            cache["last_run"] = today.isoformat()

    cutoff = today - dt.timedelta(days=APIFY.get("retention_days", 14))
    cache["jobs"] = {
        k: v for k, v in cache["jobs"].items()
        if dt.date.fromisoformat(v["last_seen"]) >= cutoff
    }
    CACHE_PATH.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    return list(cache["jobs"].values())


def dedupe(jobs):
    """Drop the same role listed on several sources (first source wins)."""
    norm = lambda x: re.sub(r"\W+", "", (x or "").lower())
    seen, out = set(), []
    for j in jobs:
        key = (norm(j["company"]), norm(j["title"]), norm(j["location"]))
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


def to_date(v):
    """Parse an ISO date/datetime string or epoch (s or ms) into a date, else None."""
    if not v:
        return None
    try:
        sv = str(v)
        if sv.replace(".", "", 1).isdigit():
            n = float(sv)
            n = n / 1000 if n > 1e11 else n
            return dt.datetime.fromtimestamp(n, dt.timezone.utc).date()
        return dt.date.fromisoformat(sv[:10])
    except (ValueError, OverflowError, OSError):
        return None


# ----------------------------- report --------------------------------------
def md(s, n=70):
    s = re.sub(r"\s+", " ", s or "").replace("|", "\\|")
    return s if len(s) <= n else s[: n - 1] + "…"


def build_body(jobs, today):
    new_cut = today - dt.timedelta(days=CFG.get("new_days", 3))
    keep_cut = today - dt.timedelta(days=CFG.get("report_days", 30))
    lines = [
        f"**{len(jobs)} matching openings**  \n"
        f"Titles: SDE-2 / frontend / full-stack · Skills ≥ {CFG.get('min_skill_matches', 1)} matched · "
        f"Experience: {EXP_MIN}-{EXP_MAX} yrs · "
        + ("Location: anywhere" if CFG.get("location_mode") == "any" else f"Locations: {', '.join(CFG['locations'][:4])}…"),
        "",
    ]
    groups = {}
    for j in jobs:  # jobs arrive sorted newest day first
        day = dt.date.fromisoformat(j["day"])
        groups.setdefault(j["day"] if day >= keep_cut else "Earlier", []).append(j)
    order = sorted((k for k in groups if k != "Earlier"), reverse=True)
    order += ["Earlier"] if "Earlier" in groups else []
    for k in order:
        if k == "Earlier":
            title = f"Earlier (before {keep_cut:%d %b %Y})"
        else:
            title = dt.date.fromisoformat(k).strftime("%a %d %b %Y") + (" · today" if k == today.isoformat() else "")
        lines += [
            f"### {title} · {len(groups[k])} jobs", "",
            "| New | Role | Company | Location | Exp | Skills | Source |",
            "|---|---|---|---|---|---|---|",
        ]
        for j in groups[k]:
            is_new = dt.date.fromisoformat(j["first_seen"]) >= new_cut
            lines.append(
                f"| {'🆕' if is_new else ''} | [{md(j['title'], 80)}]({j['url']}) | {md(j['company'], 30)} "
                f"| {md(j['location'], 40)} | {j.get('exp_label') or '—'} | {md(', '.join(j['skills'][:6]), 50)} "
                f"| {j['id'].split(':')[0]} |"
            )
        lines.append("")
    links = CFG.get("search_links") or []
    if links:
        lines += ["## Search shortcuts", ""]
        lines += [f"- [{l['name']}]({l['url']})" for l in links]
    lines += [
        "",
        "---",
        "Sources: company ATS boards (Greenhouse, Lever, Ashby), "
        "[Remotive](https://remotive.com) and [Remote OK](https://remoteok.com)"
        + (", plus LinkedIn and Naukri listings collected via Apify." if APIFY.get("enabled") else "."),
    ]
    return "\n".join(lines) + "\n"


def write_site(jobs):
    """Write docs/jobs.json for the dashboard (only when the job list changed)."""
    out = [
        dict(
            id=j["id"], title=j["title"], company=j["company"], location=j["location"],
            url=j["url"], exp=j.get("exp_label", ""), skills=j["skills"],
            first_seen=j["first_seen"], day=j["day"], source=j["id"].split(":")[0],
        )
        for j in jobs
    ]
    path = ROOT / "docs" / "jobs.json"
    path.parent.mkdir(exist_ok=True)
    old = json.loads(path.read_text()).get("jobs") if path.exists() else None
    if old != out:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        path.write_text(json.dumps({"updated": stamp, "jobs": out}, indent=1) + "\n")


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    seen = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else {}

    jobs = dedupe(filter_jobs(collect()) + apify_jobs(today))
    for j in jobs:
        j["first_seen"] = seen.setdefault(j["id"], today.isoformat())
    for j in jobs:
        posted = to_date(j.get("posted"))
        first = dt.date.fromisoformat(j["first_seen"])
        j["day"] = (posted if posted and posted <= today else first).isoformat()
    jobs.sort(key=lambda j: (j["day"], j["first_seen"], len(j["skills"])), reverse=True)

    body = build_body(jobs, today)
    marker = "<!--body-->\n"
    old = REPORT_PATH.read_text() if REPORT_PATH.exists() else ""
    if old.split(marker, 1)[-1] != body:  # only touch the file if content changed
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        REPORT_PATH.write_text(f"# Job openings\n\n_Last updated: {stamp}_\n\n{marker}{body}")
    write_site(jobs)
    SEEN_PATH.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    print(f"{len(jobs)} matching jobs")


if __name__ == "__main__":
    main()
