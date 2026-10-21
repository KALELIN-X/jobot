"""Synthetic applications prove reusable meanings without private user data."""
import copy

import pytest
from fastapi.testclient import TestClient

from jobradar.answer_memory import answer_memory_state
from jobradar.app import create_app
from jobradar.autofill import _answer, plan_form
from jobradar.models import Profile
from jobradar.question_intents import classify_question, resolve_question


HEADERS = {"X-JobRadar-Client": "dashboard"}


def field(label, **changes):
    return {"id": "a", "label": label, "name": "custom", "type": "text", "tag": "input",
            "visible": True, "required": True, "value": "", **changes}


def job(identifier="one", company="Example", question=None, **changes):
    questions = [{"question": question, "type": "text", "required": True, "options": []}] if question else []
    return {"id": identifier, "company": company, "title": "Engineer", "status": "needs_attention",
            "pending_questions": questions, **changes}


def facts(**values):
    return Profile(application_profile={"facts": values}).model_dump()


@pytest.mark.parametrize("question", [
    "Are you legally authorized to work in the United States?", "Are you authorised to work in the US?",
    "Do you have the right to work in the United States?", "Work authorization",
])
def test_work_authorization_paraphrases_use_one_fact(question):
    result = resolve_question(field(question, type="checkbox"), {"work_authorized": True})
    assert result.intent_id == "eligibility.work_authorized_us"
    assert result.value is True
    assert result.source == "profile.work_authorized"


@pytest.mark.parametrize("question", [
    "Will you have valid work authorization in the future?",
    "Are you authorized to work in the United States now and in the future?",
    "Are you authorized to work for any employer in the United States?",
])
def test_current_authorization_does_not_establish_future_or_unrestricted_status(question):
    result = resolve_question(field(question), {"work_authorized": True, "answers": {question: "Yes"}})
    assert result.status == "review" and result.value is None


@pytest.mark.parametrize("question", [
    "Do you need employer sponsorship?", "Will you now or in the future require visa sponsorship?",
    "Do you require sponsorship to work in the United States?",
])
def test_sponsorship_paraphrases(question):
    assert resolve_question(field(question), {"needs_sponsorship": False}).value is False


@pytest.mark.parametrize("question", [
    "Do you require sponsorship to work in Canada?", "Are you authorized to work in South Korea?",
    "Do you not require employer sponsorship?", "Are you authorized to work in the US without sponsorship?",
    "Have you ever required sponsorship?", "Are you eligible to work on ITAR projects?",
    "Are you authorized to work in the country in which this job is located?",
])
def test_foreign_negative_and_compound_eligibility_abstains(question):
    profile = {"work_authorized": True, "needs_sponsorship": False, "answers": {question: "Yes"}}
    assert resolve_question(field(question), profile).status == "review"
    assert not plan_form([field(question)], profile).actions


@pytest.mark.parametrize("question", [
    "Do you require visa sponsorship in Poland?", "Do you need sponsorship in Norway?",
    "Are you authorized to work in the United States through December 2028?",
    "Are you legally authorized to work in the United States for 24 months?",
    "Are you willing to work onsite for 16 hours per day?",
    "Are you willing to work onsite for sixteen hours per day?",
    "Are you willing to work onsite in Poland?",
])
def test_extra_country_date_and_working_condition_constraints_are_not_plain_facts(question):
    profile = {**facts(willing_onsite=True), "work_authorized": True, "needs_sponsorship": False}
    assert resolve_question(field(question), profile).status == "review"
    assert _answer(field(question), profile) is None


@pytest.mark.parametrize("question", [
    "Are you willing to relocate to Poland?",
    "Are you willing to relocate even if no relocation assistance is offered?",
    "Are you available to start immediately on December 1, 2027?",
    "Could you accept a full-time job after the internship for at least two years?",
    "Are you at least 18 years old with a valid driver license?",
    "Are you currently employed by the government?",
    "Have you previously worked for the government in the last five years?",
    "Are you currently bound by an NDA?",
    "Have you ever signed a noncompete agreement?",
])
def test_qualified_commitments_and_temporal_histories_need_exact_review(question):
    profile = facts(willing_to_relocate=True, needs_relocation_assistance=True, relocate_at_own_expense=False,
                    available_immediately=True, full_time_after_internship=True, age_18_or_older=True,
                    government_employment=True, has_nda=True, has_employment_restrictions=False)
    assert resolve_question(field(question), profile).status == "review"
    assert _answer(field(question), profile) is None


