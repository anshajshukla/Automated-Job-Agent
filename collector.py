"""Daily job collector: Feashliaa ATS data + gentle JobSpy -> resume match % -> Notion board.
Collection and tracking only. No applying, no proxies, no block bypassing."""
import csv, glob, gzip, hashlib, html, json, logging, os, re, subprocess, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx", "huggingface_hub", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

NOTION_DB = os.environ.get("NOTION_DB_ID") or "da6a16b97df9409a84898ee006bbb0bb"
SEEN_FILE, ALL_FILE = "data/seen.txt", "data/all_jobs_latest.csv"
STRONG, MAYBE = 65, 45          # Match % thresholds for the Notion board
MAX_EXP = 2                     # hard cut: roles asking for 3+ years are dropped
MIN_LPA = 15                    # hard cut: published pay topping out below this (lakhs/year) is dropped; unstated pay is kept
MAX_FETCH, MAX_NOTION = 600, 150
HOURS_OLD = 14                  # runs at 08:00 and 18:00 IST; overlap is removed via data/seen.txt
UA = {"User-Agent": "personal-job-collector/1.0", "Accept": "application/json"}

INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|delhi|kolkata|ahmedabad|kochi|jaipur|remote[- ]?india)\b", re.I)
TITLE_OK = re.compile(r"\b(software (development )?engineer|sde|backend|back[- ]end|java|python|ai engineer|ml engineer|machine learning|platform engineer|developer|llm|genai|applied ai|data engineer|full[- ]?stack|ai|ml|gen ?ai|data scien\w*|applied scien\w*|nlp|deep learning|computer vision|mlops|research engineer)\b", re.I)
# Target focus: AI/ML roles open to early-career engineers
AI_TITLE = re.compile(r"\b(ai|ml|machine learning|gen ?ai|llm|nlp|data scien\w*|applied scien\w*|deep learning|computer vision|mlops|agentic)\b", re.I)
ENTRY = re.compile(r"\b(junior|jr\.?|associate|entry[- ]level|graduate|new grad|fresher|early career|sde[- ]?(1|i)|engineer (1|i))\b", re.I)
AI_BONUS, ENTRY_BONUS = 5, 5
# roles outside SDE / backend / AI-ML engineering: never Strong
OFF_TARGET = re.compile(r"\b(security|cyber|threat|privileged access|analyst|consultant|operations|ops|sre|site reliability|"
                        r"devops|devsecops|network|mainframe|test engineer|engineer in test|tester|qe|quality engineer|"
                        r"oracle|guidewire|maximo|\.net|dot net|c#|databricks|power platform|power bi|bi developer|sharepoint|"
                        r"dynamics 365|pega|mulesoft|"
                        r"sap|salesforce|servicenow|ios|android|qa|testing|sdet|automation tester|embedded|firmware)\b", re.I)
LEADS_PEOPLE = re.compile(r"\b(guide|mentor|manage|lead|supervise)s?\s+(?:a |the |other |junior )*(?:team|juniors?|consultants|engineers|practitioners|developers)\b", re.I)
# skill weights: what defines your profile counts most, generic tooling barely
CORE_SKILLS = {"Java", "Spring Boot", "Kafka", "Redis", "Python", "FastAPI", "Microservices", "Distributed systems", "RAG",
               "LLM", "LangChain", "LangGraph", "MCP", "FAISS", "Embeddings", "Machine learning", "NLP", "LLM evaluation"}
MIN_CORE_FOR_STRONG = 2
GENERIC_SKILLS = {"Git", "Linux", "REST", "SQL", "CI/CD", "Docker", "AWS", "Agents", "GenAI"}
skill_w = lambda k: 3 if k in CORE_SKILLS else 0.5 if k in GENERIC_SKILLS else 2
TITLE_BAD = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|head|director|architect|vp|iii|iv|sde[- ]?(2|ii|3)|engineer (2|ii|3)|intern(ship)?|sales|support|recruit|accountant|nurse|teacher|trainer|tutor|annotator)\b", re.I)
EXP = re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2}|to\s*\d{1,2}|or more)?\s*\+?\s*(?:years?|yrs?)\b", re.I)
EXP_LABEL = re.compile(r"(?:years?|yrs?) of (?:\w+ ){0,2}experience\s*[:\-]\s*(\d{1,2})", re.I)  # "Years of experience: 5"
WORD_NUM = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten".split())}
# description says the role is senior even when the title doesn't
SENIOR_DESC = re.compile(r"\b(?:looking for|seeking|hiring|as) an? (?:highly |very )?(?:experienced |seasoned )?(?:senior|sr\.?|lead|staff|principal)\b"
                         r"|experience as an? (?:technical |tech |team |engineering )?(?:lead|manager)\b", re.I)

