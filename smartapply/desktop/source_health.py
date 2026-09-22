"""Lightweight, non-billable health checks for configured job sources."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from math import isfinite

import requests

from smartapply.config import Settings
from smartapply.scrapers.francetravail import TOKEN_URL
from smartapply.scrapers.wttj.contracts import WTTJAuthenticationError, WTTJScraperError
from smartapply.scrapers.wttj.matches_api import fetch_matches_api_page

SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"
APIFY_USER_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"


@dataclass(frozen=True)
class SourceHealth:
    """User-facing connection state for one job source."""

    configured: bool
    ready: bool
    state: str
    message: str


def source_configuration(settings: Settings) -> dict[str, bool]:
    return {
        "serpapi": bool(settings.serpapi_api_key),
        "francetravail": bool(
            settings.francetravail_client_id and settings.francetravail_client_secret
        ),
        "linkedin": bool(settings.apify_token),
        "welcometothejungle": bool(settings.wttj_cookie),
    }


def pending_source_health(settings: Settings) -> dict[str, SourceHealth]:
    """Return an instant local snapshot while live checks run in background."""
    return {
        key: SourceHealth(
            configured=configured,
            ready=False,
            state="checking" if configured else "unconfigured",
            message=(
                "Vérification en cours…"
                if configured
                else "Identifiants absents de la configuration."
            ),
        )
        for key, configured in source_configuration(settings).items()
    }


def check_source_health(
    settings: Settings,
    *,
    timeout: int = 8,
) -> dict[str, SourceHealth]:
    """Verify all configured sources concurrently without launching a paid search."""
    configured = source_configuration(settings)
    checks: dict[str, Callable[[], SourceHealth]] = {
        "serpapi": lambda: _check_serpapi(settings, timeout=timeout),
        "francetravail": lambda: _check_france_travail(settings, timeout=timeout),
        "linkedin": lambda: _check_apify(settings, timeout=timeout),
        "welcometothejungle": lambda: _check_wttj(settings, timeout=timeout),
    }
    results: dict[str, SourceHealth] = {}
    futures = {}
    with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="source-health") as pool:
        for key, check in checks.items():
            if not configured[key]:
                results[key] = SourceHealth(
                    configured=False,
                    ready=False,
                    state="unconfigured",
                    message="Identifiants absents de la configuration.",
                )
                continue
            futures[pool.submit(check)] = key

        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:  # one failed provider must not hide the others
                results[key] = SourceHealth(
                    configured=True,
                    ready=False,
                    state="unavailable",
                    message=_public_failure_message(exc),
                )

    # Preserve the stable source order expected by the QML views.
    return {key: results[key] for key in configured}


def _check_serpapi(settings: Settings, *, timeout: int) -> SourceHealth:
    response = requests.get(
        SERPAPI_ACCOUNT_URL,
        params={"api_key": settings.serpapi_api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Réponse de compte SerpAPI invalide.")

    remaining = _remaining_serpapi_searches(payload)
    if remaining is not None and remaining <= 0:
        return _unavailable("Quota SerpAPI épuisé.")
    account_status = str(payload.get("account_status") or "").strip()
    normalized_status = account_status.lower()
    if any(marker in normalized_status for marker in ("run out", "out of searches", "exhausted")):
        return _unavailable("Quota SerpAPI épuisé.")
    if normalized_status and normalized_status != "active":
        return _unavailable(f"Compte SerpAPI : {account_status.rstrip('.')}.")
    detail = (
        f"Compte valide · {remaining} recherche(s) restante(s)."
        if remaining is not None
        else "Compte et clé API valides."
    )
    return _available(detail)


def _remaining_serpapi_searches(payload: dict) -> int | None:
    direct = payload.get("total_searches_left")
    if isinstance(direct, int | float) and not isinstance(direct, bool):
        return int(direct)
    plan = payload.get("plan_searches_left")
    extra = payload.get("extra_credits")
    numeric = [
        int(value)
        for value in (plan, extra)
        if isinstance(value, int | float) and not isinstance(value, bool)
    ]
    return sum(numeric) if numeric else None


def _check_apify(settings: Settings, *, timeout: int) -> SourceHealth:
    response = requests.get(
        APIFY_USER_LIMITS_URL,
        headers={"Authorization": f"Bearer {settings.apify_token}"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Réponse de quota Apify invalide.")
    data = payload["data"]
    current, limits = data.get("current"), data.get("limits")
    if not isinstance(current, dict) or not isinstance(limits, dict):
        raise ValueError("Quota Apify impossible à vérifier.")
    used, maximum = current.get("monthlyUsageUsd"), limits.get("maxMonthlyUsageUsd")
    if not all(
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(value)
        and value >= 0
        for value in (used, maximum)
    ):
        raise ValueError("Quota Apify impossible à vérifier.")
    usage = f"{used:.3f} / {maximum:g} USD".replace(".", ",")
    if used >= maximum:
        return _unavailable(
            f"Limite mensuelle Apify atteinte ({usage}). Vérifiez le quota dans Apify."
        )
    return _available(f"Compte et jeton Apify valides · {usage} utilisés.")


def _check_france_travail(settings: Settings, *, timeout: int) -> SourceHealth:
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.francetravail_client_id,
            "client_secret": settings.francetravail_client_secret,
            "scope": settings.francetravail_scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("Réponse OAuth France Travail invalide.")
    return _available("Identifiants France Travail valides.")


def _check_wttj(settings: Settings, *, timeout: int) -> SourceHealth:
    payload = fetch_matches_api_page(
        page=1,
        cookie_header=settings.wttj_cookie,
        per_page=1,
        timeout=timeout,
    )
    if not isinstance(payload.get("data"), list):
        raise ValueError("Réponse WTTJ invalide.")
    return _available("Session WTTJ valide.")


def _available(message: str) -> SourceHealth:
    return SourceHealth(
        configured=True,
        ready=True,
        state="available",
        message=message,
    )


def _unavailable(message: str) -> SourceHealth:
    return SourceHealth(
        configured=True,
        ready=False,
        state="unavailable",
        message=message,
    )


def _public_failure_message(exc: Exception) -> str:
    """Describe a failed check without exposing URLs, cookies, keys or tokens."""
    if isinstance(exc, WTTJAuthenticationError):
        return "Session WTTJ expirée ou refusée."
    if isinstance(exc, requests.Timeout):
        return "Délai de vérification dépassé."
    if isinstance(exc, requests.RequestException):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in {401, 403}:
            return "Identifiants refusés par le service."
        if status == 429:
            return "Quota ou limite de requêtes atteint."
        if status:
            return f"Service indisponible (HTTP {status})."
        return "Connexion réseau impossible."
    if isinstance(exc, WTTJScraperError):
        return "Réponse WTTJ inexploitable."
    return str(exc).strip() or "Vérification impossible."
