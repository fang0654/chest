"""Creating tools from GitHub repositories.

Shared by the web views and the Slack poller so both behave the same way.
"""

import logging

import requests

from django.db.models import Q
from django.utils import timezone

from .models import Tag, Tool
from .utils import (
    extract_readme_content,
    extract_readme_description,
    extract_tags_from_readme,
    extract_tool_name,
    fetch_logo,
    GitHubRateLimited,
    parse_github_repo,
    resolve_github_repo,
)

logger = logging.getLogger(__name__)


class ToolImportError(Exception):
    """A GitHub URL could not be turned into a tool (message is user-facing)."""


class RepoNotFound(ToolImportError):
    """The repo no longer exists on GitHub (deleted or made private)."""


class ToolAlreadyExists(ToolImportError):
    """The repo is already catalogued."""


class TryAgainLater(ToolImportError):
    """GitHub is rate limiting or unreachable; retrying later may work."""


def canonical_github_url(github_url):
    """https://github.com/<owner>/<repo>, or None if it isn't a repo URL."""
    repo = parse_github_repo(github_url)
    if not repo:
        return None
    return f"https://github.com/{repo[0]}/{repo[1]}"


def find_existing_tool(github_url):
    """Return the tool already catalogued for this repo, if any.

    Matches regardless of case, trailing slash, ".git" or deep links
    (/tree/..., /blob/...), since GitHub treats those as the same repo.
    """
    canonical = canonical_github_url(github_url)
    if not canonical:
        return None
    return (
        Tool.objects.filter(
            Q(github_url__iexact=canonical)
            | Q(github_url__iexact=canonical + "/")
            | Q(github_url__iexact=canonical + ".git")
            | Q(github_url__istartswith=canonical + "/")
        )
        .order_by("created_at")
        .first()
    )


def import_tool_from_github(github_url, source=Tool.SOURCE_WEB, tag_ids=None,
                            full_readme=True):
    """Create a Tool from a GitHub repo and return it.

    full_readme=True stores the whole README as the description and adds tags
    named in the README (the "Import" flow); False stores a short summary
    (the "Submit" flow). Raises ToolImportError for a non-GitHub URL, a repo
    that is already catalogued, or a repo without a README (import flow).
    """
    linked = canonical_github_url(github_url)
    if not linked:
        raise ToolImportError("That isn't a GitHub repository URL")

    # Cheap check first: no GitHub request needed for an exact match
    existing = find_existing_tool(linked)
    if existing:
        raise ToolAlreadyExists(f"{existing.name} is already in Chest")

    # Renamed/transferred repos redirect; store and dedupe on the current home
    try:
        canonical = resolve_github_repo(linked)
    except GitHubRateLimited as exc:
        raise TryAgainLater(f"{exc}; try again later")
    except requests.RequestException as exc:
        raise TryAgainLater(f"Couldn't reach GitHub ({exc.__class__.__name__})")
    if not canonical:
        raise RepoNotFound(f"{linked} no longer exists on GitHub")

    existing = find_existing_tool(canonical)
    if existing:
        raise ToolAlreadyExists(f"{existing.name} is already in Chest")

    try:
        readme_content = extract_readme_content(canonical)
    except GitHubRateLimited as exc:
        raise TryAgainLater(f"{exc}; try again later")
    if full_readme and not readme_content:
        raise ToolImportError("Could not find README file in the repository")

    name = extract_tool_name(readme_content, canonical)
    description = (
        readme_content if full_readme else extract_readme_description(readme_content)
    )

    tool = Tool.objects.create(
        name=name, github_url=canonical, description=description or "",
        source=source,
    )

    if tag_ids:
        tool.tags.set(tag_ids)
    if full_readme:
        found = extract_tags_from_readme(readme_content)
        if found:
            # Tag names are Title Case ("Active Directory"), README text isn't
            q = Q()
            for tag in found:
                q |= Q(name__iexact=tag)
            tool.tags.add(*Tag.objects.filter(q))

    logo = fetch_logo(readme_content, canonical, name)
    tool.logo_checked_at = timezone.now()
    if logo:
        tool.logo.save(*logo)  # also saves logo_checked_at
    else:
        tool.save(update_fields=["logo_checked_at"])

    logger.info("Imported %s (%s) from %s", name, canonical, source)
    return tool
