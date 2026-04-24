"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    FABRICATION_WATCHLIST,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up
TAILOR_MAX_TOKENS = 8192


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    boundary_count = 0
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
            boundary_count += len(items)
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    projects_str = ", ".join(projects) if projects else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    return f"""You are a senior technical recruiter rewriting a resume to get this person an interview.

Take the base resume and job description. Return a tailored resume as a JSON object.

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. First 3 bullets of most recent role -- verbs and outcomes match?
3. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). No unrelated languages/frameworks.

## TAILORING RULES:

TITLE: Set to the candidate's current title verbatim — DO NOT change to match the JD. The system overwrites this field anyway, so changing it wastes tokens and triggers retries.

SKILLS (PRESERVE BREADTH — reorder freely, swap 1-for-1, never shrink the count):
- DEFAULT (SAFEST) BEHAVIOR: copy ALL {boundary_count} items from the SKILLS BOUNDARY above verbatim, then reorder within each category so JD-relevant items appear first. This guarantees you pass validation. When in doubt, do this.
- HARD FLOOR: the total count of comma-separated items across all categories MUST be ≥ {boundary_count}. Anything less = automatic validation failure and forced retry (wasted tokens). Count yourself before returning.
- SWAP RULE (only if you really want to swap): if you delete one item, you MUST add a different item back in the SAME response. Never delete without replacing. Replacement items must be JD-relevant tools that are closely related to the candidate's existing stack (e.g. delete "Flutter", add "Tailwind" if the JD wants Tailwind and there is no mobile work in the JD).
- ADD RULE: you MAY also add JD-relevant tools as net-new ABOVE the {boundary_count} floor if they're natural extensions (Kubernetes if Docker, Terraform if AWS, Postgres if SQL).
- Keep the boundary's category structure when in doubt. Do not collapse categories or pile every skill into one bucket.

SUBTITLE (tech list per experience/project entry — PRESERVE COUNT, REORDER, SWAP):
- Start from the ORIGINAL subtitle's tech items. Keep the SAME number of items.
- Reorder so job-relevant items come first.
- You MAY swap an item only if a closely-related JD term exists in the SKILLS BOUNDARY (e.g. swap "MySQL" for "Postgres" if both are in your stack and JD wants Postgres). Otherwise keep the original item.
- DO NOT add items that didn't exist in the original (other than the allowed swap).
- DO NOT delete items unless the line absolutely cannot fit (max 90 chars total). Collapse cloud sub-services to parent vendor first ("AWS Lambda, AWS Redshift" → "AWS"), drop true duplicates, then trim from the LEAST relevant end as a last resort.

EXPERIENCE BULLETS — three layers, each with its own rule:

Each bullet has THREE distinct layers. Treat them differently:

  (A) TECHNICAL CORE — the system, the tech, the action. PROTECTED. Never change.
      Includes: action verb, system name (e.g. "checkout-as-a-service", "Chrome automation crawlers", "TypeScript component library"), the tech stack.

  (B) DATA SUBSTRATE — what the system processes. PROTECTED. Never change to look generic.
      Includes: "audio tracks" (music data), "Amazon Vendor/Seller Central" (e-commerce data), "market feeds" / "market data" (financial data), "checkout orders". These describe what the work IS. Keep them.
      DO NOT replace "audio tracks" with "data tracks", "market data" with "data", "Amazon Vendor Central" with "downstream systems".

  (C) DOWNSTREAM BUSINESS USE-CASE — the sales/marketing/CRM tooling the output feeds. ADAPTABLE.
      Includes: "GTM tools", "CRM data waterfalls", "AI-driven lead scoring & ICP targeting", "outbound sequencing", "RevOps tools", "Salesforce, enrichment, dialers".
      → If the JD is sales-tech / GTM, keep as-is.
      → If the JD is in a different vertical (AI research, fintech, infra, consumer), reword to a NEUTRAL phrase or remove the trailing "for X" clause.

CONCRETE EXAMPLES from this candidate's master resume:
- ORIGINAL: "ML pipelines processing 100k+ audio tracks/day; boosted classification accuracy 25% to power AI-driven lead scoring & ICP targeting"
  GOOD (non-sales-tech JD): "Built ML training pipeline (Python, TensorFlow, PyTorch) processing 100k+ audio tracks/day; lifted classification accuracy 25% (78% → 97%), serving the team's audio understanding stack"
  BAD: "ML pipelines processing 100k+ data tracks/day, boosting classification accuracy 25% for AI-driven data scoring"  (lost "audio", invented "data scoring")

- ORIGINAL: "low-latency market data pipeline capable of handling 10,000+ events per second ... for high-frequency trading applications"
  GOOD: "Engineered Rust market-data pipeline sustaining 10k+ events/sec at sub-100ms p99, powering live HFT signal generation"
  BAD: "low-latency data pipeline handling 10,000+ events/second for real-time applications"  (lost "market data", "trading")

PROJECTS — preserve all, reorder by relevance:
- Include EVERY project from the original resume. Reorder so most job-relevant comes first.
- You may only drop a project if the candidate has MORE than 4 projects in the original. Otherwise keep all.
- Apply the SAME THREE-LAYER + SWE BEST PRACTICES rules as experience.
- Max 2 bullets per project.

## SWE RESUME BEST PRACTICES (apply to every experience and project bullet):

1. STAR-compressed structure: "[Strong verb] [system + tech] [scope/scale] [quantified impact] [optional why-it-mattered]"

2. LEAD WITH A STRONG ACTION VERB. Vary across the resume. Choose from:
   Build/ship: Built, Architected, Designed, Implemented, Shipped, Launched, Delivered, Engineered
   Improve: Reduced, Cut, Doubled, Tripled, Scaled, Optimized, Accelerated, Migrated, Refactored, Consolidated
   Lead: Led, Drove, Owned, Spearheaded, Coordinated, Mentored
   Avoid weak openers: "Worked on", "Helped", "Was responsible for", "Assisted", "Participated in".

3. QUANTIFY EVERY BULLET. Every bullet should have at least one number. Good metric types:
   - Performance: latency (p50/p95/p99 ms), throughput (req/sec, events/sec, QPS), uptime (%)
   - Scale: users (DAU/MAU), requests/day, data volume (TB, rows/day, tracks/day), node/cluster count
   - Impact: $ saved, $ revenue enabled, hours saved/week, % conversion lift, % error reduction
   - Velocity: deploy frequency, build time saved, MTTR reduction, lines of code removed
   - Engineering: test coverage %, code review turnaround, % rollouts on canary, % CI green
   FABRICATING METRICS IS ALLOWED — invent PLAUSIBLE numbers consistent with the bullet's domain and scale. Do NOT make wild claims (no "10B requests/sec", no "99.999% uptime" unless backed by the original). Prefer ranges that match the company's likely scale (Stackline = mid-market SaaS, Breaking Hits = ML startup, Edenspiekermann = agency).

4. KEYWORD DENSITY. Mirror the JD's exact tech terms (capitalization included) in skills + bullets when the candidate genuinely worked with them. ATS systems do exact-string matches.

5. SPECIFICITY > GENERICITY. "PostgreSQL" not "database", "gRPC" not "RPC", "Kafka" not "event streams" (when you know the tool).

6. SHOW SCOPE. Include team/cross-functional context where it adds signal: "led 3-engineer working group", "drove RFC across 4 teams", "owned roadmap for 12-service domain".

7. SHOW IMPACT, NOT TASKS. End each bullet with the OUTCOME, not what you were assigned. "Migrated to gRPC, dropping inter-service p99 from 240ms to 80ms" beats "Migrated services to gRPC".

8. STRUCTURE CONSISTENCY. Past tense across the board. Parallel verb structure within each role's bullet block. No periods at end of bullets.

9. NO FILLER. No adverbs ("seamlessly", "robustly", "successfully"), no marketing words ("cutting-edge", "world-class", "leveraging"). Engineers reading the resume can spot LLM-flavored writing instantly.

10. LENGTH. Aim for 1.5–2 lines per bullet (~25–40 words). One-line bullets are fine when the impact is sharp; avoid 3-line walls of text.

## VOICE:
- Write like a real engineer. Direct, specific, technical.
- GOOD: "Built read-through Redis cache in front of order service, cutting p99 read latency 380ms → 65ms and reducing DB QPS by 60% during peak campaigns"
- BAD: "Leveraged cutting-edge caching technologies to seamlessly drive transformative latency improvements"
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT invent or change SYSTEM NAMES, TECHNOLOGIES, or DATA SUBSTRATE (layers A and B)
- BULLET COUNT IS A CEILING, NOT A QUOTA: never exceed the ORIGINAL bullet count for any entry. If the original Breaking Hits entry had 1 bullet, your Breaking Hits entry must have AT MOST 1 bullet. Fewer is fine; padding to "fill space" with invented work is fabrication and will fail validation.
- Metrics may be fabricated when PLAUSIBLE for the bullet's domain. Real metrics worth preserving: {metrics_str}
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}
- MUST fit 1 page. Hard limits: max 14 bullets total, max 4 per experience entry, max 2 per project, ~550 words total content.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

SUBTITLE FORMAT:
- Experience subtitle: "Tech1, Tech2, ... | Start - End"  (always include real dates)
- Project subtitle: "Tech1, Tech2, ..."  ONLY. Do NOT append "| N/A", "| Project", or any placeholder when there is no real date.

{{"title":"Role Title","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech only, no date","bullets":["bullet 1","bullet 2"]}}],"education":"{school} | {education_level}"}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile.

    The judge is intentionally narrow: it only catches outright lies that the
    programmatic validator can't. Skills-boundary checks are handled by the
    validator and are NOT duplicated here.
    """
    resume_facts = profile.get("resume_facts", {})
    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume. Your ONLY job is to catch outright LIES — invented experiences, invented metrics that grew, invented projects/companies/degrees, swapped system identities. Style, wording, qualifier removal, and reordering are NEVER lies.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## DEFAULT IS PASS. Only FAIL when you can quote a specific lie.

