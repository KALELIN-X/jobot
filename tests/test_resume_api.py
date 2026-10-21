"""Queue/API integration uses synthetic state; no external browser or account."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.application_sessions import ApplicationControl
from jobradar.sources import employer_metadata
from test_resumable_postings import ASHBY, public_payload, source_job
from test_workflow import HEADERS, await_idle, configure_and_approve


@pytest.fixture
def ready(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        configure_and_approve(client)
        store = client.app.state.store
        job = employer_metadata(source_job(ASHBY), public_payload(ASHBY))
        job.update(status="needs_attention", score=95, score_detail={"eligibility": []})
        store.save_job(job)
        async def yes():
            return True
        control = ApplicationControl(SimpleNamespace(), job, {}, None, yes, yes)
        control.binding = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
        receipt = control.pause("verification", "Complete the synthetic verification.")["verification"]
        client.app.state.application_sessions.sessions[job["id"]] = control
        store.update_job(job["id"], "needs_attention", verification=receipt)
        body = {"session_id": control.session_id, "pause_id": receipt["pause_id"], "acknowledge": True, "expected_fingerprint": control.binding}
        yield client, job, control, body


def test_paused_role_is_not_requeued_or_double_selected(ready):
    client, job, _, _ = ready
    state = client.get("/api/state").json()
    saved = next(item for item in state["jobs"] if item["id"] == job["id"])
    assert saved["resume_available"] is True and not saved["queue_actions"]["can_add"]
    assert client.post(f"/api/jobs/{job['id']}/status", headers=HEADERS, json={"status": "queued"}).status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/platform/check", headers=HEADERS).status_code == 409


@pytest.mark.parametrize("change", [{"acknowledge": False}, {"session_id": "f" * 48}, {"pause_id": "e" * 32}, {"expected_fingerprint": "a" * 64}])
def test_resume_requires_current_acknowledged_session_and_approval(ready, change):
    client, job, _, body = ready
    response = client.post(f"/api/jobs/{job['id']}/application/resume", headers=HEADERS, json={**body, **change})
    assert response.status_code == 409
    assert not client.app.state.store.job(job["id"])["attempted_at"]


def test_resume_reuses_existing_session_and_reserves_exactly_once(ready, monkeypatch):
    client, job, control, body = ready
    sessions = client.app.state.application_sessions
    calls = []
    async def resume(job_id, session_id, **kwargs):
        calls.append((job_id, session_id))
        assert sessions.sessions[job_id] is control
        assert await kwargs["verify"]()
        await kwargs["reserve"]()
        return {"status": "submitted", "message": "Synthetic API receipt", "confirmation": "Test only"}
    monkeypatch.setattr(sessions, "resume", resume)
    response = client.post(f"/api/jobs/{job['id']}/application/resume", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    await_idle(client)
    saved = client.app.state.store.job(job["id"])
    assert calls == [(job["id"], control.session_id)] and saved["status"] == "submitted" and saved["attempted_at"]
    assert client.post(f"/api/jobs/{job['id']}/application/resume", headers=HEADERS, json=body).status_code == 409
    assert len(calls) == 1


def test_cancel_closes_only_local_browser_preserving_history_and_config(ready):
    client, job, control, body = ready
    store = client.app.state.store
    config = {key: store.get(key) for key in ("profile", "resume", "rules", "approval")}
    response = client.post(f"/api/jobs/{job['id']}/application/cancel", headers=HEADERS, json={key: body[key] for key in ("session_id", "pause_id")})
    assert response.status_code == 200 and control.closed
    assert store.job(job["id"])["verification"]["state"] == "cancelled"
    assert not store.destination_attempted(job)
    for key, value in config.items():
        assert store.get(key) == value


def test_history_recorded_while_browser_closes_keeps_its_status(ready, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client, job, control, body = ready
    closing, finish_close = Event(), Event()
    original_close = control.close

    async def held_close():
        closing.set()
        assert await asyncio.to_thread(finish_close.wait, 10)
        await original_close()

    monkeypatch.setattr(control, "close", held_close)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/jobs/{job['id']}/application/cancel", headers=HEADERS,
                              json={key: body[key] for key in ("session_id", "pause_id")})
        try:
            assert closing.wait(10)
            recorded = client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": job["id"]})
            assert recorded.status_code == 200
        finally:
            finish_close.set()
        assert pending.result(timeout=10).status_code == 200
    saved = client.app.state.store.job(job["id"])
    assert saved["status"] == "applied_elsewhere" and saved["verification"]["state"] == "cancelled"
    assert client.app.state.store.destination_attempted(saved)


def test_changed_approved_configuration_cannot_resume_old_browser(ready):
    client, job, _, body = ready
    profile = client.get("/api/state").json()["profile"]
    profile["phone"] = "+15555550111"
    response = client.post("/api/profile", headers=HEADERS, json={**profile, "expected_revision": client.get("/api/state").json()["profile_revision"]})
    assert response.status_code == 200
    current = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    assert client.post("/api/approve", headers=HEADERS, json={"acknowledge": True, "expected_fingerprint": current}).status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/application/resume", headers=HEADERS, json={**body, "expected_fingerprint": current}).status_code == 409


def test_restart_marks_pause_interrupted_without_requeue_or_auth_storage(ready, tmp_path):
    client, job, _, _ = ready
    data_dir = client.app.state.data_dir
    other = create_app(data_dir)
    saved = other.state.store.job(job["id"])
    assert saved["status"] == "needs_attention" and saved["verification"]["state"] == "interrupted"
    assert not saved["attempted_at"] and not other.state.application_sessions.sessions


@pytest.mark.parametrize("field,value", [("description", "Changed official requirements"), ("location", "Seattle, WA"), ("title", "Different role")])
def test_changed_posting_cannot_resume_old_browser(ready, field, value):
    client, job, _, body = ready
    client.app.state.store.update_job(job["id"], "needs_attention", **{field: value})
    response = client.post(f"/api/jobs/{job['id']}/application/resume", headers=HEADERS, json=body)
    assert response.status_code == 409 and "posting changed" in response.json()["detail"]


@pytest.mark.parametrize("action", ["cancel", "focus"])
def test_stale_browser_action_cannot_affect_newer_pause(ready, action):
    client, job, control, body = ready
    response = client.post(f"/api/jobs/{job['id']}/application/{action}", headers=HEADERS,
                           json={"session_id": body["session_id"], "pause_id": "e" * 32})
    assert response.status_code == 409 and not control.closed
