from datetime import datetime, timedelta, timezone

from src.ingestion.github_readonly import (
    GitHubRateLimitKind,
    classify_github_rate_limit,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_429_is_secondary_and_retry_after_is_bounded() -> None:
    result = classify_github_rate_limit(
        429,
        headers={"Retry-After": "900"},
        body={"message": "You have exceeded a secondary rate limit."},
        now=NOW,
        max_backoff_seconds=60,
    )
    assert result is not None
    assert result.kind is GitHubRateLimitKind.SECONDARY
    assert result.retry_after_seconds == 900
    assert result.backoff_seconds == 60
    assert result.retry_at == NOW + timedelta(seconds=60)


def test_primary_reset_is_bounded_and_non_limit_is_ignored() -> None:
    result = classify_github_rate_limit(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW.timestamp() + 900)},
        now=NOW,
        max_backoff_seconds=60,
    )
    assert result is not None
    assert result.kind is GitHubRateLimitKind.PRIMARY
    assert result.backoff_seconds == 60
    assert classify_github_rate_limit(500, body="secondary rate limit", now=NOW) is None


def test_retry_after_date_and_secret_body_are_not_retained() -> None:
    result = classify_github_rate_limit(
        403,
        headers={"Retry-After": "Thu, 01 Jan 2026 00:00:10 GMT"},
        body={"message": "secondary rate limit token=sk-test-secret"},
        now=NOW,
    )
    assert result is not None
    assert result.backoff_seconds == 10
    assert "sk-test-secret" not in result.model_dump_json()
