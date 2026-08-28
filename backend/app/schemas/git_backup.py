"""Pydantic schemas for Git backup configuration (GitHub, GitLab, Gitea, Forgejo)."""

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


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
# https://<host>[:<port>][/<prefix>…]/<owner>/<repo>[.git] plus the SSH form.
#
# This gate is OURS — upstream has no schema-level URL validation at all — and it
# is why the #2642 subpath fix is not only a provider change here. An instance
# served under a ROOT_URL prefix (https://host/gitea/owner/repo) has three path
# segments, and the `$`-anchored two-segment pattern this replaces rejected it at
# the API with "Expected format: https://your-host/owner/repo". The user could
# never save the config, so a parser that had learned to read subpaths would
# never have been handed one.
#
# The shape must keep matching services/git_providers/gitea.py::_HTTPS_REPO_RE —
# the last two segments are owner and repo, anything before them is the prefix.
# Two validators of the same string disagreeing is how the message ends up
# describing a URL the code would actually have accepted.
#
# ``(?!\.\.?(?:/|$))`` on each segment rejects ``.`` and ``..`` as whole path
# components: segments are ``[\w.-]+``, which matches ``..`` happily, and the
# prefix ends up concatenated into the API base the provider then builds requests
# against. The pattern this replaces allowed no prefix at all, so widening it is
# what would have created the vector.
_GITEA_SEGMENT = r"(?!\.\.?(?:/|$))[\w.-]+"
_GITEA_PATTERNS = [
    rf"^https?://[\w.-]+(:\d+)?(?:/{_GITEA_SEGMENT})*?/{_GITEA_SEGMENT}/{_GITEA_SEGMENT}(?:\.git)?$",
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
                f"Expected format: https://your-host/owner/repo or "
                f"https://your-host/subpath/owner/repo (self-hosted instance)."
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
    # Reported separately from ``is_running`` because the two block different
    # things: a running backup only stops another backup, while a running
    # restore is rewriting the rows a backup would read.
    restore_running: bool = Field(default=False, description="Whether a restore is currently running")
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


# --- Restore ---------------------------------------------------------------

# ⚠️ "HEAD" means "whatever the branch tip is right now"; the service resolves it
# to a concrete SHA before reading anything, so a preview and the restore that
# follows cannot straddle two different commits when a scheduled backup lands in
# between. Anything else must look like a git object name.
REF_PATTERN = r"^(?:HEAD|[0-9a-fA-F]{7,40})$"


class RestoreCategory(StrEnum):
    """Backup categories that can be restored.

    Cloud profiles are deliberately absent: restoring a preset means writing to
    a Bambu or Orca Cloud account, which is a different operation from every
    other category here — those land in the local database, or on a printer the
    instance already owns. Tracked separately.
    """

    KPROFILES = "kprofiles"
    SETTINGS = "settings"
    SPOOLS = "spools"
    ARCHIVES = "archives"


class GitCommitInfo(BaseModel):
    """One commit in the backup repository."""

    sha: str
    message: str
    author: str
    date: str


class GitCommitListResponse(BaseModel):
    """Schema for the commit picker."""

    success: bool
    message: str
    branch: str
    commits: list[GitCommitInfo] = Field(default_factory=list)


class GitRestorePreviewCategory(BaseModel):
    """What a single category looks like inside one backup commit."""

    category: RestoreCategory
    available: bool = Field(description="Whether this category is present in the commit")
    item_count: int = Field(default=0, description="Rows/profiles found, 0 when unavailable")
    detail: str | None = Field(default=None, description="Why unavailable, or extra context, in English")
    detail_code: str | None = Field(
        default=None, description="Key under backup.restoreFromGit.details, for the client to translate"
    )
    detail_params: dict[str, str | int] = Field(
        default_factory=dict, description="Interpolation values for detail_code"
    )


class GitRestorePreview(BaseModel):
    """Schema for inspecting a commit before restoring from it."""

    success: bool
    message: str
    ref: str = Field(description="The concrete commit SHA that was inspected")
    commit: GitCommitInfo | None = None
    metadata_version: str | None = Field(default=None, description="version field from backup_metadata.json")
    categories: list[GitRestorePreviewCategory] = Field(default_factory=list)


class GitRestoreRequest(BaseModel):
    """Schema for triggering a restore."""

    ref: str = Field(default="HEAD", pattern=REF_PATTERN, description="Commit SHA to restore from, or HEAD")
    categories: list[RestoreCategory] = Field(..., min_length=1, description="Categories to restore")
    overwrite_existing: bool = Field(
        default=False,
        description="Update rows that already exist locally. When false, only missing rows are inserted.",
    )

    @model_validator(mode="after")
    def deduplicate_categories(self) -> "GitRestoreRequest":
        # Same category twice would double-count the result totals.
        seen: list[RestoreCategory] = []
        for category in self.categories:
            if category not in seen:
                seen.append(category)
        self.categories = seen
        return self


class GitRestoreNote(BaseModel):
    """One tally note, as a translation code plus the values it interpolates.

    Follows the ``backup.pathCheck`` contract already in use one card down in the
    same component: the server chooses the code and supplies typed params, and
    the client renders ``t(`...${code}`, { ...params, defaultValue: message })``.
    ``message`` is the English original, so a client that does not know a code
    yet still shows something sensible rather than the raw key.
    """

    code: str = Field(description="Key under backup.restoreFromGit.notes")
    params: dict[str, str | int] = Field(default_factory=dict, description="Interpolation values for code")
    message: str = Field(description="English rendering, used as the client's defaultValue")


class GitRestoreCategoryResult(BaseModel):
    """Per-category outcome of a restore."""

    restored: int = 0
    skipped: int = 0
    failed: int = 0
    notes: list[GitRestoreNote] = Field(default_factory=list)


class GitRestoreResponse(BaseModel):
    """Schema for the restore result."""

    success: bool
    message: str
    log_id: int | None = None
    ref: str | None = Field(default=None, description="The concrete commit SHA restored from")
    results: dict[str, GitRestoreCategoryResult] = Field(default_factory=dict)
