"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
import re
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)

_DATE_PATTERN = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
    r"\s+\d{4}\s*[-–—]\s*(?:Present|\w+\s+\d{4})"
    r"|N/A)",
)

# Bullet glyphs we accept as list-item markers in the input text.
# pdfplumber/pypdf usually emit ● (U+25CF) for filled-disc bullets;
# tailored output may use • (U+2022); manual edits often use "- ".
_BULLET_PREFIXES = ("- ", "\u2022 ", "\u25cf ")
# Aliases so we can render a section regardless of which header label
# the source resume uses (e.g. "EXPERIENCE" vs "WORK EXPERIENCE").
_SECTION_ALIASES = {
    "experience": ("EXPERIENCE", "WORK EXPERIENCE", "PROFESSIONAL EXPERIENCE"),
    "projects": ("PROJECTS",),
    "skills": ("TECHNICAL SKILLS", "SKILLS"),
    "education": ("EDUCATION",),
}


def _section(sections: dict[str, str], group: str) -> str | None:
    """Return the first matching section text for an alias group."""
    for key in _SECTION_ALIASES[group]:
        if key in sections:
            return sections[key]
    return None


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (TECHNICAL SKILLS, EXPERIENCE, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before the first ALL-CAPS section header
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped
            and stripped == stripped.upper()
            and len(stripped) > 3
            and not any(stripped.startswith(p[0]) for p in _BULLET_PREFIXES)
        ):
            body_start = i
            break
        if stripped:
            header_lines.append(stripped)

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = ""
    location = ""
    contact = ""

    if len(header_lines) == 2:
        # 2-line header: name + contact/title
        if "@" in header_lines[1] or "|" in header_lines[1]:
            contact = header_lines[1]
        else:
            title = header_lines[1]
    elif len(header_lines) == 3:
        title = header_lines[1]
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]
    elif len(header_lines) >= 4:
        title = header_lines[1]
        location = header_lines[2]
        contact = header_lines[3]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and len(stripped) > 3
            and not any(stripped.startswith(p[0]) for p in _BULLET_PREFIXES)
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        bullet_prefix = next(
            (p for p in _BULLET_PREFIXES if stripped.startswith(p)), None
        )
        if bullet_prefix:
            if current:
                current["bullets"].append(stripped[len(bullet_prefix):].strip())
        elif current is None or (
            bullet_prefix is None
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


def _extract_date(text: str) -> tuple[str, str]:
    """Extract a date range from a line, splitting on tab or trailing date pattern.

    Handles formats:
      "Stackline | Seattle, WA\\tJul 2024 - Present"  → ("Stackline | Seattle, WA", "Jul 2024 - Present")
      "Stackline\\tJul 2024 - Present"                → ("Stackline", "Jul 2024 - Present")
      "Project Name | Python, Rust"                   → ("Project Name | Python, Rust", "")

    Returns:
        (content, date) tuple.
    """
    if "\t" in text:
        parts = text.split("\t", 1)
        if _DATE_PATTERN.search(parts[1]):
            return parts[0].strip(), parts[1].strip()

    m = _DATE_PATTERN.search(text)
    if m:
        before = text[:m.start()].rstrip(" |–—-\t")
        return before.strip(), m.group(0).strip()

    return text, ""


def _split_role_tech(subtitle: str) -> tuple[str, str]:
    """Split 'Role | Tech1, Tech2, ...' into (role, tech_string)."""
    if "|" in subtitle:
        role, tech = subtitle.split("|", 1)
        return role.strip(), tech.strip()
    return subtitle.strip(), ""




# ── HTML Template ────────────────────────────────────────────────────────

def _build_entry_html(entries: list[dict]) -> str:
    """Build HTML for a list of experience/project entries."""
    items = ""
    for e in entries:
        bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])



        title = e["title"]
        subtitle = e["subtitle"]

        if " at " in title:
            # Tailored format: title = "Role at Company", subtitle = "Tech | Date"
            role, company = title.rsplit(" at ", 1)
            tech, date = "", ""
            if subtitle:
                # Subtitle is "Tech1, Tech2, ... | Date" — date is after last pipe
                sub_parts = subtitle.rsplit(" | ", 1)
                if len(sub_parts) == 2 and _DATE_PATTERN.search(sub_parts[1]):
                    tech, date = sub_parts[0].strip(), sub_parts[1].strip()
                else:
                    tech = subtitle
            date_span = f'<span class="date">{date}</span>' if date and date != "N/A" else ""
            tech_part = f'<span class="entry-tech"> | {tech}</span>' if tech else ""
            title_html = (
                f'<div class="entry-title">'
                f'<span><b>{company}</b></span>'
                f'{date_span}'
                f'</div>'
            )
            subtitle_html = f'<div class="entry-sub"><b>{role}</b>{tech_part}</div>'

        elif subtitle and ("\t" in title or _DATE_PATTERN.search(title)):
            # Reference format: title = "Company\tDate", subtitle = "Role | Tech"
            company_full, date = _extract_date(title)
            # Split company/location: "Company | Location" → bold company, normal location
            if " | " in company_full:
                comp_name, comp_loc = company_full.split(" | ", 1)
                company_html = f'<b>{comp_name}</b><span class="entry-loc"> | {comp_loc}</span>'
            else:
                company_html = f'<b>{company_full}</b>'
            role, tech = _split_role_tech(subtitle)
            date_span = f'<span class="date">{date}</span>' if date else ""
            title_html = (
                f'<div class="entry-title">'
                f'<span>{company_html}</span>'
                f'{date_span}'
                f'</div>'
            )
            tech_part = f'<span class="entry-tech"> | {tech}</span>' if tech else ""
            subtitle_html = f'<div class="entry-sub"><b>{role}</b>{tech_part}</div>'

        else:
            # Project entry: title may have "Name | Tech" or "Name - Descriptor"
            name, date = _extract_date(title)
            proj_name = name.split(" - ", 1)[0] if " - " in name else name
            tech = ""
            if " | " in proj_name:
                proj_name, tech = proj_name.split(" | ", 1)
            elif subtitle:
                # Tailored project: subtitle = "Tech" or "Tech | Date" (LLM may also
                # emit placeholders like "| N/A" or "| Project" — strip those silently).
                sub_parts = subtitle.rsplit(" | ", 1)
                if len(sub_parts) == 2 and _DATE_PATTERN.search(sub_parts[1]):
                    tech, date = sub_parts[0].strip(), sub_parts[1].strip()
                elif len(sub_parts) == 2:
                    # Trailing chunk is a placeholder, not a real date — drop it
                    tech = sub_parts[0].strip()
                else:
                    tech = subtitle.strip()
            date_span = f'<span class="date">{date}</span>' if date and date != "N/A" else ""
            tech_part = f'<span class="entry-tech"> | {tech}</span>' if tech else ""
            title_html = (
                f'<div class="entry-title">'
                f'<span><b>{proj_name}</b>{tech_part}</span>'
                f'{date_span}'
                f'</div>'
            )
            subtitle_html = ""

        items += (
            f'<div class="entry">'
            f'{title_html}'
            f'{subtitle_html}'
            f'<ul>{bullets}</ul>'
            f'</div>'
        )
    return items