def test_plain_adult_and_history_questions_still_reuse_their_exact_meaning():
    profile = facts(age_18_or_older=True, government_employment=True, has_nda=True, has_employment_restrictions=False)
    assert _answer(field("Are you 18 or older?"), profile) == "Yes"
    assert _answer(field("Have you worked for a government employer?"), profile) == "Yes"
    assert _answer(field("Have you signed a nondisclosure agreement?"), profile) == "Yes"
    assert _answer(field("Do you have any employment restrictions?"), profile) == "No"


def test_unqualified_work_and_location_templates_remain_supported():
    profile = {**facts(willing_onsite=True), "work_authorized": True, "needs_sponsorship": False}
    assert _answer(field("Are you willing to work onsite?"), profile) == "Yes"
    assert _answer(field("Will you now or in the future require visa sponsorship?"), profile) == "No"
    assert _answer(field("Are you authorized to work in the United States?"), profile) == "Yes"


def test_country_of_application_template_uses_only_explicit_us_job_country():
    question = "Are you authorized to work in the country in which you are applying?"
    profile = {"work_authorized": True}
    assert resolve_question(field(question), profile, {"country": "US"}).value is True
    assert resolve_question(field(question), profile, {"location": "Remote (United States)"}).value is True
    assert resolve_question(field(question), profile, {"location": "Remote"}).status == "review"
    assert resolve_question(field(question), profile, {"country": "Canada"}).status == "review"
    assert resolve_question(field(question), profile, {"country": "US", "location": "Toronto, Canada"}).status == "review"
    assert resolve_question(field(question), profile, {"company": "US Company", "title": "US Engineer"}).status == "review"


def test_old_exact_answer_cannot_override_updated_typed_fact():
    profile = {"first_name": "Current", "answers": {"First name": "Stale"}}
    assert _answer(field("First name"), profile) == "Current"
    assert profile["answers"]["First name"] == "Stale"


def test_planner_and_readiness_share_authorization_resolution():
    question = "Are you legally authorized to work in the United States?"
    item = field(question, type="select", tag="select", options=[{"label": "Yes", "value": "Y"}, {"label": "No", "value": "N"}])
    record = job(question=question)
    record["pending_questions"][0].update(type="select", options=item["options"])
    profile = {"work_authorized": True}
    assert plan_form([item], profile).actions[0].value == "Y"
    assert answer_memory_state(profile, [record])["pending"] == []


def test_repeated_paraphrases_group_one_missing_fact_without_learning():
    profile = facts()
    before = copy.deepcopy(profile)
    state = answer_memory_state(profile, [job(question="Are you willing to relocate?"), job("two", "Another", "Are you open to relocation?")])
    assert len(state["pending"]) == 2
    assert len(state["groups"]) == 1
    assert state["groups"][0]["intent_id"] == "location.relocate"
    assert state["groups"][0]["job_count"] == 2
    assert profile == before


def test_legacy_company_history_is_never_global():
    question = "Have you worked here before?"
    profile = {"answers": {question: "No"}}
    assert _answer(field(question), profile, job()) is None
    assert answer_memory_state(profile, [job(question=question)])["pending"]


@pytest.mark.parametrize("question", ["Are you a current or former employee?", "Are you currently employed by us?", "Are you a current employee or contractor?"])
def test_present_tense_employer_history_cannot_be_reused_globally(question):
    result = classify_question(question)
    assert result.scope == "employer"
    assert _answer(field(question), {"answers": {question: "No"}}, job()) is None


def test_employer_binding_only_applies_to_same_employer_and_question():
    question = "Have you worked here before?"
    profile = Profile(application_profile={"saved_answer_bindings": [{"question": question, "answer": "No", "scope": "employer", "company": "Example"}]}).model_dump()
    assert _answer(field(question), profile, job()) == "No"
    assert _answer(field(question), profile, job("two", "Other")) is None
    assert _answer(field("Why this company?"), profile, job()) is None


def test_application_scoped_exact_answer_does_not_leak_to_other_job():
    profile = Profile(application_profile={"saved_answer_bindings": [{"question": "Preferred office?", "answer": "Boston", "scope": "application", "job_id": "one"}]}).model_dump()
    assert _answer(field("Preferred office?"), profile, job()) == "Boston"
    assert _answer(field("Preferred office?"), profile, job("two")) is None


def test_conflicting_scoped_answers_do_not_guess():
    profile = {"application_profile": {"saved_answer_bindings": [
        {"question": "A?", "answer": "One", "scope": "global", "intent_id": "identity.pronouns"},
        {"question": "B?", "answer": "Two", "scope": "global", "intent_id": "identity.pronouns"},
    ]}}
    assert resolve_question(field("Pronouns"), profile).status == "conflict"


