#!/usr/bin/env python3
"""Job opening tracker.

Pulls openings from public job APIs (company ATS boards, remote-job feeds and,
optionally, LinkedIn/Naukri via Apify), filters them, and keeps everything in ONE
file: docs/jobs.json (the dashboard reads it directly).
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = yaml.safe_load((HERE / "config.yml").read_text())
STORE_PATH = ROOT / "docs" / "jobs.json"
OTHERS_PATH = ROOT / "docs" / "others.json"
APIFY = CFG.get("apify") or {}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = CFG.get("user_agent", "job-tracker/1.0")


def warn(msg):
    print(f"::warning::{msg}", file=sys.stderr)


def report(line):
    """Print a status line and add it to the Actions run summary page."""
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {line}\n")


def get_json(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def himalayas(query):
    """Himalayas public API (remote jobs, worldwide). Docs: himalayas.app/docs/remote-jobs-api"""
    for page in (1, 2):
        data = get_json(
            "https://himalayas.app/jobs/api/search",
            params={"q": query, "sort": "recent", "page": page},
        )
        rows = data.get("jobs", [])
        for j in rows:
            places = [x.get("name", "") for x in (j.get("locationRestrictions") or []) if isinstance(x, dict)]
            seniority = j.get("seniority") or []
            yield dict(
                id=f"himalayas:{j.get('guid') or j.get('applicationLink')}",
                title=j.get("title", ""),
                company=j.get("companyName", ""),
                location=", ".join(places) or "Worldwide (remote)",
                url=j.get("applicationLink", ""),
                desc=f"{j.get('excerpt', '')} {clean(j.get('description'))} {' '.join(seniority)}",
                remote=True,
                posted=j.get("pubDate"),
            )
        if len(rows) < 20:
            break


def jobicy(tag):
    """Jobicy public API (remote jobs). Docs: jobicy.com/jobs-rss-feed"""
    data = get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag})
    for j in data.get("jobs", []):
        yield dict(
            id=f"jobicy:{j['id']}",
            title=j.get("jobTitle", ""),
            company=j.get("companyName", ""),
            location=j.get("jobGeo") or "Anywhere",
            url=j.get("url", ""),
            desc=f"{clean(j.get('jobDescription'))} {j.get('jobExcerpt', '')} {j.get('jobLevel', '')}",
            remote=True,
            posted=j.get("pubDate"),
        )


def arbeitnow(_=None):
    """Arbeitnow public API (Germany / EU tech jobs, many with relocation or visa support)."""
    for page in (1, 2, 3):
        rows = get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page}).get("data", [])
        for j in rows:
            yield dict(
                id=f"arbeitnow:{j['slug']}",
                title=j.get("title", ""),
                company=j.get("company_name", ""),
                location=j.get("location") or ("Remote" if j.get("remote") else ""),
                url=j.get("url", ""),
                desc=f"{clean(j.get('description'))} {' '.join(j.get('tags') or [])}",
                remote=bool(j.get("remote")),
                posted=j.get("created_at"),
            )
        if not rows:
            break


def scope(job_id):
    """Which feed a job came from: 'gh:stripe', 'lever:cred', 'remotive', 'linkedin' ..."""
    p = job_id.split(":")
    return ":".join(p[:2]) if p[0] in ("gh", "lever", "ashby") else p[0]


def collect():
    """Return (jobs, ok_scopes). ok_scopes = feeds that were fetched successfully this run."""
    src = CFG["sources"]
    plan = [
        ("gh", greenhouse, src.get("greenhouse", [])),
        ("lever", lever, src.get("lever", [])),
        ("ashby", ashby, src.get("ashby", [])),
        ("remotive", remotive, src.get("remotive_queries") or CFG.get("search_terms", [])[:3]),
        ("remoteok", remoteok, [None] if src.get("remoteok") else []),
        ("himalayas", himalayas, src.get("himalayas", [])),
        ("jobicy", jobicy, src.get("jobicy", [])),
        ("arbeitnow", arbeitnow, [None] if src.get("arbeitnow") else []),
    ]
    jobs, tried, failed = [], set(), set()
    for kind, fn, args in plan:
        for a in args:
            sc = kind if kind in ("remotive", "remoteok", "himalayas", "jobicy", "arbeitnow") else f"{kind}:{a}"
            tried.add(sc)
            try:
                jobs.extend(fn(a))
            except Exception as e:  # one bad source must not kill the run
                failed.add(sc)
                warn(f"{kind} {a or ''} failed: {e}")
    report(f"Free feeds: {len(tried) - len(failed)}/{len(tried)} sources OK" + (f", failed: {', '.join(sorted(failed))}" if failed else ""))
    return jobs, tried - failed


# ----------------------------- filters -------------------------------------
TITLE_INC = [re.compile(p, re.I) for p in CFG["title_include"]]
TITLE_EXC = [re.compile(p, re.I) for p in CFG["title_exclude"]]
SKILLS = {s: re.compile(rf"(?<!\w){re.escape(s)}(?!\w)", re.I) for s in CFG["skills"]}
LOCS = [l.lower() for l in CFG["locations"]]
BLOCKED_REGION = re.compile(
    r"\b(us|usa|u\.s\.|united states|uk|canada|emea|europe|latam|north america|germany|brazil)\b", re.I
)
OPEN_REGION = re.compile(r"\b(india|anywhere|worldwide|global|apac|asia)\b", re.I)
EXP_CFG = CFG.get("experience") or {}
EXP_MIN, EXP_MAX = EXP_CFG.get("min", 3), EXP_CFG.get("max", 4)
EXP_RE = re.compile(r"(\d{1,2})\s*(?:(?:-|–|—|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b", re.I)
MUST = {s.lower() for s in CFG.get("must_have_any_skill", [])}


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


def filter_jobs(jobs, label="", rejected=None):
    out, seen_ids, drops, examples = [], set(), Counter(), {}

    def drop(reason, j):
        drops[reason] += 1
        if rejected is not None and reason != "duplicate":
            rejected.append((j, reason))  # feeds the dashboard's Others tab
        if reason not in ("duplicate", "title", "location"):  # keep a few examples to help tune filters
            ex = examples.setdefault(reason, [])
            if len(ex) < 3:
                tail = f" [{j['exp_label']}]" if reason == "experience" and j.get("exp_label") else ""
                ex.append(f"{j['title']} @ {j['company']}{tail}")

    for j in jobs:
        if j["id"] in seen_ids:
            drop("duplicate", j)
            continue
        if not title_ok(j["title"]):
            drop("title", j)
            continue
        if not location_ok(j):
            drop("location", j)
            continue
        if not experience_ok(j):
            drop("experience", j)
            continue
        skills = matched_skills(j)
        if MUST and not MUST.intersection(x.lower() for x in skills):
            drop("no core skill (react etc.)", j)
            continue
        if len(skills) < j.get("min_skills", CFG.get("min_skill_matches", 1)):
            drop("too few skills", j)
            continue
        seen_ids.add(j["id"])
        j["skills"] = skills
        out.append(j)
    if label:
        why = ", ".join(f"{k} {v}" for k, v in drops.most_common())
        report(f"{label}: {len(jobs)} fetched, {len(out)} kept" + (f" (dropped: {why})" if why else ""))
        for reason, ex in examples.items():
            report(f"  {label}, dropped by {reason}, e.g. " + "; ".join(ex))
    return out


def _key(j):
    n = lambda x: re.sub(r"\W+", "", (x or "").lower())
    return (n(j["company"]), n(j["title"]), n(j["location"]))


def dedupe(jobs):
    """Drop the same role listed on several sources (first one wins)."""
    seen, out = set(), []
    for j in jobs:
        if _key(j) not in seen:
            seen.add(_key(j))
            out.append(j)
    return out


# ----------------------------- apify ---------------------------------------
def build_payload(actor, extra=None):
    """Actor input = search terms (field name differs per Actor) + its `input:` block + overrides."""
    payload = {}
    field = actor.get("search_terms_field", "searchTerms")  # set to null to send none
    if field:
        payload[field] = actor.get("search_terms") or CFG.get("search_terms", [])
    payload.update(actor["input"])
    payload.update(extra or {})
    return payload


def apify_run(actor, token, extra=None):
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
        headers=headers, params=params,
        json=build_payload(actor, extra),
        timeout=60,
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
    return ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))


def map_item(item, m):
    """Map one Actor output record to the tracker's job dict using config field names."""
    site = m.get("source_label") or _text(_first(item, m.get("source", ["site"]))) or "apify"
    raw_id = _text(_first(item, m["id"]))
    remote_keys = m.get("remote", [])
    exp_text = _text(_first(item, m.get("experience", [])))
    if not exp_text and (m.get("experience_min") or m.get("experience_max")):
        lo = _first(item, m.get("experience_min", []))
        hi = _first(item, m.get("experience_max", []))
        exp_text = f"{lo}-{hi} yrs" if lo != "" and hi != "" else (f"{lo}+ yrs" if lo != "" else "")
    return dict(
        id=f"{site}:{raw_id}",
        title=_text(_first(item, m["title"])),
        company=_text(_first(item, m["company"])),
        location=_text(_first(item, m["location"])),
        url=_text(_first(item, m["url"])),
        desc=" ".join(_text(item.get(k)) for k in m["desc"]),
        exp_text=exp_text,
        posted=_text(_first(item, m.get("posted", ["date_posted"]))),
        remote=bool(_first(item, remote_keys)) if remote_keys else False,
        has_id=bool(raw_id),
    )


