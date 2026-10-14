"""Daily job collector: Feashliaa ATS data + gentle JobSpy -> Google Sheet.
Collection and tracking only. No applying, no proxies, no block bypassing."""
import glob, gzip, hashlib, json, logging, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|delhi|kolkata|ahmedabad|kochi|jaipur|remote[- ]?india)\b", re.I)
TITLE_OK = re.compile(r"\b(software (development )?engineer|sde|backend|back[- ]end|java|python|ai engineer|ml engineer|machine learning engineer|platform engineer|developer|llm|genai|applied ai)\b", re.I)
TITLE_BAD = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|head|director|architect|vp|iii|iv|sde[- ]?(2|ii|3)|engineer (2|ii|3)|intern(ship)?|frontend|front[- ]end|ios|android|qa|test|sdet|sales|support|recruit|embedded|firmware)\b", re.I)
EXP = re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2}|to\s*\d{1,2})?\s*\+?\s*(?:years|yrs)", re.I)
BLR = re.compile(r"bengaluru|bangalore", re.I)

COLUMNS = ["key", "added_on", "relevant", "source", "company", "title", "location", "min_exp_years",
           "experience_flag", "bengaluru", "url", "posted", "apply?", "status", "notes"]

SEARCHES = [
    "SDE 1 Java", "backend engineer", "software engineer Spring Boot",
    "AI engineer", "ML engineer python",
]
SITES = {
    "indeed": dict(site_name=["indeed"], country_indeed="India", location="Bengaluru"),
    "naukri": dict(site_name=["naukri"], location="Bengaluru"),
    "linkedin": dict(site_name=["linkedin"], location="Bengaluru, India"),
}
GREEN = {"red": 0.72, "green": 0.88, "blue": 0.73}


def keep(title, location):
    return bool(title) and INDIA.search(location or "") and TITLE_OK.search(title) and not TITLE_BAD.search(title)


def min_exp(text):
    nums = [int(m.group(1)) for m in EXP.finditer(text or "") if int(m.group(1)) <= 20]
    return min(nums) if nums else None


def norm_key(company, title, location):
    s = "|".join(re.sub(r"\W+", " ", (x or "").lower()).strip() for x in (company, title, location.split(",")[0] if location else ""))
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def row(source, company, title, location, url, posted, exp, relevant):
    flag = "" if exp is None else ("OK" if exp <= 2 else "TOO SENIOR?")
    return dict(key=norm_key(company, title, location), added_on=datetime.now(timezone.utc).date().isoformat(),
                relevant="yes" if relevant and (exp is None or exp <= 2) else "", source=source, company=company or "", title=title, location=location or "",
                min_exp_years="" if exp is None else exp, experience_flag=flag,
                bengaluru="yes" if BLR.search(location or "") else "", url=url or "",
                posted=posted or "", **{"apply?": False}, status="", notes="")


def from_feashliaa(data_dir, days=3):
    if not os.path.isdir(data_dir):
        subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/Feashliaa/job-board-data", data_dir], check=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for f in sorted(glob.glob(os.path.join(data_dir, "data/chunks/jobs_chunk_*.json.gz"))):
        with gzip.open(f, "rt") as fh:
            for j in json.load(fh):
                if (j.get("first_seen") or "") < cutoff or not j.get("title") or not INDIA.search(j.get("location") or ""):
                    continue
                rel = keep(j["title"], j["location"]) and not j.get("is_recruiter") and j.get("skill_level") not in ("senior", "intern")
                out.append(row(f"ats:{j.get('ats','')}", j.get("company"), j["title"], j["location"], j.get("url"), j.get("first_seen", "")[:10], min_exp(j["title"]), rel))
    log.info("feashliaa: %d India jobs (first_seen within %d days)", len(out), days)
    return out


def from_jobspy(scrape=None, pause=20):
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    out, blocked = [], set()
    for term in SEARCHES:
        for site, kw in SITES.items():
            if site in blocked:
                continue
            try:
                df = scrape(search_term=term, results_wanted=20 if site == "linkedin" else 30, hours_old=24, **kw)
            except Exception as e:  # 429 / block / network: skip site for the day, no retries
                log.warning("jobspy %s blocked or failed (%s); skipping it today", site, str(e)[:150])
                blocked.add(site)
                continue
            if df is None or df.empty:
                log.info("jobspy %s '%s': 0 results", site, term)
            else:
                n = 0
                for r in df.to_dict("records"):
                    title, loc = str(r.get("title") or ""), str(r.get("location") or "")
                    exp = min_exp(" ".join(str(r.get(c) or "") for c in ("experience_range", "description")))
                    out.append(row(f"jobspy:{site}", r.get("company"), title, loc, r.get("job_url"), str(r.get("date_posted") or ""), exp, keep(title, loc + " India")))
                    n += 1
                log.info("jobspy %s '%s': %d results", site, term, n)
            time.sleep(pause)
    return out


def dedupe(rows, seen):
    out = {}
    for r in rows:
        if r["key"] in seen or r["key"] in out:
            continue
        out[r["key"]] = r
    return list(out.values())


def sheet():
    import gspread
    gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    ws = gc.open_by_key(os.environ.get("SHEET_ID") or "1PGRnJgCJPA89j5LjHjnUdedwbEF1WZcpnlnwcad4pGg").sheet1
    if ws.row_values(1) != COLUMNS:
        ws.update("A1", [COLUMNS])
    return ws


def main():
    dry = "--dry-run" in sys.argv
    rows = from_feashliaa(os.environ.get("FEASHLIAA_DIR", "job-board-data"))
    if "--no-jobspy" not in sys.argv:
        rows += from_jobspy()
    ws = None if dry else sheet()
    seen = set() if dry else set(ws.col_values(1)[1:])
    new = dedupe(rows, seen)
    new.sort(key=lambda r: (r["relevant"] != "yes", r["bengaluru"] != "yes", r["experience_flag"] == "TOO SENIOR?", r["company"]))
    log.info("%d new jobs after de-duplication", len(new))
    if dry:
        with open("preview.csv", "w") as f:
            import csv
            w = csv.DictWriter(f, COLUMNS); w.writeheader(); w.writerows(new)
        log.info("dry run: wrote preview.csv")
    elif new:
        start = len(ws.col_values(1)) + 1
        ws.append_rows([[r[c] for c in COLUMNS] for r in new], value_input_option="USER_ENTERED")
        last = chr(ord("A") + len(COLUMNS) - 1)
        green = [{"range": f"A{start + i}:{last}{start + i}", "format": {"backgroundColor": GREEN}}
                 for i, r in enumerate(new) if r["relevant"] == "yes"]
        if green:
            ws.batch_format(green)
        log.info("appended %d rows, %d marked green", len(new), len(green))


if __name__ == "__main__":
    main()
