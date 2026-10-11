"""Write and review generated text against an already finalized CV.

Only the title, summary and letter can be revised. Skill/project reconstruction and
static experiences are deliberately outside this service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from smartapply.config import get_llm_settings
from smartapply.cv.motivation_validator import (
    MotivationLetterValidator,
    normalize_french_elisions,
)
from smartapply.cv.validator import CvValidator
from smartapply.llm import (
    AdaptedCV,
    ApplicationTexts,
    AssembledCV,
    DocumentReviewIssue,
    DocumentTextReview,
    JobAnalysis,
    LLMError,
    LLMProvider,
    MotivationLetter,
)
from smartapply.llm.prompts import application_texts, document_review
from smartapply.llm.prompts.document_context import allowed_source_ids
from smartapply.llm.routes import provider_for
from smartapply.llm_policy import resolve_route
from smartapply.profile import Profile

# Reuse the validators' findings; never estimate lengths or cut generated text here.
OBJECTIVE_TEXT_WARNING_PREFIXES = (
    "summary_too_long:",
    "summary_too_many_lines:",
    "letter_too_short:",
    "letter_too_long:",
    "letter_not_3_paragraphs:",
)


@dataclass
class TextReviewResult:
    cv: AdaptedCV
    letter: MotivationLetter
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)


class ApplicationTextWriter:
    def __init__(self, profile: Profile, llm: LLMProvider, *, reference_date: date | None = None):
        self.profile = profile
        self.llm = llm
        self.reference_date = reference_date or getattr(llm, "reference_date", None) or date.today()
        self.cv_validator = CvValidator(profile)
        self.letter_validator = MotivationLetterValidator(profile)

    def generate_texts(
        self,
        cv: AssembledCV,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        job_description: str,
        language: str,
        job_id: int | None,
        refresh_cache: bool = False,
    ) -> tuple[AdaptedCV, MotivationLetter]:
        """Write the summary and letter from the validated, frozen CV content."""
        result = self.cv_validator.validate(cv)
        if not result.ok:
            raise LLMError("CV structure could not be finalized before text generation: "
                           + "; ".join(result.errors))
        prompt = application_texts.build_user_prompt(
            profile=self.profile,
            analysis=analysis,
            adapted_cv=cv,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            language=language,
            reference_date=self.reference_date,
        )
        provider = provider_for(self.llm, "application_texts")
        route = resolve_route(
            getattr(self.llm, "_settings", None) or get_llm_settings(), "application_texts"
        )
        texts = provider.complete_json(
            system=application_texts.build_system_prompt(),
            user=prompt,
            schema=ApplicationTexts,
            model=route.model,
            temperature=0.3,
            purpose="application_texts",
            job_id=job_id,
            refresh_cache=refresh_cache,
            effort=route.reasoning,
            max_completion_tokens=route.max_output_tokens,
        )
        # Only the new summary enters the CV. No selection or skills pass runs
        # after this point, and an overlong summary is preserved for the review.
        adapted = AdaptedCV.model_validate(
            {**cv.model_dump(), "professional_summary": texts.professional_summary}
        )
        return adapted, self._normalize_letter(texts.to_motivation_letter(), language)

    @staticmethod
    def _normalize_letter(letter: MotivationLetter, language: str) -> MotivationLetter:
        return letter.model_copy(
            update={
                "subject": normalize_french_elisions(letter.subject, language=language),
                "body": normalize_french_elisions(letter.body, language=language),
            }
        )

    def _validation_issues(
        self, cv: AdaptedCV, letter: MotivationLetter, analysis: JobAnalysis
    ) -> list[str]:
        cv_result = self.cv_validator.validate(cv)
        letter_result = self.letter_validator.validate(letter, cv=cv, analysis=analysis)
        return [
            *cv_result.errors,
            *cv_result.warnings,
            *letter_result.errors,
            *letter_result.warnings,
        ]

    @staticmethod
    def _text_fields(cv: AdaptedCV, letter: MotivationLetter) -> dict[str, str]:
        return {
            "cv_title": cv.cv_title,
            "professional_summary": cv.professional_summary,
            "motivation_letter_subject": letter.subject,
            "motivation_letter_body": letter.body,
        }

    def _review_contract_errors(
        self,
        review: DocumentTextReview,
        cv: AdaptedCV,
        letter: MotivationLetter,
        *,
        allow_corrections: bool,
    ) -> list[str]:
        """Validate exact references and disjoint edits, never infer semantic truth."""
        sources = allowed_source_ids(self.profile, cv)
        fields = self._text_fields(cv, letter)
        errors = []
        spans: dict[str, list[tuple[int, int]]] = {}
        for issue in review.issues:
            if issue.field not in fields:
                errors.append(f"text_review_forbidden_field:{issue.field}")
                continue
            text = fields[issue.field]
            start = text.find(issue.passage)
            if not issue.passage.strip() or start < 0:
                errors.append(f"text_review_invalid_passage:{issue.field}")
            if not issue.reason.strip():
                errors.append(f"text_review_missing_reason:{issue.field}")
            if not issue.source_ids or any(ref not in sources for ref in issue.source_ids):
                errors.append(f"text_review_invalid_source:{issue.field}")
            # Style findings remain advisory even if the model proposes a patch.
            if issue.replacement is None or issue.category == "style":
                continue
            if not allow_corrections:
                errors.append(f"text_review_correction_disabled:{issue.field}")
            if start >= 0 and issue.passage:
                if text.find(issue.passage, start + 1) >= 0:
                    errors.append(f"text_review_ambiguous_passage:{issue.field}")
                spans.setdefault(issue.field, []).append((start, start + len(issue.passage)))
        for name, ranges in spans.items():
            ordered = sorted(ranges)
            if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:], strict=False)):
                errors.append(f"text_review_overlapping_corrections:{name}")
        return errors

    @staticmethod
    def _issue_messages(issues: list[DocumentReviewIssue]) -> list[str]:
        return [
            f"text_review:{issue.category}:{issue.field}: {issue.reason} "
            f"[passage={issue.passage!r}; sources={', '.join(issue.source_ids)}]"
            for issue in issues
        ]

    @staticmethod
    def _blocking_issues(review: DocumentTextReview) -> list[DocumentReviewIssue]:
        return [issue for issue in review.issues if issue.category in {"factual", "language"}]

    def _apply_edits(
        self, review: DocumentTextReview, cv: AdaptedCV, letter: MotivationLetter
    ) -> tuple[AdaptedCV, MotivationLetter]:
        """Apply validated patches against original offsets, preserving all other text."""
        fields = self._text_fields(cv, letter)
        for name, text in fields.items():
            edits = [
                (text.index(issue.passage), issue.passage, issue.replacement)
                for issue in review.issues
                if issue.field == name
                and issue.category != "style"
                and issue.replacement is not None
            ]
            for start, passage, replacement in sorted(edits, reverse=True):
                text = text[:start] + replacement + text[start + len(passage) :]
            fields[name] = text
        return (
            cv.model_copy(
                deep=True,
                update={
                    "cv_title": fields["cv_title"],
                    "professional_summary": fields["professional_summary"],
                }
            ),
            letter.model_copy(
                deep=True,
                update={
                    "subject": fields["motivation_letter_subject"],
                    "body": fields["motivation_letter_body"],
                }
            ),
        )

    def _hard_errors(
        self, cv: AdaptedCV, letter: MotivationLetter, analysis: JobAnalysis
    ) -> list[str]:
        cv_result = self.cv_validator.validate(cv)
        letter_result = self.letter_validator.validate(letter, cv=cv, analysis=analysis)
        return [
            *cv_result.errors,
            *letter_result.errors,
            *[
                issue
                for issue in [*cv_result.warnings, *letter_result.warnings]
                if issue.startswith(OBJECTIVE_TEXT_WARNING_PREFIXES)
            ],
            *[
                f"empty_field:{name}"
                for name, text in self._text_fields(cv, letter).items()
                if not text.strip()
            ],
        ]

    def _validation_warnings(
        self, cv: AdaptedCV, letter: MotivationLetter, analysis: JobAnalysis
    ) -> list[str]:
        return [
            *self.cv_validator.validate(cv).warnings,
            *self.letter_validator.validate(letter, cv=cv, analysis=analysis).warnings,
        ]

    def _correction_errors(
        self,
        cv: AdaptedCV,
        letter: MotivationLetter,
        revised_cv: AdaptedCV,
        revised_letter: MotivationLetter,
        analysis: JobAnalysis,
    ) -> list[str]:
        errors = self._hard_errors(revised_cv, revised_letter, analysis)
        if revised_cv.model_dump(exclude={"cv_title", "professional_summary"}) != cv.model_dump(
            exclude={"cv_title", "professional_summary"}
        ):
            errors.append("text_review_frozen_cv_changed")
        if revised_letter.model_dump(exclude={"subject", "body"}) != letter.model_dump(
            exclude={"subject", "body"}
        ):
            errors.append("text_review_frozen_letter_changed")
        # Preserve the existing canonical project-reference check. Other
        # editorial/lexical hints cannot veto a grounded factual correction.
        old_issues = set(self._validation_issues(cv, letter, analysis))
        new_issues = set(self._validation_issues(revised_cv, revised_letter, analysis))
        errors.extend(
            item
            for item in sorted(new_issues - old_issues)
            if item.startswith("unselected_project_in_letter:")
        )
        return errors

    def review(
        self,
        cv: AdaptedCV,
        letter: MotivationLetter,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        job_description: str,
        language: str,
        job_id: int | None,
        refresh_cache: bool = False,
    ) -> TextReviewResult:
        """One review, at most one separate correction and one fresh confirmation.

        Unconfirmed revisions never replace the original documents. Failure or
        remaining semantic issues are explicit errors, not silent approvals.
        """
        result = TextReviewResult(cv=cv, letter=letter)
        candidate_cv, candidate_letter = cv, letter
        first_review: DocumentTextReview | None = None

        def reject_revision(messages: list[str], *, incomplete: bool = False) -> None:
            initial_blocking = self._blocking_issues(first_review) if first_review else []
            # A failed controller never certifies the original or proves a new
            # candidate defect. Retain findings from earlier *valid* reviews.
            result.errors.extend(self._issue_messages(initial_blocking))
            if first_review is not None:
                result.warnings.extend(
                    self._issue_messages(
                        [issue for issue in first_review.issues if issue not in initial_blocking]
                    )
                )
            result.errors.extend(messages)
            result.errors.extend(self._hard_errors(cv, letter, analysis))
            result.warnings.extend(self._validation_warnings(cv, letter, analysis))
            if incomplete:
                result.errors.append("text_review_incomplete: contrôle non terminé")
                result.attempts[-1].update(status="incomplete", decision="incomplete")

        for attempt in range(2):
            route = resolve_route(
                getattr(self.llm, "_settings", None) or get_llm_settings(), "document_review"
            )
            trace = {
                "schema_version": 2,
                "attempt": len(result.attempts) + 1,
                "model": route.model,
                "purpose": "document_review",
                "reasoning": route.reasoning,
                "reference_date": self.reference_date.isoformat(),
            }
            prompt = document_review.build_user_prompt(
                profile=self.profile,
                analysis=analysis,
                adapted_cv=candidate_cv,
                letter=candidate_letter,
                job_title=job_title,
                job_company=job_company,
                job_description=job_description,
                language=language,
                validation_issues=self._validation_issues(candidate_cv, candidate_letter, analysis),
                allow_corrections=False,
                reference_date=self.reference_date,
            )
            try:
                provider = provider_for(self.llm, "document_review")
                review = provider.complete_json(
                    system=document_review.build_system_prompt(),
                    user=prompt,
                    schema=DocumentTextReview,
                    model=route.model,
                    temperature=0.0,
                    purpose="document_review",
                    job_id=job_id,
                    use_cache=False,
                    refresh_cache=refresh_cache,
                    effort=route.reasoning,
                    max_completion_tokens=route.max_output_tokens,
                )
            except LLMError:
                # Provider errors may contain request contents. Persist only a
                # stable, actionable status, not credentials or raw exceptions.
                result.attempts.append(
                    {**trace, "status": "unavailable", "decision": "unavailable"}
                )
                reject_revision(
                    ["text_review_unavailable: vérification non terminée"], incomplete=True
                )
                return result

            trace.update(status="completed", review=review.model_dump(), decision="rejected")
            result.attempts.append(trace)
            contract_errors = self._review_contract_errors(
                review, candidate_cv, candidate_letter, allow_corrections=False
            )
            if contract_errors:
                # Keep the raw response in the trace, but do not promote its
                # unvalidated accusations to factual errors about the candidate.
                reject_revision(contract_errors, incomplete=True)
                return result

            if attempt == 0 and any(issue.category != "style" for issue in review.issues):
                first_review = review
                trace["decision"] = "correction_requested"
                correction_route = resolve_route(
                    getattr(self.llm, "_settings", None) or get_llm_settings(),
                    "document_correction",
                )
                correction_trace = {
                    "schema_version": 2,
                    "attempt": len(result.attempts) + 1,
                    "model": correction_route.model,
                    "purpose": "document_correction",
                    "reasoning": correction_route.reasoning,
                    "reference_date": self.reference_date.isoformat(),
                }
                correction_prompt = document_review.build_user_prompt(
                    profile=self.profile,
                    analysis=analysis,
                    adapted_cv=cv,
                    letter=letter,
                    job_title=job_title,
                    job_company=job_company,
                    job_description=job_description,
                    language=language,
                    validation_issues=self._validation_issues(cv, letter, analysis),
                    allow_corrections=True,
                    reference_date=self.reference_date,
                    correction_findings=[issue.model_dump() for issue in review.issues],
                )
                try:
                    provider = provider_for(self.llm, "document_correction")
                    review = provider.complete_json(
                        system=document_review.build_system_prompt(),
                        user=correction_prompt,
                        schema=DocumentTextReview,
                        model=correction_route.model,
                        temperature=0.0,
                        purpose="document_correction",
                        job_id=job_id,
                        use_cache=False,
                        refresh_cache=refresh_cache,
                        effort=correction_route.reasoning,
                        max_completion_tokens=correction_route.max_output_tokens,
                    )
                except LLMError:
                    result.attempts.append(
                        {**correction_trace, "status": "unavailable", "decision": "unavailable"}
                    )
                    reject_revision(
                        ["text_review_unavailable: correction non terminée"], incomplete=True
                    )
                    return result
                trace = correction_trace
                trace.update(status="completed", review=review.model_dump(), decision="rejected")
                result.attempts.append(trace)
                contract_errors = self._review_contract_errors(
                    review, cv, letter, allow_corrections=True
                )
                if [issue.model_dump(exclude={"replacement"}) for issue in review.issues] != [
                    issue.model_dump(exclude={"replacement"}) for issue in first_review.issues
                ]:
                    contract_errors.append("text_review_correction_findings_changed")
                if contract_errors:
                    reject_revision(contract_errors, incomplete=True)
                    return result

            blocking = self._blocking_issues(review)
            advisory = [issue for issue in review.issues if issue not in blocking]
            result.warnings.extend(
                self._issue_messages([issue for issue in review.issues if issue.category == "style"])
            )
            changes = any(
                issue.category != "style"
                and issue.replacement is not None
                and issue.replacement != issue.passage
                for issue in review.issues
            )
            if not blocking and not changes:
                structural_errors = self._hard_errors(candidate_cv, candidate_letter, analysis)
                if structural_errors:
                    reject_revision(
                        [f"text_review_structural_error:{error}" for error in structural_errors]
                    )
                    return result
                result.cv, result.letter = candidate_cv, candidate_letter
                result.warnings.extend(self._issue_messages(advisory))
                result.warnings.extend(
                    self._validation_warnings(candidate_cv, candidate_letter, analysis)
                )
                trace["decision"] = "accepted"
                if attempt:
                    result.warnings.append("text_review_corrected: correction vérifiée")
                return result
            uncorrected_blocking = any(
                issue.replacement is None or issue.replacement == issue.passage
                for issue in blocking
            )
            if attempt or not changes or uncorrected_blocking:
                result.warnings.extend(self._issue_messages(advisory))
                reject_revision(
                    [
                        *self._issue_messages(blocking),
                        "text_review_unresolved: relecture nécessaire",
                    ],
                )
                return result

            first_review = review
            candidate_cv, candidate_letter = self._apply_edits(review, cv, letter)
            correction_errors = self._correction_errors(
                cv, letter, candidate_cv, candidate_letter, analysis
            )
            if correction_errors:
                reject_revision(
                    [f"text_review_invalid_correction:{item}" for item in correction_errors],
                    incomplete=True,
                )
                return result
            trace["decision"] = "revision_proposed"
        return result
