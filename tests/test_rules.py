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
    assert patches["3"]["properties"]["Exp check"]["select"]["name"] == "Verify"  # no description anywhere
