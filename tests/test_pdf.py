"""Tests for applypilot.scoring.pdf — parsing, date splitting, and HTML output.

Uses real sample resumes from ~/.applypilot/tailored_resumes/ plus a minimal
hand-crafted fixture for controlled assertions.
"""

import re

import pytest

from applypilot.scoring.pdf import (
    _build_entry_html,
    _split_date,
    build_html,
    parse_entries,
    parse_resume,
    parse_skills,
)


# ── parse_resume ─────────────────────────────────────────────────────────


class TestParseResume:
    def test_header_fields(self, minimal_resume_text: str):
        r = parse_resume(minimal_resume_text)
        assert r["name"] == "Jane Doe"
        assert r["title"] == "Backend Engineer"
        assert r["contact"] == "jane@example.com | 5551234567 | https://github.com/janedoe"

    def test_all_sections_found(self, minimal_resume_text: str):
        r = parse_resume(minimal_resume_text)
        expected = {"TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}
        assert set(r["sections"].keys()) == expected

    def test_education_content(self, minimal_resume_text: str):
        r = parse_resume(minimal_resume_text)
        assert "MIT" in r["sections"]["EDUCATION"]

    def test_location_detection(self):
        text = (
            "Alice\nEngineer\nSan Francisco, CA\n"
            "a@b.com | 555\n\nTECHNICAL SKILLS\nLanguages: Python"
        )
        r = parse_resume(text)
        assert r["location"] == "San Francisco, CA"
        assert r["contact"] == "a@b.com | 555"

    def test_contact_without_location(self):
        text = "Bob\nDev\nb@c.com | 555\n\nTECHNICAL SKILLS\nLanguages: Go"
        r = parse_resume(text)
        assert r["contact"] == "b@c.com | 555"
        assert r["location"] == ""

    def test_real_sample_sections(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        assert r["name"] == "Erik Chan"
        assert "EXPERIENCE" in r["sections"]
        assert "TECHNICAL SKILLS" in r["sections"]
        assert "PROJECTS" in r["sections"]
        assert "EDUCATION" in r["sections"]

    def test_real_alt_sample(self, alt_resume_text: str):
        r = parse_resume(alt_resume_text)
        assert r["name"] == "Erik Chan"
        assert len(r["sections"]) >= 4


# ── parse_skills ─────────────────────────────────────────────────────────


class TestParseSkills:
    def test_basic_parsing(self):
        text = "Languages: Python, Go\nFrameworks: Flask, FastAPI"
        skills = parse_skills(text)
        assert skills == [("Languages", "Python, Go"), ("Frameworks", "Flask, FastAPI")]

    def test_skips_non_colon_lines(self):
        text = "Some random line\nLanguages: Python"
        skills = parse_skills(text)
        assert len(skills) == 1
        assert skills[0][0] == "Languages"

    def test_real_sample(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        skills = parse_skills(r["sections"]["TECHNICAL SKILLS"])
        categories = [cat for cat, _ in skills]
        assert "Languages" in categories
        assert "Frameworks" in categories
        assert len(skills) >= 3


# ── parse_entries ────────────────────────────────────────────────────────


class TestParseEntries:
    def test_basic_structure(self, minimal_resume_text: str):
        r = parse_resume(minimal_resume_text)
        entries = parse_entries(r["sections"]["EXPERIENCE"])
        assert len(entries) == 2

        first = entries[0]
        assert "Acme Corp" in first["title"]
        assert "Python" in first["subtitle"]
        assert len(first["bullets"]) == 2

    def test_bullet_text_clean(self, minimal_resume_text: str):
        r = parse_resume(minimal_resume_text)
        entries = parse_entries(r["sections"]["EXPERIENCE"])
        for e in entries:
            for b in e["bullets"]:
                assert not b.startswith("- ")
                assert not b.startswith("• ")

    def test_projects_entries(self, minimal_resume_text: str):
        r = parse_resume(minimal_resume_text)
        entries = parse_entries(r["sections"]["PROJECTS"])
        assert len(entries) == 1
        assert "CLI" in entries[0]["title"]
        assert "Rust" in entries[0]["subtitle"]

    def test_real_sample_experience(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        entries = parse_entries(r["sections"]["EXPERIENCE"])
        assert len(entries) == 3
        assert "Stackline" in entries[0]["title"]
        assert "Breaking Hits" in entries[1]["title"]
        assert "Edenspiekermann" in entries[2]["title"]

    def test_real_sample_projects(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        entries = parse_entries(r["sections"]["PROJECTS"])
        assert len(entries) == 2

    def test_all_entries_have_bullets(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        for section in ("EXPERIENCE", "PROJECTS"):
            for e in parse_entries(r["sections"][section]):
                assert len(e["bullets"]) >= 1, f"No bullets in entry: {e['title']}"


# ── _split_date ──────────────────────────────────────────────────────────


class TestSplitDate:
    def test_standard_date_range(self):
        content, date = _split_date("Python, Flask | Jul 2024 - Present")
        assert content == "Python, Flask"
        assert date == "Jul 2024 - Present"

    def test_date_with_end_month(self):
        content, date = _split_date("TypeScript, React | Mar 2023 - Sep 2023")
        assert content == "TypeScript, React"
        assert date == "Mar 2023 - Sep 2023"

    def test_en_dash(self):
        content, date = _split_date("Python | Oct 2023 – Jul 2024")
        assert content == "Python"
        assert date == "Oct 2023 – Jul 2024"

    def test_na_date(self):
        content, date = _split_date("Flask, Next.js | N/A")
        assert content == "Flask, Next.js"
        assert date == "N/A"

    def test_no_pipe(self):
        content, date = _split_date("Just some text without pipe")
        assert content == "Just some text without pipe"
        assert date == ""

    def test_pipe_without_date(self):
        content, date = _split_date("React | Next.js | Flask")
        assert content == "React | Next.js | Flask"
        assert date == ""

    def test_empty_string(self):
        content, date = _split_date("")
        assert content == ""
        assert date == ""


# ── build_html (CSS / styling) ───────────────────────────────────────────


class TestBuildHtmlStyling:
    """Verify the generated HTML matches the .docx reference styling."""

    @pytest.fixture
    def html(self, minimal_resume_text: str) -> str:
        return build_html(parse_resume(minimal_resume_text))

    def test_font_family_calibri(self, html: str):
        assert "'Calibri'" in html

    def test_name_30pt(self, html: str):
        assert "font-size: 30pt" in html

    def test_name_black(self, html: str):
        name_css = re.search(r"\.name\s*\{[^}]+\}", html)
        assert name_css, ".name CSS rule not found"
        assert "color: #000" in name_css.group()

    def test_no_blue_colors(self, html: str):
        old_blue_colors = ["#2a7ab5", "#1a3a5c", "#3a6b8c", "#4a7a9b", "#2c3e50"]
        for color in old_blue_colors:
            assert color not in html, f"Old blue color {color} still present in HTML"

    def test_section_title_14pt(self, html: str):
        section_css = re.search(r"\.section-title\s*\{[^}]+\}", html)
        assert section_css, ".section-title CSS rule not found"
        assert "font-size: 14pt" in section_css.group()

    def test_section_border_black(self, html: str):
        section_css = re.search(r"\.section-title\s*\{[^}]+\}", html)
        assert section_css
        rule = section_css.group()
        assert "solid #000" in rule
        assert "#2a7ab5" not in rule

    def test_no_header_border(self, html: str):
        header_css = re.search(r"\.header\s*\{[^}]+\}", html)
        assert header_css
        assert "border" not in header_css.group()

    def test_contact_12pt_bold(self, html: str):
        contact_css = re.search(r"\.contact\s*\{[^}]+\}", html)
        assert contact_css
        rule = contact_css.group()
        assert "font-size: 12pt" in rule
        assert "font-weight: 700" in rule

    def test_contact_gray(self, html: str):
        contact_css = re.search(r"\.contact\s*\{[^}]+\}", html)
        assert contact_css
        assert "#595959" in contact_css.group()

    def test_entry_title_12pt(self, html: str):
        css = re.search(r"\.entry-title\s*\{[^}]+\}", html)
        assert css
        assert "font-size: 12pt" in css.group()

    def test_entry_subtitle_not_italic(self, html: str):
        css = re.search(r"\.entry-sub\s*\{[^}]+\}", html)
        assert css
        assert "italic" not in css.group()

    def test_body_font_11pt(self, html: str):
        body_css = re.search(r"body\s*\{[^}]+\}", html)
        assert body_css
        assert "font-size: 11pt" in body_css.group()

    def test_page_margins(self, html: str):
        assert "margin: 0.2in 0.6in" in html

    def test_bullet_list_style_disc(self, html: str):
        ul_css = re.search(r"\bul\s*\{[^}]+\}", html)
        assert ul_css
        assert "disc" in ul_css.group()

    def test_edu_12pt_bold(self, html: str):
        css = re.search(r"\.edu\s*\{[^}]+\}", html)
        assert css
        rule = css.group()
        assert "font-size: 12pt" in rule
        assert "font-weight: 700" in rule


# ── build_html (content / structure) ─────────────────────────────────────


class TestBuildHtmlContent:
    @pytest.fixture
    def html(self, minimal_resume_text: str) -> str:
        return build_html(parse_resume(minimal_resume_text))

    def test_name_in_output(self, html: str):
        assert "Jane Doe" in html

    def test_contact_in_output(self, html: str):
        assert "jane@example.com" in html

    def test_section_titles_present(self, html: str):
        assert "Technical Skills" in html
        assert "Experience" in html
        assert "Projects" in html
        assert "Education" in html

    def test_summary_excluded(self, html: str):
        assert "Summary" not in html
        assert "Experienced backend engineer." not in html

    def test_section_order(self, html: str):
        exp_pos = html.index("Experience")
        proj_pos = html.index("Projects")
        skills_pos = html.index("Technical Skills")
        edu_pos = html.index("Education")
        assert exp_pos < proj_pos < skills_pos < edu_pos

    def test_skills_rendered(self, html: str):
        assert "Python, Go, SQL" in html
        assert "Flask, FastAPI" in html

    def test_bullets_rendered(self, html: str):
        assert "Built REST APIs" in html
        assert "Migrated legacy monolith" in html

    def test_date_right_aligned(self, html: str):
        assert 'class="date"' in html
        assert "Jan 2023 - Present" in html
        assert "Jun 2021 - Dec 2022" in html

    def test_na_date_hidden(self, html: str):
        assert ">N/A<" not in html

    def test_project_tech_shown_without_na(self, html: str):
        assert "Rust" in html

    def test_education_content(self, html: str):
        assert "MIT" in html

    def test_valid_html_structure(self, html: str):
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "<body>" in html
        assert "</body>" in html


# ── build_html with real samples ─────────────────────────────────────────


class TestBuildHtmlRealSamples:
    def test_real_sample_renders(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        html = build_html(r)
        assert "Erik Chan" in html
        assert "Stackline" in html
        assert "Jul 2024 - Present" in html
        assert 'class="date"' in html

    def test_alt_sample_renders(self, alt_resume_text: str):
        r = parse_resume(alt_resume_text)
        html = build_html(r)
        assert "Erik Chan" in html
        assert "Experience" in html

    def test_real_sample_no_old_styling(self, sample_resume_text: str):
        html = build_html(parse_resume(sample_resume_text))
        assert "#1a3a5c" not in html
        assert "#2a7ab5" not in html
        assert "font-style: italic" not in html

    def test_real_sample_all_entries_have_bullets(self, sample_resume_text: str):
        html = build_html(parse_resume(sample_resume_text))
        assert html.count("<li>") >= 8

    def test_real_sample_dates_extracted(self, sample_resume_text: str):
        r = parse_resume(sample_resume_text)
        entries = parse_entries(r["sections"]["EXPERIENCE"])
        for e in entries:
            _, date = _split_date(e["subtitle"])
            assert date, f"No date found in subtitle: {e['subtitle']}"


# ── _build_entry_html ────────────────────────────────────────────────────


class TestBuildEntryHtml:
    def test_entry_with_date(self):
        entries = [{"title": "Dev at FooCorp", "subtitle": "Python | Jan 2024 - Present", "bullets": ["Did stuff."]}]
        html = _build_entry_html(entries)
        assert 'class="entry-title"' in html
        assert "FooCorp" in html
        assert "Dev" in html
        assert 'class="date"' in html
        assert "Jan 2024 - Present" in html
        assert "<li>Did stuff.</li>" in html

    def test_entry_na_date_hidden(self):
        entries = [{"title": "My Project", "subtitle": "Rust | N/A", "bullets": ["Built it."]}]
        html = _build_entry_html(entries)
        assert "N/A" not in html
        assert "Rust" in html

    def test_entry_no_subtitle(self):
        entries = [{"title": "Solo Project", "subtitle": "", "bullets": ["Did it."]}]
        html = _build_entry_html(entries)
        assert "entry-sub" not in html
        assert "Solo Project" in html

    def test_multiple_entries(self):
        entries = [
            {"title": "A", "subtitle": "X | Jan 2024 - Present", "bullets": ["b1"]},
            {"title": "B", "subtitle": "Y | Mar 2023 - Dec 2023", "bullets": ["b2"]},
        ]
        html = _build_entry_html(entries)
        assert html.count('class="entry"') == 2
        assert html.count('class="date"') == 2
