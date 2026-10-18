import pytest

from jobradar.ranking import rank_job


def job(**updates):
    result = {"title": "Software Engineer", "company": "Acme", "location": "Boston, MA", "work_model": "On Site", "ats": "lever", "closed": False,
              "description": "Python is required. Docker is preferred. Develop Java services.",
              "required_skills": ["Python"], "preferred_skills": ["Docker"]}
    result.update(updates)
    return result


def profile(**updates):
    result = {"location": "Boston, MA", "approved_work_locations": ["Boston, MA"], "skills": [], "experience_months": 12,
              "graduation_date": "2027-05-15", "work_authorized": True, "needs_sponsorship": False}
    result.update(updates)
    return result


def status(result, label):
    return next(entry["status"] for entry in result["eligibility"] if entry["label"] == label)


def test_weighted_score_uses_resume_evidence_and_excerpts():
    result = rank_job(job(), profile(), "Built Python APIs for a student project. Shipped Java services.", {})
    assert result["score"] == 83  # Python 3 + Java 2, out of Python 3 + Java 2 + Docker 1.
    assert any("Built Python APIs" in match for match in result["matches"])
    assert result["not_evidenced"] == ["Docker (weight 1): not found in the supplied resume"]
    assert "not an interview probability" in result["reasons"][0]


def test_profile_skill_claim_does_not_invent_resume_evidence():
    result = rank_job(job(), profile(skills=["Python", "Docker"]), "Experience with Excel and presentations.", {})
    assert result["score"] == 0
    assert not result["matches"]


def test_no_resume_has_no_score_and_requests_upload():
    result = rank_job(job(), profile(skills=["Python"]), "  \n", {})
    assert result["score"] is None
    assert not result["matches"]
    assert not result["not_evidenced"]
    assert any("upload" in reason.lower() and "resume" in reason.lower() for reason in result["reasons"])


def test_missing_description_has_no_score_even_with_title_match():
    result = rank_job(job(description="", title="Python Developer"), profile(), "Python developer", {})
    assert result["score"] is None
    assert status(result, "Full description") == "unknown"


def test_no_supported_keywords_has_no_misleading_score():
    assert rank_job(job(description="Build excellent products and collaborate with clients."), profile(), "Excellent products", {})["score"] is None


def test_java_does_not_match_javascript_and_c_does_not_match_cpp():
    result = rank_job(job(description="Java and C are required."), profile(), "JavaScript and C++ developer", {})
    assert result["score"] == 0
    assert len(result["not_evidenced"]) == 2


def test_aliases_count_once():
    result = rank_job(job(description="AWS (Amazon Web Services) and PostgreSQL/Postgres."), profile(), "Worked with Amazon Web Services and Postgres", {})
    assert result["score"] == 100
    assert len(result["matches"]) == 2


def test_high_score_does_not_override_experience_failure():
    result = rank_job(job(description="Python required. Minimum 3 years of software development experience."), profile(experience_months=12), "Built Python services", {})
    assert result["score"] == 100
    assert status(result, "Experience") == "fail"


def test_required_experience_unknown_without_profile_answer_and_preferred_not_gate():
    required = rank_job(job(description="Python. At least 2 years of relevant experience required."), profile(experience_months=None), "Python", {})
    assert status(required, "Experience") == "unknown"
    preferred = rank_job(job(description="Python. 3 years of software experience preferred."), profile(experience_months=0), "Python", {})
    assert status(preferred, "Experience") == "pass"


def test_required_graduation_year_is_separate_and_month_window_unknown():
    result = rank_job(job(title="Software Engineer (2026 Graduates)"), profile(graduation_date="2027-05-15"), "Python Docker Java", {})
    assert result["score"] == 100
    assert status(result, "Graduation date") == "fail"
    seasonal = rank_job(job(title="Graduation Date Fall 2026 - Summer 2027"), profile(), "Python", {})
    assert status(seasonal, "Graduation date") == "unknown"


