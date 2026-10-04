"""
Daily job-alert bot for Joshua Amoke (Data Analyst / Data Science).
Fetches openings from free job APIs, filters out scams and stale posts,
scores them against your CV, and sends only NEW matches to Telegram/email.
"""
import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

SEEN_FILE = Path("seen.json")
HEADERS = {"User-Agent": "personal-job-alert-bot/1.0"}
TIMEOUT = 25
MAX_AGE_DAYS = 7       # ignore posts older than this (stale posts are often ghost jobs)
MIN_MATCH = 45         # minimum CV-match score (0-100)
MIN_LEGIT = 70         # minimum legitimacy score (0-100)
MAX_PER_DIGEST = 25

# ---------- YOUR PROFILE (from your CV) ----------
ANALYST_TITLES = [
    "data analyst", "business intelligence", "bi analyst", "bi developer",
    "reporting analyst", "analytics analyst", "junior analyst", "data analytics",
    "power bi", "insights analyst", "business analyst", "product analyst",
    "marketing analyst", "sales analyst", "financial analyst", "operations analyst",
    "data associate", "data intern",
]
SCIENCE_TITLES = [
    "data scientist", "data science", "machine learning", "ml engineer",
    "junior data scientist", "applied scientist", "ai engineer", "analytics engineer",
    "data engineer",
]
SKILLS = [
    "excel", "power bi", "tableau", "sql", "python", "r ", "dax", "mysql",
    "sql server", "azure", "dbt", "google analytics", "google sheets", "git",
    "jupyter", "data visualization", "dashboard", "etl", "statistics", "eda",
    "pandas", "numpy", "scikit-learn", "machine learning", "reporting", "kpi",
]
ENTRY_WORDS = ["junior", "entry", "associate", "intern", "graduate", "trainee", "early career"]
SENIOR_WORDS = ["senior", "sr.", "lead", "principal", "head of", "director", "manager", "staff", "vp "]

# Remote jobs are only useful if open to Nigeria
ACCEPT_LOC = ["worldwide", "anywhere", "global", "africa", "nigeria", "emea", "lagos", "abuja", "international"]
RESTRICT_LOC = [
    "usa", "united states", "u.s.", "us only", "canada", "uk only", "united kingdom", "germany",
    "france", "australia", "india", "latam", "north america", "europe only", "eu only",
    "must reside", "must be located", "work authorization", "right to work",
]

SCAM_PATTERNS = re.compile(
    r"registration fee|processing fee|training fee|pay (a )?(fee|deposit)|send (us )?money|"
    r"western union|gift card|whatsapp (only|number)|telegram (only|@)|"
    r"no experience.{0,40}(\$|earn)|earn \$?\d+.{0,25}(daily|per day|weekly)|"
    r"@(gmail|yahoo|outlook|hotmail)\.com|crypto wallet|bank (details|account number)",
    re.I,
)
TRUSTED_SOURCES = {"remotive", "arbeitnow", "remoteok", "jobicy"}


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_dt(v):
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def job(source, jid, title, company, location, url, posted, desc, remote=True):
    return {
        "id": f"{source}:{jid}", "source": source, "title": (title or "").strip(),
        "company": (company or "").strip(), "location": (location or "").strip(),
        "url": url, "posted": parse_dt(posted), "desc": strip_html(desc), "remote": remote,
    }