# Skill vocabulary. Resume skills are whichever of these appear in resume.txt;
# the rest only count against a job when the job asks for them.
SKILLS = {
    "Java": r"\bjava\b(?!script)", "Python": r"\bpython\b", "SQL": r"\bsql\b", "C++": r"c\+\+",
    "Spring Boot": r"spring", "FastAPI": r"fastapi", "REST": r"\brest(ful)?\b", "Microservices": r"micro-?services?",
    "Kafka": r"kafka", "Redis": r"redis", "MySQL": r"mysql", "PostgreSQL": r"postgres", "Distributed systems": r"distributed systems?",
    "RAG": r"\brag\b|retrieval[- ]augmented", "LLM": r"\bllms?\b|large language model", "LangChain": r"langchain", "LangGraph": r"langgraph",
    "FAISS": r"faiss|vector (db|database|search)", "Embeddings": r"(?:vector|text|semantic|sentence|word) embeddings?|embeddings? models?|\bembeddings\b", "MCP": r"\bmcp\b|model context protocol",
    "Hugging Face": r"hugging ?face", "GenAI": r"gen(erative)? ?ai", "Agents": r"\bagent(s|ic)\b",
    "scikit-learn": r"scikit|sklearn", "TensorFlow": r"tensorflow", "Machine learning": r"machine learning|\bml\b", "NLP": r"\bnlp\b|natural language|\bllms?\b",
    "Deep learning": r"deep learning|neural net|lstm|transformer", "Time series": r"time[- ]series|forecast",
    "LightGBM/XGBoost": r"lightgbm|xgboost", "LLM evaluation": r"\bevals?\b|evaluation harness|llm evaluation",
    "Prompt engineering": r"prompt engineering|prompting",
    "Pandas": r"pandas", "Docker": r"docker", "CI/CD": r"ci/cd|github actions|jenkins", "AWS": r"\baws\b|amazon web services",
    "Linux": r"linux", "Git": r"\bgit\b", "Payments": r"payment|fintech|upi",
    # not on the resume (yet): count as missing when a job asks for them
    "Go": r"\bgolang\b|\bgo\b(?= ?(lang|developer|engineer|,|/))", "Node.js": r"node\.?js", "TypeScript": r"typescript",
    "JavaScript": r"javascript", "React": r"\breact\b", "Angular": r"angular", "Kotlin": r"kotlin", "Scala": r"\bscala\b",
    "Rust": r"\brust\b", "C#/.NET": r"c#|\.net\b", "PHP": r"\bphp\b", "Ruby": r"\bruby\b|rails", "Kubernetes": r"kubernetes|\bk8s\b",
    "Terraform": r"terraform", "GCP": r"\bgcp\b|google cloud", "Azure": r"azure", "Spark": r"\bspark\b", "Airflow": r"airflow",
    "MongoDB": r"mongo", "Elasticsearch": r"elastic", "PyTorch": r"pytorch", "Computer vision": r"computer vision|opencv|\bcv\b", "MLOps": r"mlops|mlflow|kubeflow|sagemaker", "Salesforce": r"salesforce", "SAP": r"\bsap\b",
    "ServiceNow": r"servicenow", "Embedded C": r"embedded|firmware", "Android/iOS": r"\bandroid\b|\bios\b|swift|flutter",
    "Selenium/QA": r"selenium|manual testing|\bqa\b", "Power BI/Tableau": r"power ?bi|tableau",
}
SKILL_RE = {k: re.compile(v, re.I) for k, v in SKILLS.items()}


def skills_in(text):
    return {k for k, rx in SKILL_RE.items() if rx.search(text or "")}