def test_company_location_ats_allowlists_gate_independently():
    result = rank_job(job(), profile(), "Python Docker Java", {"companies": ["Other"], "locations": ["Chicago"], "allowed_ats": ["ashby"], "min_score": 50})
    assert result["score"] == 100
    assert status(result, "Approved company") == "fail"
    assert status(result, "Approved location") == "fail"
    assert status(result, "Approved ATS") == "fail"


def test_sponsorship_inference_does_not_guarantee_sponsorship():
    result = rank_job(job(description="Python required. We cannot provide visa sponsorship."), profile(needs_sponsorship=True), "Python", {})
    assert status(result, "Sponsorship") == "fail"
    unknown = rank_job(job(), profile(needs_sponsorship=True), "Python", {})
    assert status(unknown, "Sponsorship") == "unknown"
    unrelated_no = rank_job(job(description="Python. No prior experience required. We offer visa sponsorship."), profile(needs_sponsorship=True), "Python", {})
    assert status(unrelated_no, "Sponsorship") != "fail"


def test_location_allowlist_matches_whole_terms():
    result = rank_job(job(location="Austin, TX"), profile(), "Python", {"locations": ["US"]})
    assert status(result, "Approved location") == "fail"


def test_fetch_error_does_not_assert_closed():
    result = rank_job(job(closed=None, fetch_error="HTTP 403"), profile(), "Python", {})
    assert status(result, "Posting availability") == "unknown"
    assert not any(entry["label"] == "Posting availability" and entry["status"] == "fail" for entry in result["eligibility"])


def test_missing_location_or_authorization_remains_unknown():
    result = rank_job(job(location="Austin, TX"), profile(location="Boston, MA", work_authorized=None), "Python", {})
    assert status(result, "Work location") == "unknown"
    assert status(result, "Work authorization") == "unknown"


def test_pdf_token_variants_and_camelcase_preserve_actual_excerpt():
    result = rank_job(job(description="AWS and Spring Boot required.", required_skills=[]), profile(),
                      "Shipped SpringBoot services using A WS infrastructure.", {})
    assert result["score"] == 100
    assert len(result["matches"]) == 2
    assert any("using A WS infrastructure" in entry for entry in result["matches"])
    assert any("Shipped SpringBoot" in entry for entry in result["matches"])


def test_phrase_whitespace_and_known_split_acronyms_count_once():
    result = rank_job(job(description="Amazon Web Services and Spring Boot.", required_skills=[]), profile(),
                      "Amazon\u00a0Web\nServices (A\tWS) and Spring\tBoot.", {})
    assert result["score"] == 100
    assert len(result["matches"]) == 2


def test_alias_recovery_does_not_join_arbitrary_words_or_substrings():
    result = rank_job(job(description="AWS and SpringBoot required.", required_skills=[]), profile(),
                      "A W Store sells springboards. Experienced in drawings and Javascript.", {})
    assert result["score"] == 0
    assert len(result["not_evidenced"]) == 2


def test_explicit_language_options_are_one_requirement_with_evidenced_choice():
    result = rank_job(job(description="Proficiency in at least one language such as Python, Go, Java, Rust, or C++. Docker is required.",
                          required_skills=[], preferred_skills=[]), profile(), "Built Java services.", {})
    assert result["score"] == 50  # One language requirement 3, Docker requirement 3.
    assert len(result["matches"]) == 1
    assert "matched Java" in result["matches"][0]
    assert "Built Java services" in result["matches"][0]
    assert result["not_evidenced"] == ["Docker (weight 3): not found in the supplied resume"]


def test_one_of_options_with_no_resume_evidence_is_one_missing_requirement():
    result = rank_job(job(description="One of Python, Java, or Rust is required.", required_skills=[]), profile(), "Docker", {})
    assert result["score"] == 0
    assert len(result["not_evidenced"]) == 1
    assert result["not_evidenced"][0].startswith("One of Python / Java / Rust")


def test_all_required_languages_still_receive_independent_penalties():
    result = rank_job(job(description="Python, Go, Java, Rust, and C++ are all required.", required_skills=[]), profile(), "Java", {})
    assert result["score"] == 20
    assert len(result["not_evidenced"]) == 4