def test_no_day_manufactured_from_graduation_month():
    profile = {"graduation_month": "2028-05"}
    assert _answer(field("Expected graduation date", type="date"), profile) is None
    assert _answer(field("Expected graduation date", type="month"), profile) == "2028-05"
    assert _answer(field("Expected graduation year"), profile) == "2028"


def test_future_sponsorship_is_not_assumed_from_positive_aggregate():
    result = resolve_question(field("Will you require sponsorship in the future?"), {"needs_sponsorship": True})
    assert result.status == "missing"
    both = facts(sponsorship_now=False, sponsorship_future=True)
    assert resolve_question(field("Will you now or in the future require sponsorship?"), both).value is True


def test_conflicting_sponsorship_facts_require_review():
    profile = {**facts(sponsorship_future=True), "needs_sponsorship": False}
    assert resolve_question(field("Will you now or in the future require sponsorship?"), profile).status == "conflict"


def test_optional_answer_unavailable_is_warning_not_submission_blocker():
    item = field("Preferred office?", required=False, type="select", tag="select", options=[{"label": "Boston", "value": "bos"}])
    plan = plan_form([item], {"answers": {"Preferred office?": "Elsewhere"}})
    assert not plan.actions and not plan.blockers
    assert plan.pending_questions[0]["reason"] == "choice_unavailable"


def test_privacy_read_ack_is_not_a_profile_fact_even_with_injected_binding():
    question = "I have read the candidate privacy notice"
    profile = {"application_profile": {"saved_answer_bindings": [{"question": question, "answer": True, "scope": "application", "job_id": "one"}]}}
    assert classify_question(question).intent_id == "review.privacy_acknowledgment"
    assert resolve_question(field(question), profile, job()).status == "review"


def academic_profile():
    return Profile(application_profile={"education": [
        {"id": "bachelors", "institution": "Example University", "degree": "Bachelor of Science", "status": "completed", "end_month": "2024-07", "gpa": 3.2, "gpa_scale": 4.0},
        {"id": "masters", "institution": "Example Institute", "degree": "Master of Science", "status": "incoming", "start_month": "2027-01", "end_month": "2028-05"},
    ]}).model_dump()


def test_undergraduate_and_incoming_graduate_records_remain_distinct():
    profile = academic_profile()
    assert _answer(field("Undergraduate institution"), profile) == "Example University"
    assert _answer(field("Graduate institution"), profile) == "Example Institute"
    assert _answer(field("Undergraduate GPA"), profile) == "3.2"
    assert _answer(field("Graduate GPA"), profile) is None
    assert _answer(field("Current university"), profile) is None
    assert _answer(field("Are you currently enrolled?", type="checkbox"), profile) is False


def test_ambiguous_school_does_not_choose_first_or_use_old_global_answer():
    profile = {**academic_profile(), "answers": {"School": "Old School"}}
    assert resolve_question(field("School"), profile).status == "conflict"
    assert _answer(field("School"), profile) is None


def test_explicit_primary_answers_generic_school_but_does_not_claim_current_enrollment():
    profile = academic_profile()
    profile["application_profile"]["education"][1]["primary_for_applications"] = True
    assert _answer(field("School"), profile) == "Example Institute"
    assert _answer(field("Degree"), profile) == "Master of Science"
    assert _answer(field("GPA"), profile) is None
    assert _answer(field("Current university"), profile) is None
    assert _answer(field("Expected graduation year"), profile) == "2028"
    assert _answer(field("Expected graduation date", type="date"), profile) is None
    choices = [{"label": "Bachelor's Degree", "value": "b"}, {"label": "Master's Degree", "value": "m"}]
    assert plan_form([field("Degree", type="select", tag="select", options=choices)], profile).actions[0].value == "m"


@pytest.mark.parametrize("label", ["Master’s degree (completed)", "Master's degree earned", "Master's degree (currently enrolled)", "Master's degree (in progress)"])
def test_incoming_primary_degree_never_selects_a_status_qualified_degree_option(label):
    profile = academic_profile()
    profile["application_profile"]["education"][1]["primary_for_applications"] = True
    item = field("Degree", type="select", tag="select", options=[{"label": label, "value": "qualified"}])
    assert not plan_form([item], profile).actions