# ---------- FETCHERS (each fails safely) ----------
def get_json(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def fetch_remotive():
    out = []
    d = get_json("https://remotive.com/api/remote-jobs", params={"category": "data"})
    for j in d.get("jobs", []):
        out.append(job("remotive", j["id"], j["title"], j["company_name"],
                       j.get("candidate_required_location"), j["url"],
                       j.get("publication_date"), j.get("description")))
    return out


def fetch_arbeitnow():
    out = []
    for page in (1, 2):
        d = get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        for j in d.get("data", []):
            out.append(job("arbeitnow", j["slug"], j["title"], j["company_name"],
                           j.get("location"), j["url"], j.get("created_at"),
                           j.get("description"), remote=bool(j.get("remote"))))
    return out


def fetch_remoteok():
    d = get_json("https://remoteok.com/api")
    return [job("remoteok", j["id"], j.get("position"), j.get("company"),
                j.get("location") or "Worldwide", j.get("url"), j.get("date"),
                j.get("description")) for j in d if isinstance(j, dict) and j.get("id")]


def fetch_jobicy():
    out = []
    for tag in ("data+analyst", "data+scientist"):
        d = get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag})
        for j in d.get("jobs", []):
            out.append(job("jobicy", j["id"], j.get("jobTitle"), j.get("companyName"),
                           j.get("jobGeo"), j.get("url"), (j.get("pubDate") or "").replace(" ", "T"),
                           j.get("jobDescription")))
    return out


