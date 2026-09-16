"""The project tools: name resolution, and reading a health body.

The design point these tests defend is in the module docstring: the model
picks a project by *name*, never by URL. A URL argument would turn a prompt
injection into a request forgery with NOVA's network position -- it sits on a
home network, behind no proxy, and can reach the router's admin page. The set
of reachable addresses is fixed in configuration, and these tests are what
keeps it that way.

The rest is reading a ``/health`` body, which every framework shapes
differently. Understanding the common ones is worth doing and worth testing;
failing when a project returns prose is not.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from nova.core.config import IntegrationSettings, ProjectTarget
from nova.tools.base import Permission, ToolContext
from nova.tools.errors import ToolError
from nova.tools.projects import (
    ProjectHealthInput,
    _dependencies,
    _is_ok,
    build_project_tools,
)

PROJECTS = [
    ProjectTarget(
        name="SnapWorth",
        base_url="http://192.168.1.50:8000",
        health_path="/health",
        description="Receipt valuation",
    ),
    ProjectTarget(name="AIInterviewCoach", base_url="http://192.168.1.51:8000"),
]


def tools(handler: Any, *, projects: list[ProjectTarget] | None = None) -> dict[str, Any]:
    settings = IntegrationSettings(projects=PROJECTS if projects is None else projects)
    built = build_project_tools(
        settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return {tool.spec.name: tool for tool in built}


def context() -> ToolContext:
    return ToolContext(user_id=uuid.uuid4())


def response(status: int = 200, **kwargs: Any) -> Any:
    return lambda request: httpx.Response(status, **kwargs)


class TestListing:
    async def test_configured_projects_are_listed_with_their_descriptions(self) -> None:
        result = await tools(response())["project_list"].execute(
            ProjectHealthInput(project="x"), context()
        )

        assert "SnapWorth — Receipt valuation" in result.content
        assert "AIInterviewCoach" in result.content
        assert len(result.data["projects"]) == 2

    async def test_nothing_configured_says_so_rather_than_returning_nothing(self) -> None:
        """An empty string reads to a model as a failure, and it retries."""
        result = await tools(response(), projects=[])["project_list"].execute(
            ProjectHealthInput(project="x"), context()
        )

        assert result.content == "No projects are configured for NOVA to monitor."
        assert result.data == {"projects": []}


class TestNameResolution:
    async def test_a_name_is_matched_regardless_of_case_or_padding(self) -> None:
        """A model that has read "snapworth" in a log line should still get
        the right project."""
        result = await tools(response(200, json={}))["project_health"].execute(
            ProjectHealthInput(project="  snapWORTH  "), context()
        )

        assert result.data["project"] == "SnapWorth"

    async def test_an_unknown_name_lists_the_ones_that_exist(self) -> None:
        """So the model's next attempt is a correction rather than a guess."""
        with pytest.raises(ToolError) as caught:
            await tools(response())["project_health"].execute(
                ProjectHealthInput(project="Nonesuch"), context()
            )

        assert caught.value.code == "project_unknown"
        assert "AIInterviewCoach" in str(caught.value)
        assert "SnapWorth" in str(caught.value)

    async def test_with_nothing_configured_the_message_says_none(self) -> None:
        with pytest.raises(ToolError) as caught:
            await tools(response(), projects=[])["project_health"].execute(
                ProjectHealthInput(project="SnapWorth"), context()
            )

        assert "Configured projects: none." in str(caught.value)

    def test_a_url_is_not_an_accepted_argument(self) -> None:
        """The design point of the whole module.

        If a URL could be passed, a line in a container log saying "check
        http://192.168.1.1/reboot" would become a request NOVA makes from
        inside the home network.
        """
        assert set(ProjectHealthInput.model_fields) == {"project"}

        with pytest.raises(ValidationError):
            ProjectHealthInput(project="SnapWorth", url="http://192.168.1.1/admin")

    async def test_a_name_that_looks_like_a_url_is_still_only_a_name(self) -> None:
        """It resolves against the configured set or it fails; it is never
        fetched."""
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            requested.append(str(request.url))
            return httpx.Response(200)

        with pytest.raises(ToolError):
            await tools(handler)["project_health"].execute(
                ProjectHealthInput(project="http://192.168.1.1/"), context()
            )

        assert requested == []

    async def test_the_url_checked_is_the_configured_one(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        await tools(handler)["project_health"].execute(
            ProjectHealthInput(project="SnapWorth"), context()
        )

        assert seen == ["http://192.168.1.50:8000/health"]


class TestHealthVerdict:
    async def test_a_healthy_service_is_reported_with_its_status_and_latency(self) -> None:
        result = await tools(response(200, json={}))["project_health"].execute(
            ProjectHealthInput(project="SnapWorth"), context()
        )

        assert result.data["healthy"] is True
        assert result.data["reachable"] is True
        assert result.data["status_code"] == 200
        assert result.data["latency_ms"] >= 0

    async def test_a_failing_service_is_reported_as_unhealthy_not_as_an_error(self) -> None:
        result = await tools(response(503, json={}))["project_health"].execute(
            ProjectHealthInput(project="SnapWorth"), context()
        )

        assert result.is_error is False
        assert result.data["healthy"] is False
        assert "answered 503 (unhealthy)" in result.content

    async def test_an_unreachable_service_is_an_answer_not_a_failure(self) -> None:
        """ "Is SnapWorth up" has been correctly answered with "no".

        Returning a tool error instead would make NOVA say something went
        wrong, which is not what happened.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        result = await tools(handler)["project_health"].execute(
            ProjectHealthInput(project="SnapWorth"), context()
        )

        assert result.is_error is False
        assert result.data["reachable"] is False
        assert result.data["healthy"] is False
        assert "is not responding" in result.content

    async def test_a_dependency_report_reaches_the_summary(self) -> None:
        body = {
            "dependencies": [
                {"name": "database", "healthy": True},
                {"name": "redis", "healthy": False},
            ]
        }

        result = await tools(response(200, json=body))["project_health"].execute(
            ProjectHealthInput(project="SnapWorth"), context()
        )

        assert "database: ok" in result.content
        assert "redis: failing" in result.content


class TestReadingDependencies:
    def test_novas_own_shape(self) -> None:
        body = {"dependencies": [{"name": "database", "healthy": True}]}

        assert _dependencies(httpx.Response(200, json=body)) == {"database": True}

    def test_a_listed_entry_with_no_name_is_skipped(self) -> None:
        body = {"dependencies": [{"healthy": True}, {"name": "redis", "healthy": True}]}

        assert _dependencies(httpx.Response(200, json=body)) == {"redis": True}

    def test_a_flat_map_of_strings(self) -> None:
        body = {"dependencies": {"database": "ok", "redis": "down"}}

        assert _dependencies(httpx.Response(200, json=body)) == {"database": True, "redis": False}

    @pytest.mark.parametrize("key", ["checks", "services", "components"])
    def test_the_common_nested_keys(self, key: str) -> None:
        """Spring, ASP.NET and the Go ecosystem each picked a different one."""
        body = {key: {"database": {"status": "up"}}}

        assert _dependencies(httpx.Response(200, json=body)) == {"database": True}

    @pytest.mark.parametrize(
        "body",
        [
            b"OK",  # prose, not JSON
            b'"healthy"',  # JSON, but not an object
            b"[]",
            b"{}",
            b'{"status": "ok"}',  # an object with no dependency report
        ],
    )
    def test_a_body_with_no_report_gives_up_quietly(self, body: bytes) -> None:
        """A health endpoint that returns prose is still a health endpoint.

        The status code carries the verdict; the body is a bonus.
        """
        assert _dependencies(httpx.Response(200, content=body)) == {}

    def test_a_body_too_large_to_be_a_health_report_is_not_parsed(self) -> None:
        """A misconfigured health path pointing at an HTML page, or at an
        endpoint that returns the whole database. Parsing megabytes of JSON
        to look for a "dependencies" key is not worth doing.
        """
        huge = b'{"dependencies": {"database": "ok"}, "padding": "' + b"x" * 5000 + b'"}'

        assert _dependencies(httpx.Response(200, content=huge)) == {}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("ok", True),
            ("UP", True),
            ("healthy", True),
            ("pass", True),
            ("true", True),
            ("down", False),
            ("degraded", False),
            (None, False),
            (0, False),
            ({"status": "ok"}, True),
            ({"healthy": True}, True),
            ({"status": "down"}, False),
            ({}, False),
        ],
    )
    def test_what_counts_as_ok(self, value: Any, expected: bool) -> None:
        assert _is_ok(value) is expected


class TestTheGroup:
    def test_both_tools_are_read_only(self) -> None:
        built = tools(response())

        assert all(tool.spec.permission is Permission.READ for tool in built.values())
        assert sorted(built) == ["project_health", "project_list"]
