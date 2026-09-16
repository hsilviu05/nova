"""Tools that read GitHub.

The token never leaves this module. It is unwrapped once, put in a header on
a client that lives for the process, and is not part of any tool definition,
argument, or result -- so there is no path by which a model could be shown it
or persuaded to echo it back.

Everything here is a read, and the token should be scoped to reads. NOVA does
not open pull requests, comment, or merge; being asked about the work is a
different thing from doing it.

Note what is *not* trusted here: a pull-request title, an issue body, a
branch name, and a commit message are all attacker-controllable by anyone who
can open an issue on a public repository. They are returned as data and are
neutralised on the way to the model by
:func:`~nova.tools.safety.clean_tool_output`.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from nova.core.config import IntegrationSettings
from nova.core.logging import get_logger
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
    narrow,
)
from nova.tools.errors import ToolError, ToolUnavailableError

logger = get_logger(__name__)

# "owner/name", as GitHub itself defines them.
_REPO_PATTERN = r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$"


class _GitHubClient:
    """One HTTP client for every GitHub tool.

    Shared so the connection pool is shared, and so there is exactly one
    place the credential is attached.
    """

    def __init__(
        self, settings: IntegrationSettings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        token = settings.github_token.get_secret_value() if settings.github_token else ""
        self._owner = settings.github_owner

        # Injectable so the tests can drive real GitHub response bodies
        # through the real translation code without a network or a token.
        self._client = client or httpx.AsyncClient(
            base_url=settings.github_api_url.rstrip("/"),
            timeout=settings.request_timeout_seconds,
        )
        # Applied here rather than only on the client built above, so an
        # injected client is authenticated the same way. Otherwise the tests
        # would exercise a code path production never takes -- which is the
        # one way a test doubles as a lie.
        self._client.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "nova-terminal",
            }
        )

    @property
    def owner(self) -> str | None:
        return self._owner

    async def get(self, path: str, **params: Any) -> Any:
        """One GET, with failures translated into NOVA's vocabulary.

        The response body is deliberately not included in the error message.
        GitHub echoes request context in errors, and a 401 body is not
        something to forward to a model.
        """
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            logger.warning("github_request_failed", error=type(exc).__name__)
            raise ToolUnavailableError(
                "GitHub is not reachable from here.", code="github_unreachable"
            ) from exc

        if response.status_code == 404:
            raise ToolError(
                "No such repository, or the token cannot see it.", code="github_not_found"
            )
        if response.status_code in (401, 403):
            logger.warning("github_rejected", status=response.status_code)
            raise ToolError(
                "GitHub rejected NOVA's token, or the rate limit is exhausted.",
                code="github_forbidden",
            )
        if not response.is_success:
            logger.warning("github_error", status=response.status_code)
            raise ToolError(f"GitHub answered {response.status_code}.", code="github_error")

        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class _GitHubTool(Tool):
    def __init__(self, client: _GitHubClient) -> None:
        self._github = client


class RepositoriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(default=10, ge=1, le=50)] = 10


class GitHubRepositoriesTool(_GitHubTool):
    """The repositories the token can see, most recently pushed first."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_repositories",
            description=(
                "The user's GitHub repositories, most recently pushed first, "
                "with language, visibility, and open issue count."
            ),
            group=ToolGroup.GITHUB,
            permission=Permission.READ,
            input_model=RepositoriesInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, RepositoriesInput)

        body = await self._github.get(
            "/user/repos", sort="pushed", per_page=payload.limit, affiliation="owner"
        )
        repositories = [
            {
                "full_name": item.get("full_name"),
                "private": item.get("private"),
                "language": item.get("language"),
                "pushed_at": item.get("pushed_at"),
                "open_issues": item.get("open_issues_count"),
                "description": item.get("description"),
            }
            for item in _as_list(body)
        ]
        if not repositories:
            return ToolResult(content="No repositories visible.", data={"repositories": []})

        lines = [
            f"{r['full_name']}  {r['language'] or '—'}  "
            f"{'private' if r['private'] else 'public'}  pushed {r['pushed_at']}"
            for r in repositories
        ]
        return ToolResult(content="\n".join(lines), data={"repositories": repositories})


class RepositoryScopedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Annotated[
        str,
        Field(
            min_length=3,
            max_length=201,
            pattern=_REPO_PATTERN,
            description='Repository as "owner/name", e.g. "hsilviu05/nova".',
        ),
    ]
    limit: Annotated[int, Field(default=10, ge=1, le=50)] = 10


