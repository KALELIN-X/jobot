"""Real-job dashboard projection keeps synthetic audit history in local storage."""
import copy

from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.store import Store


def listing(identifier, *, demo=False, status="needs_attention", question="Which office can you work in?"):
    return {"id": identifier, "company": "Synthetic fixture employer", "title": "Backend engineer",
            "status": status, "demo": demo, "description": "Synthetic test description.",
            "pending_questions": [{"question": question, "type": "text", "required": True, "options": []}]}


def test_api_shows_only_real_listing_questions_and_statistics_without_deleting_demo(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    store.save_job(listing("real-review", question="Actual recorded office question?"))
    store.save_job(listing("real-submitted", status="submitted", question=""))
    store.save_job(listing("synthetic-old", demo=True, status="submitted", question="Synthetic-only question?"))
    original_demo = copy.deepcopy(store.job("synthetic-old"))
    store.event("application_progress", "Real employer form needs an answer.", "real-review")
    store.event("application_progress", "Synthetic form was confirmed.", "synthetic-old")
    store.event("demo", "Local synthetic run completed.", "synthetic-old")
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert {job["id"] for job in state["jobs"]} == {"real-review", "real-submitted"}
        assert state["stats"]["total"] == 2 and state["stats"]["submitted"] == 1
        assert state["stats"]["needs_attention"] == 1
        assert [question["question"] for question in state["answer_memory"]["pending"]] == ["Actual recorded office question?"]
        assert [event["message"] for event in state["events"]] == ["Real employer form needs an answer."]
        assert store.job("synthetic-old") == original_demo
        assert len(store.events()) == 3


def test_saved_demo_run_is_idle_in_projection_but_remains_in_storage(tmp_path):
    store = Store(tmp_path / "jobradar.sqlite3")
    saved = {"id": "local-fixture-run", "mode": "demo", "status": "completed", "stage": "completed",
             "message": "Run finished", "submitted": 1, "total": 1, "processed": 1,
             "active_job": {"id": "synthetic-old", "company": "Synthetic fixture", "title": "Test only"}, "timeline": []}
    store.set("last_run", saved)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["run"]["status"] == "idle"
        assert state["run"].get("submitted", 0) == 0
        assert not state["run"].get("active_job")
        assert state["jobs"] == []
        assert app.state.store.get("last_run") == saved


def test_saved_real_queue_run_stays_visible(tmp_path):
    store = Store(tmp_path / "jobradar.sqlite3")
    store.set("last_run", {"id": "real-queue-run", "mode": "queue", "status": "completed", "stage": "completed",
                           "submitted": 1, "total": 1, "processed": 1, "timeline": [], "active_job": None})
    with TestClient(create_app(tmp_path)) as client:
        run = client.get("/api/state").json()["run"]
        assert run["id"] == "real-queue-run" and run["status"] == "completed" and run["submitted"] == 1


def event(identifier, kind, message, job_id=None):
    return {"id": identifier, "type": kind, "message": message, "job_id": job_id}


def test_event_projection_hides_demo_spans_and_keeps_interleaved_real_edits_and_input_order():
    from jobradar.dashboard import application_events
    chronological = [
        event(1, "profile", "Contact profile saved."),
        event(2, "run_started", "Local demo worker started with 1 selected role(s)."),
        event(3, "application_progress", "Synthetic form submitted.", "synthetic-old"),
        event(4, "answer_memory_saved", "Saved a real application answer."),
        event(5, "profile", "Profile note contains the word demo."),
        event(6, "run_finished", "Run finished: 1 processed, 1 confirmed."),
        event(7, "run_started", "Application queue worker started with 1 selected role(s)."),
        event(8, "application_progress", "Opening the real employer form.", "real-review"),
        event(9, "run_finished", "Run finished: 1 processed, 0 confirmed."),
        event(10, "demo_capture", "Synthetic-only capture event."),
    ]
    supplied = list(reversed(chronological))
    original = copy.deepcopy(supplied)
    visible = application_events(supplied, {"synthetic-old"})
    assert [item["id"] for item in visible] == [9, 8, 7, 5, 4, 1]
    assert supplied == original


def test_unattributed_run_summary_is_preserved_instead_of_guessed_to_be_demo():
    from jobradar.dashboard import application_events
    events = [event(100, "run_finished", "Run finished: 1 processed, 1 confirmed."),
              event(99, "application_progress", "A real listing was checked.", "real-job"),
              event(98, "demo", "Synthetic run event."),
              event(97, "demo_failed", "Synthetic run failed.")]
    assert [item["id"] for item in application_events(events, set())] == [100, 99]


def test_demo_interruption_span_hides_worker_events_but_retains_user_changes():
    from jobradar.dashboard import application_events
    events = [event(1, "run_started", "Local demo worker started with 1 selected role(s)."),
              event(2, "application_progress", "An old synthetic browser stage."),
              event(3, "answer_memory_saved", "Real answer saved during the local test."),
              event(4, "run_interrupted", "The previous run was interrupted."),
              event(5, "profile", "Actual profile saved after the interruption.")]
    assert [item["id"] for item in application_events(events, set())] == [3, 5]