def norm_text(text):
    """Undo JobSpy's markdown escaping ("5\\+ years"), unify dashes, spell out number words."""
    t = (text or "").replace("\\", "").replace("–", "-").replace("—", "-")
    return re.sub(r"\b(" + "|".join(WORD_NUM) + r")\b(?=\s*(?:\+|or more)?\s*(?:years?|yrs?)\b)",
                  lambda m: WORD_NUM[m.group(1).lower()], t, flags=re.I)


ADV_DEG = re.compile(r"master|m\.?\s?tech|\bm\.?s\b|\bmba\b|ph\.?\s?d|doctor", re.I)
BACH_DEG = re.compile(r"bachelor|b\.?\s?tech|\bb\.?e\b|\bbs\b|undergrad", re.I)
PREFERRED = re.compile(r"preferred(?: qualifications| skills)?\s*:|preferred qualifications|nice to have|good to have|bonus points", re.I)


NOT_EXP = re.compile(r"\W*(?:ago|old|of (?:history|excellence|experience in the industry|legacy|innovation|operations)|"
                     r"in (?:business|the market)|anniversary|of (?:our|its|the company))", re.I)


def exp_info(text):
    """(years, evidence) for the experience a bachelor's-level candidate needs.
    'Bachelor's + 7 yrs, or Master's + 4, or PhD + 1' -> 7; numbers under 'Preferred' only count if nothing else does."""
    text = norm_text(text)
    pref = PREFERRED.search(text)
    found = []
    for rx in (EXP, EXP_LABEL):
        for m in rx.finditer(text):
            n = int(m.group(1))
            if n > 15 or NOT_EXP.match(text, m.end()):
                continue
            before = text[max(0, m.start() - 90):m.start()]
            adv = [x.start() for x in ADV_DEG.finditer(before)]
            bach = [x.start() for x in BACH_DEG.finditer(before)]
            deg = None if not (adv or bach) else ("adv" if max(adv, default=-1) > max(bach, default=-1) else "bach")
            snippet = text[max(0, m.start() - 70):m.end() + 50].replace("\n", " ").strip()
            found.append((n, deg, bool(pref and m.start() > pref.start()), snippet))
    if not found:
        return None, ""
    for pool in ([f for f in found if not f[2]], found):
        pool = [f for f in pool if f[1] != "adv"] or pool
        bach = [f for f in pool if f[1] == "bach"]
        if pool:
            # the headline requirement is the largest one: "10+ years Java, at least 2 in microservices" -> 10
            n, _, _, ev = max(bach or pool, key=lambda f: f[0])
            return n, ev
    return None, ""


def min_exp(text):
    return exp_info(text)[0]


def link_key(url):
    """Comparable link. Keeps the query string: Indeed/LinkedIn job ids live there (viewjob?jk=...);
    only tracking parameters are dropped. Empty links never match each other."""
    from urllib.parse import parse_qsl, urlencode, urlsplit
    if not url:
        return "nolink:" + str(id(url)) + os.urandom(4).hex()
    u = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith(("utm_", "ref", "src", "source", "trk"))]
    return f"{u.netloc.lower()}{u.path.rstrip('/')}?{urlencode(sorted(q))}"


def dup_key(j):
    keys = sorted(co_keys(j["company"])) or [(j["company"] or "").lower()]
    return keys[0] + "|" + title_norm(j["title"])


def norm_key(company, title, location):
    s = "|".join(re.sub(r"\W+", " ", (x or "").lower()).strip() for x in (company, title, (location or "").split(",")[0]))
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or "")))).strip()


def job(source, company, title, location, url, posted, desc="", ats=""):
    return dict(key=norm_key(company, title, location), source=source, ats=ats, company=company or "", title=title,
                location=location or "", url=url or "", posted=(posted or "")[:10], desc=desc or "")


# ---------------- sources ----------------

# India careers-site jobs by normalized company name, used to find the company's own posting
# for Indeed/LinkedIn/Naukri cards (filled by from_feashliaa)
ATS_INDEX = defaultdict(list)
CO_STOP = {"pvt", "private", "ltd", "limited", "inc", "llc", "llp", "india", "technologies", "technology", "tech",
           "solutions", "services", "group", "corp", "corporation", "the", "co", "company", "careers", "global",
           "systems", "labs", "plc", "gmbh", "software", "consulting"}


