import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app

HEADERS = {"X-JobRadar-Client": "dashboard"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def configure(client):
    assert client.post("/api/profile", headers=HEADERS, json={"name": "Alex Example", "email": "alex@example.test"}).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json={"companies": ["Example"], "company_boards": {"Example": "example"}}).status_code == 200
    assert client.post("/api/resume", headers=HEADERS, files={"file": ("resume.txt", b"Java REST APIs and AWS", "text/plain")}).status_code == 200


def approve(client, **changes):
    body = {"acknowledge": True, "expected_fingerprint": client.get("/api/state").json()["approval"]["candidate_fingerprint"]}
    return client.post("/api/approve", headers=HEADERS, json={**body, **changes})


def test_private_empty_defaults_and_run_gate(client):
    state = client.get("/api/state").json()
    assert state["profile"]["email"] == ""
    assert state["approval"]["approved"] is False
    assert state["automation"]["enabled"] is False
    assert client.post("/api/run", headers=HEADERS).status_code == 409


def test_origin_and_header_guards(client):
    assert client.post("/api/profile", json={}).status_code == 403
    assert client.post("/api/profile", headers={**HEADERS, "Origin": "https://evil.test"}, json={}).status_code == 403
    assert client.post("/api/profile", headers={**HEADERS, "Origin": "null"}, json={}).status_code == 403
    assert client.get("/api/state", headers={"Host": "evil.test"}).status_code == 400


def test_profile_malformed_and_unknown_fields(client):
    assert client.post("/api/profile", headers=HEADERS, json={"email": "bad"}).status_code == 422
    assert client.post("/api/profile", headers=HEADERS, json={"email": "a@example.test", "magic_credentials": "no"}).status_code == 422
    assert client.post("/api/rules", headers=HEADERS, json={"daily_limit": 500}).status_code == 422
    assert client.post("/api/rules", headers=HEADERS, json={"company_boards": {"Example": "../../evil"}}).status_code == 422


@pytest.mark.parametrize("name,data", [("resume.txt", b"   "), ("resume.txt", b"a\x00b"), ("resume.pdf", b"not a PDF"), ("resume.exe", b"data")])
def test_bad_resume_uploads(client, name, data):
    assert client.post("/api/resume", headers=HEADERS, files={"file": (name, data)}).status_code == 400


def test_resume_size_limit(client):
    assert client.post("/api/resume", headers=HEADERS, files={"file": ("resume.txt", b"a" * (5 * 1024 * 1024 + 1))}).status_code == 413


def test_approval_requires_acknowledgement_and_revokes_after_edit(client):
    configure(client)
    assert approve(client, acknowledge=False).status_code == 400
    assert approve(client).status_code == 200
    assert client.get("/api/state").json()["approval"]["approved"]
    assert client.post("/api/automation", headers=HEADERS, json={"enabled": True}).status_code == 200
    client.post("/api/profile", headers=HEADERS, json={"name": "Alex Example", "email": "changed@example.test"})
    state = client.get("/api/state").json()
    assert not state["approval"]["approved"]
    assert not state["automation"]["enabled"]


def test_approval_requires_exact_employer_board(client):
    configure(client)
    client.post("/api/rules", headers=HEADERS, json={"companies": ["Unconfigured employer"]})
    response = approve(client)
    assert response.status_code == 400
    assert "Lever board" in response.json()["detail"]


def test_stale_tab_cannot_approve_unreviewed_settings(client):
    configure(client)
    reviewed = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    client.post("/api/rules", headers=HEADERS, json={"companies": ["Example"], "company_boards": {"Example": "example"}, "daily_limit": 9})
    assert approve(client, expected_fingerprint=reviewed).status_code == 409
    assert not client.get("/api/state").json()["approval"]["approved"]
    assert approve(client).status_code == 200


@pytest.mark.parametrize("endpoint,body", [("approve", []), ("approve", {"acknowledge": "true"}), ("automation", {"enabled": "true"}), ("automation", None), ("jobs/missing/status", {"status": "submitted"})])
def test_action_bodies_reject_malformed_or_coerced_values(client, endpoint, body):
    assert client.post(f"/api/{endpoint}", headers=HEADERS, json=body).status_code == 422


def test_mock_endpoint_rejects_real_identity(client):
    response = client.post("/mock/thanks", data={"name": "Real Person", "email": "real@example.test"})
    assert response.status_code == 400


def test_mock_opaque_origin_requires_one_use_token_and_synthetic_data(client):
    import re
    form = client.get("/mock/apply").text
    token = re.search('name="mock_token" value="([a-f0-9]+)"', form).group(1)
    data = {"name": "Alex Example", "email": "alex@example.test", "mock_token": token}
    files = {"resume": ("demo.txt", b"Synthetic demo resume. Test only.")}
    assert client.post("/mock/thanks", headers={"Origin": "null"}, data=data, files=files).status_code == 200
    assert client.post("/mock/thanks", headers={"Origin": "null"}, data=data, files=files).status_code == 403


def test_attempted_app_cannot_be_requeued(client):
    store = client.app.state.store
    store.save_job({"id": "attempted", "status": "filling"})
    store.reserve_attempt("attempted", "2026-10-18", 5)
    store.update_job("attempted", "uncertain")
    response = client.post("/api/jobs/attempted/status", headers=HEADERS, json={"status": "queued"})
    assert response.status_code == 409
