"""Daily job collector: Feashliaa ATS data + gentle JobSpy -> resume match % -> Notion board.
Collection and tracking only. No applying, no proxies, no block bypassing."""
import csv, glob, gzip, hashlib, html, json, logging, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

NOTION_DB = os.environ.get("NOTION_DB_ID") or "da6a16b97df9409a84898ee006bbb0bb"
SEEN_FILE, ALL_FILE = "data/seen.txt", "data/all_jobs_latest.csv"
STRONG, MAYBE = 55, 45          # Match % thresholds for the Notion board
MAX_EXP = 3                     # hard cut: roles asking for more years are dropped
MAX_FETCH, MAX_NOTION = 600, 150
HOURS_OLD = 14                  # runs at 08:00 and 18:00 IST; overlap is removed via data/seen.txt
UA = {"User-Agent": "personal-job-collector/1.0", "Accept": "application/json"}

INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|delhi|kolkata|ahmedabad|kochi|jaipur|remote[- ]?india)\b", re.I)
TITLE_OK = re.compile(r"\b(software (development )?engineer|sde|backend|back[- ]end|java|python|ai engineer|ml engineer|machine learning|platform engineer|developer|llm|genai|applied ai|data engineer|full[- ]?stack)\b", re.I)
TITLE_BAD = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|head|director|architect|vp|iii|iv|sde[- ]?(2|ii|3)|engineer (2|ii|3)|intern(ship)?|sales|support|recruit|accountant|nurse|teacher)\b", re.I)
EXP = re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2}|to\s*\d{1,2})?\s*\+?\s*(?:years|yrs)", re.I)

# Skill vocabulary. Resume skills are whichever of these appear in resume.txt;
# the rest only count against a job when the job asks for them.
SKILLS = {
    "Java": r"\bjava\b(?!script)", "Python": r"\bpython\b", "SQL": r"\bsql\b", "C++": r"c\+\+",
    "Spring Boot": r"spring", "FastAPI": r"fastapi", "REST": r"\brest(ful)?\b", "Microservices": r"micro-?services?",
    "Kafka": r"kafka", "Redis": r"redis", "MySQL": r"mysql", "PostgreSQL": r"postgres", "Distributed systems": r"distributed systems?",
    "RAG": r"\brag\b|retrieval[- ]augmented", "LLM": r"\bllms?\b|large language model", "LangChain": r"langchain", "LangGraph": r"langgraph",
    "FAISS": r"faiss|vector (db|database|search)", "Embeddings": r"embedding", "MCP": r"\bmcp\b|model context protocol",
    "Hugging Face": r"hugging ?face", "GenAI": r"gen(erative)? ?ai", "Agents": r"\bagent(s|ic)\b",
    "scikit-learn": r"scikit|sklearn", "TensorFlow": r"tensorflow", "Machine learning": r"machine learning|\bml\b",
    "Pandas": r"pandas", "Docker": r"docker", "CI/CD": r"ci/cd|github actions|jenkins", "AWS": r"\baws\b|amazon web services",
    "Linux": r"linux", "Git": r"\bgit\b", "Payments": r"payment|fintech|upi",
    # not on the resume (yet): count as missing when a job asks for them
    "Go": r"\bgolang\b|\bgo\b(?= ?(lang|developer|engineer|,|/))", "Node.js": r"node\.?js", "TypeScript": r"typescript",
    "JavaScript": r"javascript", "React": r"\breact\b", "Angular": r"angular", "Kotlin": r"kotlin", "Scala": r"\bscala\b",
    "Rust": r"\brust\b", "C#/.NET": r"c#|\.net\b", "PHP": r"\bphp\b", "Ruby": r"\bruby\b|rails", "Kubernetes": r"kubernetes|\bk8s\b",
    "Terraform": r"terraform", "GCP": r"\bgcp\b|google cloud", "Azure": r"azure", "Spark": r"\bspark\b", "Airflow": r"airflow",
    "MongoDB": r"mongo", "Elasticsearch": r"elastic", "PyTorch": r"pytorch", "Salesforce": r"salesforce", "SAP": r"\bsap\b",
    "ServiceNow": r"servicenow", "Embedded C": r"embedded|firmware", "Android/iOS": r"\bandroid\b|\bios\b|swift|flutter",
    "Selenium/QA": r"selenium|manual testing|\bqa\b", "Power BI/Tableau": r"power ?bi|tableau",
}
SKILL_RE = {k: re.compile(v, re.I) for k, v in SKILLS.items()}