def fetch_jooble():
    """Nigeria-based jobs. Needs a free API key from https://jooble.org/api/about"""
    key = os.getenv("JOOBLE_API_KEY")
    if not key:
        return []
    out = []
    for kw in ("data analyst", "data scientist", "business intelligence analyst", "power bi"):
        r = requests.post(f"https://jooble.org/api/{key}", headers=HEADERS, timeout=TIMEOUT,
                          json={"keywords": kw, "location": "Nigeria"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            out.append(job("jooble", j.get("id") or j["link"], j.get("title"), j.get("company"),
                           j.get("location") or "Nigeria", j["link"], j.get("updated"),
                           j.get("snippet"), remote=False))
    return out


# (fetcher, minimum minutes between calls). Keeps us polite to free APIs even
# though the workflow runs every 10 minutes.
FETCHERS = [
    (fetch_remoteok, 10),
    (fetch_arbeitnow, 10),
    (fetch_jooble, 10),
    (fetch_jobicy, 60),
    (fetch_remotive, 360),
]
STATE_FILE = Path("state.json")


# ---------- FILTERING & SCORING ----------
def eligible(j):
    loc = j["location"].lower()
    if j["source"] == "jooble" or "nigeria" in loc:
        return True
    text = f"{loc} {j['desc'][:600].lower()}"
    if any(a in loc for a in ACCEPT_LOC):
        return not any(x in text for x in ("must reside in the us", "us citizens only"))
    if any(x in text for x in RESTRICT_LOC):
        return False
    return j["remote"]  # remote with no stated restriction


def track(j):
    t = j["title"].lower()
    if any(k in t for k in SCIENCE_TITLES):
        return "Data Science"
    if any(k in t for k in ANALYST_TITLES):
        return "Data Analysis"
    return None


def match_score(j, now):
    t, d = j["title"].lower(), j["desc"].lower() + " "
    s = 45
    if any(w in t for w in ENTRY_WORDS):
        s += 15
    if any(w in t for w in SENIOR_WORDS):
        s -= 40
    yrs = [int(x) for x in re.findall(r"(\d{1,2})\+?\s*(?:-\s*\d+\s*)?years", d)]
    if yrs and min(yrs) >= 4:
        s -= 25
    hits = [k for k in SKILLS if k in d or k in t]
    s += min(len(hits) * 4, 30)
    if j["posted"]:
        age = (now - j["posted"]).days
        s += 10 if age <= 3 else 5 if age <= 7 else 0
    j["skills"] = [h.strip() for h in hits][:6]
    return max(0, min(100, s))


def legit_score(j, now):
    text = f"{j['title']} {j['company']} {j['desc']}"
    if SCAM_PATTERNS.search(text):
        return 0
    s = 55
    s += 10 if j["company"] and len(j["company"]) > 2 else -20
    s += 10 if (j["url"] or "").startswith("https://") else -10
    s += 10 if j["source"] in TRUSTED_SOURCES else 5
    if j["posted"]:
        age = (now - j["posted"]).days
        s += 15 if age <= 7 else 5 if age <= 14 else -20
    else:
        s -= 5
    s += 5 if len(j["desc"]) > 300 else -10
    return max(0, min(100, s))


def process(jobs, seen, now):
    keep, uniq = [], set()
    for j in jobs:
        key = (j["title"].lower(), j["company"].lower())
        if j["id"] in seen or key in uniq or not j["url"]:
            continue
        j["track"] = track(j)
        if not j["track"]:
            continue
        if j["posted"] and (now - j["posted"]) > timedelta(days=MAX_AGE_DAYS):
            continue
        if not eligible(j):
            continue
        j["match"], j["legit"] = match_score(j, now), legit_score(j, now)
        if j["match"] >= MIN_MATCH and j["legit"] >= MIN_LEGIT:
            uniq.add(key)
            keep.append(j)
    keep.sort(key=lambda x: (x["match"], x["legit"]), reverse=True)
    return keep[:MAX_PER_DIGEST]


# ---------- NOTIFICATIONS ----------
def build_messages(jobs):
    lines = []
    for j in jobs:
        posted = j["posted"].strftime("%d %b") if j["posted"] else "date n/a"
        lines.append(
            f"<b>{html.escape(j['title'])}</b> ({j['track']})\n"
            f"{html.escape(j['company'])} | {html.escape(j['location'] or 'Remote')} | posted {posted}\n"
            f"Match {j['match']}/100 | Trust {j['legit']}/100 | {html.escape(', '.join(j['skills']))}\n"
            f"{j['url']}\n"
        )
    header = f"<b>{len(jobs)} new data roles for you</b>\n\n"
    chunks, cur = [], header
    for l in lines:
        if len(cur) + len(l) > 3800:
            chunks.append(cur)
            cur = ""
        cur += l + "\n"
    chunks.append(cur)
    return chunks


def send_telegram(chunks):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=TIMEOUT,
                          json={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                "disable_web_page_preview": True})
        r.raise_for_status()
    return True


def send_email(chunks):
    host, user, pwd, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (host and user and pwd and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Daily data job matches", user, to
    msg.set_content(re.sub(r"<[^>]+>", "", html.unescape("\n".join(chunks))))
    msg.add_alternative("<br>".join(c.replace("\n", "<br>") for c in chunks), subtype="html")
    with smtplib.SMTP_SSL(host, 465, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.send_message(msg)
    return True


# ---------- MAIN ----------
def main():
    now = datetime.now(timezone.utc)
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    seen = {k: v for k, v in seen.items() if (now - parse_dt(v)).days < 60}

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    jobs = []
    for f, every_min in FETCHERS:
        last = parse_dt(state.get(f.__name__))
        if last and (now - last) < timedelta(minutes=every_min - 2):
            continue  # not due yet
        try:
            got = f()
            print(f"{f.__name__}: {len(got)} jobs")
            jobs += got
            state[f.__name__] = now.isoformat()
        except Exception as e:  # one broken source must not stop the run
            print(f"{f.__name__} failed: {e}", file=sys.stderr)
    STATE_FILE.write_text(json.dumps(state, indent=1))

    picks = process(jobs, seen, now)
    print(f"{len(picks)} new matches")
    if picks:
        chunks = build_messages(picks)
        sent = False
        for fn in (send_telegram, send_email):
            try:
                sent = fn(chunks) or sent
            except Exception as e:
                print(f"{fn.__name__} failed: {e}", file=sys.stderr)
        if not sent:
            print(re.sub(r"<[^>]+>", "", html.unescape("\n".join(chunks))))
            print("No notifier configured/working; not marking as seen.")
            return
        for j in picks:
            seen[j["id"]] = now.isoformat()
    SEEN_FILE.write_text(json.dumps(seen, indent=1))


if __name__ == "__main__":
    main()
