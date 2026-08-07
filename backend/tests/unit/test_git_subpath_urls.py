"""Gitea/Forgejo served under a URL path prefix (upstream #2642).

An instance with a ROOT_URL like ``https://host/gitea`` puts repositories at
``/<prefix>/<owner>/<repo>``. Three things had to agree for that to work, and in
our tree only two of them are upstream's:

1. ``parse_repo_url`` — took the two path segments as owner/repo, so a third
   raised "Cannot parse repository URL";
2. ``get_api_base`` — built from scheme+host alone, so it would have addressed
   ``https://host/api/v1`` on an instance answering at ``https://host/gitea/api/v1``;
3. **the schema validator, which is ours** — upstream has no schema-level URL
   validation at all. Its ``$``-anchored two-segment pattern rejected the URL at
   the API, so the config could not even be saved and a fixed parser would never
   have been handed a subpath URL.

Root-hosted instances must be bit-for-bit unaffected, and Forgejo inherits both
methods from Gitea rather than overriding them, so one fix covers both providers.
"""

from __future__ import annotations

import pytest

from backend.app.schemas.git_backup import ProviderType, _validate_repo_url
from backend.app.services.git_providers.forgejo import ForgejoBackend
from backend.app.services.git_providers.gitea import GiteaBackend

SUBPATH = "https://git.example.com/gitea/owner/repo"


@pytest.fixture(params=[GiteaBackend, ForgejoBackend], ids=["gitea", "forgejo"])
def backend(request):
    """Both providers, because Forgejo inherits the parsing it never overrides."""
    return request.param()


class TestSubpathHostedInstances:
    def test_owner_and_repo_come_from_the_last_two_segments(self, backend) -> None:
        assert backend.parse_repo_url(SUBPATH) == ("owner", "repo")

    def test_api_base_keeps_the_prefix(self, backend) -> None:
        assert backend.get_api_base(SUBPATH) == "https://git.example.com/gitea/api/v1"

    def test_a_multi_segment_prefix_works(self, backend) -> None:
        url = "https://git.example.com/services/forge/owner/repo.git"
        assert backend.parse_repo_url(url) == ("owner", "repo")
        assert backend.get_api_base(url) == "https://git.example.com/services/forge/api/v1"

    def test_prefix_with_a_port(self, backend) -> None:
        url = "http://192.168.1.5:3000/gitea/team/backups"
        assert backend.parse_repo_url(url) == ("team", "backups")
        assert backend.get_api_base(url) == "http://192.168.1.5:3000/gitea/api/v1"

    def test_the_schema_accepts_a_subpath_url(self) -> None:
        """Ours, and the reason the provider fix alone would have been inert."""
        assert _validate_repo_url(SUBPATH, ProviderType.GITEA) == SUBPATH
        assert _validate_repo_url(SUBPATH, ProviderType.FORGEJO) == SUBPATH


class TestRootHostedIsUnaffected:
    @pytest.mark.parametrize(
        ("url", "api_base"),
        [
            ("https://git.example.com/owner/repo", "https://git.example.com/api/v1"),
            ("https://git.example.com:3000/owner/repo", "https://git.example.com:3000/api/v1"),
            ("https://git.example.com/owner/repo.git", "https://git.example.com/api/v1"),
            ("https://git.example.com/owner/repo/", "https://git.example.com/api/v1"),
        ],
    )
    def test_unchanged(self, backend, url, api_base) -> None:
        assert backend.parse_repo_url(url) == ("owner", "repo")
        assert backend.get_api_base(url) == api_base

    def test_ssh_form_still_parses(self, backend) -> None:
        assert backend.parse_repo_url("git@git.example.com:owner/repo.git") == ("owner", "repo")


class TestDotSegmentsAreRejected:
    """Allowing a prefix is what creates this vector, so it is closed in the same
    change. Segments are ``[\\w.-]+``, which matches ``..`` happily, and the prefix
    is concatenated into the API base the provider then addresses — a URL like
    ``…/a/b/../../admin/owner/repo`` would build requests against a path the user
    never named. The two-segment pattern this replaces had no prefix at all.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://git.example.com/a/b/../../etc/owner/repo",
            "https://git.example.com/../owner/repo",
            "https://git.example.com/./owner/repo",
            "https://git.example.com/owner/..",
        ],
    )
    def test_parser_refuses(self, backend, url) -> None:
        with pytest.raises(ValueError, match="Cannot parse repository URL"):
            backend.parse_repo_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://git.example.com/a/b/../../etc/owner/repo",
            "https://git.example.com/../owner/repo",
        ],
    )
    def test_schema_refuses_too(self, url) -> None:
        with pytest.raises(ValueError, match="Invalid Gitea repository URL"):
            _validate_repo_url(url, ProviderType.GITEA)

    def test_a_name_merely_starting_with_a_dot_is_still_allowed(self, backend) -> None:
        """The guard rejects ``.`` and ``..`` as WHOLE segments, not any leading
        dot — ``.dotfiles`` is an odd repository name, not a traversal."""
        assert backend.parse_repo_url("https://git.example.com/owner/.dotfiles") == ("owner", ".dotfiles")
        assert backend.parse_repo_url("https://git.example.com/.config/owner/repo") == ("owner", "repo")


class TestStillRejectsWhatItAlwaysDid:
    @pytest.mark.parametrize(
        "url",
        ["https://git.example.com/repo", "not-a-url", "https://git.example.com", ""],
    )
    def test_parser(self, backend, url) -> None:
        with pytest.raises(ValueError):
            backend.parse_repo_url(url)

    def test_api_base_needs_a_repository_url(self, backend) -> None:
        with pytest.raises(ValueError, match="Cannot derive API base"):
            backend.get_api_base("not-a-url")


class TestOtherProvidersUntouched:
    """Upstream is explicit that GitHub and GitLab are out of scope, and they
    have their own parse_repo_url — Gitea only overrides its own."""

    def test_github_still_rejects_three_segments(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub repository URL"):
            _validate_repo_url("https://ghe.example.com/a/b/c", ProviderType.GITHUB)

    def test_gitlab_still_rejects_three_segments(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitLab repository URL"):
            _validate_repo_url("https://gitlab.example.com/group/sub/project", ProviderType.GITLAB)