def test_primary_degree_and_core_graduation_conflict_needs_one_profile_correction():
    profile = academic_profile()
    profile["application_profile"]["education"][1]["primary_for_applications"] = True
    profile["graduation_month"] = "2024-07"
    assert resolve_question(field("Expected graduation year"), profile).status == "conflict"
    assert _answer(field("Expected graduation year"), profile) is None


def test_primary_record_does_not_fill_every_repeated_school_entry_with_same_degree():
    profile = academic_profile()
    profile["application_profile"]["education"][1]["primary_for_applications"] = True
    controls = [field("School", name="school--0"), field("School", id="b", name="school--1")]
    plan = plan_form(controls, profile)
    assert not plan.actions
    assert len(plan.pending_questions) == 2
    assert all(question["ambiguous_record_control"] for question in plan.pending_questions)


def test_employment_dates_require_known_precision_and_unique_record():
    profile = Profile(application_profile={"employment": [{"id": "intern", "company": "Example", "title": "Technical Intern", "kind": "internship", "start_month": "2024-07", "end_month": "2024-09", "current": False}]}).model_dump()
    assert _answer(field("Internship start date", type="month"), profile) == "2024-07"
    assert _answer(field("Internship end date", type="date"), profile) is None


def test_decorated_boolean_choices_need_explicit_choice_not_just_a_true_fact():
    options = [{"label": "Yes, I am authorized", "value": "authorized"}, {"label": "No, I am not", "value": "unauthorized"}]
    item = field("Work authorization", type="select", tag="select", options=options)
    assert not plan_form([item], {"work_authorized": True}).actions
    item["options"].append({"label": "Yes, with limitations", "value": "limited"})
    assert not plan_form([item], {"work_authorized": True}).actions


@pytest.mark.parametrize("label", ["Yes, but I require visa sponsorship", "Yes, I am an F-1 student", "Yes, I hold a Secret clearance"])
def test_qualified_yes_choice_does_not_fabricate_additional_personal_facts(label):
    item = field("Work authorization", type="select", tag="select", options=[{"label": label, "value": "qualified"}, {"label": "No", "value": "N"}])
    assert not plan_form([item], {"work_authorized": True}).actions


def test_posted_range_salary_policy_never_manufactures_a_numeric_target():
    profile = facts(salary_mode="posted_range")
    assert "posted range" in _answer(field("Salary expectations"), profile)
    assert _answer(field("Salary expectations", type="number"), profile) is None


def test_history_of_nda_is_distinct_from_acceptance_of_new_nda():
    profile = facts(has_nda=True)
    question = "Have you signed a non-disclosure agreement?"
    assert _answer(field(question), profile) == "Yes"
    assert not plan_form([field(question)], profile).blockers
    assert plan_form([field("I accept this non-disclosure agreement")], profile).blockers


def test_salary_ask_mode_and_foreign_currency_do_not_reuse_old_amount():
    profile = facts(salary_mode="ask", target_base_salary=120000.0, salary_currency="USD")
    assert _answer(field("Salary expectations", type="number"), profile) is None
    profile = facts(salary_mode="target", target_base_salary=120000.0, salary_currency="EUR")
    assert _answer(field("Salary expectations", type="number"), profile) is None


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as value:
        yield value


def test_explicit_intent_confirmation_saves_canonical_fact_and_keeps_legacy(client):
    store = client.app.state.store
    store.set("profile", Profile(answers={"Legacy unrelated?": "Keep"}).model_dump())
    question = "Are you willing to relocate?"
    response = client.post("/api/answers", headers=HEADERS, json={"question": question, "answer": "Yes", "intent_id": "location.relocate", "confirm_intent": True, "scope": "global"})
    assert response.status_code == 200, response.text
    profile = store.get("profile")
    assert profile["application_profile"]["facts"]["willing_to_relocate"] is True
    assert profile["answers"] == {"Legacy unrelated?": "Keep"}
    assert _answer(field("Are you open to relocation?"), profile) == "Yes"
    assert not store.get("approval")["approved"]


def test_observed_answer_not_promoted_without_explicit_intent_confirmation(client):
    response = client.post("/api/answers", headers=HEADERS, json={"question": "Are you willing to relocate?", "answer": "Yes"})
    assert response.status_code == 200
    profile = client.app.state.store.get("profile")
    assert profile["application_profile"]["facts"]["willing_to_relocate"] is None
    assert _answer(field("Are you open to relocation?"), profile) is None


def test_wrong_intent_mapping_cannot_change_another_fact(client):
    response = client.post("/api/answers", headers=HEADERS, json={"question": "Are you willing to relocate?", "answer": "Yes", "intent_id": "eligibility.us_citizen", "confirm_intent": True})
    assert response.status_code == 422
    assert not client.app.state.store.get("profile", {}).get("answers")


