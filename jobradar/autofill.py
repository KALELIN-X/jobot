"""Conservative, single-attempt browser automation for approved ATS forms.

The planner is deliberately independent of Playwright.  A field is answerable only
from an explicit profile value or an exact approved question/answer pair.  The
runner never retries a submission whose outcome is unknown.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import calendar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

from .destinations import GREENHOUSE_HOSTS, LEVER_HOSTS, parse_destination
from .form_adapters import GreenhouseTransport, SNAPSHOT_JS, choose_combobox, enrich_comboboxes
from .progress import ProgressCallback, ProgressReporter


_APPLY_PATH = re.compile(
    r"^/[A-Za-z0-9][A-Za-z0-9_-]*/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/apply/?$"
)
_LEGAL = re.compile(
    r"\b(?:agree(?:ment)?|consent|certif(?:y|ication)|acknowledg\w*|"
    r"attest\w*|declaration|signature|privacy|terms(?: of)?|i authorize)\b",
    re.I,
)
_DECLARATION = re.compile(
    r"(?:\bby\s+(?:clicking|submitting|applying)\b[\s\S]{0,350}"
    r"\b(?:agree|consent|certify|acknowledge|authorize|attest)\b|"
    r"\bi\s+(?:hereby\s+)?(?:certify|attest|declare)\b)",
    re.I,
)
_PRIVATE_CHALLENGE = re.compile(
    r"\b(?:password|passcode|captcha|verification code|security code|one[\s-]+time[\s-]+code|"
    r"otp|totp|2fa|mfa|api key|access token|auth(?:entication)? token|"
    r"social security|ssn|bank account|routing number)\b", re.I,
)
_CONFIRMATION = (
    r"(?:application\s+(?:(?:has been|was)\s+)?(?:successfully\s+)?submitted"
    r"|(?:we\s+(?:have\s+)?received\s+your\s+application)"
    r"|thank\s+you\s+for\s+(?:applying|submitting|your\s+application))"
)


class URLNotApproved(ValueError):
    """The URL is outside the narrowly supported application boundary."""


def _url_parts(url: str):
    if not isinstance(url, str) or not url or any(c.isspace() for c in url):
        raise URLNotApproved("Application URL is invalid.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise URLNotApproved("Application URL is invalid.") from exc
    if parts.username or parts.password or not parts.hostname:
        raise URLNotApproved("Application URL credentials or host are invalid.")
    if "\\" in url or parts.fragment or unquote(parts.path) != parts.path:
        raise URLNotApproved("Application URL path is not supported.")
    if any(segment in {".", ".."} for segment in parts.path.split("/")):
        raise URLNotApproved("Application URL path is not supported.")
    return parts, port


def validate_application_url(
    url: str, allowed_hosts: set[str], *, fixture: bool = False
) -> str:
    """Validate an initial URL and return its exact normalized host.

    ``fixture`` is an explicit capability for the local demonstration form.  It
    does not enable arbitrary localhost pages, HTTP Lever URLs, or other ATSes.
    """
    parts, port = _url_parts(url)
    host = parts.hostname.lower()
    approved = {h.lower() for h in allowed_hosts}
    if host not in approved:
        raise URLNotApproved("Application host has not been approved.")
    if fixture:
        if (
            parts.scheme != "http"
            or host != "127.0.0.1"
            or port is None
            or parts.path.rstrip("/") != "/mock/apply"
        ):
            raise URLNotApproved("Only the local mock application is supported in fixture mode.")
    else:
        destination = parse_destination(url)
        if destination is None or destination["ats"] not in {"lever", "greenhouse"}:
            raise URLNotApproved("Only approved HTTPS Lever or Greenhouse application URLs are supported.")
        if destination["ats"] == "lever" and not _APPLY_PATH.fullmatch(parts.path):
            raise URLNotApproved("A Lever application must use its exact apply URL.")
    return host


def validate_destination_url(
    url: str,
    initial_url: str,
    allowed_hosts: set[str],
    *,
    fixture: bool = False,
    document: bool = False,
) -> None:
    """Require the original approved origin for every document destination."""
    original_host = validate_application_url(initial_url, allowed_hosts, fixture=fixture)
    parts, port = _url_parts(url)
    original, original_port = _url_parts(initial_url)
    if (
        parts.hostname.lower() != original_host
        or parts.scheme != original.scheme
        or (port or (443 if parts.scheme == "https" else 80))
        != (original_port or (443 if original.scheme == "https" else 80))
    ):
        raise URLNotApproved("The application navigated outside its approved origin.")
    if fixture and parts.path.rstrip("/") not in {"/mock/apply", "/mock/thanks"}:
        raise URLNotApproved("The mock application navigated outside its approved paths.")
    if not fixture and original_host in GREENHOUSE_HOSTS:
        destination = parse_destination(initial_url)
        # Remain on this exact board/posting, including its own confirmation.
        # Same-origin API requests are checked separately by the request guard.
        if destination and (document or re.search(r"/jobs/\d+", parts.path)):
            allowed_path = urlsplit(destination["canonical_url"]).path.rstrip("/")
            if parts.path.rstrip("/").casefold() not in {allowed_path, allowed_path + "/confirmation"}:
                raise URLNotApproved("The application navigated to a different Greenhouse posting.")


def _label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\s*(?:[*✱∗]|\(required\))\s*$", "", text, flags=re.I).strip()
    return text


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _us_or_unspecified_authorization(question: str) -> bool:
    if question in {"work authorization", "work authorized"}:
        return True
    match = re.search(r"\bauthorized to work\b", question)
    if not match:
        return False
    suffix = question[match.end():].strip()
    return not suffix or bool(re.fullmatch(r"(?:for any employer )?in (?:the )?(?:united states(?: of america)?|us|u s|usa|u s a)", suffix))


@dataclass(frozen=True)
class FieldAction:
    field_id: str
    kind: str
    value: Any


@dataclass
class FormPlan:
    actions: list[FieldAction] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    pending_questions: list[dict] = field(default_factory=list)


def reusable_question(question: str) -> bool:
    """Only ordinary questions belong in reusable memory, never challenges or agreements."""
    return bool(question and len(question) <= 500 and not _LEGAL.search(question) and not _PRIVATE_CHALLENGE.search(question))


def _pending_question(plan: FormPlan, item: dict, *, reason: str, options: list[dict] | None = None) -> None:
    # Keep the visible question and choices, never the site's prefilled value.
    # A machine input name alone is not a question that the user can review.
    question = _label(item.get("label"))
    kind = "select" if item.get("tag") == "select" else str(item.get("type", "text")).lower()
    if not reusable_question(question) or kind not in {"text", "email", "tel", "url", "number", "date", "month", "search", "textarea", "select", "checkbox", "radio"}:
        return
    if any(candidate["question"] == question for candidate in plan.pending_questions):
        return
    choices = []
    for option in options if options is not None else item.get("options", []):
        label = re.sub(r"\s+", " ", str(option.get("label") or "")).strip()
        value = str(option.get("value", ""))
        if option.get("disabled") or not label or not value or len(label) > 500 or len(value) > 500:
            continue
        if {"label": label, "value": value} not in choices:
            choices.append({"label": label, "value": value})
    if len(plan.pending_questions) < 60:
        plan.pending_questions.append({
            "id": hashlib.sha256(question.encode("utf-8")).hexdigest()[:20],
            "key": question, "question": question, "type": kind,
            "required": bool(item.get("required")), "options": choices[:300], "reason": reason,
        })


def _answer(item: dict, profile: dict) -> Any:
    """Return only explicitly provided facts; never infer first/last name, etc."""
    label = _label(item.get("label"))
    answers = profile.get("answers") or {}
    if isinstance(answers, dict) and label in answers:
        return answers[label]
    # A supplied graduation month can answer month/year fields, but never
    # manufacture a day for a full-date field.
    graduation = profile.get("graduation_month")
    question = _normal(label)
    if isinstance(graduation, str) and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", graduation):
        year, month = graduation.split("-")
        if question in {"graduation month", "expected graduation month"}:
            if item.get("type") == "month":
                return graduation
            if item.get("type") == "number":
                return str(int(month))
            choices = {month, str(int(month)), calendar.month_name[int(month)], calendar.month_abbr[int(month)]}
            available = [option for option in item.get("options", []) if option.get("label") in choices or str(option.get("value")) in choices]
            return available[0]["label"] if len(available) == 1 else calendar.month_name[int(month)]
        if question in {"graduation year", "expected graduation year"}:
            return year
        if item.get("type") == "month" and question in {"graduation date", "expected graduation date", "graduation month year"}:
            return graduation
    name_aliases = {
        "name": {"name", "full name", "fullname", "your name", "legal name", "full legal name"},
        "first_name": {"first name", "firstname", "given name", "givenname", "legal first name", "legal given name"},
        "last_name": {"last name", "lastname", "family name", "familyname", "surname", "legal last name", "legal family name"},
        "preferred_first_name": {"preferred first name", "preferredfirstname", "preferred given name"},
    }
    # A visible name label takes precedence over an ambiguous input name. Never
    # split, concatenate, or substitute legal and preferred names for one another.
    for identifier in (_normal(label), _normal(item.get("name"))):
        for key, candidates in name_aliases.items():
            if identifier in candidates:
                return profile.get(key)
    names = {_normal(label), _normal(item.get("name"))}
    aliases = {
        "email": {"email", "email address", "emailaddress", "your email"},
        "phone": {"phone", "phone number", "phonenumber", "telephone", "mobile phone"},
        "location": {"location", "current location", "currentlocation", "location city", "city country"},
        "current_company": {"current company", "currentcompany", "current employer", "organization"},
        "linkedin": {"linkedin", "linkedin url", "linkedin profile", "urls linkedin"},
        "github": {"github", "github url", "github profile", "urls github"},
        "portfolio": {"portfolio", "portfolio url", "personal website", "website", "urls portfolio"},
        "graduation_date": {"graduation date", "graduationdate", "expected graduation date"},
        "experience_months": {"experience months", "months of experience", "experience in months"},
        "skills": {"skills", "technical skills"},
    }
    for key, candidates in aliases.items():
        if names & candidates and key in profile:
            value = profile[key]
            if key == "skills" and isinstance(value, list) and all(isinstance(v, str) for v in value):
                return ", ".join(value)
            return value
    question = _normal(label)
    if (
        re.search(r"\b(?:legally )?authorized to work\b", question)
        or question in {"work authorization", "work authorized"}
    ) and type(profile.get("work_authorized")) is bool and _us_or_unspecified_authorization(question):
        return profile["work_authorized"]
    if (
        "sponsorship" in question
        and re.search(r"\b(?:need|require|requires|will|now|future)\b", question)
        and type(profile.get("needs_sponsorship")) is bool
    ):
        return profile["needs_sponsorship"]
    return None


def _option_match(options: list[dict], answer: Any) -> dict | None:
    if isinstance(answer, bool):
        words = {"yes", "true"} if answer else {"no", "false"}
        matches = [o for o in options if _normal(o.get("label")) in words and not o.get("disabled")]
    else:
        expected = str(answer)
        matches = [
            o for o in options
            if not o.get("disabled")
            and (str(o.get("label", "")).strip() == expected or str(o.get("value", "")) == expected)
        ]
    return matches[0] if len(matches) == 1 else None


def _phone_country_option(item: dict, answer: Any, profile: dict) -> dict | None:
    """Match a dial-code decoration only on the explicit phone-country control."""
    if not (item.get("combobox") and _normal(item.get("label")) == "country" and _normal(item.get("name")) == "country" and isinstance(answer, str)):
        return None
    phone = re.sub(r"[\s().-]", "", str(profile.get("phone") or ""))
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        return None
    matches = []
    for option in item.get("options", []):
        if option.get("disabled"):
            continue
        match = re.fullmatch(re.escape(answer.strip()) + r"\s+\+([1-9]\d{0,3})", str(option.get("label", "")))
        if match and phone.startswith("+" + match.group(1)):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def plan_form(fields: list[dict], profile: dict, resume_path: Path | None = None) -> FormPlan:
    """Build safe actions and human-review blockers from a DOM field snapshot.

    Metadata records use ``id, label, name, type, tag, required, visible,
    disabled, value, checked, options``.  A radio's label is its question and
    ``option_label`` is the visible text for that particular radio.
    """
    plan = FormPlan()
    radios: dict[str, list[dict]] = {}
    for item in fields:
        kind = str(item.get("type", "text")).lower()
        if item.get("disabled") or kind in {"hidden", "submit", "reset", "button", "image"}:
            continue
        if not item.get("visible", True) and kind != "file":
            if item.get("required"):
                plan.blockers.append("A required field is hidden or uses an unsupported custom control.")
            continue
        label = _label(item.get("label"))
        name = _label(item.get("name"))
        description = label or name or "Unlabelled field"
        required = bool(item.get("required"))
        if _LEGAL.search(label + " " + name) and (required or kind in {"checkbox", "radio"}):
            plan.blockers.append(f"Human review required for declaration or agreement: {description[:160]}")
            continue
        if _PRIVATE_CHALLENGE.search(label + " " + name):
            plan.blockers.append(f"A verification, account, or private-data field requires human review: {description[:160]}")
            continue
        if item.get("multiple"):
            plan.blockers.append(f"A multiple-selection field requires human review: {description[:160]}")
            continue
        if kind == "radio":
            radios.setdefault(str(item.get("name") or label), []).append(item)
            continue
        if kind == "file":
            is_resume = bool(re.search(r"\b(?:resume|cv|curriculum vitae)\b", _normal(label + " " + name)))
            if is_resume and resume_path is not None:
                if not item.get("has_file"):
                    plan.actions.append(FieldAction(item["id"], "file", str(resume_path)))
            elif required:
                plan.blockers.append(f"A supplied file is required: {description[:160]}")
            continue
        answer = _answer(item, profile)
        if answer is None or answer == "" or isinstance(answer, (dict, list, tuple, set)):
            _pending_question(plan, item, reason="missing_answer")
            if required:
                plan.blockers.append(f"No explicit approved answer for required field: {description[:160]}")
            continue
        if kind == "checkbox":
            if type(answer) is not bool:
                _pending_question(plan, item, reason="boolean_required")
                plan.blockers.append(f"An explicit boolean answer is required: {description[:160]}")
            elif required and not answer:
                _pending_question(plan, item, reason="required_checkbox_conflict")
                plan.blockers.append(f"Required checkbox conflicts with the approved answer: {description[:160]}")
            elif bool(item.get("checked")) != answer:
                plan.actions.append(FieldAction(item["id"], "check", answer))
        elif item.get("tag") == "select" or kind == "select":
            option = _option_match(item.get("options", []), answer) or _phone_country_option(item, answer, profile)
            if option is None or str(option.get("value", "")) == "":
                _pending_question(plan, item, reason="choice_unavailable")
                plan.blockers.append(f"No exact available choice for approved answer: {description[:160]}")
            elif item.get("combobox"):
                if _label(item.get("selected_label")) != _label(option["label"]):
                    plan.actions.append(FieldAction(item["id"], "combobox", str(option["label"])))
            elif str(item.get("value", "")) != str(option["value"]):
                plan.actions.append(FieldAction(item["id"], "select", str(option["value"])))
        elif kind in {"text", "email", "tel", "url", "number", "date", "month", "search", "textarea"}:
            if isinstance(answer, bool):
                _pending_question(plan, item, reason="text_required")
                plan.blockers.append(f"A typed answer needs human review: {description[:160]}")
            elif str(item.get("value", "")) != str(answer):
                plan.actions.append(FieldAction(item["id"], "fill", str(answer)))
        else:
            if required:
                plan.blockers.append(f"Unsupported required field: {description[:160]}")
    for group in radios.values():
        example = group[0]
        label = _label(example.get("label")) or _label(example.get("name"))
        answer = _answer(example, profile)
        options = [dict(i, label=i.get("option_label", i.get("value", ""))) for i in group]
        selected = _option_match(options, answer) if answer is not None else None
        if selected is None:
            _pending_question(plan, dict(example, required=any(i.get("required") for i in group)), reason="missing_choice", options=options)
            if any(i.get("required") for i in group) or answer is not None:
                plan.blockers.append(f"No exact approved choice for required question: {label[:160]}")
        elif not selected.get("checked"):
            plan.actions.append(FieldAction(selected["id"], "check", True))
    return plan


_SNAPSHOT_JS = SNAPSHOT_JS

def _form_blockers(snapshot: dict, initial_url: str, allowed_hosts: set[str], fixture: bool) -> list[str]:
    blockers = []
    if snapshot.get("captcha"):
        blockers.append("A CAPTCHA requires human attention.")
    if snapshot.get("login"):
        blockers.append("A login requires human attention.")
    if _DECLARATION.search(snapshot.get("body", "")):
        blockers.append("A submission declaration or agreement requires human review.")
    if re.search(_CONFIRMATION, snapshot.get("body", ""), re.I):
        blockers.append("The page already displays an application confirmation; review before any new attempt.")
    buttons = snapshot.get("buttons", [])
    if len(buttons) != 1:
        blockers.append("The application does not have one recognized submit button.")
    else:
        fields = [f for f in snapshot.get("fields", []) if f["id"] in buttons[0].get("form_fields", []) and f.get("visible", True) and not f.get("disabled") and f.get("type") != "hidden"]
        has_name = any(_normal(f.get("label")) in {"name", "full name", "your name"} or _normal(f.get("name")) in {"name", "fullname", "full name"} for f in fields)
        has_first = any(_normal(f.get("label")) in {"first name", "given name", "legal first name"} or _normal(f.get("name")) in {"first name", "firstname"} for f in fields)
        has_last = any(_normal(f.get("label")) in {"last name", "family name", "surname", "legal last name"} or _normal(f.get("name")) in {"last name", "lastname"} for f in fields)
        has_email = any(_normal(f.get("label")) in {"email", "email address", "your email"} or _normal(f.get("name")) in {"email", "emailaddress", "email address"} for f in fields)
        if not (has_name or has_first and has_last) or not has_email:
            blockers.append("This is not a recognized application form with name and email fields.")
        try:
            validate_destination_url(buttons[0]["action"], initial_url, allowed_hosts, fixture=fixture)
        except (URLNotApproved, KeyError):
            blockers.append("The form submits outside its approved destination.")
    return blockers


def _fingerprint(snapshot: dict) -> str:
    # IDs change between scans; include the real field state and structure instead.
    data = {
        "fields": [{k: v for k, v in f.items() if k != "id"} for f in snapshot.get("fields", [])],
        "buttons": [{"action": b.get("action"), "count": len(b.get("form_fields", []))} for b in snapshot.get("buttons", [])],
        "captcha": snapshot.get("captcha"), "login": snapshot.get("login"),
        "declaration": bool(_DECLARATION.search(snapshot.get("body", ""))),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


async def run_application(
    job: dict,
    profile: dict,
    resume_path: Path | None,
    *,
    allowed_hosts: set[str],
    artifacts_dir: Path,
    submit: bool = True,
    fixture: bool = False,
    before_submit: Callable[[], Awaitable[Any]] | None = None,
    fixture_transport: dict | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Fill an approved form and, when fully cleared, click submit at most once.

    The caller must separately verify the employer, job fingerprint, profile, and
    user rule approvals.  ``before_submit`` must atomically reserve the attempt
    and revalidate those rules; returning ``False`` refuses the attempt.
    ``on_progress`` receives best-effort stage/count events, never answers.
    """
    progress = ProgressReporter(on_progress)

    async def early_result(status: str, message: str) -> dict:
        await progress.finish(status)
        return {"status": status, "message": message}

    url = job.get("apply_url") or job.get("application_url") or job.get("url")
    try:
        validate_application_url(url, allowed_hosts, fixture=fixture)
    except (URLNotApproved, TypeError):
        return await early_result("needs_attention", "Application URL is not within the approved supported destinations.")
    if resume_path is not None:
        resume_path = Path(resume_path).resolve()
        if not resume_path.is_file():
            return await early_result("failed", "The supplied resume file is unavailable.")
    if fixture_transport is not None and not (fixture and job.get("demo") is True):
        return await early_result("needs_attention", "Local transport fixtures are restricted to synthetic demonstration jobs.")
    try:
        transport = GreenhouseTransport(url, resume_path, local_fixture=fixture_transport)
    except (ValueError, KeyError, OSError):
        return await early_result("needs_attention", "The hosted form transport could not be safely initialized.")
    clicked = False
    screenshot = None
    page = None
    loaded_document = False
    pending_questions: list[dict] = []
    blocked_requests: list[str] = []
    submission_responses: list[int] = []
    final_request_count = 0

    async def result(status: str, message: str, *, terminal: bool = True, **extra) -> dict:
        nonlocal screenshot
        if page is not None:
            try:
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                path = artifacts_dir / f"application-{uuid.uuid4().hex}.png"
                await page.screenshot(path=str(path), full_page=True, timeout=5000)
                screenshot = str(path.resolve())
            except Exception:
                pass
        payload = {"status": status, "message": message, "pending_questions": pending_questions, **extra}
        if screenshot:
            payload["screenshot"] = screenshot
        if terminal:
            await progress.finish(status, pending_question_count=len(pending_questions), screenshot=screenshot)
        return payload

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")

                async def guard(route):
                    nonlocal final_request_count
                    request = route.request
                    if urlsplit(request.url).hostname == "c.spl.greenhouse.io":
                        # Hosted-board analytics are unnecessary for applying.
                        # Drop the payload, without treating telemetry as a form.
                        await route.abort()
                        return
                    # Document navigations and data-bearing requests must stay on
                    # the approved origin. No cookies or storage are imported.
                    if request.is_navigation_request() or request.method not in {"GET", "HEAD", "OPTIONS"}:
                        if transport.enabled and request.method not in {"GET", "HEAD", "OPTIONS"}:
                            if clicked and request.method == "POST" and transport.is_submit_url(request.url) and final_request_count == 0:
                                final_request_count += 1
                                await progress.emit("submitting")
                                await route.continue_()
                                await progress.emit("awaiting_confirmation")
                                return
                            if not request.is_navigation_request():
                                upload_body = transport.prepare_upload(request.url, request.method, request.headers, request.post_data_buffer)
                                if upload_body is not None:
                                    await progress.emit("uploading_resume")
                                    await route.continue_(post_data=upload_body)
                                    return
                            blocked_requests.append("unapproved_transport")
                            await route.abort()
                            return
                        if not clicked and (request.method not in {"GET", "HEAD", "OPTIONS"} or loaded_document and request.is_navigation_request()):
                            blocked_requests.append("premature_submission")
                            await route.abort()
                            return
                        try:
                            validate_destination_url(request.url, url, allowed_hosts, fixture=fixture, document=request.is_navigation_request())
                        except URLNotApproved:
                            blocked_requests.append("destination")
                            await route.abort()
                            return
                        if clicked and request.method == "POST":
                            if final_request_count:
                                blocked_requests.append("duplicate_submission")
                                await route.abort()
                                return
                            final_request_count += 1
                            await progress.emit("submitting")
                            await route.continue_()
                            await progress.emit("awaiting_confirmation")
                            return
                    await route.continue_()

                await context.route("**/*", guard)
                page = await context.new_page()
                page.on("response", transport.observe)
                def observe_response(response):
                    if clicked and response.request.method not in {"GET", "HEAD", "OPTIONS"}:
                        if transport.enabled:
                            if response.request.method == "POST" and transport.is_submit_url(response.url):
                                submission_responses.append(response.status)
                            return
                        try:
                            validate_destination_url(response.url, url, allowed_hosts, fixture=fixture)
                        except URLNotApproved:
                            if not transport.is_submit_url(response.url):
                                return
                        submission_responses.append(response.status)
                page.on("response", observe_response)
                page.set_default_timeout(8000)
                await progress.emit("opening_form")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                loaded_document = True
                validate_destination_url(page.url, url, allowed_hosts, fixture=fixture, document=True)
                await progress.emit("inspecting_form")
                await page.locator("input,select,textarea").first.wait_for(state="attached", timeout=10000)

                async def snapshot() -> dict:
                    validate_destination_url(page.url, url, allowed_hosts, fixture=fixture, document=True)
                    if blocked_requests:
                        raise URLNotApproved("An unapproved destination was blocked.")
                    await progress.emit("inspecting_form")
                    current = await page.evaluate(_SNAPSHOT_JS, uuid.uuid4().hex)
                    if transport.upload_success:
                        for field in current.get("fields", []):
                            if field.get("type") == "file" and re.search(r"resume|\bcv\b", field.get("label", ""), re.I):
                                # The exact selected bytes completed the verified
                                # upload, even if the renderer reset its input.
                                field["has_file"] = True
                    if not current.get("captcha") and not current.get("login") and not _DECLARATION.search(current.get("body", "")):
                        current = await enrich_comboboxes(page, current, lambda item: _answer(item, profile))
                    return current

                state = await snapshot()
                for _ in range(3):
                    await progress.emit("checking_answers")
                    blockers = _form_blockers(state, url, allowed_hosts, fixture)
                    plan = plan_form(state["fields"], profile, resume_path)
                    pending_questions = plan.pending_questions
                    blockers += plan.blockers
                    if blockers:
                        return await result("needs_attention", " ".join(dict.fromkeys(blockers))[:1200])
                    if not plan.actions:
                        break
                    field_actions = [action for action in plan.actions if action.kind != "file"]
                    completed_fields = 0
                    if field_actions:
                        await progress.emit("filling_fields", field_count=len(field_actions), completed_fields=0)
                    for action in plan.actions:
                        validate_destination_url(page.url, url, allowed_hosts, fixture=fixture, document=True)
                        if blocked_requests:
                            raise URLNotApproved("An unapproved destination was blocked.")
                        control = page.locator(f'[data-jobradar-field="{action.field_id}"]')
                        if action.kind == "fill":
                            await control.fill(action.value)
                        elif action.kind == "select":
                            await control.select_option(value=action.value)
                        elif action.kind == "combobox":
                            await choose_combobox(page, action.field_id, action.value)
                        elif action.kind == "check":
                            await control.set_checked(action.value)
                        elif action.kind == "file":
                            if transport.enabled:
                                try:
                                    await transport.begin_upload()
                                    await control.set_input_files(action.value)
                                    await transport.finish_upload()
                                except Exception:
                                    transport.upload_active = False
                                    return await result("needs_attention", "The hosted form's verified resume upload could not be completed; review it manually.")
                            else:
                                await control.set_input_files(action.value)
                        if action.kind != "file":
                            completed_fields += 1
                            await progress.emit("filling_fields", field_count=len(field_actions), completed_fields=completed_fields)
                    state = await snapshot()
                else:
                    return await result("needs_attention", "The application kept changing while fields were filled.")

                await progress.emit("checking_answers")
                final_plan = plan_form(state["fields"], profile, resume_path)
                pending_questions = final_plan.pending_questions
                blockers = _form_blockers(state, url, allowed_hosts, fixture) + final_plan.blockers
                if final_plan.actions or blockers:
                    return await result("needs_attention", " ".join(blockers)[:1200] or "The form needs human review after filling.")
                if not await page.locator("form").evaluate_all("forms => forms.filter(f => f.getClientRects().length).every(f => f.checkValidity())"):
                    return await result("needs_attention", "The form reports missing or invalid fields.")
                if not submit:
                    return await result("needs_attention", "Approved fields were filled. Submission was disabled for this run.")
                if before_submit is None:
                    return await result("needs_attention", "An atomic attempt reservation and approval recheck are required before submission.")
                # Capture the review state before the reservation. No profile data
                # enters logs, exception messages, or filenames.
                await result("needs_attention", "Ready for final approval recheck.", terminal=False)
                state = await snapshot()
                stamp = _fingerprint(state)
                try:
                    permitted = await before_submit()
                except Exception:
                    return await result("needs_attention", "The final approval or attempt reservation check did not pass.")
                if permitted is False:
                    return await result("needs_attention", "The final approval or attempt reservation check refused submission.")
                checked_state = await snapshot()
                if _fingerprint(checked_state) != stamp:
                    return await result("needs_attention", "The form changed during the final approval check.")
                blockers = _form_blockers(checked_state, url, allowed_hosts, fixture)
                if blockers:
                    return await result("needs_attention", " ".join(blockers)[:1200])
                button = page.locator(f'[data-jobradar-submit="{checked_state["buttons"][0]["id"]}"]')
                clicked = True
                await button.click(timeout=10000)
                await page.wait_for_function(
                    "pattern => new RegExp(pattern, 'i').test(document.body ? document.body.innerText : '')",
                    arg=_CONFIRMATION,
                    timeout=15000,
                )
                validate_destination_url(page.url, url, allowed_hosts, fixture=fixture, document=True)
                if blocked_requests:
                    return await result("uncertain", "A destination was blocked after the single submission attempt. Do not retry automatically.")
                if not submission_responses or any(status >= 400 for status in submission_responses) or not any(200 <= status < 400 for status in submission_responses):
                    return await result("uncertain", "A confirmation appeared without a successful application request. Verify the employer outcome; do not retry automatically.")
                return await result("submitted", "The approved site displayed an application confirmation.", confirmation="Recognized application-submitted or thank-you message on the approved origin.")
            finally:
                await browser.close()
    except URLNotApproved:
        return await result("uncertain" if clicked else "needs_attention", "An unapproved application destination was blocked." + (" Do not retry automatically." if clicked else ""))
    except Exception:
        # Exception strings can contain field values or URLs; never persist them.
        return await result("uncertain" if clicked else "failed", "The single submission attempt could not be confirmed. Do not retry automatically." if clicked else "The browser could not complete the guarded application workflow; review it locally.")
