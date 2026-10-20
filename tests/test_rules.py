"""Regression tests built from postings that were mis-scored on the board.
Run: python -m pytest tests -q   (add MODEL_CACHE=<dir> to reuse the embedding model)"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import collector as c  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def jb(title, desc, source="jobspy:indeed", company="co"):
    return c.job(source, company, title, "Bengaluru", "u", "", desc)


@pytest.mark.parametrize("text, years", [
    ((FIX / "cisco_ai_ops.txt").read_text(), 7),        # bachelor's 7+ / master's 4+ / PhD 1+ -> 7, not 1
    ((FIX / "deloitte_ai_security.txt").read_text(), 2),
    ("Requirements: 5\\+ years of experience in Python", 5),  # JobSpy markdown escaping
    ("3–5 years experience", 3),
    ("Minimum five years of experience", 5),
    ("Years of experience: 4", 4),
    ("5 or more years building APIs", 5),
    ("Bachelor's degree with 2+ years. Preferred: 5+ years in fintech.", 2),  # preferred section ignored
    ("Preferred qualifications: 3+ years", 3),          # only a preferred number -> still used
    ("fresh graduates welcome", None),
])
def test_experience(text, years):
    assert c.min_exp(text) == years


def test_cisco_dropped_deloitte_kept():
    assert not c.hard_filter(jb("AI Operations Engineer- AI Agent, AI Tool", (FIX / "cisco_ai_ops.txt").read_text()))
    assert c.hard_filter(jb("Cyber- DT&P- Infrastructure - AI Security Analyst", (FIX / "deloitte_ai_security.txt").read_text()))


@pytest.mark.parametrize("title, desc, ok", [
    ("Backend AI Engineer", "We are looking for a Senior Backend AI Engineer to lead", False),
    ("AI Engineer", "You will work with senior engineers. 0-2 years", True),
    ("AI Trainer / LLM Model Trainer", "Evaluate LLM responses. 2-6 years", False),
])
def test_hard_filter(title, desc, ok):
    assert c.hard_filter(jb(title, desc)) is ok


@pytest.fixture(scope="module")
def scorer():
    return c.Scorer((ROOT / "resume.txt").read_text(), use_embeddings=True)


def scored(scorer, title, desc):
    j = jb(title, desc)
    assert c.hard_filter(j)
    scorer.score([j])
    return j


def test_scoring_order(scorer):
    backend = scored(scorer, "Backend Engineer", "Payment microservices in Java, Spring Boot, Kafka, Redis, PostgreSQL. 1-3 years.")
    ai = scored(scorer, "AI Engineer", "Build RAG pipelines and LLM agents in Python, FastAPI, LangChain, embeddings. 0-2 years.")
    security = scored(scorer, "Cyber- DT&P- Infrastructure - AI Security Analyst", (FIX / "deloitte_ai_security.txt").read_text())
    generic = scored(scorer, "Software Engineer", "Git, Linux, SQL, REST APIs. 1 year.")
    golang = scored(scorer, "Software Engineer", "Golang, Kubernetes, Terraform, GCP microservices. 2 years.")
    title_only = scored(scorer, "ML Engineer", "")
    assert backend["tier"] == ai["tier"] == "Strong"
    assert security["tier"] != "Strong" and security["fit"] == "off-target"
    assert generic["tier"] != "Strong" and generic["fit"] == "no core skill"
    assert golang["tier"] != "Strong"
    assert title_only["tier"] != "Strong"
    assert backend["match"] > security["match"]


def test_recheck_rescores_and_trashes(scorer, monkeypatch):
    def page(pid, src, role, link):
        rt = lambda v: {"rich_text": [{"plain_text": v}]}
        return {"id": pid, "properties": {"Source": {"select": {"name": src}}, "Company": rt("co"), "Location": rt("Bengaluru"),
                                          "Role": {"title": [{"plain_text": role}]}, "Link": {"url": link}}}
    pages = [page("1", "jobspy:indeed", "AI Operations Engineer- AI Agent, AI Tool", "https://in.indeed.com/viewjob?jk=1"),
             page("2", "jobspy:indeed", "AI Engineer", "https://in.indeed.com/viewjob?jk=2"),
             page("3", "jobspy:linkedin", "Backend Engineer", "https://linkedin.com/jobs/3")]
    bodies = {"1": (FIX / "cisco_ai_ops.txt").read_text(),
              "2": "Build RAG pipelines and LLM agents in Python, FastAPI, LangChain. 0-2 years.", "3": ""}
    monkeypatch.setattr(c, "notion_query", lambda h, flt: pages)
    monkeypatch.setattr(c, "page_text", lambda h, pid: bodies[pid])
    monkeypatch.setattr(c, "find_on_ats", lambda j: None)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    patches = {}

    class R:
        ok, status_code, text = True, 200, ""

    monkeypatch.setattr(c.requests, "patch", lambda url, **k: (patches.setdefault(url.split("/")[-1], k["json"]), R())[1])
    c.recheck("t", scorer)
    assert patches["1"] == {"archived": True}                      # 7+ years -> trashed
    assert patches["2"]["properties"]["Tier"]["select"]["name"] == "Strong"
    p3 = patches["3"]["properties"]                                    # no description anywhere
    assert p3["Exp check"]["select"]["name"] == "Verify" and p3["Tier"]["select"]["name"] != "Strong"


# ---- cases found in the 2026-10-20 board audit ----

@pytest.mark.parametrize("text, years", [
    ("10+ years of hands-on experience in Java development, with at least 2 years in microservices architecture.", 10),
    ("Required Skills 7+ years in data engineering; 2+ years with Databricks.", 7),
    ("AI Engineer with 3-7 years of experience, including a minimum of 2 years in Generative AI (GenAI)", 3),
    ("We are a company with 12 years of history. You bring 1-2 years of experience.", 1),
])
def test_experience_headline_requirement(text, years):
    assert c.min_exp(text) == years


def test_lead_experience_is_senior():
    assert not c.hard_filter(jb("Software Engineer (C++)", "2-5 years of experience as a Technical Lead or Manager"))


@pytest.mark.parametrize("title, desc", [
    ("Associate Software Engineer", "Join our payments team. 0-2 years."),                       # one domain word only
    ("Guidewire Configuration Developer - ClaimCenter", "Payments, REST APIs, Java. 2-5 years."),
    ("Software Development Engineer in Test", "Python, Java, LLM test automation. 2 years."),
    ("Software Engineer (DevSecOps)", "Python, Java, Kafka pipelines. 2 years."),
])
def test_not_strong(scorer, title, desc):
    assert scored(scorer, title, desc)["tier"] != "Strong"


def test_closed_posting_is_dropped(monkeypatch):
    class R:
        status_code, ok, url = 404, False, "https://x/job"
    monkeypatch.setattr(c.requests, "get", lambda *a, **k: R())
    j = c.job("ats:Greenhouse", "acme", "Software Engineer", "Bengaluru", "https://job-boards.greenhouse.io/acme/jobs/1", "", ats="Greenhouse")
    with pytest.raises(c.PostingClosed):
        c.fetch_desc(j)
    j["closed"] = True
    assert not c.hard_filter(j)


def test_same_role_from_two_sources_is_one_key():
    a = c.job("jobspy:indeed", "HP", "Data Engineer", "KA, India", "u1", "")
    b = c.job("ats:Workday", "hp", "Data Engineer", "Bengaluru, Karnataka, India", "u2", "")
    assert c.dup_key(a) == c.dup_key(b)


def test_indeed_links_are_not_duplicates():
    # regression: stripping the query made every Indeed card look identical
    a, b = "https://in.indeed.com/viewjob?jk=395324ab6bf2aa76", "https://in.indeed.com/viewjob?jk=f4191a64e95b7acd"
    assert c.link_key(a) != c.link_key(b)
    assert c.link_key(a) == c.link_key(a + "&utm_source=x")
    assert c.link_key("") != c.link_key("")


def test_recheck_keeps_distinct_indeed_cards(scorer, monkeypatch):
    def page(pid, role, company, link):
        rt = lambda v: {"rich_text": [{"plain_text": v}]}
        return {"id": pid, "properties": {"Source": {"select": {"name": "jobspy:indeed"}}, "Company": rt(company),
                                          "Location": rt("Bengaluru"), "Role": {"title": [{"plain_text": role}]}, "Link": {"url": link}}}
    pages = [page("1", "Software Engineer : AI", "Apple", "https://in.indeed.com/viewjob?jk=1"),
             page("2", "Applied Scientist II", "Amazon", "https://in.indeed.com/viewjob?jk=2"),
             page("3", "Data Engineer", "HP", "https://in.indeed.com/viewjob?jk=3"),
             page("4", "Data Engineer", "hp", "https://hp.wd5.myworkdayjobs.com/x/job/y")]   # real duplicate of 3
    monkeypatch.setattr(c, "notion_query", lambda h, flt: pages)
    monkeypatch.setattr(c, "page_text", lambda h, pid: "")
    monkeypatch.setattr(c, "find_on_ats", lambda j: None)
    monkeypatch.setattr(c, "fetch_desc", lambda j: "")
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    patches = {}

    class R:
        ok, status_code, text = True, 200, ""

    monkeypatch.setattr(c.requests, "patch", lambda url, **k: (patches.setdefault(url.split("/")[-1], k["json"]), R())[1])
    c.recheck("t", scorer)
    trashed = [k for k, v in patches.items() if v == {"archived": True}]
    assert trashed == ["4"] or trashed == ["3"]   # only the HP duplicate goes


@pytest.mark.parametrize("desc, pay, ok", [
    ("Java backend. 1-3 years. CTC 6-12 LPA", {}, False),          # tops out below 15
    ("Java backend. 1-3 years. CTC 10-18 LPA", {}, True),          # range reaches 15+
    ("Java backend. 1-3 years.", {}, True),                         # not stated -> kept
    ("Java backend. 1-3 years.", {"min_amount": 400000, "max_amount": 900000, "interval": "yearly", "currency": "INR"}, False),
    ("Java backend. 1-3 years.", {"min_amount": 150000, "max_amount": 200000, "interval": "monthly", "currency": "INR"}, True),  # 24 LPA
])
def test_min_salary(desc, pay, ok):
    j = jb("Backend Engineer", desc)
    j["pay"] = pay
    assert c.hard_filter(j) is ok


def test_min_salary_uses_card_value_in_recheck():
    j = jb("Backend Engineer", "")
    j["known_max_lpa"] = 8
    assert not c.hard_filter(j)


def test_notion_call_retries_timeouts(monkeypatch):
    calls = []

    class R:
        status_code, ok, headers = 200, True, {}

    def flaky(url, **k):
        calls.append(url)
        if len(calls) < 3:
            raise c.requests.exceptions.ReadTimeout("slow")
        return R()

    monkeypatch.setattr(c.requests, "get", flaky)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    assert c.notion_call("get", "https://api.notion.com/v1/x").ok and len(calls) == 3