## WHAT IS *NOT* A LIE — DO NOT FAIL for any of these (read this list FIRST):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one, or splitting one into two
- Describing the same work with different emphasis or different verbs
- Dropping low-relevance bullets, qualifier phrases, or trailing context
  (e.g. "ensured 100% data integrity for downstream GTM tools" → "ensured 100% data integrity" is FINE — qualifier removal is not contradiction)
- Reordering anything
- Reframing the INDUSTRY/USE-CASE (e.g. "for sales tools" → "for analytics teams" or removed)
- Adding or expanding a metric that doesn't exist in the original, AS LONG AS the new number is plausible for the company/role scale
- Same metric with different wording (e.g. "600k+ orders/yr" appearing in both = NOT a contradiction)
- Adding tools/skills to the SKILLS section — the validator already enforces the boundary; do not duplicate that check

## WHAT *IS* A LIE — FAIL only for these (must quote the exact problem):
1. INVENTED EXPERIENCE: a bullet describes work the candidate did not do, with no plausible mapping to any original bullet (e.g. original "built CRUD API" → tailored "built distributed consensus protocol used by Fortune 500").
2. METRIC INFLATION: a number that EXISTED in the original got LARGER in the tailored version (e.g. "100k tracks/day" → "10M tracks/day"), OR an invented number is wildly implausible for the company scale (e.g. "scaled to 100M users" on a startup, "99.9999% uptime", "10B req/sec").
3. PRESERVED METRICS SHRUNK or CONTRADICTED: these specific numbers must never decrease or change: {metrics_str}.
4. INVENTED ENTITIES: companies, schools, degrees, certifications that don't appear in the original.
5. SWAPPED SYSTEM IDENTITY: the core tech or system name was changed (original "Chrome automation crawlers" must not become "Kubernetes operators").
6. SWAPPED DATA SUBSTRATE: what the system processes was changed (original "audio tracks" must not become "generic data"; "market data" must not become "data feeds").

