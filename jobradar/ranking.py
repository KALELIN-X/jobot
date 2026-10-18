"""Explainable resume evidence ranking, separate from application eligibility."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .models import company_scope

# Aliases describe one concept, so e.g. AWS is never counted twice.
_SKILLS: dict[str, tuple[str, ...]] = {
    "Python": ("python",), "Java": ("java",),
    "JavaScript": ("javascript", "ecmascript"), "TypeScript": ("typescript",),
    "C++": ("c++",), "C#": ("c#", "c sharp"), "C": ("c",),
    "Go": ("golang", "go"), "Rust": ("rust",), "Ruby": ("ruby",),
    "PHP": ("php",), "Swift": ("swift",), "Kotlin": ("kotlin",),
    "SQL": ("sql",), "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",), "MongoDB": ("mongodb",), "Redis": ("redis",),
    "React": ("react", "reactjs", "react.js"), "Angular": ("angular",),
    "Vue": ("vue", "vue.js", "vuejs"), "Node.js": ("node.js", "nodejs"),
    ".NET": (".net", "dotnet"), "Django": ("django",), "Flask": ("flask",),
    "FastAPI": ("fastapi",), "Spring": ("spring", "spring boot", "springboot"),
    "HTML": ("html", "html5"), "CSS": ("css", "css3"),
    # PDF extraction can insert spaces inside this acronym. Keep recovery
    # explicit instead of joining arbitrary words or fuzzy-matching skills.
    "AWS": ("aws", "a ws", "aw s", "a w s", "amazon web services"),
    "Azure": ("azure", "microsoft azure"),
    "GCP": ("gcp", "google cloud", "google cloud platform"),
    "Docker": ("docker",), "Kubernetes": ("kubernetes", "k8s"),
    "Terraform": ("terraform",), "Linux": ("linux",), "Git": ("git",),
    "GraphQL": ("graphql",), "REST APIs": ("rest", "restful", "rest api", "rest apis"),
    "Kafka": ("kafka", "apache kafka"), "Spark": ("spark", "apache spark"),
    "PyTorch": ("pytorch",), "TensorFlow": ("tensorflow",),
    "Machine learning": ("machine learning",),
    "Distributed systems": ("distributed systems",),
    "Data structures": ("data structures",), "Algorithms": ("algorithms",),
    "System design": ("system design",),
    "Unit testing": ("unit testing", "unit tests", "unit test"),
    "CI/CD": ("ci/cd", "continuous integration", "continuous delivery"),
}


def _pattern(alias: str) -> re.Pattern:
    expression = re.escape(alias).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![\w+#])" + expression + r"(?![\w+#])", re.I)


def _find(text: str, aliases: tuple[str, ...]) -> re.Match | None:
    found = [match for alias in aliases if (match := _pattern(alias).search(text))]
    return min(found, key=lambda match: match.start()) if found else None


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _excerpt(text: str, match: re.Match) -> str:
    start = max(text.rfind("\n", 0, match.start()), text.rfind(". ", 0, match.start())) + 1
    if start < 0 or match.start() - start > 85:
        start = max(0, match.start() - 65)
    end_points = [end for marker in ("\n", ". ") if (end := text.find(marker, match.end())) >= 0]
    end = min(end_points) if end_points else len(text)
    end = min(end, match.end() + 105)
    result = re.sub(r"\s+", " ", text[start:end]).strip(" .")
    return ("…" if start and text[start - 1] not in "\n." else "") + result + ("…" if end < len(text) and text[end] not in "\n." else "")


def _entry(label: str, status: str, detail: str) -> dict:
    return {"label": label, "status": status, "detail": detail}


def _description_units(description: str) -> list[tuple[str, int | None]]:
    """Keep heading scope while separating individual requirement sentences."""
    section: int | None = None
    units: list[tuple[str, int | None]] = []
    for line in description.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.sub(r"\s+", " ", line.strip(" *#:\t")).casefold()
        if re.fullmatch(r"(?:required|minimum|basic|essential)(?: skills| qualifications| requirements)?(?: and experience)?|requirements|qualifications", heading):
            section = 3
            continue
        if re.fullmatch(r"(?:preferred|desired)(?: skills| qualifications| requirements)?(?: and experience)?|nice[ -]to[ -]have|bonus(?: points)?(?: if(?: you)?(?: have)?| for)?", heading):
            section = 1
            continue
        if re.fullmatch(r"about(?: us| the role| the company)?|overview|responsibilities|benefits|what you(?:'|’)ll do|the role", heading):
            section = None
            continue
        # An unfamiliar short colon-ended heading ends the preceding scope.
        # Do not mistake a technical bullet such as "Python:" for a heading.
        if len(heading) <= 80 and line.endswith(":") and not any(_find(line, aliases) for aliases in _SKILLS.values()):
            section = None
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|;\s*", line):
            if sentence.strip():
                units.append((sentence.strip(), section))
    return units


def _mentions(text: str, keywords: dict[str, tuple[str, ...]]) -> list[tuple[str, re.Match]]:
    """Return skill spans, preferring a full alias to its shorter overlap."""
    mentions: list[tuple[str, re.Match]] = []
    for label, aliases in keywords.items():
        found = [match for alias in aliases for match in _pattern(alias).finditer(text)]
        accepted: list[re.Match] = []
        for match in sorted(found, key=lambda item: (item.start(), -item.end())):
            if not any(match.start() < other.end() and match.end() > other.start() for other in accepted):
                accepted.append(match)
                mentions.append((label, match))
    return sorted(mentions, key=lambda item: (item[1].start(), -item[1].end()))


def _alternative_groups(text: str, mentions: list[tuple[str, re.Match]]) -> list[tuple[list[str], int, int]]:
    """Recognize explicit one-of lists, leaving unclear prose ungrouped.

    Plain "Python or Java" is not enough: that can describe examples or separate
    duties. A one-of/either introducer and an uninterrupted list are required.
    Mixed "or ... and" conjunctions and unknown intervening words remain manual
    interpretation rather than silently relaxing the ranking requirement.
    """
    groups: list[tuple[list[str], int, int]] = []
    prefix = re.compile(r"\b(?:at\s+least\s+one|(?:any\s+)?one\s+of|either)\b", re.I)
    intro_words = re.compile(r"\b(?:of|the|these|following|programming|coding|back[ -]?end|front[ -]?end|general[ -]purpose|language|languages|technology|technologies|framework|frameworks|database|databases|cloud|provider|providers|such|as|including|like)\b", re.I)
    for match in prefix.finditer(text):
        candidates = [(label, span) for label, span in mentions if span.start() >= match.end()]
        if not candidates:
            continue
        intro = text[match.end():candidates[0][1].start()]
        if len(intro) > 100 or re.search(r"\w", intro_words.sub("", intro)):
            continue
        members = [candidates[0]]
        connectors: set[str] = set()
        for candidate in candidates[1:]:
            gap = text[members[-1][1].end():candidate[1].start()]
            if not re.fullmatch(r"(?:\s|,|/|\band\b|\bor\b)+", gap, re.I):
                break
            connectors.update(word.casefold() for word in re.findall(r"\b(?:and|or)\b", gap, re.I))
            members.append(candidate)
        labels = list(dict.fromkeys(label for label, _ in members))
        if len(labels) < 2 or len(connectors) > 1:
            continue
        if match.group().casefold() == "either" and "or" not in connectors:
            continue
        tail = text[members[-1][1].end():]
        if re.search(r"\b(?:but\s+(?:both|all)|(?:both|all)\s+(?:are\s+)?required)\b", tail, re.I):
            continue
        start, end = members[0][1].start(), members[-1][1].end()
        if not any(start < previous_end and end > previous_start for _, previous_start, previous_end in groups):
            groups.append((labels, start, end))
    return groups


def _keyword_weight(text: str, labels: list[str], keywords: dict[str, tuple[str, ...]],
                    section: int | None, required: str, preferred: str) -> int:
    # A preferred heading applies to all its bullets, even a bullet that says
    # "proficiency in". An inline preference is also explicit evidence of weight.
    if section == 1 or re.search(r"preferred|nice.to.have|bonus|a plus", text, re.I):
        return 1
    if (section == 3 or re.search(r"required|must|minimum|proficien", text, re.I)
            or any(_find(required, keywords[label]) for label in labels)):
        return 3
    if any(_find(preferred, keywords[label]) for label in labels):
        return 1
    return 2


def _graduation_check(text: str, profile: dict) -> dict:
    sentences = re.split(r"[\n;]|(?<=[.!?])\s+", text)
    relevant = [part for part in sentences if re.search(r"graduat(?:e|es|ed|ion|ing)", part, re.I)
                and re.search(r"20\d\d", part)]
    if not relevant:
        return _entry("Graduation date", "pass", "No explicit graduation-date constraint was found in the available description.")
    constraint = " ".join(relevant)[:600]
    supplied = profile.get("graduation_date")
    try:
        graduation = date.fromisoformat(str(supplied)) if supplied else None
    except ValueError:
        graduation = None
    if not graduation:
        return _entry("Graduation date", "unknown", f"Posting states: {constraint} Supply your actual graduation date.")
    # Unambiguous year or year range. Month/season windows remain unknown unless
    # they can be interpreted exactly; a year-only comparison must not approve them.
    years = [int(year) for year in re.findall(r"\b20\d\d\b", constraint)]
    if re.search(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)\b", constraint, re.I):
        return _entry("Graduation date", "unknown", f"Your date is {graduation.isoformat()}; verify the posting's specific month/season window: {constraint}")
    if len(set(years)) == 1 and re.search(r"\b(?:before|by|after|since|prior|within)\b", constraint, re.I):
        return _entry("Graduation date", "unknown", f"Verify the graduation cutoff against {graduation.isoformat()}: {constraint}")
    if years:
        lower, upper = min(years), max(years)
        return _entry("Graduation date", "pass" if lower <= graduation.year <= upper else "fail",
                      f"Posting graduation years {lower}–{upper}; your supplied date is {graduation.isoformat()}.")
    return _entry("Graduation date", "unknown", f"Verify the graduation requirement: {constraint}")


def _experience_requirement(text: str, job: dict) -> dict:
    """Extract explicit numeric constraints without turning a job title into one."""
    requirements = job.get("requirements") or []
    pattern = re.compile(r"\b(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*(\+)?\s*(years?|months?)\s+"
                         r"(?:(?:of\s+)?(?:[\w/-]+\s+){0,5}experience\b"
                         r"|as\s+(?:a\s+)?(?:software\s+engineer|(?:software\s+)?developer|programmer)\b)", re.I)
    found: list[tuple[int, int | None, str]] = []
    uncertain: list[str] = []
    units = _description_units(text)
    units.extend((value, 3) for value in requirements if isinstance(value, str))
    ambiguity = re.compile(r"\b(?:approximately|about|around|up to|more than|less than)\b"
                           r"|\bor\b.{0,50}\b(?:equivalent|education|degree|training|internships?|projects?)\b", re.I)
    for sentence, section in units:
        if section == 1 or re.search(r"preferred|nice.to.have|bonus|ideally|a plus", sentence, re.I):
            continue
        if re.search(r"\bno\s+(?:(?:prior|previous|professional|work|relevant)\s+)*experience\s+(?:is\s+)?(?:required|necessary)\b"
                     r"|\bzero\s+(?:years?|months?)\s+(?:of\s+)?(?:[\w/-]+\s+){0,5}experience\s+(?:is\s+)?required\b", sentence, re.I):
            if ambiguity.search(sentence):
                uncertain.append(sentence.strip())
            else:
                found.append((0, None, sentence.strip()))
        for match in pattern.finditer(sentence):
            multiplier = 12 if match.group(4).lower().startswith("year") else 1
            months = int(match.group(1)) * multiplier
            if ambiguity.search(sentence):
                uncertain.append(sentence.strip())
                continue
            if match.group(2) is not None:
                upper = int(match.group(2)) * multiplier
                # A plain bounded range has two explicit limits. Qualifiers or
                # alternative education paths can change their meaning; do not
                # turn those into a hard interval without review.
                ambiguous = (upper < months or match.group(3) or re.search(r"\b(?:minimum|at least)\b", sentence, re.I))
                if ambiguous:
                    uncertain.append(sentence.strip())
                else:
                    found.append((months, upper, sentence.strip()))
                continue
            # Years are a hard gate only when explicitly mandatory, in a structured
            # required qualification, or expressed as a minimum with '+'.
            if match.group(3) or section == 3 or re.search(r"must|require|minimum|at least", sentence, re.I):
                found.append((months, None, sentence.strip()))
            else:
                uncertain.append(sentence.strip())
    result = {"minimum_months": None, "maximum_months": None, "status": "unspecified",
              "evidence": list(dict.fromkeys(sentence for _, _, sentence in found)) + list(dict.fromkeys(uncertain)),
              "explanation": "No explicit mandatory numeric experience requirement was found; entry-level/new-grad titles do not establish a number."}
    if uncertain:
        result.update(status="unknown", explanation="An experience statement needs interpretation before its numeric limits can be confirmed: " + uncertain[0][:400])
        return result
    if not found:
        return result
    required_months = max(lower for lower, _, _ in found)
    upper_bounds = [upper for _, upper, _ in found if upper is not None]
    maximum_months = min(upper_bounds) if upper_bounds else None
    evidence = "; ".join(result["evidence"])[:400]
    if maximum_months is not None and required_months > maximum_months:
        result.update(status="unknown", explanation="The available experience statements have conflicting ranges; review their separate scopes. " + evidence)
        return result
    if (len({lower for lower, _, _ in found}) > 1
            and re.search(r"\b(?:bachelor|master|ph\.?d|degree|depending)\b", evidence, re.I)):
        result.update(status="unknown", explanation="Experience differs by education or another pathway; confirm the applicable requirement. " + evidence)
        return result
    constraint = (f"{required_months}–{maximum_months} months" if maximum_months is not None
                  else f"at least {required_months} months")
    result.update(minimum_months=required_months, maximum_months=maximum_months, status="known",
                  explanation=f"The posting explicitly states {constraint}. {evidence}")
    return result


def _experience_check(requirement: dict, profile: dict) -> dict:
    if requirement["status"] == "unknown":
        return _entry("Experience", "unknown", requirement["explanation"])
    if requirement["status"] == "unspecified":
        return _entry("Experience", "pass", "No explicit minimum number of experience months/years was found.")
    required_months = requirement["minimum_months"]
    maximum_months = requirement["maximum_months"]
    months = profile.get("experience_months")
    constraint = (f"{required_months}–{maximum_months} months" if maximum_months is not None
                  else f"at least {required_months} months")
    if not isinstance(months, (int, float)) or isinstance(months, bool) or months < 0:
        return _entry("Experience", "unknown", f"Posting states {constraint}. Supply your actual relevant experience. " + requirement["explanation"][:300])
    meets = months >= required_months and (maximum_months is None or months <= maximum_months)
    return _entry("Experience", "pass" if meets else "fail",
                  f"Posting experience: {constraint}; you supplied {int(months)} months. " + requirement["explanation"][:300])


def _new_grad_signal(title: str, description: str) -> bool:
    pattern = re.compile(r"\b(?:new[ -]?|recent\s+)(?:(?:college|university)\s+)?grad(?:uate)?s?\b"
                         r"|\bentry[ -]level\b|\bgraduate\s+(?:(?:software\s+)?engineer|(?:software\s+)?engineering\s+(?:program|role|position)|program|role|position)\b", re.I)
    for sentence in [title] + [text for text, _ in _description_units(description)]:
        for match in pattern.finditer(sentence):
            before = sentence[max(0, match.start() - 50):match.start()]
            if re.search(r"\b(?:not|isn't|isn’t)\b.{0,30}$", before, re.I):
                continue
            if re.search(r"\b(?:mentor(?:ing)?|supervis(?:e|ing)|manag(?:e|ing))\b.{0,40}$", before, re.I):
                continue
            return True
    return False


def _eligibility(job: dict, profile: dict, rules: dict, description: str, experience: dict) -> list[dict]:
    result = []
    companies = rules.get("companies") or []
    if company_scope(rules) == "selected":
        allowed = _norm(job.get("company")) in {_norm(company) for company in companies}
        result.append(_entry("Approved company", "pass" if allowed else "fail", f"{job.get('company') or 'Unknown company'} {'is' if allowed else 'is not'} in your company allowlist."))
    locations = rules.get("locations") or []
    location = str(job.get("location") or "")
    work_model = str(job.get("work_model") or "")
    if locations:
        actual = _norm(location + " " + work_model)
        allowed = any(_norm(value) and re.search(r"(?<!\w)" + re.escape(_norm(value)) + r"(?!\w)", actual)
                      for value in locations)
        result.append(_entry("Approved location", "pass" if allowed else "fail", f"Posting location: {location or 'unknown'} ({work_model or 'work model unknown'}); compared with your location allowlist."))
    ats = str(job.get("ats") or "unsupported")
    allowed_ats = rules.get("allowed_ats") or []
    if allowed_ats:
        result.append(_entry("Approved ATS", "pass" if ats in allowed_ats else "fail", f"Posting ATS: {ats}. Your approved ATS: {', '.join(str(value) for value in allowed_ats)}."))
    if job.get("closed") is True:
        result.append(_entry("Posting availability", "fail", "The public source explicitly marks this job as deleted/closed."))
    elif job.get("closed") is False:
        result.append(_entry("Posting availability", "pass", "Public job data is not marked deleted; availability will be rechecked before submission."))
    else:
        result.append(_entry("Posting availability", "unknown", "Current employer posting availability has not been verified. Fetch/parse failures do not mean it is closed."))

    if not description.strip():
        result.append(_entry("Full description", "unknown", "Resolve the full job description before checking job requirements."))
        return result
    text = str(job.get("title") or "") + "\n" + description
    result.extend((_graduation_check(text, profile), _experience_check(experience, profile)))
    experience_limit = rules.get("max_required_experience_months")
    if isinstance(experience_limit, int) and not isinstance(experience_limit, bool):
        if experience["status"] == "known":
            minimum = experience["minimum_months"]
            result.append(_entry("Experience filter", "pass" if minimum <= experience_limit else "fail",
                                 f"The posting's explicit minimum is {minimum} months; your filter allows a required minimum of at most {experience_limit} months. This filter compares the required minimum, not the applicant's maximum experience."))
        else:
            result.append(_entry("Experience filter", "unknown", "Cannot confirm the posting's required minimum against your experience filter. " + experience["explanation"]))
    job_location = _norm(location)
    is_remote = "remote" in _norm(work_model)
    approved_location = next((str(value) for value in (profile.get("approved_work_locations") or [])
                              if isinstance(value, str) and _norm(value)
                              and re.search(r"(?<!\w)" + re.escape(_norm(value)) + r"(?!\w)", job_location)), None)
    if not location:
        result.append(_entry("Work location", "unknown", "Posting work location is missing."))
    elif approved_location:
        result.append(_entry("Work location", "pass", f"Posting location {location} matches your explicitly approved work location: {approved_location}."))
    else:
        result.append(_entry("Work location", "unknown", f"Verify {'remote geographic restrictions' if is_remote else 'on-site/hybrid attendance or relocation'} for {location}; add an explicit approved work location after confirming. Your contact location is {profile.get('location') or 'missing'}."))

    authorization = profile.get("work_authorized")
    requires_auth = bool(re.search(r"(?:must|require[ds]?|need to|have to|eligible to|legally)\s+(?:be\s+)?(?:legally\s+)?(?:authorized|eligible)\s+to\s+work|work authorization (?:is )?required", text, re.I))
    if authorization is True:
        result.append(_entry("Work authorization", "pass", "You supplied that you are authorized to work; verify this applies to the posting's country."))
    elif authorization is False:
        result.append(_entry("Work authorization", "fail" if requires_auth else "unknown", "You supplied that you are not authorized to work. " + ("The posting explicitly requires authorization." if requires_auth else "The employer's authorization pathway needs review.")))
    else:
        result.append(_entry("Work authorization", "unknown", "Supply your actual work authorization for the posting's country."))
    no_sponsor = bool(re.search(
        r"\b(?:no|without)\s+(?:(?:visa|employment|immigration|work|employer)\s+){0,3}sponsorship\b"
        r"|\b(?:cannot|can't|will not|does not|do not|unable to|not able to)\s+"
        r"(?:(?:provide|offer|support)\s+)?(?:(?:visa|employment|immigration|work)\s+)?sponsor(?:ship)?\b"
        r"|\bsponsorship\s+(?:is\s+)?(?:not available|not provided|not offered)\b", text, re.I))
    needs = profile.get("needs_sponsorship")
    if needs is False:
        result.append(_entry("Sponsorship", "pass", "You supplied that you do not need employer sponsorship."))
    elif needs is True and no_sponsor:
        result.append(_entry("Sponsorship", "fail", "You need sponsorship and the posting explicitly says sponsorship is unavailable."))
    elif needs is True:
        result.append(_entry("Sponsorship", "unknown", "You need sponsorship. Confirm this employer will sponsor this particular role; a company sponsorship history is not a guarantee."))
    else:
        result.append(_entry("Sponsorship", "unknown", "Supply whether you need employer sponsorship."))
    if re.search(r"(?:citizen(?:ship)?|security clearance).{0,45}(?:required|must)|(?:must|required).{0,45}(?:citizen|security clearance)", text, re.I):
        result.append(_entry("Citizenship or clearance", "unknown", "The posting has a citizenship or clearance requirement. Review the original requirement; no answer is inferred."))
    return result


def rank_job(job: dict, profile: dict, resume_text: str, rules: dict) -> dict:
    """Return an evidence score, missing evidence, and independent eligibility gates.

    Technical concepts get weight 2, explicit required concepts 3, and preferred
    concepts 1. Explicit one-of lists form one requirement; one evidenced option
    earns that requirement's points. Only text actually found in the resume earns
    credit. This is a sorting aid, not interview probability.
    """
    description = str(job.get("description") or "").strip()
    resume = str(resume_text or "")[:200_000]
    title = str(job.get("title") or "")
    experience = _experience_requirement(title + "\n" + description, job)
    result: dict = {"score": None, "matches": [], "not_evidenced": [],
                    "experience_requirement": experience,
                    "is_new_grad": _new_grad_signal(title, description),
                    "eligibility": _eligibility(job, profile, rules, description, experience),
                    "reasons": ["Score measures weighted technical keyword evidence in your resume; it is not an interview probability or an eligibility decision."]}
    if not description:
        result["reasons"].append("No score: the full job description is unavailable. The repository title alone is insufficient.")
        return result
    if not resume.strip():
        result["reasons"].append("No score: upload a text-readable resume to compare your actual experience with this job.")
        return result
    keywords = dict(_SKILLS)
    for custom in (profile.get("skills") or [])[:100]:
        if isinstance(custom, str) and 2 <= len(custom.strip()) <= 60:
            custom = custom.strip()
            if not any(_find(custom, aliases) for aliases in keywords.values()):
                keywords[custom] = (custom.casefold(),)
    required = "\n".join(str(value) for value in (job.get("required_skills") or []) + (job.get("requirements") or []))
    preferred = "\n".join(str(value) for value in job.get("preferred_skills") or [])
    requirements: dict[tuple[str, ...], tuple[list[str], int]] = {}
    for sentence, section in _description_units(description):
        mentions = _mentions(sentence, keywords)
        alternatives = _alternative_groups(sentence, mentions)
        candidates = [labels for labels, _, _ in alternatives]
        candidates.extend([label] for label, span in mentions
                          if not any(start <= span.start() and span.end() <= end for _, start, end in alternatives))
        for labels in candidates:
            key = tuple(sorted(labels))
            weight = _keyword_weight(sentence, labels, keywords, section, required, preferred)
            if key not in requirements or weight > requirements[key][1]:
                requirements[key] = (labels, weight)
    total = earned = 0
    for labels, weight in requirements.values():
        label = labels[0] if len(labels) == 1 else "One of " + " / ".join(labels)
        total += weight
        evidenced = [(option, evidence) for option in labels if (evidence := _find(resume, keywords[option]))]
        if evidenced:
            option, evidence = min(evidenced, key=lambda item: item[1].start())
            earned += weight
            matched_option = f", matched {option}" if len(labels) > 1 else ""
            result["matches"].append(f"{label} (weight {weight}){matched_option}: “{_excerpt(resume, evidence)}”")
        else:
            result["not_evidenced"].append(f"{label} (weight {weight}): not found in the supplied resume")
    if total:
        result["score"] = round(100 * earned / total)
        result["reasons"].append(f"Earned {earned} of {total} technical-keyword weight points. Required terms weigh 3, general technical terms 2, preferred terms 1. Missing evidence does not prove you lack the skill.")
        if any(len(labels) > 1 for labels, _ in requirements.values()):
            result["reasons"].append("Explicit one-of/either skill lists count as one requirement; any listed option with resume evidence earns its points. Unclear alternatives remain separate keywords for manual review.")
    else:
        result["reasons"].append("No score: no supported technical keywords were identified in the description. Review the role manually.")
    threshold = rules.get("min_score")
    if isinstance(threshold, (int, float)) and result["score"] is not None:
        result["reasons"].append(f"Ranking {'meets' if result['score'] >= threshold else 'is below'} your minimum score of {int(threshold)}.")
    failures = [entry["label"] for entry in result["eligibility"] if entry["status"] == "fail"]
    unknowns = [entry["label"] for entry in result["eligibility"] if entry["status"] == "unknown"]
    if failures:
        result["reasons"].append("Eligibility/rule failures are separate from the score: " + ", ".join(failures) + ".")
    if unknowns:
        result["reasons"].append("Needs verification: " + ", ".join(unknowns) + ".")
    return result