def skills_in(text):
    return {k for k, rx in SKILL_RE.items() if rx.search(text or "")}


def min_exp(text):
    nums = [int(m.group(1)) for m in EXP.finditer(text or "") if int(m.group(1)) <= 20]
    return min(nums) if nums else None


def norm_key(company, title, location):
    s = "|".join(re.sub(r"\W+", " ", (x or "").lower()).strip() for x in (company, title, (location or "").split(",")[0]))
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or "")))).strip()


def job(source, company, title, location, url, posted, desc="", ats=""):
    return dict(key=norm_key(company, title, location), source=source, ats=ats, company=company or "", title=title,
                location=location or "", url=url or "", posted=(posted or "")[:10], desc=desc or "")


# ---------------- sources ----------------

def from_feashliaa(data_dir, days=2):
    if not os.path.isdir(data_dir):
        subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/Feashliaa/job-board-data", data_dir], check=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for f in sorted(glob.glob(os.path.join(data_dir, "data/chunks/jobs_chunk_*.json.gz"))):
        with gzip.open(f, "rt") as fh:
            for j in json.load(fh):
                if (j.get("first_seen") or "") >= cutoff and j.get("title") and INDIA.search(j.get("location") or ""):
                    jb = job(f"ats:{j.get('ats', '')}", j.get("company"), j["title"], j["location"], j.get("url"), j.get("first_seen"), ats=j.get("ats", ""))
                    jb["skill_level"], jb["is_recruiter"] = j.get("skill_level"), j.get("is_recruiter")
                    out.append(jb)
    log.info("feashliaa: %d India jobs (first_seen within %d days)", len(out), days)
    return out


SEARCHES = ["SDE 1 Java", "backend engineer", "software engineer Spring Boot", "AI engineer", "ML engineer python"]
SITES = {
    "indeed": dict(site_name=["indeed"], country_indeed="India", location="Bengaluru"),
    "naukri": dict(site_name=["naukri"], location="Bengaluru"),
    "linkedin": dict(site_name=["linkedin"], location="Bengaluru, India", linkedin_fetch_description=False),
}


def from_jobspy(scrape=None, pause=20):
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    out, blocked = [], set()
    for term in SEARCHES:
        for site, kw in SITES.items():
            if site in blocked:
                continue
            try:
                df = scrape(search_term=term, results_wanted=20 if site == "linkedin" else 30, hours_old=HOURS_OLD, **kw)
            except Exception as e:  # 429 / block / network: skip site for the day, no retries
                log.warning("jobspy %s blocked or failed (%s); skipping it today", site, str(e)[:150])
                blocked.add(site)
                continue
            n = 0 if df is None else len(df)
            for r in ([] if df is None else df.to_dict("records")):
                desc = " ".join(str(r.get(c)) for c in ("experience_range", "skills", "description") if isinstance(r.get(c), str))
                out.append(job(f"jobspy:{site}", str(r.get("company") or ""), str(r.get("title") or ""),
                               str(r.get("location") or ""), r.get("job_url"), str(r.get("date_posted") or ""), desc))
            log.info("jobspy %s '%s': %d results", site, term, n)
            time.sleep(pause)
    return out


# ---------------- descriptions (official public ATS job APIs) ----------------

_ashby_boards = {}


