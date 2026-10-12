"""HTML templates and PDF rendering for application documents.

The generated CV follows the architecture of the reference PDF:
header, italic summary, contact line, blue section labels with black rules,
two-column experience/education rows, then compact skills/projects/languages.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from smartapply.company_identity import document_company_name
from smartapply.cv.certificate_selection import selected_certificates
from smartapply.cv.education import education_entries_for_english
from smartapply.cv.links import render_bullet_html
from smartapply.cv.skill_profile import infer_skill_profile_id
from smartapply.llm import AdaptedCV, MotivationLetter
from smartapply.profile import Profile, Project

TEMPLATES_DIR = Path(__file__).with_name("templates")
_PDF_COUNT_RE = re.compile(rb"/Count\s+(\d+)")
_CHROME_RENDER_TIMEOUT_SECONDS = 60
_CHROME_POLL_SECONDS = 0.1
_CHROME_TERMINATE_GRACE_SECONDS = 2


@lru_cache(maxsize=1)
def _weasyprint_html():
    """Return WeasyPrint's HTML renderer when its native libraries load."""
    try:
        from weasyprint import HTML  # type: ignore
    except (ImportError, OSError):
        return None
    return HTML


def _chrome_executable() -> str | None:
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _stop_chrome(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_CHROME_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_CHROME_TERMINATE_GRACE_SECONDS)


def _check_render_cancelled(stop_requested: Callable[[], bool] | None) -> None:
    if stop_requested is not None and stop_requested():
        # Import here because the pipeline imports this CV renderer at startup.
        from smartapply.pipeline.errors import TaskCancelledError

        raise TaskCancelledError()


