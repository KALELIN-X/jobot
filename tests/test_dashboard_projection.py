"""The dashboard needs bounded posting facts, not repeated full descriptions."""
from fastapi.testclient import TestClient

from jobradar.app import create_app


def test_compact_dashboard_keeps_worker_source_text_and_pending_questions(tmp_path):
    app = create_app(tmp_path)
    posting = {
        "id": "compact-posting", "company": "Synthetic employer", "title": "Software Engineer",
        "location": "Boston, MA", "status": "needs_attention", "score": 80,
        "description": "Requirements\nBachelor's degree or equivalent experience.\n"
                       "Must have 1 year of software development experience.\n"
                       "Graduating in 2026.\nBase salary USD $100,000 to $120,000 per year.\n"
                       + "Company background. " * 1500,
        "requirements": ["Python required"], "required_skills": ["Python"], "preferred_skills": [],
        "published_pay": [{"minimum": 100000, "maximum": 120000, "currency": "USD", "period": "year"}],
        "pay_description": "Base pay, without equity.",
        "pending_questions": [{"question": "Preferred office?", "type": "text", "required": True, "options": []}],
    }
    app.state.store.save_job(posting)
    with TestClient(app) as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        state = response.json()
        compact = state["jobs"][0]
        for key in ("description", "requirements", "required_skills", "preferred_skills", "published_pay", "pay_description"):
            assert key not in compact
        assert set(compact["facts"]) == {"location", "experience", "degree", "graduation", "pay"}
        assert "Boston" in compact["facts"]["location"]["summary"]
        assert compact["facts"]["experience"]["minimum_months"] == 12
        assert "USD 100,000–120,000/year" in compact["facts"]["pay"]["summary"]
        assert "2026" in compact["facts"]["graduation"]["summary"]
        assert compact["queue_actions"]["can_add"] is True
        assert state["answer_memory"]["pending"][0]["question"] == "Preferred office?"
        assert len(response.content) < len(posting["description"])
        stored = app.state.store.job(posting["id"])
        assert stored["description"] == posting["description"]
        assert stored["requirements"] == posting["requirements"]
        assert stored["published_pay"] == posting["published_pay"]


def test_unavailable_posting_facts_remain_unknown_without_inventing_pay_or_qualifications(tmp_path):
    app = create_app(tmp_path)
    app.state.store.save_job({"id": "no-details", "company": "Synthetic", "title": "New grad engineer", "status": "new"})
    with TestClient(app) as client:
        job = client.get("/api/state").json()["jobs"][0]
        assert all(job["facts"][key]["status"] == "not_stated" for key in ("location", "degree", "graduation", "pay", "experience"))
        assert job["facts"]["experience"]["minimum_months"] is None
        assert job["facts"]["pay"]["ranges"] == []