@pytest.mark.parametrize("description", [
    "Experience with Python or Java is required.",
    "One of Python or Java and Docker is required.",
    "At least one year of Python and Java experience required.",
    "Not just one of Python or Java, but both are required.",
])
def test_ambiguous_alternatives_are_not_silently_relaxed(description):
    result = rank_job(job(description=description, required_skills=[]), profile(), "Java", {})
    assert result["score"] < 100
    assert not any(entry.startswith("One of ") for entry in result["matches"])


def test_section_headings_weight_preferred_bullets_and_reset_at_responsibilities():
    result = rank_job(job(description="Required qualifications:\nPython\nBonus points:\nProficiency in Terraform\nDocker\nResponsibilities:\nBuild Java services.",
                          required_skills=[], preferred_skills=[]), profile(), "Python Java", {})
    assert result["score"] == 71  # Python 3 + Java 2, out of 3+1+1+2.
    assert result["not_evidenced"] == [
        "Terraform (weight 1): not found in the supplied resume",
        "Docker (weight 1): not found in the supplied resume",
    ]


def test_separate_requirement_clauses_keep_independent_preference_weights():
    result = rank_job(job(description="Python required; Docker preferred.", required_skills=[]), profile(), "Python", {})
    assert result["score"] == 75
    assert result["not_evidenced"] == ["Docker (weight 1): not found in the supplied resume"]


def test_bonus_heading_experience_is_not_a_hard_gate():
    result = rank_job(job(description="Python required.\nBonus points:\n3+ years of professional engineering experience."),
                      profile(experience_months=12), "Python", {})
    assert status(result, "Experience") == "pass"
    assert result["experience_requirement"]["status"] == "unspecified"


@pytest.mark.parametrize("months, expected", [(0, "pass"), (8, "pass"), (24, "pass"), (25, "fail"), (None, "unknown")])
def test_explicit_bounded_experience_compares_actual_months(months, expected):
    result = rank_job(job(description="Python. 0–2 years of professional software engineering experience."),
                      profile(experience_months=months), "Python", {})
    assert status(result, "Experience") == expected
    requirement = result["experience_requirement"]
    assert requirement["status"] == "known"
    assert requirement["minimum_months"] == 0
    assert requirement["maximum_months"] == 24
    assert "0–2 years" in requirement["evidence"][0]


@pytest.mark.parametrize("description", [
    "Approximately 0–2 years of professional engineering experience.",
    "Minimum 0–2 years of professional engineering experience.",
    "2–0 years of professional engineering experience.",
    "0–2 years of engineering experience or equivalent education.",
])
def test_ambiguous_experience_ranges_remain_unknown(description):
    result = rank_job(job(description=description), profile(experience_months=12), "Python", {"max_required_experience_months": 12})
    assert status(result, "Experience") == "unknown"
    assert status(result, "Experience filter") == "unknown"
    assert result["experience_requirement"]["minimum_months"] is None
    assert result["experience_requirement"]["maximum_months"] is None


def test_explicit_approved_work_location_does_not_change_contact_address():
    actual = profile(location="Boston, MA", approved_work_locations=["San Jose"])
    result = rank_job(job(location="San Jose, CA"), actual, "Python", {})
    assert status(result, "Work location") == "pass"
    detail = next(item["detail"] for item in result["eligibility"] if item["label"] == "Work location")
    assert "explicitly approved work location" in detail
    assert actual["location"] == "Boston, MA"


def test_contact_address_and_substring_are_not_work_location_approvals():
    result = rank_job(job(), profile(approved_work_locations=[]), "Python", {})
    assert status(result, "Work location") == "unknown"
    substring = rank_job(job(location="Austin, TX"), profile(approved_work_locations=["US"]), "Python", {})
    assert status(substring, "Work location") == "unknown"


