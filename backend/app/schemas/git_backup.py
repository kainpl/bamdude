"""Pydantic schemas for Git backup configuration (GitHub, GitLab, Gitea, Forgejo)."""

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.core.compat import StrEnum


class ScheduleType(StrEnum):
    """Backup schedule types."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


class ProviderType(StrEnum):
    """Git backup provider types."""

    GITHUB = "github"
    GITLAB = "gitlab"
    GITEA = "gitea"
    FORGEJO = "forgejo"


# URL patterns per provider
_GITHUB_PATTERNS = [
    r"^https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?$",
    r"^git@github\.com:[\w.-]+/[\w.-]+(?:\.git)?$",
    # GitHub Enterprise / self-hosted: any https host with /<owner>/<repo>
    r"^https://[\w.-]+(:\d+)?/[\w.-]+/[\w.-]+(?:\.git)?$",
    r"^git@[\w.-]+:[\w.-]+/[\w.-]+(?:\.git)?$",
]

_GITLAB_PATTERNS = [
    r"^https://gitlab\.com/[\w.-]+/[\w.-]+(?:\.git)?$",
    r"^git@gitlab\.com:[\w.-]+/[\w.-]+(?:\.git)?$",
    # Self-hosted GitLab: any HTTPS URL with at least 2 path segments
    r"^https://[\w.-]+/[\w.-]+/[\w.-]+(?:\.git)?$",
    r"^git@[\w.-]+:[\w.-]+/[\w.-]+(?:\.git)?$",
]

# Gitea/Forgejo are always self-hosted — no canonical public host. Accept any
# https://<host>[:<port>]/<owner>/<repo>[.git] (matches the parser shape in
# services/git_providers/gitea.py::parse_repo_url) plus the SSH form.
_GITEA_PATTERNS = [
    r"^https?://[\w.-]+(:\d+)?/[\w.-]+/[\w.-]+(?:\.git)?$",
    r"^git@[\w.-]+:[\w.-]+/[\w.-]+(?:\.git)?$",
]


def _validate_repo_url(url: str, provider: str) -> str:
    """Validate repository URL based on provider."""
    url = url.strip().rstrip("/")
    if provider == ProviderType.GITHUB:
        if not any(re.match(p, url) for p in _GITHUB_PATTERNS):
            raise ValueError("Invalid GitHub repository URL. Expected format: https://github.com/owner/repo")
    elif provider == ProviderType.GITLAB:
        if not any(re.match(p, url) for p in _GITLAB_PATTERNS):
            raise ValueError(
                "Invalid GitLab repository URL. Expected format: https://gitlab.com/group/project "
                "or https://your-host/group/project"
            )
    elif provider in (ProviderType.GITEA, ProviderType.FORGEJO):
        if not any(re.match(p, url) for p in _GITEA_PATTERNS):
            raise ValueError(
                f"Invalid {provider.value.title()} repository URL. "
                f"Expected format: https://your-host/owner/repo (self-hosted instance)."
            )
    return url


class GitBackupConfigCreate(BaseModel):
    """Schema for creating Git backup config."""

    provider: ProviderType = Field(
        default=ProviderType.GITHUB,
        description="Git provider: github, gitlab, gitea, or forgejo",
    )
    repository_url: str = Field(..., min_length=1, max_length=500, description="Repository URL")
    access_token: str = Field(..., min_length=1, description="Personal Access Token")
    branch: str = Field(default="main", max_length=100, description="Branch to push to")
    api_base_url: str | None = Field(default=None, max_length=500, description="API base URL for self-hosted GitLab")

    schedule_enabled: bool = Field(default=False, description="Enable scheduled backups")
    schedule_type: ScheduleType = Field(default=ScheduleType.DAILY, description="Schedule frequency")

    backup_kprofiles: bool = Field(default=True, description="Backup K-profiles")
    backup_cloud_profiles: bool = Field(default=True, description="Backup Bambu Cloud profiles")
    backup_settings: bool = Field(default=False, description="Backup app settings")
    backup_spools: bool = Field(default=False, description="Backup spool inventory")
    backup_archives: bool = Field(default=False, description="Backup print archive metadata")

    enabled: bool = Field(default=True, description="Enable backup feature")

    @model_validator(mode="after")
    def validate_url_for_provider(self):
        """Validate repository URL matches the selected provider."""
        self.repository_url = _validate_repo_url(self.repository_url, self.provider)
        return self


class GitBackupConfigUpdate(BaseModel):
    """Schema for updating Git backup config (all fields optional)."""

    provider: ProviderType | None = None
    repository_url: str | None = Field(default=None, max_length=500)
    access_token: str | None = Field(default=None)
    branch: str | None = Field(default=None, max_length=100)
    api_base_url: str | None = Field(default=None, max_length=500)

    schedule_enabled: bool | None = None
    schedule_type: ScheduleType | None = None

    backup_kprofiles: bool | None = None
    backup_cloud_profiles: bool | None = None
    backup_settings: bool | None = None
    backup_spools: bool | None = None
    backup_archives: bool | None = None

    enabled: bool | None = None

    @field_validator("repository_url")
    @classmethod
    def validate_repo_url(cls, v: str | None) -> str | None:
        """Basic URL format check. Full provider-aware validation happens in model_validator."""
        if v is None:
            return v
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def validate_url_with_provider(self):
        """Validate URL if both provider and repository_url are provided."""
        if self.repository_url is not None and self.provider is not None:
            self.repository_url = _validate_repo_url(self.repository_url, self.provider)
        return self


class GitBackupConfigResponse(BaseModel):
    """Schema for Git backup config API response."""

    id: int
    provider: str
    repository_url: str
    has_token: bool = Field(description="Whether an access token is configured")
    branch: str
    api_base_url: str | None

    schedule_enabled: bool
    schedule_type: str

    backup_kprofiles: bool
    backup_cloud_profiles: bool
    backup_settings: bool
    backup_spools: bool
    backup_archives: bool

    enabled: bool
    last_backup_at: datetime | None
    last_backup_status: str | None
    last_backup_message: str | None
    last_backup_commit_sha: str | None
    next_scheduled_run: datetime | None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GitBackupLogResponse(BaseModel):
    """Schema for backup log API response."""

    id: int
    config_id: int
    started_at: datetime
    completed_at: datetime | None
    status: str
    trigger: str
    commit_sha: str | None
    files_changed: int
    error_message: str | None

    class Config:
        from_attributes = True


class GitBackupStatus(BaseModel):
    """Schema for current backup status."""

    configured: bool = Field(description="Whether backup is configured")
    enabled: bool = Field(description="Whether backup is enabled")
    is_running: bool = Field(description="Whether a backup is currently running")
    progress: str | None = Field(default=None, description="Current backup progress message")
    last_backup_at: datetime | None
    last_backup_status: str | None
    next_scheduled_run: datetime | None


class GitTestConnectionResponse(BaseModel):
    """Schema for test connection response."""

    success: bool
    message: str
    repo_name: str | None = None
    permissions: dict | None = None
    # True iff the provider's API confirms the repo is private. False means
    # public / internal-visibility (GitLab). None means the connection test
    # never reached the visibility-bearing field — fail-closed when used as
    # a privacy gate.
    is_private: bool | None = None


class GitBackupTriggerResponse(BaseModel):
    """Schema for manual backup trigger response."""

    success: bool
    message: str
    log_id: int | None = None
    commit_sha: str | None = None
    files_changed: int = 0
