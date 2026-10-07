"""Clean raw job descriptions to keep only useful, matchable content.

Removes boilerplate that dilutes LLM context: legal disclaimers, equal-opp
statements, generic company values, redundant benefits, broken HTML
fragments. Targets French + English vocabulary.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape

from bs4 import BeautifulSoup

# Lines that match any of these patterns get dropped entirely.
BOILERPLATE_LINE_PATTERNS = [
    r"equal[- ]opportunity employer",
    r"reasonable accommodation",
    r"diversity, equity (and|&) inclusion",
    r"hiring without regard to race",
    r"qualified applicants with criminal histories",
    r"americans with disabilities act",
    r"ADA accommodation",
    r"e[- ]verify",
    r"\b(applicantaccommodation|hrhelp)@",
    r"^\s*compensation:?\s",
    r"^\s*benefits eligibility",
    r"^\s*the (hourly|annual) rate",
    r"^\s*starting (hourly|annual) (rate|salary)",
    r"^\s*starbucks coffee company",
    r"^\s*r[ée]gime des donn[ée]es personnelles",
    r"^\s*\bRGPD\b",
    r"^\s*Toutes nos offres",
    r"^\s*Conform[ée]ment.*RGPD",
    r"#fpstate=tldetail",
    r"^\s*utm_(source|campaign|medium)=",
    r"^\s*All other duties",
]

# Section headings that signal noise to drop (the section *after* the heading
# is dropped until the next heading or paragraph break).
NOISE_SECTION_HEADINGS = [
    "benefits",
    "avantages",
    "compensation data",
    "equal opportunity",
    "about us",
    "about the company",
    "à propos de nous",
    "about target",
    "our mission",
    "our values",
    "qui sommes nous",
    "qui sommes-nous",
    "americans with disabilities act",
]

# A heading must start with a letter (uppercase preferred). Lines starting
# with bullets, dashes or list markers are NOT headings.
_HEADING_RE = re.compile(
    r"^(?:>+\s*)?(?P<title>[A-ZÉÈÊÀÂÔÎÛÇ][\w\s&/'-]{1,60})\s*:?\s*$",
    re.UNICODE,
)

# Only structural HTML elements create a line break. A separator between every
# text node would split sentences around inline formatting such as <strong>.
_HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "caption", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table",
    "td", "th", "tr", "ul",
}


def strip_html(text: str) -> str:
    """Remove HTML while keeping inline text joined and block boundaries distinct."""
    if not text:
        return ""
    if "<" not in text:
        return unescape(text)
    soup = BeautifulSoup(text, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all(_HTML_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    # Adjacent/nested blocks share one boundary; inline nodes retain their actual
    # spaces and punctuation instead of gaining a newline (or an extra space).
    cleaned = soup.get_text()
    cleaned = re.sub(r"\n[ \t\r\n]*\n", "\n", cleaned)
    return normalize_whitespace(cleaned)


def normalize_whitespace(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[   ]", " ", text)  # non-breaking spaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _line_is_boilerplate(line: str) -> bool:
    for pattern in BOILERPLATE_LINE_PATTERNS:
        if re.search(pattern, line, flags=re.IGNORECASE):
            return True
    return False


def _heading_is_noise(line: str) -> bool:
    m = _HEADING_RE.match(line.strip())
    if not m:
        return False
    title = m.group("title").strip().lower()
    return title in NOISE_SECTION_HEADINGS


def drop_boilerplate(text: str) -> str:
    """Drop boilerplate lines and noisy sections."""
    if not text:
        return ""

    lines = text.splitlines()
    output: list[str] = []
    skip_section = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            output.append("")
            skip_section = False
            continue

        if skip_section:
            if _HEADING_RE.match(stripped) and not _heading_is_noise(stripped):
                skip_section = False
            else:
                continue

        if _heading_is_noise(stripped):
            skip_section = True
            continue

        if _line_is_boilerplate(stripped):
            continue

        output.append(line)

    return "\n".join(output)


def clean_description(text: str) -> str:
    """Full cleaning pipeline: html → whitespace → boilerplate → whitespace."""
    text = strip_html(text)
    text = normalize_whitespace(text)
    text = drop_boilerplate(text)
    return normalize_whitespace(text)