def fetch_desc(j):
    u, ats = j["url"], j["ats"]
    if ats == "Greenhouse":
        m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", u)
        board, jid = m.groups() if m else (j["company"], (re.search(r"gh_jid=(\d+)", u) or [None, None])[1])
        if not jid:
            return ""
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}", headers=UA, timeout=15)
        return strip_html(r.json().get("content")) if r.ok else ""
    if ats == "Lever":
        m = re.search(r"lever\.co/([^/]+)/([\w-]+)", u)
        r = m and requests.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", headers=UA, timeout=15)
        if not r or not r.ok:
            return ""
        d = r.json()
        return " ".join([d.get("descriptionPlain", "")] + [strip_html(x.get("content")) for x in d.get("lists", [])])
    if ats == "Ashby":
        m = re.search(r"ashbyhq\.com/([^/]+)/([\w-]+)", u)
        if not m:
            return ""
        if m.group(1) not in _ashby_boards:
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}", headers=UA, timeout=20)
            _ashby_boards[m.group(1)] = {x["id"]: x.get("descriptionPlain", "") for x in (r.json().get("jobs", []) if r.ok else [])}
        return _ashby_boards[m.group(1)].get(m.group(2), "")
    if ats == "Workday":
        m = re.search(r"https://(([^.]+)\.[^/]+)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+)", u)
        r = m and requests.get(f"https://{m.group(1)}/wday/cxs/{m.group(2)}/{m.group(3)}/job/{m.group(4)}", headers=UA, timeout=15)
        return strip_html(r.json().get("jobPostingInfo", {}).get("jobDescription")) if r and r.ok else ""
    return ""


def add_descriptions(jobs):
    todo = [j for j in jobs if not j["desc"] and j["ats"]][:MAX_FETCH]
    ok = 0
    for j in todo:
        try:
            j["desc"] = fetch_desc(j)
            ok += bool(j["desc"])
        except Exception as e:
            log.debug("desc fetch failed %s: %s", j["url"], e)
        time.sleep(0.3)
    log.info("descriptions: fetched %d of %d attempted", ok, len(todo))


# ---------------- filtering + resume match ----------------

def hard_filter(j):
    """Drop only what is clearly wrong; the match % decides the rest."""
    if not INDIA.search(j["location"] + (" India" if j["source"].startswith("jobspy") else "")):
        return False
    if TITLE_BAD.search(j["title"]) or j.get("is_recruiter") or j.get("skill_level") in ("senior", "intern"):
        return False
    exp = min_exp(j["title"] + " " + j["desc"][:3000])
    j["min_exp"] = exp
    return exp is None or exp <= MAX_EXP


class Scorer:
    def __init__(self, resume_text, use_embeddings=True):
        self.resume_skills = skills_in(resume_text)
        self.model = None
        if use_embeddings:
            from fastembed import TextEmbedding
            import numpy as np
            self.np = np
            self.model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=os.environ.get("MODEL_CACHE", "model_cache"))
            self.r_vec = self._embed([resume_text])[0]

    def _embed(self, texts):
        v = self.np.array(list(self.model.embed(texts)))
        return v / self.np.linalg.norm(v, axis=1, keepdims=True)

    def score(self, jobs):
        sims = [0.5] * len(jobs)
        if self.model and jobs:
            sims = list(self._embed([(j["title"] + ". " + j["desc"])[:2000] for j in jobs]) @ self.r_vec)
        for j, cos in zip(jobs, sims):
            wanted = skills_in(j["title"] + " " + j["desc"])
            matched, missing = wanted & self.resume_skills, wanted - self.resume_skills
            # share of the job's asked skills you have; a long JD listing 15 skills is judged on its top 8
            skill = min(len(matched) / min(max(len(wanted), 1 if j["desc"] else 3), 8), 1.0) if wanted else 0.3
            if wanted and not matched:
                skill -= 0.15 * min(len(missing), 3)
            sem = min(max((float(cos) - 0.55) / 0.2, 0), 1)
            title = 1.0 if TITLE_OK.search(j["title"]) else 0.0
            pct = 100 * (0.5 * max(skill, 0) + 0.25 * sem + 0.25 * title)
            if not j["desc"]:
                pct *= 0.85  # title-only: less evidence, keep it a notch lower
            if not title:
                pct = min(pct, STRONG - 1)  # non-engineering title: at most "Maybe"
            j.update(match=round(pct), matched=", ".join(sorted(matched)), missing=", ".join(sorted(missing)),
                     tier="Strong" if pct >= STRONG else "Maybe" if pct >= MAYBE else "")