def test_employer_question_defaults_to_employer_scope_and_rejects_global(client):
    question = "Have you worked here before?"
    client.app.state.store.save_job(job(question=question))
    body = {"question": question, "answer": "No", "job_id": "one"}
    assert client.post("/api/answers", headers=HEADERS, json={**body, "scope": "global"}).status_code == 422
    result = client.post("/api/answers", headers=HEADERS, json=body)
    assert result.status_code == 200, result.text
    profile = client.app.state.store.get("profile")
    assert question not in profile["answers"]
    assert profile["application_profile"]["saved_answer_bindings"][0]["scope"] == "employer"
    assert _answer(field(question), profile, job("different", "Other")) is None


def test_contextual_us_question_can_be_explicitly_confirmed_via_same_resolver(client):
    question = "Are you authorized to work in the country in which you are applying?"
    record = job(question=question, country="US")
    client.app.state.store.save_job(record)
    response = client.post("/api/answers", headers=HEADERS, json={"question": question, "answer": "Yes", "job_id": "one",
                                                               "scope": "global", "confirm_intent": True, "intent_id": "eligibility.work_authorized_us"})
    assert response.status_code == 200, response.text
    assert client.app.state.store.get("profile")["work_authorized"] is True


def test_scoped_choices_keep_sources_and_can_be_forgotten_separately(client):
    record = job(question="Preferred office?")
    record["pending_questions"][0].update(type="select", options=[{"label": "Boston", "value": "bos"}])
    client.app.state.store.save_job(record)
    response = client.post("/api/answers", headers=HEADERS, json={"question": "Preferred office?", "answer": "Boston", "job_id": "one", "scope": "application"})
    assert response.status_code == 200, response.text
    memory = answer_memory_state(client.app.state.store.get("profile"), [record])
    assert memory["saved"][0]["sources"][0]["options"][0]["label"] == "Boston"
    deleted = client.request("DELETE", "/api/answers", headers=HEADERS, json={"question": "Preferred office?", "scope": "application", "job_id": "one"})
    assert deleted.status_code == 200
    assert not client.app.state.store.get("profile")["application_profile"]["saved_answer_bindings"]


def test_confirmed_location_choice_formats_known_city_but_expires_after_city_changes(client):
    store = client.app.state.store
    store.set("profile", Profile(location="Example City, TX").model_dump())
    record = job(question="Current location")
    record["pending_questions"][0].update(type="select", options=[{"label": "Example City, Texas, United States", "value": "city"}])
    store.save_job(record)
    response = client.post("/api/answers", headers=HEADERS, json={"question": "Current location", "answer": "city", "job_id": "one", "scope": "application"})
    assert response.status_code == 200, response.text
    profile = store.get("profile")
    item = field("Current location", type="select", tag="select", options=record["pending_questions"][0]["options"])
    assert plan_form([item], profile, job=record).actions[0].value == "city"
    profile["location"] = "Another City, NY"
    assert not plan_form([item], profile, job=record).actions
    assert plan_form([item], profile, job=record).blockers
    # Reconfirming the same actual choice after a canonical formatting change
    # must refresh its fact-version hash instead of treating it as a no-op.
    profile["location"] = "Example City, Texas"
    client.app.state.store.set("profile", profile)
    refreshed = client.post("/api/answers", headers=HEADERS, json={"question": "Current location", "answer": "city", "job_id": "one", "scope": "application"})
    assert refreshed.status_code == 200 and refreshed.json()["changed"]
    assert plan_form([item], store.get("profile"), job=record).actions[0].value == "city"


def test_scoped_choice_cannot_invert_known_boolean_fact(client):
    store = client.app.state.store
    store.set("profile", Profile(work_authorized=True).model_dump())
    question = "Work authorization"
    record = job(question=question)
    record["pending_questions"][0].update(type="select", options=[{"label": "Yes", "value": "Y"}, {"label": "No", "value": "N"}])
    store.save_job(record)
    response = client.post("/api/answers", headers=HEADERS, json={"question": question, "answer": "No", "job_id": "one", "scope": "application"})
    assert response.status_code == 422
    assert store.get("profile")["work_authorized"] is True


def test_continue_requires_reviewed_fingerprint_and_application(client):
    response = client.post("/api/answers", headers=HEADERS, json={"question": "Favorite language?", "answer": "Python", "approve_and_continue": True})
    assert response.status_code == 422
    assert not client.app.state.store.get("profile", {}).get("answers")
