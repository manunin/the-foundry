from __future__ import annotations

from foundry.config import Settings
from foundry.forges.base import (
    ChangeFeedback,
    ChangeRequestInput,
    CheckResult,
    FeedbackItem,
    ForgeChange,
    ForgeComment,
    ForgeIssue,
    ForgeProvider,
    IssueQuery,
    TRACK_CI_FEEDBACK,
)
from foundry.forges.github import GitHubProvider
from foundry.forges.gitlab import GitLabProvider
from foundry.models import ForgeKind


def provider_for(settings: Settings) -> ForgeProvider:
    if settings.forge is ForgeKind.GITLAB:
        return GitLabProvider(settings.forge_host)
    return GitHubProvider(settings.forge_host)


__all__ = [
    "ChangeFeedback", "ChangeRequestInput", "CheckResult", "FeedbackItem",
    "ForgeChange", "ForgeComment", "ForgeIssue", "ForgeKind", "ForgeProvider",
    "IssueQuery", "TRACK_CI_FEEDBACK", "provider_for",
]