def co_keys(name):
    """'Hewlett Packard Enterprise | HPE' -> {'hewlettpackardenterprise', 'hpe'}; 'doordashindia' -> {'doordash'}."""
    keys = set()
    for part in re.split(r"[|/(),]", (name or "").lower()):
        toks = [t for t in re.findall(r"[a-z0-9]+", part) if t not in CO_STOP]
        if toks:
            keys.add("".join(toks))
    return {re.sub(r"(india|careers|jobs|global)$", "", k) or k for k in keys}


def title_norm(t):
    return " ".join(re.findall(r"[a-z0-9+#]+", (t or "").lower()))


def find_on_ats(j, min_ratio=0.9):
    """Same company + closest title among India careers-site jobs, or None."""
    from difflib import SequenceMatcher
    cands = [c for k in co_keys(j["company"]) for c in ATS_INDEX.get(k, [])]
    best, best_r = None, min_ratio
    for c in cands:
        r = SequenceMatcher(None, title_norm(j["title"]), title_norm(c["title"])).ratio()
        if r >= best_r:
            best, best_r = c, r
    return best


def from_feashliaa(data_dir, days=2):
    if not os.path.isdir(data_dir):
        subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/Feashliaa/job-board-data", data_dir], check=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for f in sorted(glob.glob(os.path.join(data_dir, "data/chunks/jobs_chunk_*.json.gz"))):
        with gzip.open(f, "rt") as fh:
            for j in json.load(fh):
                if not (j.get("title") and INDIA.search(j.get("location") or "")):
                    continue
                for k in co_keys(j.get("company")):
                    ATS_INDEX[k].append(dict(title=j["title"], url=j.get("url", ""), ats=j.get("ats", ""), company=j.get("company")))
                if (j.get("first_seen") or "") >= cutoff:
                    jb = job(f"ats:{j.get('ats', '')}", j.get("company"), j["title"], j["location"], j.get("url"), j.get("first_seen"), ats=j.get("ats", ""))
                    jb["skill_level"], jb["is_recruiter"] = j.get("skill_level"), j.get("is_recruiter")
                    out.append(jb)
    log.info("feashliaa: %d India jobs (first_seen within %d days); %d companies indexed for cross-checks",
             len(out), days, len(ATS_INDEX))
    return out


SEARCHES = ["AI engineer", "machine learning engineer", "GenAI LLM engineer", "data scientist", "Python developer", "backend engineer"]
LINKEDIN_SEARCHES = ["AI engineer", "machine learning engineer"]  # LinkedIn returned 429 after 4 searches, so keep it to 2
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
            if site in blocked or (site == "linkedin" and term not in LINKEDIN_SEARCHES):
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
                jb = job(f"jobspy:{site}", str(r.get("company") or ""), str(r.get("title") or ""),
                         str(r.get("location") or ""), r.get("job_url"), str(r.get("date_posted") or ""), desc)
                jb["pay"] = {k: r.get(k) for k in ("min_amount", "max_amount", "interval", "currency")}
                out.append(jb)
            log.info("jobspy %s '%s': %d results", site, term, n)
            time.sleep(pause)
    return out


# ---------------- salary ----------------

LPA_RANGE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:-|–|to)\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:lpa|l\.p\.a|lakhs?|lacs?)\b", re.I)
LPA_ONE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:lpa|l\.p\.a|lakhs? per annum)\b", re.I)
INR_RANGE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{7,})\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*([\d,]{7,})", re.I)
PER_YEAR = {"yearly": 1, "monthly": 12}


def _num(x):
    try:
        x = float(x)
        return None if x != x else x  # NaN from pandas
    except (TypeError, ValueError):
        return None


def salary(j):
    """(display text, max in lakhs/year or None). Published pay only; blank when not stated."""
    p = j.get("pay") or {}
    lo, hi, cur = _num(p.get("min_amount")), _num(p.get("max_amount")), str(p.get("currency") or "")
    interval = str(getattr(p.get("interval"), "value", p.get("interval")) or "").lower()
    if (lo or hi) and cur.upper() == "INR" and interval in PER_YEAR:
        lo_l, hi_l = [round(v * PER_YEAR[interval] / 1e5, 1) if v else None for v in (lo, hi)]
        return (f"₹{lo_l}–{hi_l} LPA" if lo_l and hi_l else f"₹{lo_l or hi_l} LPA"), hi_l or lo_l
    if lo or hi:
        return f"{cur} {int(lo or 0):,}–{int(hi or lo):,} {interval}".strip(), None
    d = j["desc"]
    if m := LPA_RANGE.search(d):
        return f"₹{m.group(1)}–{m.group(2)} LPA", float(m.group(2))
    if m := INR_RANGE.search(d):
        a, b = (round(int(g.replace(",", "")) / 1e5, 1) for g in m.groups())
        return f"₹{a}–{b} LPA", b
    if m := LPA_ONE.search(d):
        return f"₹{m.group(1)} LPA", float(m.group(1))
    return "", None


