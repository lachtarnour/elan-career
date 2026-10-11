"""LLM provider abstraction + schemas + prompts."""

from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.cache import make_cache_key
from smartapply.llm.factory import get_llm_provider
from smartapply.llm.mock_provider import MockLLMProvider
from smartapply.llm.openai_provider import OpenAIProvider
from smartapply.llm.provider import (
    LLMError,
    LLMProvider,
    LLMValidationError,
)
from smartapply.llm.schemas import (
    AdaptedBullet,
    AdaptedCV,
    AdaptedExperience,
    ApplicationTexts,
    AssembledCV,
    CVPersonalization,
    CVSelection,
    DocumentReviewIssue,
    DocumentTextReview,
    JobAnalysis,
    JobFitAssessment,
    JobFitDecisionEvaluation,
    MotivationLetter,
    MotivationLetterRepair,
    OfferAnalysis,
    OfferExtractionOutput,
    ProjectSelectionReason,
    SkillSelectionBlock,
)
from smartapply.llm.usage import OPENAI_PRICING, estimate_cost_usd

__all__ = [
    "AnthropicProvider",
    "AdaptedBullet",
    "AdaptedCV",
    "AdaptedExperience",
    "ApplicationTexts",
    "AssembledCV",
    "CVPersonalization",
    "CVSelection",
    "DocumentReviewIssue",
    "DocumentTextReview",
    "JobAnalysis",
    "JobFitAssessment",
    "JobFitDecisionEvaluation",
    "MotivationLetter",
    "MotivationLetterRepair",
    "OfferAnalysis",
    "OfferExtractionOutput",
    "ProjectSelectionReason",
    "SkillSelectionBlock",
    "LLMError",
    "LLMProvider",
    "LLMValidationError",
    "MockLLMProvider",
    "OPENAI_PRICING",
    "OpenAIProvider",
    "estimate_cost_usd",
    "get_llm_provider",
    "make_cache_key",
]
