"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from applypilot.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import re
import logging

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}

MAX_RESUME_BULLETS = 14
MAX_RESUME_WORDS = 550  # bumped from 475 to allow more descriptive bullets while still fitting one page
MAX_SUBTITLE_TECH_ITEMS = 8
MAX_SUBTITLE_LENGTH = 90  # chars; keeps "Role | Tech list" on one PDF line


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, list):
            allowed.update(s.lower().strip() for s in category)
        elif isinstance(category, set):
            allowed.update(s.lower().strip() for s in category)
    return allowed


def _term_allowed(term: str, allowed: set[str]) -> bool:
    """Check if a detected term is explicitly allowed by profile skills."""
    t = term.lower().strip()
    for skill in allowed:
        if t == skill or t in skill or skill in t:
            return True
    return False


def _extract_master_project_names(resume_text: str) -> list[str]:
    """Extract project names from the master resume's PROJECTS section.

    A project header is the line immediately after the PROJECTS heading
    (and after each project's bullets), formatted like "Name | Tech stack".
    Returns just the name part (before "|").
    """
    lines = resume_text.splitlines()
    in_projects = False
    names: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        upper = line.upper().strip()
        if upper in ("PROJECTS", "PROJECT", "SIDE PROJECTS"):
            in_projects = True
            continue
        if in_projects and upper in ("SKILLS", "EDUCATION", "TECHNICAL SKILLS", "WORK EXPERIENCE", "EXPERIENCE"):
            break
        if not in_projects:
            continue
        # Skip bullet lines (start with bullet/dash markers)
        if line.startswith(("?", "-", "•", "*", "·")):
            continue
        # Header line: "Project Name | Tech stack" — keep just the name
        name = line.split("|", 1)[0].strip()
        if name:
            names.append(name)
    return names


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def validate_json_fields(data: dict, profile: dict, mode: str = "normal", resume_text: str = "") -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:        Parsed JSON from the LLM (title, skills, experience, projects, education).
        profile:     User profile dict from load_profile().
        mode:        Validation strictness — "strict", "normal", or "lenient".
                     strict  → banned words are errors (trigger retries)
                     normal  → banned words are warnings (no retry)
                     lenient → banned words ignored entirely
        resume_text: Optional master resume text. When provided, enables project
                     preservation checks (drops are flagged unless master has 5+ projects).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required keys — always checked regardless of mode
    for key in ("title", "skills", "experience", "projects", "education"):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # Collect all text for bulk checks
    all_text_parts: list[str] = []

    # Skills: check for fabrication (always enforced)
    allowed_skills = _build_skills_set(profile)
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text and not _term_allowed(fake, allowed_skills):
                errors.append(f"Fabricated skill: '{fake}'")

        # Breadth check: the tailored skills section must list at least as many
        # comma-separated items as the SKILLS BOUNDARY. The LLM may swap items
        # irrelevant to the JD for JD-relevant ones, but cannot shrink total
        # count -- that signals the candidate as less versatile.
        boundary = profile.get("skills_boundary", {})
        boundary_count = sum(
            len(items) for items in boundary.values() if isinstance(items, list)
        )
        tailored_count = sum(
            len([s for s in str(v).split(",") if s.strip()])
            for v in data["skills"].values()
        )
        if tailored_count < boundary_count:
            shortfall = boundary_count - tailored_count
            errors.append(
                f"Skills count is {tailored_count}, must be at least {boundary_count}. "
                f"Add {shortfall} more comma-separated skill(s). Swapping irrelevant items "
                f"for JD-relevant ones is fine; reducing total count is not."
            )

    # Experience: preserved companies must be present (always enforced)
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        for company in preserved_companies:
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Projects: collect bullets + enforce preservation (when master resume provided)
    if isinstance(data["projects"], list):
        for entry in data["projects"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

        # Preservation: if the master resume has 4 or fewer projects, all must be kept.
        # (LLM may only drop projects when the candidate has 5+ — page-fit constraint.)
        if resume_text:
            master_projects = _extract_master_project_names(resume_text)
            if 0 < len(master_projects) <= 4:
                tailored_headers = " || ".join(
                    str(p.get("header", "")).lower() for p in data["projects"]
                )
                missing_projects = [
                    name for name in master_projects
                    if name.lower() not in tailored_headers
                ]
                if missing_projects:
                    errors.append(
                        f"Projects section dropped {len(missing_projects)} project(s): "
                        f"{', '.join(missing_projects)}. Reorder, do not delete "
                        f"(only allowed when master has 5+ projects)."
                    )

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if preserved_school.lower() not in edu.lower():
            errors.append(f"Education '{preserved_school}' missing")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 1-page guard: subtitle (tech list) must stay short to fit on one line
    for section_name in ("experience", "projects"):
        for entry in data.get(section_name, []) or []:
            sub = str(entry.get("subtitle", "")).strip()
            if not sub:
                continue
            # Strip trailing "| Date" before counting tech items
            tech_part = sub.rsplit("|", 1)[0].strip() if "|" in sub else sub
            tech_items = [t.strip() for t in tech_part.split(",") if t.strip()]
            if len(tech_items) > MAX_SUBTITLE_TECH_ITEMS:
                errors.append(
                    f"{section_name} subtitle has {len(tech_items)} tech items "
                    f"(max {MAX_SUBTITLE_TECH_ITEMS}); keep most relevant only. "
                    f"Use parent vendor (e.g. 'AWS') instead of listing sub-services."
                )
            if len(sub) > MAX_SUBTITLE_LENGTH:
                errors.append(
                    f"{section_name} subtitle is {len(sub)} chars "
                    f"(max {MAX_SUBTITLE_LENGTH}); shorten to fit one line."
                )

    # 1-page guard: enforce bullet count and word budget
    total_bullets = len(all_text_parts)
    if total_bullets > MAX_RESUME_BULLETS:
        errors.append(
            f"Too many bullets ({total_bullets}). Max {MAX_RESUME_BULLETS} to fit 1 page. "
            "Cut low-impact bullets."
        )
    total_words = len(all_text.split())
    skills_words = sum(len(str(v).split()) for v in data.get("skills", {}).values())
    content_words = total_words + skills_words
    if content_words > MAX_RESUME_WORDS:
        errors.append(
            f"Resume too long ({content_words} words). Max {MAX_RESUME_WORDS} to fit 1 page. "
            "Shorten bullets."
        )

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        allowed_skills = _build_skills_set(profile)
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block and not _term_allowed(fake, allowed_skills):
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        allowed_skills = _build_skills_set(profile)
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                if not _term_allowed(fake, allowed_skills):
                    warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", or "lenient".
              strict  → banned words are errors (trigger retries); word limit enforced
              normal  → banned words are warnings; word limit is soft (+25 words)
              lenient → banned words ignored; word count not checked

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")
    # lenient: no word count check

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" — always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