# ---------------- outputs ----------------

def notion_add(jobs, token):
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    txt = lambda s: {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}
    added = 0
    for j in jobs:
        props = {
            "Role": {"title": [{"text": {"content": j["title"][:200]}}]}, "Company": txt(j["company"]),
            "Match %": {"number": j["match"]}, "Tier": {"select": {"name": j["tier"]}}, "Status": {"status": {"name": "New"}},
            "Location": txt(j["location"]), "Source": {"select": {"name": j["source"][:90]}}, "Link": {"url": j["url"] or None},
            "Matched skills": txt(j["matched"]), "Missing skills": txt(j["missing"]),
            "Desc": {"select": {"name": "full" if j["desc"] else "title-only"}}, "Key": txt(j["key"]),
        }
        if j.get("min_exp") is not None:
            props["Min exp (yrs)"] = {"number": j["min_exp"]}
        if re.match(r"\d{4}-\d{2}-\d{2}$", j["posted"]):
            props["Posted"] = {"date": {"start": j["posted"]}}
        body = {"parent": {"database_id": NOTION_DB}, "properties": props}
        r = requests.post("https://api.notion.com/v1/pages", headers=h, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            r = requests.post("https://api.notion.com/v1/pages", headers=h, json=body, timeout=30)
        if r.ok:
            added += 1
        else:
            log.warning("notion: %s %s", r.status_code, r.text[:200])
        time.sleep(0.35)
    log.info("notion: added %d of %d", added, len(jobs))


def main():
    args = set(sys.argv[1:])
    os.makedirs("data", exist_ok=True)
    seen = set(open(SEEN_FILE).read().split()) if os.path.exists(SEEN_FILE) else set()

    # --jobspy-only (evening run): Feashliaa refreshes once a day, so the morning run covers it
    jobs = [] if "--jobspy-only" in args else from_feashliaa(os.environ.get("FEASHLIAA_DIR", "job-board-data"))
    if "--no-jobspy" not in args:
        jobs += from_jobspy()
    uniq = {}
    for j in jobs:
        if j["key"] not in seen:
            uniq.setdefault(j["key"], j)
    jobs = list(uniq.values())
    cands = [j for j in jobs if hard_filter(j)]
    log.info("%d new jobs, %d pass the hard filter", len(jobs), len(cands))

    if "--no-fetch" not in args:
        add_descriptions(sorted(cands, key=lambda j: not TITLE_OK.search(j["title"])))
        cands = [j for j in cands if hard_filter(j)]  # re-check experience now that descriptions are in
    Scorer(open("resume.txt").read(), "--no-embed" not in args).score(cands)
    board = sorted([j for j in cands if j["tier"]], key=lambda j: -j["match"])
    log.info("board: %d Strong, %d Maybe", sum(j["tier"] == "Strong" for j in board), sum(j["tier"] == "Maybe" for j in board))

    cols = ["match", "tier", "title", "company", "location", "source", "min_exp", "matched", "missing", "url", "posted", "key"]
    with open(ALL_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(cands, key=lambda j: -j["match"]))

    if "--dry-run" in args:
        log.info("dry run: wrote %s, Notion skipped", ALL_FILE)
        return
    notion_add(board[:MAX_NOTION], os.environ["NOTION_TOKEN"])
    with open(SEEN_FILE, "a") as f:
        f.writelines(j["key"] + "\n" for j in jobs)


if __name__ == "__main__":
    main()
