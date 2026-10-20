"""Application assistant: block layout, follow-up dates and pack flow (Notion and Claude mocked)."""
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import assist as a  # noqa: E402


def test_follow_up_states():
    t = dt.date(2026, 10, 30)
    assert a.follow_up_state("2026-10-27", t) == "Waiting"
    assert a.follow_up_state("2026-10-23", t) == "Due"
    assert a.follow_up_state("2026-10-01", t) == "No response"


def test_blocks_cover_every_key():
    b = a.as_blocks("Application pack", {"cover_note": "hi", "cv_bullets": ["x", "y"], "form_answers": {"Notice period": "[fill in]"}})
    kinds = [x["type"] for x in b]
    assert kinds == ["heading_2", "heading_3", "paragraph", "heading_3", "bulleted_list_item", "bulleted_list_item",
                     "heading_3", "bulleted_list_item"]


def test_referral_url_is_people_search():
    u = a.referral_url("Cisco", "machine learning engineer")
    assert u.startswith("https://www.linkedin.com/search/results/people/") and "Cisco%20machine" in u


def page(pid, status, desc_len=400):
    rt = lambda v: {"rich_text": [{"plain_text": v}]}
    return {"id": pid, "properties": {"Role": {"title": [{"plain_text": "AI Engineer"}]}, "Company": rt("Acme"),
                                      "Location": rt("Bengaluru"), "Status": {"status": {"name": status}}}}


def test_packs_skip_cards_without_description(monkeypatch):
    monkeypatch.setattr(a.c, "notion_query", lambda h, f: [page("1", "New"), page("2", "New")])
    monkeypatch.setattr(a.c, "page_text", lambda h, pid: "x" * 500 if pid == "1" else "")
    monkeypatch.setattr(a, "llm", lambda p, k, kind="github": {"cover_note": "hi", "referral_search": "ml engineer"})
    writes = []
    monkeypatch.setattr(a, "write", lambda h, pid, props=None, blocks=None: writes.append((pid, props)))
    a.make_packs({}, "k", "cv")
    assert [w[0] for w in writes] == ["1"] and writes[0][1]["Pack"]["select"]["name"] == "Ready"


def test_track_stamps_date_and_respects_sent(monkeypatch):
    p_new, p_sent = page("1", "Applied"), page("2", "Applied")
    p_sent["properties"]["Applied on"] = {"date": {"start": "2026-10-01"}}
    p_sent["properties"]["Follow-up"] = {"select": {"name": "Sent"}}
    monkeypatch.setattr(a.c, "notion_query", lambda h, f: [p_new, p_sent])
    writes = []
    monkeypatch.setattr(a, "write", lambda h, pid, props=None, blocks=None: writes.append((pid, props)))
    a.track({}, dt.date(2026, 10, 30))
    assert writes == [("1", {"Applied on": {"date": {"start": "2026-10-30"}}, "Follow-up": {"select": {"name": "Waiting"}}})]


CV = "Java, Spring Boot, Kafka for payments. Raised transaction success rate by 6%. Caught 11/16 bugs. 1,000+ problems."


def test_fact_check_flags_invented_claims():
    ok = {"cover_note": "Java and Kafka in payments; raised success rate by 6%, solved 1000+ problems, 2 projects.",
          "fit_summary": "Gap: no Kubernetes."}  # gaps may be named in the summary
    assert a.unsupported(ok, CV) == []
    bad = {"cv_bullets": ["Built Kubernetes clusters cutting latency 35%"]}
    assert set(a.unsupported(bad, CV)) == {"Kubernetes", "35%"}


def test_pack_retries_then_drops_invented_claims(monkeypatch):
    monkeypatch.setattr(a.c, "notion_query", lambda h, f: [page("1", "New")])
    monkeypatch.setattr(a.c, "page_text", lambda h, pid: "x" * 500)
    calls = []
    monkeypatch.setattr(a, "llm", lambda p, k, kind="github": calls.append(p) or {"cover_note": "Led a 40% cost cut"})
    writes = []
    monkeypatch.setattr(a, "write", lambda *x, **k: writes.append(x))
    a.make_packs({}, "k", CV)
    assert len(calls) == 2 and "40%" in calls[1] and writes == []


def test_bad_reply_stops_after_first_card(monkeypatch):
    monkeypatch.setattr(a.c, "notion_query", lambda h, f: [page("1", "New"), page("2", "New")])
    monkeypatch.setattr(a.c, "page_text", lambda h, pid: "x" * 500)
    calls = []

    def boom(p, k, kind="github"):
        calls.append(p)
        raise a.BadReply("200 text/html")
    monkeypatch.setattr(a, "llm", boom)
    a.make_packs({}, "k", CV)
    assert len(calls) == 1