class GitHubCommitsTool(_GitHubTool):
    """Recent commits on the default branch."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_recent_commits",
            description="Recent commits on a GitHub repository's default branch.",
            group=ToolGroup.GITHUB,
            permission=Permission.READ,
            input_model=RepositoryScopedInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, RepositoryScopedInput)

        body = await self._github.get(
            f"/repos/{payload.repository}/commits", per_page=payload.limit
        )
        commits = [
            {
                "sha": (item.get("sha") or "")[:7],
                "author": _commit_author(item),
                "date": (item.get("commit") or {}).get("author", {}).get("date"),
                "subject": ((item.get("commit") or {}).get("message") or "").split("\n")[0],
            }
            for item in _as_list(body)
        ]
        if not commits:
            return ToolResult(
                content=f"No commits found on {payload.repository}.",
                data={"repository": payload.repository, "commits": []},
            )

        lines = [f"{c['sha']}  {c['date']}  {c['author']}  {c['subject']}" for c in commits]
        return ToolResult(
            content=f"{payload.repository}:\n" + "\n".join(lines),
            data={"repository": payload.repository, "commits": commits},
        )


class GitHubPullRequestsTool(_GitHubTool):
    """Open pull requests."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_pull_requests",
            description="Open pull requests on a GitHub repository.",
            group=ToolGroup.GITHUB,
            permission=Permission.READ,
            input_model=RepositoryScopedInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, RepositoryScopedInput)

        body = await self._github.get(
            f"/repos/{payload.repository}/pulls", state="open", per_page=payload.limit
        )
        pulls = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "author": (item.get("user") or {}).get("login"),
                "draft": item.get("draft"),
                "updated_at": item.get("updated_at"),
            }
            for item in _as_list(body)
        ]
        if not pulls:
            return ToolResult(
                content=f"No open pull requests on {payload.repository}.",
                data={"repository": payload.repository, "pull_requests": []},
            )

        lines = [
            f"#{p['number']}  {p['title']}  by {p['author']}" + ("  [draft]" if p["draft"] else "")
            for p in pulls
        ]
        return ToolResult(
            content=f"{payload.repository}, {len(pulls)} open:\n" + "\n".join(lines),
            data={"repository": payload.repository, "pull_requests": pulls},
        )


class GitHubIssuesTool(_GitHubTool):
    """Open issues, excluding pull requests."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_issues",
            description="Open issues on a GitHub repository, excluding pull requests.",
            group=ToolGroup.GITHUB,
            permission=Permission.READ,
            input_model=RepositoryScopedInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, RepositoryScopedInput)

        body = await self._github.get(
            f"/repos/{payload.repository}/issues", state="open", per_page=payload.limit
        )
        # The issues endpoint returns pull requests too, distinguished only
        # by a "pull_request" key. Filtered here so "how many open issues"
        # gets the answer a person means.
        issues = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "author": (item.get("user") or {}).get("login"),
                "labels": [label.get("name") for label in item.get("labels") or []],
                "updated_at": item.get("updated_at"),
            }
            for item in _as_list(body)
            if "pull_request" not in item
        ]
        if not issues:
            return ToolResult(
                content=f"No open issues on {payload.repository}.",
                data={"repository": payload.repository, "issues": []},
            )

        lines = [f"#{i['number']}  {i['title']}  by {i['author']}" for i in issues]
        return ToolResult(
            content=f"{payload.repository}, {len(issues)} open:\n" + "\n".join(lines),
            data={"repository": payload.repository, "issues": issues},
        )


def _as_list(body: Any) -> list[dict[str, Any]]:
    """GitHub's array responses, with anything unexpected discarded."""
    if not isinstance(body, list):
        return []
    return [item for item in body if isinstance(item, dict)]


def _commit_author(item: dict[str, Any]) -> str | None:
    """Prefer the GitHub account, falling back to the commit's own author.

    A commit made through the web UI, or by a bot, has no linked account.
    """
    account = item.get("author")
    if isinstance(account, dict) and account.get("login"):
        return str(account["login"])
    return ((item.get("commit") or {}).get("author") or {}).get("name")


def build_github_tools(
    settings: IntegrationSettings, *, http_client: httpx.AsyncClient | None = None
) -> list[Tool]:
    client = _GitHubClient(settings, client=http_client)
    return [
        GitHubRepositoriesTool(client),
        GitHubCommitsTool(client),
        GitHubPullRequestsTool(client),
        GitHubIssuesTool(client),
    ]