# ---------------- descriptions (official public ATS job APIs) ----------------

_ashby_boards = {}


class PostingClosed(Exception):
    """The careers site says this job no longer exists (404/410)."""


def _closed(r):
    if r is not None and r.status_code in (404, 410):
        raise PostingClosed(r.url)


def fetch_desc(j):
    u, ats = j["url"], j["ats"]
    if ats == "Greenhouse":
        m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", u)
        board, jid = m.groups() if m else (j["company"], (re.search(r"gh_jid=(\d+)", u) or [None, None])[1])
        if not jid:
            return ""
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}", headers=UA, timeout=15)
        _closed(r)
        return strip_html(r.json().get("content")) if r.ok else ""
    if ats == "Lever":
        m = re.search(r"lever\.co/([^/]+)/([\w-]+)", u)
        r = m and requests.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", headers=UA, timeout=15)
        _closed(r or None)
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
        board = _ashby_boards[m.group(1)]
        if board and m.group(2) not in board:
            raise PostingClosed(u)  # board listed fine but this job is gone
        return board.get(m.group(2), "")
    if ats == "Workday":
        m = re.search(r"https://(([^.]+)\.[^/]+)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+)", u)
        r = m and requests.get(f"https://{m.group(1)}/wday/cxs/{m.group(2)}/{m.group(3)}/job/{m.group(4)}", headers=UA, timeout=15)
        _closed(r or None)
        return strip_html(r.json().get("jobPostingInfo", {}).get("jobDescription")) if r and r.ok else ""
    return ""


def cross_check(jobs, limit=200):
    """Job-board cards with no description or no stated experience: look up the same job on the
    company's own careers site (official ATS API) and add that description."""
    todo = [j for j in jobs if j["source"].startswith("jobspy") and (not j["desc"] or min_exp(j["desc"]) is None)][:limit]
    found = got = 0
    for j in todo:
        hit = find_on_ats(j)
        if not hit:
            continue
        found += 1
        j["portal"] = hit["url"]
        try:
            d = fetch_desc(dict(hit, desc=""))
        except Exception as e:
            log.debug("cross-check fetch failed %s: %s", hit["url"], e)
            d = ""
        if d:
            got += 1
            j["desc"] = (j["desc"] + "\n\n" + d).strip()
        time.sleep(0.3)
    log.info("cross-check: %d job-board jobs checked, %d found on company careers sites, %d descriptions added",
             len(todo), found, got)


def add_descriptions(jobs, workers=4):
    """Only engineering-looking titles; up to 4 hosts in parallel, one request at a time per host."""
    todo = [j for j in jobs if not j["desc"] and j["ats"] and TITLE_OK.search(j["title"])][:MAX_FETCH]
    by_host = defaultdict(list)
    for j in todo:
        by_host[urlparse(j["url"]).netloc].append(j)
    tried, got = Counter(), Counter()

    def run(host_jobs):
        for j in host_jobs:
            try:
                j["desc"] = fetch_desc(j)
            except PostingClosed:
                j["closed"] = True
            except Exception as e:
                log.debug("desc fetch failed %s: %s", j["url"], e)
            tried[j["ats"]] += 1
            got[j["ats"]] += bool(j["desc"])
            time.sleep(0.3)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(run, by_host.values()))
    log.info("descriptions: fetched %d of %d attempted (%s)", sum(got.values()), len(todo),
             ", ".join(f"{a} {got[a]}/{tried[a]}" for a in sorted(tried)))


# ---------------- filtering + resume match ----------------

