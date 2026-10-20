"""Application assistant for the Notion Job Tracker. It prepares, you apply: nothing here submits anything anywhere.

  python assist.py            packs for new Strong cards + follow-up dates + interview prep
  python assist.py --packs    only application packs
  python assist.py --track    only Applied/Interview follow-ups

Application pack (Strong cards in New): tailored CV bullets, a short cover note, answers to the usual form
questions, a referral message and a LinkedIn people-search link to find someone to send it to.
Interview prep (cards moved to Interview): likely topics, questions and which of your projects to talk about.
Every text is written from resume.txt only; anything the CV does not say is left as [fill in]."""
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import urllib.parse

import requests

import collector as c

log = logging.getLogger("assist")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
GH_MODEL = os.environ.get("GH_MODEL", "openai/gpt-4.1")  # GitHub Models: free with the workflow's GITHUB_TOKEN
MAX_PACKS = int(os.environ.get("MAX_PACKS", "15"))  # per run; keeps API cost to a few cents
FOLLOW_UP_DAYS, NO_REPLY_DAYS = 7, 21
DESC_CHARS = {"claude": 12000, "github": 9000}  # GitHub Models' free tier caps input at about 8k tokens
PACK_HEADING, PREP_HEADING = "Application pack", "Interview prep"

RULES = """Rules:
- Use ONLY facts from the CV. Never invent employers, numbers, skills, degrees or years. If the job wants something
  the CV lacks, do not claim it; mention a related real skill instead.
- Unknown personal facts (notice period, current or expected pay, relocation) are written as [fill in].
- Plain, specific, human tone. No buzzwords, no "I am writing to express my interest".
- Reply with one JSON object only, no markdown fences."""

PACK_PROMPT = """You help a software engineer apply for a job. CV:
<cv>
{cv}
</cv>
Job: {title} at {company} ({location})
<job_description>
{desc}
</job_description>
{rules}
JSON keys:
"fit_summary": 2 sentences: why this CV fits, and the biggest gap to be honest about.
"cv_bullets": list of 4-6 CV bullets reworded and reordered to lead with what this job asks for (same facts, the job's words).
"cover_note": 60-90 words for an application form or email.
"form_answers": object with "Why this company", "Why this role", "Relevant experience", "Years of experience",
  "Notice period", "Expected CTC" (short answers).
"referral_message": under 70 words, for a LinkedIn message to an engineer at the company asking for a referral; names
  the role and one concrete matching project or achievement; has [Name] for the recipient.
"referral_search": 2-4 words to search LinkedIn people at this company, e.g. "machine learning engineer"."""

PREP_PROMPT = """A software engineer has an interview for this job. CV:
<cv>
{cv}
</cv>
Job: {title} at {company}
<job_description>
{desc}
</job_description>
{rules}
JSON keys:
"rounds": list of the likely interview rounds for this kind of role and company (say it is a typical pattern, not known).
"dsa_topics": list of 5-8 DSA topics worth revising for this role.
"design_questions": list of 3-5 system or ML design questions this role could ask.
"tech_questions": list of 6-10 likely technical questions drawn from the job description.
"project_talking_points": list of 3-5 points: which CV project or work answers which job requirement, with the numbers to quote.
"weak_spots": list of the job's requirements the CV is thin on, each with an honest one-line way to answer.
"questions_to_ask": list of 3 good questions to ask the interviewer."""