def build_html(resume: dict, profile: dict | None = None) -> str:
    """Build professional resume HTML from parsed data.

    Styled to match the reference .docx resume: Calibri font, black/gray
    color scheme, thin black section borders, right-aligned dates.

    Args:
        resume: Parsed resume dict from parse_resume().
        profile: Optional user profile dict (from load_profile()) for
            dynamic education fields (gpa, start_date, end_date).

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills — consolidate into Languages, Frameworks, Technologies
    skills_html = ""
    skills_text = _section(sections, "skills")
    if skills_text:
        skills = parse_skills(skills_text)
        keep = {"Languages", "Frameworks"}
        tech_values: list[str] = []
        rows = ""
        for cat, val in skills:
            if cat in keep:
                rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
            else:
                tech_values.append(val)
        if tech_values:
            merged = ", ".join(tech_values)
            rows += f'<div class="skill-row"><span class="skill-cat">Technologies:</span> {merged}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    exp_text = _section(sections, "experience")
    if exp_text:
        entries = parse_entries(exp_text)
        items = _build_entry_html(entries)
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    proj_text = _section(sections, "projects")
    if proj_text:
        entries = parse_entries(proj_text)
        items = _build_entry_html(entries)
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education — pull GPA and dates from profile if available
    edu_cfg = (profile or {}).get("education", {})
    gpa = edu_cfg.get("gpa", "")
    edu_start = edu_cfg.get("start_date", "")
    edu_end = edu_cfg.get("end_date", "")

    edu_html = ""
    edu_section = _section(sections, "education")
    if edu_section:
        edu_text = edu_section.strip()
        edu_text = re.sub(r"\s*\|\s*Bachelor'?s.*", "", edu_text)
        # Strip any inline date range so it doesn't compete with the right-aligned span
        date_match = _DATE_PATTERN.search(edu_text)
        if date_match:
            edu_text = (
                edu_text[: date_match.start()].rstrip(" |\t") + edu_text[date_match.end():].lstrip()
            ).rstrip(" |\t")
            if not (edu_start or edu_end):
                edu_start = edu_end = date_match.group(0)
        # Only inject GPA from profile if the education line doesn't already carry one
        if gpa and "GPA" not in edu_text:
            edu_text = f"{edu_text} | GPA: {gpa}"
        date_span = ""
        if edu_start and edu_end and edu_start != edu_end:
            date_span = f'<span class="date">{edu_start} – {edu_end}</span>'
        elif edu_start:
            date_span = f'<span class="date">{edu_start}</span>'
        edu_html = (
            f'<div class="section"><div class="section-title">Education</div>'
            f'<div class="edu">{edu_text}{date_span}</div></div>'
        )

    # Contact line parsing — convert raw URLs to hyperlinks, format phone
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    if resume["location"]:
        contact_parts.append(resume["location"])

    rendered_parts: list[str] = []
    for part in contact_parts:
        if "github.com" in part:
            url = part if part.startswith("http") else f"https://{part}"
            rendered_parts.append(f'<a href="{url}">GitHub</a>')
        elif "linkedin.com" in part:
            url = part if part.startswith("http") else f"https://{part}"
            rendered_parts.append(f'<a href="{url}">LinkedIn</a>')
        elif part.startswith("http://") or part.startswith("https://"):
            rendered_parts.append(f'<a href="{part}">Website</a>')
        elif re.fullmatch(r"\d{10}", part):
            rendered_parts.append(f"({part[:3]}) {part[3:6]}-{part[6:]}")
        else:
            rendered_parts.append(part)
    contact_html = " | ".join(rendered_parts)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.2;
    color: #000;
    max-width: 8.5in;
    margin: 0 auto;
    padding: 0.22in 0.6in;
}}
.header {{
    text-align: center;
    margin-bottom: 0;
}}
.name {{
    font-size: 22pt;
    font-weight: 700;
    color: #000;
    line-height: 1.1;
}}
.title {{
    font-size: 11pt;
    color: #595959;
    font-weight: 700;
}}
.location {{
    font-size: 11pt;
    color: #595959;
    font-weight: 700;
}}
.contact {{
    font-size: 10.5pt;
    color: #595959;
    font-weight: 400;
    margin-top: 2pt;
}}
.contact a {{
    color: #0563C1;
    text-decoration: underline;
}}
.section {{
    margin-top: 5pt;
}}
.section-title {{
    font-size: 12pt;
    font-weight: 700;
    color: #0D0D0D;
    text-transform: uppercase;
    border-bottom: 0.5pt solid #000;
    padding-bottom: 2pt;
    margin-bottom: 4pt;
    letter-spacing: 0.5pt;
}}
.skill-row {{
    font-size: 11pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-row + .skill-row {{
    margin-top: 2pt;
}}
.skill-cat {{
    font-weight: 700;
}}
.entry {{
    margin-top: 5pt;
    break-inside: avoid;
}}
.entry-title {{
    font-size: 12pt;
    color: #0D0D0D;
    display: flex;
    justify-content: space-between;
    line-height: 1.3;
}}
.entry-title b {{
    font-weight: 700;
}}
.entry-loc {{
    font-weight: 400;
}}
.entry-tech {{
    font-weight: 400;
    font-size: 11pt;
}}
.entry-title .date {{
    font-weight: 700;
    font-size: 10.5pt;
    white-space: nowrap;
    margin-left: 12pt;
}}
.entry-sub {{
    font-size: 11pt;
    color: #000;
    margin-top: 2pt;
    line-height: 1.3;
}}
.entry-sub b {{
    font-weight: 700;
}}
ul {{
    margin-left: 0;
    padding-left: 0.22in;
    margin-top: 2pt;
    list-style-type: disc;
}}
li {{
    font-size: 11pt;
    margin-top: 2pt;
    padding-left: 2pt;
    line-height: 1.3;
}}
.edu {{
    font-size: 11pt;
    font-weight: 700;
    color: #0D0D0D;
    margin-top: 3pt;
    display: flex;
    justify-content: space-between;
    line-height: 1.3;
}}
.edu .date {{
    font-weight: 700;
    font-size: 10.5pt;
    white-space: nowrap;
    margin-left: 12pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="contact">{contact_html}</div>
</div>
{exp_html}
{proj_html}
{skills_html}
{edu_html}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
            prefer_css_page_size=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    from applypilot.config import load_profile

    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = None
    html = build_html(resume, profile=profile)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
