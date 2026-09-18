"""Write and review generated text against an already finalized CV.

Only the summary and letter can be revised. Skill/project reconstruction and
static experiences are deliberately outside this service.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from smartapply.cv.motivation_validator import (
    MotivationLetterValidator,
    normalize_french_elisions,
)
from smartapply.cv.validator import CvValidator
from smartapply.llm import (
    AdaptedCV,
    DocumentTextReview,
    JobAnalysis,
    LLMError,
    LLMProvider,
    MotivationLetter,
)
from smartapply.llm.prompts import document_review, motivation_letter
from smartapply.llm.prompts.document_context import allowed_source_ids
from smartapply.profile import Profile


@dataclass
class TextReviewResult:
    cv: AdaptedCV
    letter: MotivationLetter
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)


class ApplicationTextWriter:
    def __init__(self, profile: Profile, llm: LLMProvider):
        self.profile = profile
        self.llm = llm
        self.cv_validator = CvValidator(profile)
        self.letter_validator = MotivationLetterValidator(profile)

    def generate_letter(
        self,
        cv: AdaptedCV,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        job_description: str,
        language: str,
        job_id: int | None,
        refresh_cache: bool = False,
    ) -> MotivationLetter:
        prompt = motivation_letter.build_user_prompt(
            profile=self.profile,
            analysis=analysis,
            adapted_cv=cv,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            language=language,
        )
        letter = self.llm.complete_json(
            system=motivation_letter.SYSTEM,
            user=prompt,
            schema=MotivationLetter,
            model=self.llm.smart_model,
            temperature=0.3,
            purpose="motivation_letter",
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        return self._normalize_letter(letter, language)

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
            "professional_summary": cv.professional_summary,
            "motivation_letter_subject": letter.subject,
            "motivation_letter_body": letter.body,
        }

    def _review_contract_errors(
        self, review: DocumentTextReview, cv: AdaptedCV, letter: MotivationLetter
    ) -> list[str]:
        """Check references and edit scope, never infer truth from vocabulary."""
        sources = allowed_source_ids(self.profile, cv)
        fields = self._text_fields(cv, letter)
        errors = []
        issue_fields = {issue.field for issue in review.issues}
        for issue in review.issues:
            if not issue.passage.strip() or issue.passage not in fields[issue.field]:
                errors.append(f"text_review_invalid_passage:{issue.field}")
            if not issue.reason.strip():
                errors.append(f"text_review_missing_reason:{issue.field}")
            if not issue.source_ids or any(ref not in sources for ref in issue.source_ids):
                errors.append(f"text_review_invalid_source:{issue.field}")
        for name in fields:
            replacement = getattr(review, name)
            if replacement is None:
                continue
            if name not in issue_fields:
                errors.append(f"text_review_unjustified_correction:{name}")
            if not replacement.strip():
                errors.append(f"text_review_empty_correction:{name}")
        return errors

    @staticmethod
    def _issue_messages(review: DocumentTextReview) -> list[str]:
        return [
            f"text_review:{issue.field}: {issue.reason} "
            f"[passage={issue.passage!r}; sources={', '.join(issue.source_ids)}]"
            for issue in review.issues
        ]

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
        """One review, at most one proposed revision and one fresh confirmation.

        Unconfirmed revisions never replace the original documents. Failure or
        remaining semantic issues are explicit errors, not silent approvals.
        """
        result = TextReviewResult(cv=cv, letter=letter)
        candidate_cv, candidate_letter = cv, letter
        initial_validation = set(self._validation_issues(cv, letter, analysis))
        first_review: DocumentTextReview | None = None
        for attempt in range(2):
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
                allow_corrections=attempt == 0,
            )
            try:
                review = self.llm.complete_json(
                    system=document_review.SYSTEM,
                    user=prompt,
                    schema=DocumentTextReview,
                    model=self.llm.smart_model,
                    temperature=0.0,
                    purpose="document_review",
                    job_id=job_id,
                    use_cache=False,
                    refresh_cache=refresh_cache,
                )
            except LLMError:
                # Provider errors may contain request contents. Persist only a
                # stable, actionable status, not credentials or raw exceptions.
                result.attempts.append({"attempt": attempt + 1, "status": "unavailable"})
                result.errors.append("text_review_unavailable: vérification non terminée")
                if first_review is not None:
                    result.errors.extend(self._issue_messages(first_review))
                return result

            result.attempts.append(
                {"attempt": attempt + 1, "status": "completed", "review": review.model_dump()}
            )
            contract_errors = self._review_contract_errors(review, candidate_cv, candidate_letter)
            if contract_errors:
                result.errors.extend(contract_errors)
                if first_review is not None:
                    result.errors.extend(self._issue_messages(first_review))
                return result

            changes = {
                name: getattr(review, name)
                for name, value in self._text_fields(candidate_cv, candidate_letter).items()
                if getattr(review, name) is not None and getattr(review, name) != value
            }
            if not review.issues and not changes:
                structural_errors = [
                    *self.cv_validator.validate(candidate_cv).errors,
                    *self.letter_validator.validate(
                        candidate_letter, cv=candidate_cv, analysis=analysis
                    ).errors,
                ]
                if structural_errors:
                    result.errors.extend(
                        f"text_review_structural_error:{error}" for error in structural_errors
                    )
                    return result
                result.cv, result.letter = candidate_cv, candidate_letter
                if attempt:
                    result.warnings.append("text_review_corrected: correction vérifiée")
                return result
            if attempt or not changes:
                if first_review is not None:
                    result.errors.extend(self._issue_messages(first_review))
                result.errors.extend(self._issue_messages(review))
                result.errors.append("text_review_unresolved: relecture nécessaire")
                return result

            first_review = review
            candidate_cv = cv.model_copy(
                update={"professional_summary": changes["professional_summary"]}
                if "professional_summary" in changes
                else {}
            )
            candidate_letter = self._normalize_letter(
                letter.model_copy(
                    update={
                        short: changes[long]
                        for short, long in (
                            ("subject", "motivation_letter_subject"),
                            ("body", "motivation_letter_body"),
                        )
                        if long in changes
                    }
                ),
                language,
            )
            new_validation = set(self._validation_issues(candidate_cv, candidate_letter, analysis))
            if new_validation - initial_validation:
                result.errors.extend(self._issue_messages(review))
                result.errors.extend(
                    f"text_review_invalid_correction:{item}"
                    for item in sorted(new_validation - initial_validation)
                )
                return result
        return result