@pytest.mark.parametrize("description, expected", [
    ("Minimum 2 years of professional software engineering experience. Python.", "fail"),
    ("1+ years of professional software engineering experience. Python.", "pass"),
    ("0–2 years of professional software engineering experience. Python.", "pass"),
    ("No prior experience required. Python.", "pass"),
    ("Zero years of professional experience required. Python.", "pass"),
    ("Python required.", "unknown"),
    ("3 years of professional engineering experience preferred. Python.", "unknown"),
])
def test_max_required_experience_filter_compares_explicit_minimum(description, expected):
    result = rank_job(job(description=description), profile(experience_months=12), "Python", {"max_required_experience_months": 12})
    assert status(result, "Experience filter") == expected


def test_entry_level_title_never_invents_numeric_experience_or_boosts_score():
    junior = rank_job(job(title="Software Engineer — Entry Level", description="Python and Docker required.", required_skills=[]),
                      profile(), "Python", {"max_required_experience_months": 12, "prefer_new_grad": True})
    general = rank_job(job(title="Software Engineer", description="Python and Docker required.", required_skills=[]), profile(), "Python", {})
    assert junior["is_new_grad"] is True
    assert junior["experience_requirement"]["minimum_months"] is None
    assert status(junior, "Experience filter") == "unknown"
    assert junior["score"] == general["score"] == 50


@pytest.mark.parametrize("description, expected", [
    ("We welcome recent graduates. Python.", True),
    ("This is not an entry-level role. Python.", False),
    ("Mentoring entry-level engineers is part of this role. Python.", False),
    ("Develop software and collaborate with a team. Python.", False),
])
def test_new_grad_preference_needs_an_explicit_applicable_signal(description, expected):
    result = rank_job(job(description=description), profile(), "Python", {})
    assert result["is_new_grad"] is expected


def test_hyphenated_new_grad_title_and_graduate_degree_are_distinct():
    assert rank_job(job(title="Software Engineer, New-Grad"), profile(), "Python", {})["is_new_grad"] is True
    result = rank_job(job(description="A graduate engineering degree is preferred. Python."), profile(), "Python", {})
    assert result["is_new_grad"] is False


@pytest.mark.parametrize("descriptor", ["backend", "frontend", "general-purpose"])
def test_explicit_one_of_language_descriptors_do_not_require_every_option(descriptor):
    description = ("Required qualifications:\n"
                   f"Strong fluency in at least one {descriptor} language (Python, TypeScript, or similar) and a modern web stack.\n"
                   "Docker")
    result = rank_job(job(description=description, required_skills=[], preferred_skills=[]), profile(), "Built Python services.", {})
    assert result["score"] == 50  # One language requirement 3; Docker requirement 3.
    assert len(result["matches"]) == 1
    assert "matched Python" in result["matches"][0]
    assert result["not_evidenced"] == ["Docker (weight 3): not found in the supplied resume"]


@pytest.mark.parametrize("role", ["a software engineer", "software engineer", "a developer", "a programmer"])
def test_explicit_years_as_software_role_has_known_bounds(role):
    description = f"Required qualifications:\n0–1 years as {role} (strong candidates have shipped something with real users).\nPython"
    result = rank_job(job(description=description), profile(experience_months=12), "Python", {"max_required_experience_months": 12})
    requirement = result["experience_requirement"]
    assert requirement["status"] == "known"
    assert requirement["minimum_months"] == 0
    assert requirement["maximum_months"] == 12
    assert f"0–1 years as {role}" in requirement["evidence"][0]
    assert status(result, "Experience") == "pass"
    assert status(result, "Experience filter") == "pass"


@pytest.mark.parametrize("description", [
    "We have operated for 0–1 years. Python.",
    "Required qualifications:\n0–1 years as a financial analyst.\nPython.",
])
def test_unrelated_years_are_not_software_experience_bounds(description):
    result = rank_job(job(description=description), profile(), "Python", {"max_required_experience_months": 12})
    assert result["experience_requirement"]["status"] == "unspecified"
    assert result["experience_requirement"]["minimum_months"] is None
    assert result["experience_requirement"]["maximum_months"] is None
    assert status(result, "Experience filter") == "unknown"