def html_to_pdf(
    html: str,
    pdf_path: str | Path,
    *,
    base_dir: str | Path | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> Path:
    """Render HTML to PDF with WeasyPrint when installed, otherwise Chrome.

    Chrome is intentionally supported because it is commonly available on the
    user's Mac and avoids forcing heavy system dependencies for a first V1.
    """
    pdf_path = Path(pdf_path).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    _check_render_cancelled(stop_requested)

    weasyprint_html = _weasyprint_html()
    if weasyprint_html is not None:
        try:
            weasyprint_html(
                string=html,
                base_url=str(base_dir or TEMPLATES_DIR),
            ).write_pdf(str(pdf_path))
            return pdf_path
        except OSError:
            pass

    chrome = _chrome_executable()
    if not chrome:
        raise RuntimeError(
            "PDF rendering requires WeasyPrint or Chrome/Chromium. "
            "Install one of them, then rerun the command."
        )

    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".html",
        encoding="utf-8",
        delete=False,
        dir=str(pdf_path.parent),
    ) as tmp:
        tmp.write(html)
        tmp_html = Path(tmp.name)

    fd, temporary_pdf = tempfile.mkstemp(suffix=".pdf", dir=str(pdf_path.parent))
    os.close(fd)
    tmp_pdf = Path(temporary_pdf)
    tmp_pdf.unlink()

    try:
        command = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={tmp_pdf}",
            tmp_html.resolve().as_uri(),
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + _CHROME_RENDER_TIMEOUT_SECONDS
            while True:
                _check_render_cancelled(stop_requested)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Chrome PDF rendering timed out")
                try:
                    returncode = process.wait(timeout=min(_CHROME_POLL_SECONDS, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if returncode:
                raise subprocess.CalledProcessError(returncode, command)
            _check_render_cancelled(stop_requested)
            tmp_pdf.replace(pdf_path)
        finally:
            _stop_chrome(process)
    finally:
        tmp_html.unlink(missing_ok=True)
        tmp_pdf.unlink(missing_ok=True)

    return pdf_path


def pdf_page_count(pdf_path: str | Path) -> int:
    """Best-effort page count without adding a heavy PDF dependency."""
    data = Path(pdf_path).read_bytes()
    counts = [int(match.group(1)) for match in _PDF_COUNT_RE.finditer(data)]
    if counts:
        return max(counts)
    return max(1, len(re.findall(rb"/Type\s*/Page\b", data)))


class HtmlApplicationRenderer:
    """Render CV and motivation letter HTML/PDF documents."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(("html", "xml", "j2")),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    @property
    def pdf_available(self) -> bool:
        return _weasyprint_html() is not None or _chrome_executable() is not None

    def render_cv_html(self, adapted: AdaptedCV, *, force_one_page: bool = False) -> str:
        template = self.env.get_template("cv.html.j2")
        exp_by_id = {exp.id: exp for exp in self.profile.experiences}
        project_by_id = {project.id: project for project in self.profile.projects}

        experiences = []
        for adapted_exp in adapted.selected_experiences:
            source = exp_by_id.get(adapted_exp.source_id)
            if source:
                bullets = adapted_exp.bullets
                if bullets:
                    source_links_by_id = {b.id: b.links for b in source.bullets}
                    bullets_view = [
                        {
                            "text": b.text,
                            "html": render_bullet_html(
                                b.text,
                                source_links_by_id.get(b.source_id, []),
                            ),
                        }
                        for b in bullets
                    ]
                    experiences.append(
                        {
                            "source": source,
                            "bullets": bullets_view,
                        }
                    )

        selected_projects = self._select_projects(
            adapted=adapted,
            project_by_id=project_by_id,
        )
        skill_categories = self._skill_categories_for(adapted)
        layout_class = self._layout_class(
            experiences=experiences,
            projects=selected_projects,
            skill_categories=skill_categories,
            force_one_page=force_one_page,
        )

        return template.render(
            profile=self.profile,
            adapted=adapted,
            experiences=experiences,
            education=education_entries_for_english(self.profile.education),
            skill_categories=skill_categories,
            projects=selected_projects,
            languages=list(self.profile.languages),
            certificates=selected_certificates(self.profile, adapted.selected_certificate_ids),
            contact_line=self._contact_line(),
            contact_items=self._contact_items(),
            layout_class=layout_class,
        )

    def render_letter_html(
        self,
        *,
        letter: MotivationLetter,
        job_title: str,
        job_company: str,
        language: str = "fr",
        letter_headline: str | None = None,
    ) -> str:
        template = self.env.get_template("motivation_letter.html.j2")
        labels = self._letter_labels(language)
        paragraphs = self._letter_paragraphs(letter.body)
        headline = (letter_headline or "").strip() or self.profile.identity.title
        return template.render(
            profile=self.profile,
            subject=letter.subject,
            paragraphs=paragraphs,
            job_title=job_title,
            job_company=document_company_name(job_company),
            letter_headline=headline,
            contact_line=self._contact_line(),
            contact_items=self._contact_items(),
            labels=labels,
        )

    def save_cv_html(self, adapted: AdaptedCV, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_cv_html(adapted), encoding="utf-8")
        return path

    def save_letter_html(
        self,
        *,
        letter: MotivationLetter,
        job_title: str,
        job_company: str,
        path: str | Path,
        language: str = "fr",
        letter_headline: str | None = None,
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        html = self.render_letter_html(
            letter=letter,
            job_title=job_title,
            job_company=job_company,
            language=language,
            letter_headline=letter_headline,
        )
        path.write_text(html, encoding="utf-8")
        return path

    def save_cv_pdf(
        self, adapted: AdaptedCV, path: str | Path, *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> Path:
        render_options = {"stop_requested": stop_requested} if stop_requested else {}
        path = html_to_pdf(self.render_cv_html(adapted), path, base_dir=TEMPLATES_DIR,
                           **render_options)
        if pdf_page_count(path) > 1:
            path = html_to_pdf(
                self.render_cv_html(adapted, force_one_page=True),
                path,
                base_dir=TEMPLATES_DIR,
                **render_options,
            )
        return path

    def save_letter_pdf(
        self,
        *,
        letter: MotivationLetter,
        job_title: str,
        job_company: str,
        path: str | Path,
        language: str = "fr",
        letter_headline: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> Path:
        html = self.render_letter_html(
            letter=letter,
            job_title=job_title,
            job_company=job_company,
            language=language,
            letter_headline=letter_headline,
        )
        render_options = {"stop_requested": stop_requested} if stop_requested else {}
        return html_to_pdf(html, path, base_dir=TEMPLATES_DIR, **render_options)

    def _contact_line(self) -> str:
        return "  |  ".join(item["label"] for item in self._contact_items())

    def _contact_items(self) -> list[dict[str, str | None]]:
        identity = self.profile.identity
        bits = [
            {"label": str(identity.email), "href": f"mailto:{identity.email}"},
            {"label": identity.phone, "href": None} if identity.phone else None,
            {"label": identity.location, "href": None},
            {"label": "GitHub", "href": str(identity.github)} if identity.github else None,
            {"label": "LinkedIn", "href": str(identity.linkedin)} if identity.linkedin else None,
            (
                {"label": self._display_url(str(identity.website)), "href": str(identity.website)}
                if identity.website
                else None
            ),
        ]
        return [bit for bit in bits if bit and bit["label"]]

    def _skill_categories_for(self, adapted: AdaptedCV) -> list[dict[str, list[str] | str]]:
        selected_rows = self._selected_skill_rows(adapted)
        if selected_rows:
            return selected_rows

        profile_id = (
            adapted.skills_profile_id
            if self.profile.skills.profile_by_id(adapted.skills_profile_id) is not None
            else self._infer_skill_profile_id(adapted)
        )

        by_id = {category.id: category for category in self.profile.skills.categories}
        merged = self.profile.skills.effective_category_skills(profile_id)
        rows = []
        for category_id, skills in merged.items():
            category = by_id.get(category_id)
            if category and skills:
                rows.append({"name": category.name, "skills": skills})
        if rows:
            return rows

        ordered_ids = [cid for cid in adapted.skills_order if cid in by_id] + [
            cid for cid in by_id if cid not in adapted.skills_order
        ]
        return [
            {
                "name": by_id[cid].name,
                "skills": self._select_display_skills(by_id[cid], adapted),
            }
            for cid in ordered_ids
        ]

    def _select_projects(
        self,
        *,
        adapted: AdaptedCV,
        project_by_id: dict[str, Project],
    ) -> list[Project]:
        """Resolve the final selection in order without adding profile projects.

        A historical empty selection stays empty: HTML and PDF respect the same
        project set used to write the CV summary and motivation letter.
        """
        selected: list[Project] = []
        seen: set[str] = set()
        for project_id in adapted.selected_project_ids:
            project = project_by_id.get(project_id)
            if project is not None and project_id not in seen:
                selected.append(project)
                seen.add(project_id)

        return selected

    def _infer_skill_profile_id(self, adapted: AdaptedCV) -> str:
        return (
            infer_skill_profile_id(
                self.profile,
                self._adapted_context(adapted),
                default="mixed",
            )
            or "mixed"
        )

    def _select_display_skills(self, category, adapted: AdaptedCV) -> list[str]:
        """Surface CV-relevant skills first without applying an arbitrary cap."""
        context = self._adapted_context(adapted)
        matched = [skill for skill in category.skills if skill.lower() in context]
        rest = [skill for skill in category.skills if skill not in matched]
        return matched + rest

    def _selected_skill_rows(self, adapted: AdaptedCV) -> list[dict[str, list[str] | str]]:
        if not adapted.selected_skills:
            return []
        by_id = {category.id: category for category in self.profile.skills.categories}
        canonical = {skill.lower(): skill for skill in self.profile.skills.allowed_skills}
        ordered_ids = [cid for cid in adapted.skills_order if cid in by_id] + [
            block.category_id
            for block in adapted.selected_skills
            if block.category_id in by_id and block.category_id not in adapted.skills_order
        ]
        selected_by_category = {
            block.category_id: block.skills for block in adapted.selected_skills
        }
        rows: list[dict[str, list[str] | str]] = []
        for cid in ordered_ids:
            category = by_id[cid]
            skills: list[str] = []
            seen: set[str] = set()
            for skill in selected_by_category.get(cid, []):
                canonical_skill = canonical.get(skill.lower())
                if canonical_skill and canonical_skill not in seen:
                    skills.append(canonical_skill)
                    seen.add(canonical_skill)
            if skills:
                rows.append({"name": category.name, "skills": skills})
        return rows

    @staticmethod
    def _adapted_context(adapted: AdaptedCV) -> str:
        context_bits = [
            adapted.cv_title,
            adapted.professional_summary,
            " ".join(adapted.selected_project_ids),
            " ".join(bullet.text for exp in adapted.selected_experiences for bullet in exp.bullets),
        ]
        return " ".join(context_bits).lower()

    @staticmethod
    def _layout_class(
        *,
        experiences: list[dict],
        projects: list,
        skill_categories: list[dict[str, list[str] | str]],
        force_one_page: bool,
    ) -> str:
        if force_one_page:
            return "cv-ultra"
        bullet_count = sum(len(item["bullets"]) for item in experiences)
        skill_count = sum(len(row["skills"]) for row in skill_categories)
        density = (
            len(experiences) * 1.4 + bullet_count * 0.9 + len(projects) * 0.8 + skill_count * 0.12
        )
        # Density bands tune font size, line-height, and vertical rhythm
        # together. Dense modes tighten the layout but never remove content.
        if density <= 8:
            return "cv-very-airy"
        if density <= 14:
            return "cv-airy"
        if density >= 22:
            return "cv-ultra"
        if density >= 18:
            return "cv-compact"
        return "cv-balanced"

    @staticmethod
    def _display_url(url: str) -> str:
        return url.removeprefix("https://").removeprefix("http://").rstrip("/")

    def _letter_paragraphs(self, body: str) -> list[str]:
        """Strip LLM-provided signatures; template appends one canonically."""
        candidate = re.escape(self.profile.identity.full_name.lower())
        signoff_patterns = [
            r"^(cordialement|bien cordialement|sincèrement|salutations|best regards|kind regards|regards|sincerely)[,.;]?\s*$",
            rf"^{candidate}$",
            r"^lachtar nour$",
            r"^nour$",
        ]
        paragraphs: list[str] = []
        for paragraph in body.replace("\r\n", "\n").split("\n\n"):
            cleaned_lines: list[str] = []
            for line in paragraph.splitlines():
                line_clean = line.strip()
                normalized = line_clean.lower()
                if not line_clean:
                    continue
                if any(re.search(pattern, normalized) for pattern in signoff_patterns):
                    continue
                cleaned_lines.append(line_clean)
            cleaned = " ".join(cleaned_lines).strip()
            if cleaned:
                paragraphs.append(cleaned)
        return paragraphs

    @staticmethod
    def _letter_labels(language: str) -> dict[str, str]:
        if language == "en":
            return {
                "document_title": "Motivation Letter",
                "role": "Role",
                "contact": "Contact",
                "subject": "Subject",
                "separator": ":",
                "signoff": "Best regards,",
            }
        return {
            "document_title": "Lettre de motivation",
            "role": "Poste",
            "contact": "Contact",
            "subject": "Objet",
            "separator": " :",
            "signoff": "Cordialement,",
        }