def backend():
    """(name, key): Claude if ANTHROPIC_API_KEY is set, else GitHub Models via GITHUB_TOKEN (free), else None."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude", os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("GITHUB_TOKEN"):
        return "github", os.environ["GITHUB_TOKEN"]
    return None, None


def llm(prompt, key, kind="github"):
    for attempt in range(3):
        if kind == "claude":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, json={
                "model": MODEL, "max_tokens": 2500, "messages": [{"role": "user", "content": prompt}]},
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        else:
            r = requests.post("https://models.github.ai/inference/chat/completions", timeout=120, json={
                "model": GH_MODEL, "max_tokens": 2500, "temperature": 0.3, "messages": [{"role": "user", "content": prompt}]},
                headers={"Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json"})
        if r.status_code in (429, 500, 502, 503, 529):
            if r.status_code == 429 and attempt == 2:
                raise DailyLimit(r.text[:200])
            time.sleep(float(r.headers.get("Retry-After", 0)) or 15 * (attempt + 1))
            continue
        r.raise_for_status()
        d = r.json()
        text = "".join(b.get("text", "") for b in d["content"]) if kind == "claude" else d["choices"][0]["message"]["content"]
        return json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    r.raise_for_status()


class DailyLimit(Exception):
    pass


CLAIM_KEYS = ("cv_bullets", "cover_note", "referral_message")  # fit_summary may name gaps; form answers hold [fill in]
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def unsupported(pack, cv):
    """Skills and numbers in the pack's claims that the CV does not contain."""
    text = " ".join(json.dumps(pack.get(k, "")) for k in CLAIM_KEYS)
    bad = sorted(c.skills_in(text) - c.skills_in(cv))
    cv_nums = {n.replace(",", "") for n in NUM.findall(cv)}
    for n in NUM.findall(text):
        v = n.replace(",", "")
        small = not v.endswith("%") and v.replace(".", "").isdigit() and float(v) < 5  # "2 projects", "1.5 years"
        if v not in cv_nums and v.rstrip("%") not in cv_nums and not small:
            bad.append(n)
    return bad


def as_blocks(heading, data):
    """Turn the JSON answer into Notion blocks: a heading, then a sub-heading + paragraph/bullets per key."""
    txt = lambda s: [{"text": {"content": str(s)[:1900]}}]
    blk = lambda kind, s: {"object": "block", "type": kind, kind: {"rich_text": txt(s)}}
    out = [blk("heading_2", heading)]
    for k, v in data.items():
        out.append(blk("heading_3", k.replace("_", " ").capitalize()))
        if isinstance(v, dict):
            out += [blk("bulleted_list_item", f"{a}: {b}") for a, b in v.items()]
        elif isinstance(v, list):
            out += [blk("bulleted_list_item", x) for x in v]
        else:
            out.append(blk("paragraph", v))
    return out


def referral_url(company, words):
    q = urllib.parse.quote(f"{company} {words}".strip())
    return f"https://www.linkedin.com/search/results/people/?keywords={q}&origin=GLOBAL_SEARCH_HEADER"


def card(pg):
    pr = pg["properties"]
    return {"id": pg["id"], "title": c._plain(pr["Role"]), "company": c._plain(pr["Company"]),
            "location": c._plain(pr["Location"]), "status": pr["Status"]["status"]["name"],
            "pack": (pr.get("Pack", {}).get("select") or {}).get("name"),
            "prep": (pr.get("Prep", {}).get("select") or {}).get("name"),
            "applied_on": ((pr.get("Applied on", {}).get("date") or {}).get("start")),
            "follow_up": (pr.get("Follow-up", {}).get("select") or {}).get("name")}


def write(h, page_id, props=None, blocks=None):
    if blocks:
        for i in range(0, len(blocks), 90):  # Notion takes at most 100 children per request
            c.notion_call("patch", f"https://api.notion.com/v1/blocks/{page_id}/children", headers=h,
                          json={"children": blocks[i:i + 90]}, timeout=30).raise_for_status()
    if props:
        c.notion_call("patch", f"https://api.notion.com/v1/pages/{page_id}", headers=h,
                      json={"properties": props}, timeout=30).raise_for_status()
    time.sleep(0.35)


