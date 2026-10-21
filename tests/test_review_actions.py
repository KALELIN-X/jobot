import pytest

from jobradar.dashboard import application_review_actions


@pytest.mark.parametrize("message,kind", [
    ("A CAPTCHA requires human attention.", "captcha"),
    ("A login requires human attention.", "login"),
    ("A submission declaration or agreement requires human review.", "agreement"),
    ("The required field uses an unsupported control.", "form"),
    ("A resume upload needs review.", "form"),
])
def test_site_actions_are_distinct_from_reusable_questions(message, kind):
    job = {"id": "test", "company": "Example", "title": "Engineer", "status": "needs_attention", "message": message,
           "apply_url": "https://jobs.example.test/engineer"}
    result = application_review_actions([job])
    assert result[0]["kind"] == kind
    assert result[0]["apply_url"] == job["apply_url"]
    assert "answer" not in result[0]


def test_uncertain_is_never_offered_as_a_new_application():
    result = application_review_actions([{"id": "attempted", "status": "uncertain"}])
    assert result[0]["kind"] == "uncertain"
    assert "outcome verification" in result[0]["message"]
    assert "retry" not in result[0]


def test_plain_factual_questions_and_completed_or_synthetic_roles_are_not_site_actions():
    jobs = [{"id": "question", "status": "needs_attention", "message": "Preferred office still needs an answer."},
            {"id": "done", "status": "submitted", "message": "Agreement already handled."},
            {"id": "demo", "status": "needs_attention", "message": "CAPTCHA", "demo": True}]
    assert application_review_actions(jobs) == []


def test_review_links_cannot_embed_credentials_or_unsafe_protocols():
    for url in ("javascript:alert(1)", "https://secret:password@example.test/role", "https://example.test/\nprivate", "https://["):
        result = application_review_actions([{"id": "test", "status": "needs_attention", "message": "Unsupported form", "apply_url": url}])
        assert result[0]["apply_url"] is None