def hard_filter(j):
    """Drop only what is clearly wrong; the match % decides the rest."""
    if j.get("closed"):
        return False
    if not INDIA.search(j["location"] + (" India" if j["source"].startswith("jobspy") else "")):
        return False
    if TITLE_BAD.search(j["title"]) or j.get("is_recruiter") or j.get("skill_level") in ("senior", "intern"):
        return False
    if SENIOR_DESC.search(norm_text(j["desc"])):
        return False
    j["salary"], j["max_lpa"] = salary(j)
    if j["max_lpa"] is None:
        j["max_lpa"] = j.get("known_max_lpa")  # value already on the Notion card (recheck)
    if j["max_lpa"] is not None and j["max_lpa"] < MIN_LPA:
        return False
    exp, j["exp_evidence"] = exp_info(j["title"] + " " + j["desc"][:20000])
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
            # weighted share of everything the job asks for; +1.5 keeps one lucky keyword from scoring 100%
            mw, xw = sum(map(skill_w, matched)), sum(map(skill_w, missing))
            skill = mw / (mw + xw + 1.5) if wanted else 0.3
            if wanted and not matched:
                skill = 0
            sem = min(max((float(cos) - 0.55) / 0.2, 0), 1)
            title = 1.0 if TITLE_OK.search(j["title"]) else 0.0
            pct = 100 * (0.5 * max(skill, 0) + 0.25 * sem + 0.25 * title)
            if AI_TITLE.search(j["title"]):
                pct += AI_BONUS
            if ENTRY.search(j["title"]) or (j.get("min_exp") is not None and j["min_exp"] <= 2):
                pct += ENTRY_BONUS
            pct = min(pct, 100)
            if not j["desc"]:
                pct = min(pct * 0.85, STRONG - 1)  # title-only: too little evidence for Strong
            if LEADS_PEOPLE.search(j["desc"]):
                pct -= 10  # leading/guiding a team usually means a more senior hire
            fit = ("off-target" if OFF_TARGET.search(j["title"]) else "" if title else "non-engineering") or \
                  ("" if len(matched & CORE_SKILLS) >= MIN_CORE_FOR_STRONG or not j["desc"] else "no core skill")
            if fit:
                pct = min(pct, STRONG - 1)  # security/ops/QA/... or non-engineering title: at most "Maybe"
            j["fit"] = fit or "on-target"
            j.update(match=round(pct), matched=", ".join(sorted(matched)), missing=", ".join(sorted(missing)),
                     tier="Strong" if pct >= STRONG else "Maybe" if pct >= MAYBE else "")


# ---------------- outputs ----------------

DESC_MARKER = "Job description (as scored):"