def make_packs(h, key, cv, kind="github"):
    flt = {"and": [{"property": "Tier", "select": {"equals": "Strong"}},
                   {"property": "Status", "status": {"equals": "New"}},
                   {"property": "Pack", "select": {"is_empty": True}}]}
    cards = [card(p) for p in c.notion_query(h, flt)][:MAX_PACKS]
    done = 0
    for j in cards:
        desc = c.page_text(h, j["id"])
        if len(desc) < 300:
            log.info("pack: skip '%s' at %s (no description saved)", j["title"], j["company"])
            continue
        prompt = PACK_PROMPT.format(cv=cv, desc=desc[:DESC_CHARS[kind]], rules=RULES, **j)
        try:
            d = llm(prompt, key, kind)
            if bad := unsupported(d, cv):  # one retry, told exactly what it made up
                d = llm(prompt + f"\nYour previous answer claimed things not in the CV: {bad}. Remove them.", key, kind)
                if bad := unsupported(d, cv):
                    log.warning("pack: '%s' at %s dropped, still claims %s", j["title"], j["company"], bad)
                    continue
        except DailyLimit:
            log.warning("pack: model's daily limit reached; the rest wait for the next run")
            break
        except Exception as e:
            log.warning("pack: '%s' at %s failed: %s", j["title"], j["company"], e)
            continue
        words = d.pop("referral_search", "software engineer")
        write(h, j["id"], blocks=as_blocks(PACK_HEADING, d),
              props={"Pack": {"select": {"name": "Ready"}}, "Referral search": {"url": referral_url(j["company"], words)}})
        done += 1
    log.info("packs: %d written (%d Strong cards waiting)", done, len(cards))


def make_prep(h, key, cv, kind="github"):
    flt = {"and": [{"property": "Status", "status": {"equals": "Interview"}},
                   {"property": "Prep", "select": {"is_empty": True}}]}
    for j in map(card, c.notion_query(h, flt)):
        try:
            d = llm(PREP_PROMPT.format(cv=cv, desc=c.page_text(h, j["id"])[:DESC_CHARS[kind]] or "(not saved)", rules=RULES, **j), key, kind)
        except DailyLimit:
            log.warning("prep: model's daily limit reached")
            break
        except Exception as e:
            log.warning("prep: '%s' failed: %s", j["title"], e)
            continue
        write(h, j["id"], blocks=as_blocks(PREP_HEADING, d), props={"Prep": {"select": {"name": "Ready"}}})
        log.info("prep: ready for '%s' at %s", j["title"], j["company"])


def follow_up_state(applied_on, today):
    days = (today - dt.date.fromisoformat(applied_on[:10])).days
    return "No response" if days >= NO_REPLY_DAYS else "Due" if days >= FOLLOW_UP_DAYS else "Waiting"


def track(h, today=None):
    """Applied cards: stamp the date you moved them, then mark when a follow-up is due."""
    today = today or dt.date.today()
    for j in map(card, c.notion_query(h, {"property": "Status", "status": {"equals": "Applied"}})):
        props = {}
        if not j["applied_on"]:
            j["applied_on"] = today.isoformat()
            props["Applied on"] = {"date": {"start": j["applied_on"]}}
        state = follow_up_state(j["applied_on"], today)
        if state != j["follow_up"] and j["follow_up"] != "Sent":  # you set Sent after following up
            props["Follow-up"] = {"select": {"name": state}}
        if props:
            write(h, j["id"], props=props)
            log.info("track: '%s' at %s -> %s", j["title"], j["company"], state)


def main():
    args = set(sys.argv[1:])
    h = {"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}", "Notion-Version": "2022-06-28",
         "Content-Type": "application/json"}
    if "--packs" not in args:
        track(h)
    if "--track" in args:
        return
    kind, key = backend()
    if not key:
        log.warning("no model key (ANTHROPIC_API_KEY or GITHUB_TOKEN): skipping packs and interview prep")
        return
    log.info("writing with %s", MODEL if kind == "claude" else GH_MODEL)
    cv = open("resume.txt").read()
    make_packs(h, key, cv, kind)
    if "--packs" not in args:
        make_prep(h, key, cv, kind)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