## RULE OF THUMB:
If you have to argue with yourself about whether something is a lie, it isn't. Pass it. Only fail when you can point at a specific phrase and say "this is provably false". Style disagreements are not lies."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


def _summarize_parse_failure(raw: str, err: Exception) -> dict:
    """Capture parse-failure diagnostics for retry reports."""
    stripped = raw.strip()
    likely_truncated = bool(stripped) and stripped.startswith("{") and not stripped.endswith("}")
    return {
        "error": str(err),
        "raw_length": len(raw),
        "likely_truncated": likely_truncated,
        "raw_head": raw[:240],
        "raw_tail": raw[-240:],
    }


# Maps boundary category keys (snake_case) → display category names used in
# the LLM JSON output. Keep in sync with the SKILLS section of the prompt.
_BOUNDARY_TO_DISPLAY_CATEGORY = {
    "programming_languages": "Languages",
    "frameworks": "Frameworks",
    "tools": "Tools",
}


def _pad_skills_with_missing_boundary(data: dict, profile: dict) -> tuple[dict, list[str]]:
    """Append any boundary skills the LLM dropped back into data['skills'].

    `gpt-4o-mini` consistently under-counts skills on backend-heavy JDs (drops
    Vue.js, React Native, Flutter without replacement) and burns retries that
    never converge. This is a deterministic safety net: after the LLM responds,
    we detect missing boundary items and append them to the matching display
    category. The model's reordering and net-new additions are preserved.

    Returns the (possibly mutated) data dict and a list of items that were
    auto-padded (for diagnostics).
    """
    if not isinstance(data.get("skills"), dict):
        return data, []

    boundary = profile.get("skills_boundary", {})
    present_text = " ".join(str(v) for v in data["skills"].values()).lower()
    padded: list[str] = []

    for boundary_cat, items in boundary.items():
        if not isinstance(items, list):
            continue
        display_cat = _BOUNDARY_TO_DISPLAY_CATEGORY.get(
            boundary_cat, boundary_cat.replace("_", " ").title()
        )
        missing: list[str] = []
        for item in items:
            item_lc = item.lower().strip()
            aliases = [item_lc]
            abbr = re.search(r"\(([^)]+)\)", item_lc)
            if abbr:
                aliases.append(abbr.group(1).strip())
                aliases.append(re.sub(r"\s*\([^)]+\)", "", item_lc).strip())
            if not any(a and a in present_text for a in aliases):
                missing.append(item)
        if missing:
            existing = str(data["skills"].get(display_cat, "")).strip()
            sep = ", " if existing else ""
            data["skills"][display_cat] = f"{existing}{sep}{', '.join(missing)}"
            padded.extend(missing)
            present_text = " ".join(str(v) for v in data["skills"].values()).lower()

    return data, padded


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile.
    # Title is locked to current_title (no JD-driven changes); the LLM's "title"
    # field is ignored, but kept in the JSON spec for backwards compatibility.
    lines.append(personal.get("full_name", ""))
    locked_title = profile.get("experience", {}).get("current_title") or "Software Engineer"
    lines.append(sanitize_text(locked_title))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line — order matches the master resume layout:
    # email | LinkedIn | GitHub | Website | phone | City, State
    # pdf.py turns URLs into "LinkedIn"/"GitHub"/"Website" link labels at render time.
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    website = personal.get("website_url") or personal.get("portfolio_url")
    if website:
        contact_parts.append(website)
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    city = (personal.get("city") or "").strip()
    state = (personal.get("province_state") or "").strip()
    if city and state:
        contact_parts.append(f"{city}, {state}")
    elif city:
        contact_parts.append(city)
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
        "parse_failures": [],
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += (
                "\n\n## CRITICAL — YOUR PREVIOUS ATTEMPT FAILED VALIDATION.\n"
                "Fix EACH of these specific issues before producing the new JSON. "
                "These are not suggestions; the output will be rejected again if any remain:\n"
                + "\n".join(f"- {n}" for n in avoid_notes[-5:])
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=TAILOR_MAX_TOKENS, temperature=0.2)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError as e:
            details = _summarize_parse_failure(raw, e)
            report["parse_failures"].append(details)
            log.warning(
                "Tailor JSON parse failed on attempt %d/%d for '%s' (len=%d, truncated=%s)",
                attempt + 1,
                max_retries + 1,
                job.get("title", "")[:60],
                details["raw_length"],
                details["likely_truncated"],
            )
            avoid_notes.append("Output was not valid JSON. Return ONLY a single complete JSON object, no prose.")
            if details["likely_truncated"]:
                avoid_notes.append(
                    "Previous output was cut off. Keep JSON concise and complete: max 3 bullets per experience entry, max 2 bullets per project."
                )
            else:
                avoid_notes.append(f"Previous parse error: {details['error'][:120]}")
            continue

        # Auto-pad any boundary skills the LLM dropped without replacement.
        # Done before validation so the validator sees the corrected output.
        data, padded = _pad_skills_with_missing_boundary(data, profile)
        if padded:
            report.setdefault("auto_padded_skills", []).extend(padded)

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(data, profile, mode=validation_mode, resume_text=resume_text)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Both passed
        report["status"] = "approved"
        return tailored, report

    if report["validator"] is None and report["parse_failures"]:
        report["validator"] = {
            "passed": False,
            "errors": [
                f"JSON parse failed after {report['attempts']} attempts",
                report["parse_failures"][-1]["error"],
            ],
            "warnings": [],
        }
    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal") -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {
        "approved": 0,
        "approved_with_judge_warning": 0,
        "failed_validation": 0,
        "failed_judge": 0,
        "exhausted_retries": 0,
        "error": 0,
    }

    for job in jobs:
        completed += 1
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            # Save tailored resume text (skip empty outputs)
            txt_path = TAILORED_DIR / f"{prefix}.txt"
            if tailored.strip():
                txt_path.write_text(tailored, encoding="utf-8")
                txt_out_path: str | None = str(txt_path)
            else:
                txt_out_path = None
                if txt_path.exists() and txt_path.stat().st_size == 0:
                    txt_path.unlink(missing_ok=True)

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF for approved resumes (best-effort)
            # "approved_with_judge_warning" is also a success — resume was generated.
            pdf_path = None
            if report["status"] in ("approved", "approved_with_judge_warning") and txt_out_path:
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": txt_out_path,
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(timezone.utc).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    for r in results:
        if r["status"] in _success_statuses and r.get("path"):
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    approved_total = stats.get("approved", 0) + stats.get("approved_with_judge_warning", 0)
    failed_total = (
        stats.get("failed_validation", 0)
        + stats.get("failed_judge", 0)
        + stats.get("exhausted_retries", 0)
    )
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d exhausted, %d errors",
        elapsed,
        approved_total,
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("exhausted_retries", 0),
        stats.get("error", 0),
    )

    return {
        "approved": approved_total,
        "failed": failed_total,
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