def desc_blocks(j):
    para = lambda t: {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": t}}]}}
    text = re.sub(r"\s+\n", "\n", j["desc"]).strip()[:12000]
    return [para(f"Experience evidence: {j.get('exp_evidence') or 'not stated'}"), para(DESC_MARKER)] + \
           [para(text[i:i + 1900]) for i in range(0, len(text), 1900)]


def card_props(j, update=False):
    txt = lambda s: {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}
    props = {
        "Match %": {"number": j["match"]}, "Tier": {"select": {"name": j["tier"]}},
        "Matched skills": txt(j["matched"]), "Missing skills": txt(j["missing"]),
        "Desc": {"select": {"name": "full" if j["desc"] else "title-only"}},
        "Exp check": {"select": {"name": "Verify" if j.get("min_exp") is None else "Checked"}},
        "Exp evidence": txt(j.get("exp_evidence")), "Role fit": {"select": {"name": j.get("fit", "on-target")}},
        "Min exp (yrs)": {"number": j.get("min_exp")},
    }
    if j.get("portal"):
        props["Company portal"] = {"url": j["portal"]}
    if not update:
        props.update({
            "Role": {"title": [{"text": {"content": j["title"][:200]}}]}, "Company": txt(j["company"]),
            "Status": {"status": {"name": "New"}}, "Location": txt(j["location"]),
            "Source": {"select": {"name": j["source"][:90]}}, "Link": {"url": j["url"] or None},
            "Key": txt(j["key"]), "Salary": txt(j.get("salary")),
        })
        if j.get("max_lpa") is not None:
            props["Max salary (LPA)"] = {"number": j["max_lpa"]}
        if re.match(r"\d{4}-\d{2}-\d{2}$", j["posted"]):
            props["Posted"] = {"date": {"start": j["posted"]}}
    return props


def notion_add(jobs, token):
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    txt = lambda s: {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}
    r = requests.get(f"https://api.notion.com/v1/databases/{NOTION_DB}", headers=h, timeout=30)
    if not r.ok:  # 401 = wrong token, 404 = board not connected to the integration
        log.error("notion: cannot open Job Tracker (%s %s). Nothing marked as seen; fix access and re-run.", r.status_code, r.text[:200])
        sys.exit(1)
    on_board = set()
    for pg in notion_query(h, None):
        pr = pg["properties"]
        on_board.add(link_key(pr["Link"].get("url")))
        on_board.add(dup_key({"company": _plain(pr["Company"]), "title": _plain(pr["Role"])}))
    added, failed, skipped = 0, set(), 0
    for j in jobs:
        if dup_key(j) in on_board or link_key(j["url"]) in on_board:
            skipped += 1
            continue
        on_board.add(dup_key(j))
        props = card_props(j)
        body = {"parent": {"database_id": NOTION_DB}, "properties": props, **({"children": desc_blocks(j)} if j["desc"] else {})}
        r = requests.post("https://api.notion.com/v1/pages", headers=h, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            r = requests.post("https://api.notion.com/v1/pages", headers=h, json=body, timeout=30)
        if r.ok:
            added += 1
        else:
            failed.add(j["key"])
            log.warning("notion: %s %s", r.status_code, r.text[:200])
        time.sleep(0.35)
    log.info("notion: added %d of %d (%d already on the board)", added, len(jobs), skipped)
    return failed


def _plain(prop):
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text") or prop.get("title") or [])


def notion_query(h, flt):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100, **({"filter": flt} if flt else {}), **({"start_cursor": cursor} if cursor else {})}
        r = requests.post(f"https://api.notion.com/v1/databases/{NOTION_DB}/query", headers=h, json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        pages += d["results"]
        if not d.get("has_more"):
            return pages
        cursor = d["next_cursor"]


def page_text(h, page_id):
    """Plain text of a card's body (the description saved when the card was created)."""
    r = requests.get(f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100", headers=h, timeout=30)
    if not r.ok:
        return ""
    out = []
    for b in r.json().get("results", []):
        rt = b.get(b.get("type"), {}).get("rich_text", [])
        out.append("".join(t.get("plain_text", "") for t in rt))
    return "\n".join(out).split(DESC_MARKER, 1)[-1].strip()


def recheck(token, scorer):
    """Re-score every card still in 'New' with the current rules. Description comes from the careers-site API,
    else the copy saved in the card, else the same job on the company's careers site. Cards that now fail the
    rules (3+ yrs, senior, below Maybe) go to Notion trash; the rest get updated scores and evidence."""
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    pages = notion_query(h, {"property": "Status", "status": {"equals": "New"}})
    plain = lambda p, k: "".join(t["plain_text"] for t in p["properties"][k].get("rich_text") or p["properties"][k].get("title") or [])
    stats, cards = Counter(), []
    for pg in pages:
        pr = pg["properties"]
        src = (pr["Source"]["select"] or {}).get("name", "")
        j = job(src, plain(pg, "Company"), plain(pg, "Role"), plain(pg, "Location"), pr["Link"]["url"], "",
                ats=src[4:] if src.startswith("ats:") else "")
        j["page_id"], hit = pg["id"], None
        j["known_max_lpa"] = (pr.get("Max salary (LPA)") or {}).get("number")
        body = page_text(h, pg["id"])
        j["had_body"] = bool(body)
        try:
            if j["ats"]:
                j["desc"], j["desc_from"] = fetch_desc(j), "careers site"
            if not j["desc"] and body:
                j["desc"], j["desc_from"] = body, "saved copy"
            if not j["desc"] and (hit := find_on_ats(j)):
                j["desc"], j["desc_from"], j["portal"] = fetch_desc(dict(hit, desc="")), "company site match", hit["url"]
        except PostingClosed:
            j["closed"] = True
        except Exception as e:
            log.debug("recheck fetch failed %s: %s", j["url"], e)
        stats["desc from " + (j.get("desc_from", "none") if j["desc"] else "none")] += 1
        time.sleep(0.3)
        cards.append(j)
    keep = [j for j in cards if hard_filter(j)]
    scorer.score(keep)
    best = {}  # duplicates (same company + role, or same link): keep the best-scored card
    for j in sorted(keep, key=lambda j: -(j.get("match") or 0)):
        for k in (dup_key(j), link_key(j["url"])):
            if k in best:
                j["dup_of"] = best[k]
            best.setdefault(k, j["title"])  # cards with no description anywhere are scored title-only (Maybe at most)
    for j in cards:
        if j in keep and not j["desc"] and not j.get("tier"):
            j["tier"] = "Maybe"  # too little information to judge: keep it for you to verify, never delete
        if j not in keep or not j.get("tier") or j.get("dup_of"):
            why = ("posting closed" if j.get("closed") else "fails rules" if j not in keep else
                   "duplicate card" if j.get("dup_of") else f"match {j['match']}% below Maybe")
            log.info("recheck: trash '%s' at %s (%s; min exp %s)", j["title"], j["company"], why, j.get("min_exp"))
            patch, stats["trashed"] = {"archived": True}, stats["trashed"] + 1
        else:
            stats["title-only" if not j["desc"] else "rescored"] += 1
            patch = {"properties": card_props(j, update=True)}
        r = requests.patch(f"https://api.notion.com/v1/pages/{j['page_id']}", headers=h, json=patch, timeout=30)
        if not r.ok:
            log.warning("recheck: notion %s %s", r.status_code, r.text[:200])
        elif "properties" in patch and j["desc"] and not j["had_body"]:
            requests.patch(f"https://api.notion.com/v1/blocks/{j['page_id']}/children", headers=h,
                           json={"children": desc_blocks(j)}, timeout=30)
        time.sleep(0.35)
    log.info("recheck: %d New cards: %s", len(cards), dict(stats))


def main():
    args = set(sys.argv[1:])
    if "--recheck" in args:
        from_feashliaa(os.environ.get("FEASHLIAA_DIR", "job-board-data"))  # builds ATS_INDEX for cross-checks
        recheck(os.environ["NOTION_TOKEN"], Scorer(open("resume.txt").read()))
        return
    os.makedirs("data", exist_ok=True)
    seen = set(open(SEEN_FILE).read().split()) if os.path.exists(SEEN_FILE) else set()

    # --jobspy-only (evening run): Feashliaa refreshes once a day, so the morning run covers it
    recent = from_feashliaa(os.environ.get("FEASHLIAA_DIR", "job-board-data"))  # also builds ATS_INDEX
    jobs = [] if "--jobspy-only" in args else recent
    if "--no-jobspy" not in args:
        jobs += from_jobspy()
    uniq, by_role = {}, set()
    for j in jobs:
        if j["key"] in seen or dup_key(j) in by_role:
            continue  # same company + title from another source/city is one card
        by_role.add(dup_key(j))
        uniq.setdefault(j["key"], j)
    jobs = list(uniq.values())
    cands = [j for j in jobs if hard_filter(j)]
    log.info("%d new jobs, %d pass the hard filter", len(jobs), len(cands))

    if "--no-fetch" not in args:
        add_descriptions(sorted(cands, key=lambda j: not TITLE_OK.search(j["title"])))
        cross_check(cands)
        cands = [j for j in cands if hard_filter(j)]  # re-check experience now that descriptions are in
    Scorer(open("resume.txt").read(), "--no-embed" not in args).score(cands)
    board = sorted([j for j in cands if j["tier"]], key=lambda j: -j["match"])
    log.info("board: %d Strong, %d Maybe", sum(j["tier"] == "Strong" for j in board), sum(j["tier"] == "Maybe" for j in board))

    cols = ["match", "tier", "title", "company", "location", "source", "salary", "min_exp", "matched", "missing", "url", "posted", "key"]
    with open(ALL_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(cands, key=lambda j: -j["match"]))

    if "--dry-run" in args:
        log.info("dry run: wrote %s, Notion skipped", ALL_FILE)
        return
    # board jobs that failed or overflowed the cap stay unseen, so the next run retries them
    retry = notion_add(board[:MAX_NOTION], os.environ["NOTION_TOKEN"]) | {j["key"] for j in board[MAX_NOTION:]}
    with open(SEEN_FILE, "a") as f:
        f.writelines(j["key"] + "\n" for j in jobs if j["key"] not in retry)


if __name__ == "__main__":
    main()