def apify_jobs(today, store, rejected=None):
    """Run the Apify Actors that are due. Returns (fresh matching jobs, feeds fetched).

    Each Actor has its own schedule (`min_interval_days`, default from the apify section), so cheap
    Actors can run more often than expensive ones. A manual run (FORCE_APIFY=true) runs them all.
    """
    if not APIFY.get("enabled"):
        return [], set()
    runs = store.setdefault("apify_runs", {})
    forced = os.environ.get("FORCE_APIFY") == "true"
    token = os.environ.get("APIFY_TOKEN")

    raw, ok, warned_token = [], set(), False
    for idx, actor in enumerate(APIFY.get("actors", [])):
        key = actor.get("name") or f"{idx}:{actor['id']}"
        last = runs.get(key)
        gap = (today - dt.date.fromisoformat(last)).days if last else None
        interval = actor.get("min_interval_days", APIFY.get("min_interval_days", 2))
        if not (forced or gap is None or gap >= interval):
            report(f"Apify {key}: skipped (ran {last}; every {interval} days; use Run workflow to force)")
            continue
        if not token:
            if not warned_token:
                report("Apify: SKIPPED because the APIFY_TOKEN secret is missing or empty")
                warned_token = True
            continue
        # Catch-up window: after missed days, look back further so nothing slips through.
        hours = 168 if gap is None else min((gap + 2) * 24, 336)
        extra = {}
        if "hoursOld" in actor["input"]:
            extra["hoursOld"] = max(actor["input"]["hoursOld"], hours)
        if "postedWithinDays" in actor["input"]:
            cur = actor["input"]["postedWithinDays"]
            days = max(int(cur), -(-hours // 24))
            extra["postedWithinDays"] = str(days) if isinstance(cur, str) else days
        try:
            items = apify_run(actor, token, extra)
        except Exception as e:
            warn(f"apify {key} failed: {e}")
            continue
        runs[key] = today.isoformat()
        got = Counter()
        for it in items:
            j = map_item(it, actor["map"])
            if j.pop("has_id") and j["title"] and j["url"]:
                j["min_skills"] = actor.get("min_skill_matches", CFG.get("min_skill_matches", 1))
                raw.append(j)
                ok.add(scope(j["id"]))  # only feeds that actually returned rows count as fetched
                got[scope(j["id"])] += 1
        report(f"Apify {key}: " + (", ".join(f"{k} {v}" for k, v in sorted(got.items())) or "0 rows"))
        for site in actor["input"].get("sites", []):
            if site not in got and items:
                warn(f"Apify {key}: no rows for '{site}' (blocked, or nothing matched)")
        if not items:
            warn(f"Apify {key}: returned 0 rows (blocked, or nothing matched the search)")
    return (filter_jobs(raw, "Apify", rejected) if raw else []), ok


# ----------------------------- store ---------------------------------------
def load_store():
    if STORE_PATH.exists():
        d = json.loads(STORE_PATH.read_text())
        return {"apify_runs": d.get("apify_runs") or {}, "jobs": {j["id"]: j for j in d.get("jobs", [])}}
    return {"apify_runs": {}, "jobs": {}}


def save_store(store):
    """Write docs/jobs.json: compact, one job per line. Skips the write if nothing changed."""
    jobs = sorted(
        store["jobs"].values(),
        key=lambda j: (j["day"], j["first_seen"], len(j["skills"])),
        reverse=True,
    )
    meta = {"apify_runs": store.get("apify_runs") or {}, "links": CFG.get("search_links") or []}
    old = json.loads(STORE_PATH.read_text()) if STORE_PATH.exists() else {}
    if old.get("jobs") == jobs and all(old.get(k) == v for k, v in meta.items()):
        return False
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    head = dump({"updated": stamp, **meta})[:-1]
    STORE_PATH.parent.mkdir(exist_ok=True)
    STORE_PATH.write_text(head + ',"jobs":[\n' + ",\n".join(dump(j) for j in jobs) + "\n]}\n")
    return True


def send_digest(new_jobs, today):
    """Send new jobs to a Telegram group/channel as a digest. Returns True if sent."""
    cfg = (CFG.get("notify") or {}).get("telegram") or {}
    if not cfg.get("enabled"):
        return False
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        report("Notifications: skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets missing)")
        return False

    cap = cfg.get("max_jobs", 20)
    ranked = sorted(new_jobs, key=lambda j: (len(j["skills"]), j["day"]), reverse=True)
    shown, more = ranked[:cap], max(0, len(ranked) - cap)
    esc = lambda x: html.escape(str(x or ""), quote=False)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    dash = f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/" if "/" in repo else ""

    head = f"🆕 <b>{len(ranked)} new job{'s' if len(ranked) != 1 else ''}</b> · {today:%a %d %b}\n"
    blocks = []
    for i, j in enumerate(shown, 1):
        meta = " · ".join(x for x in [esc(j["company"]), esc(j["location"]), esc(j.get("exp")), esc(j["source"])] if x)
        blocks.append(f'{i}. <a href="{html.escape(j["url"], quote=True)}">{esc(j["title"])}</a>\n    {meta}')
    tail = (f"\n+{more} more on the dashboard" if more else "") + (f'\n<a href="{dash}">Open dashboard</a>' if dash else "")

    messages, cur = [], head
    for b in blocks:  # Telegram caps a message at 4096 characters
        if len(cur) + len(b) + 2 > 3800:
            messages.append(cur)
            cur = ""
        cur += "\n" + b + "\n"
    messages.append(cur + tail)

    for text in messages:
        r = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:  # never print the request URL: it contains the bot token
            warn(f"Telegram send failed: HTTP {r.status_code} {r.text[:200]}")
            return False
        time.sleep(1)
    report(f"Notifications: sent {len(shown)} of {len(ranked)} new jobs to Telegram")
    return True


def load_others():
    if OTHERS_PATH.exists():
        return {j["id"]: j for j in json.loads(OTHERS_PATH.read_text()).get("jobs", [])}
    return {}


def update_others(others, rejected, matches, ok, today):
    """Jobs we fetched that did NOT pass your filters, with the reason (shown in the Others tab)."""
    match_keys = {_key(j) for j in matches.values()}
    fresh = {}
    for j, why in rejected:
        if j["id"] in matches or _key(j) in match_keys:
            continue
        old = others.get(j["id"])
        posted = to_date(j.get("posted"))
        day = old["day"] if old else (posted if posted and posted <= today else today).isoformat()
        fresh[j["id"]] = dict(
            id=j["id"], title=j["title"], company=j["company"], location=j["location"],
            url=j["url"], exp=j.get("exp_label", ""), why=why, day=day, source=j["id"].split(":")[0],
        )
    for jid, rec in others.items():  # feeds that were not fetched this run keep their old rows
        if jid not in fresh and jid not in matches and scope(jid) not in ok and _key(rec) not in match_keys:
            fresh[jid] = rec
    recs = sorted(fresh.values(), key=lambda r: r["day"], reverse=True)
    recs = sorted(recs, key=lambda r: r["why"] == "title")  # near-misses (passed the title filter) survive the cap
    recs = dedupe(recs)[: CFG.get("others_max", 1500)]
    return sorted(recs, key=lambda r: r["day"], reverse=True)


def save_others(recs):
    old = json.loads(OTHERS_PATH.read_text()).get("jobs") if OTHERS_PATH.exists() else None
    if old == recs:
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    OTHERS_PATH.parent.mkdir(exist_ok=True)
    OTHERS_PATH.write_text(
        dump({"updated": stamp})[:-1] + ',"jobs":[\n' + ",\n".join(dump(r) for r in recs) + "\n]}\n"
    )


def main(today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    store = load_store()
    jobs = store["jobs"]
    was_empty = not jobs

    rejected = []
    free, ok = collect()
    apify_fresh, apify_ok = apify_jobs(today, store, rejected)
    fresh = dedupe(filter_jobs(free, "Free feeds", rejected) + apify_fresh)
    ok |= apify_ok

    seen_now = set()
    for j in fresh:
        old = jobs.get(j["id"])
        first = old["first_seen"] if old else today.isoformat()
        posted = to_date(j.get("posted"))
        day = old["day"] if old else (posted if posted and posted <= today else today).isoformat()
        jobs[j["id"]] = dict(
            id=j["id"], title=j["title"], company=j["company"], location=j["location"],
            url=j["url"], exp=j.get("exp_label", ""), skills=j["skills"],
            day=day, first_seen=first, source=j["id"].split(":")[0],
        )
        if old and old.get("notified"):
            jobs[j["id"]]["notified"] = True
        seen_now.add(j["id"])

    # A job that used to match but no longer passes your filters moves to the Others tab.
    for j, _ in rejected:
        if j["id"] in jobs and j["id"] not in seen_now:
            del jobs[j["id"]]

    # Jobs missing from a feed that WAS fetched successfully are marked "gone" (kept for a while).
    keep_days = CFG.get("retention_days", 30)
    for jid, rec in list(jobs.items()):
        if jid in seen_now:
            continue
        if scope(jid) in ok and "gone" not in rec:
            rec["gone"] = today.isoformat()
        if "gone" in rec and (today - dt.date.fromisoformat(rec["gone"])).days > keep_days:
            del jobs[jid]

    # Same role on several sources: keep one, preferring jobs that are still listed.
    ordered = sorted(jobs.values(), key=lambda r: "gone" in r)
    store["jobs"] = {j["id"]: j for j in dedupe(ordered)}

    # Alerts: each job is announced once. The very first run only records a baseline (no flood).
    notify_on = ((CFG.get("notify") or {}).get("telegram") or {}).get("enabled")
    pending = [j for j in store["jobs"].values() if "gone" not in j and not j.get("notified")]
    if notify_on and pending:
        if was_empty:
            for j in pending:
                j["notified"] = True
            report(f"Notifications: first run, {len(pending)} jobs recorded without sending")
        else:
            recent = [j for j in pending if (today - dt.date.fromisoformat(j["first_seen"])).days <= 2]
            if not recent or send_digest(recent, today):
                for j in pending:  # older leftovers are marked silently so they never flood later
                    j["notified"] = True
    save_store(store)
    if CFG.get("store_others", True):
        recs = update_others(load_others(), rejected, store["jobs"], ok, today)
        save_others(recs)
        report(f"Others tab: {len(recs)} fetched jobs that did not match your filters")
    n_gone = sum(1 for j in store["jobs"].values() if "gone" in j)
    print(f"{len(store['jobs']) - n_gone} listed, {n_gone} no longer listed")


if __name__ == "__main__":
    main()
