"""The registry, and the invariants it will not let a tool break.

Most of these assert on things that *fail at startup*. That is deliberate:
a tool with a missing confirmation prompt or a mislabelled permission is a
programming error, and the right time to find one is before the process is
serving requests, not when somebody's container is already gone.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from nova.core.config import IntegrationSettings, ProjectTarget, ToolSettings
from nova.tools.base import (
    NoArguments,
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
)
from nova.tools.errors import ToolNotFoundError
from nova.tools.registry import ToolRegistry, build_registry


class _Stub(Tool):
    """A tool that does nothing, so the registry's rules can be tested alone."""

    def __init__(
        self,
        *,
        name: str = "stub",
        group: ToolGroup = ToolGroup.SYSTEM,
        permission: Permission = Permission.READ,
        confirmation_prompt: str | None = None,
        input_model: type[BaseModel] = NoArguments,
    ) -> None:
        self._spec = ToolSpec(
            name=name,
            description="A stub.",
            group=group,
            permission=permission,
            input_model=input_model,
            confirmation_prompt=confirmation_prompt,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(content="ok")


class TestRegistration:
    def test_a_registered_tool_can_be_found(self) -> None:
        registry = ToolRegistry()
        registry.register(_Stub(name="alpha"))

        assert registry.has("alpha")
        assert registry.get("alpha").spec.name == "alpha"

    def test_an_unknown_tool_is_not_found(self) -> None:
        with pytest.raises(ToolNotFoundError):
            ToolRegistry().get("nothing_like_this")

    def test_a_disabled_group_is_indistinguishable_from_a_missing_tool(self) -> None:
        """One error for both, on purpose.

        Distinguishing them would tell a caller which capabilities exist but
        are switched off on this machine, which is exactly the map somebody
        probing it would want.
        """
        registry = build_registry(
            tools=ToolSettings(docker_enabled=False),
            integrations=IntegrationSettings(),
        )

        with pytest.raises(ToolNotFoundError) as caught:
            registry.get("docker_containers")
        assert caught.value.code == "tool_not_found"

    def test_duplicate_names_are_refused(self) -> None:
        registry = ToolRegistry()
        registry.register(_Stub(name="alpha"))

        with pytest.raises(ValueError, match="already registered"):
            registry.register(_Stub(name="alpha"))

    def test_specs_are_ordered_stably(self) -> None:
        # The app renders this list; a set's iteration order would reshuffle
        # the Tools screen between launches.
        registry = ToolRegistry()
        registry.register(_Stub(name="zulu", group=ToolGroup.SYSTEM))
        registry.register(_Stub(name="alpha", group=ToolGroup.SYSTEM))

        assert [spec.name for spec in registry.specs()] == ["alpha", "zulu"]


class TestPermissionInvariants:
    def test_a_destructive_tool_must_say_what_it_is_asking(self) -> None:
        """An unlabelled "Are you sure?" is not informed consent."""
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="confirmation_prompt"):
            registry.register(_Stub(name="dangerous", permission=Permission.DESTRUCTIVE))

    def test_write_is_confined_to_memory(self) -> None:
        """WRITE runs without asking, so its meaning has to stay narrow.

        The tempting shortcut for a future tool that keeps hitting the
        confirmation prompt is to relabel it WRITE. That shortcut should not
        work.
        """
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="not in the knowledge group"):
            registry.register(
                _Stub(name="restart", group=ToolGroup.DOCKER, permission=Permission.WRITE)
            )

    def test_memory_writes_are_allowed_without_confirmation(self) -> None:
        registry = ToolRegistry()
        registry.register(
            _Stub(name="remember", group=ToolGroup.KNOWLEDGE, permission=Permission.WRITE)
        )

        assert registry.get("remember").spec.permission.needs_confirmation is False

    def test_only_destructive_needs_confirmation(self) -> None:
        assert Permission.READ.needs_confirmation is False
        assert Permission.WRITE.needs_confirmation is False
        assert Permission.DESTRUCTIVE.needs_confirmation is True


class TestSchemas:
    def test_a_tool_definition_hides_the_permission_from_the_model(self) -> None:
        """The model gets a name, a description and a schema.

        Not the permission: it must not be able to reason about which calls
        get past the gate.
        """
        spec = _Stub(name="alpha").spec
        definition = spec.as_definition()

        assert definition.name == "alpha"
        assert "permission" not in str(definition.input_schema)
        assert not hasattr(definition, "permission")

    def test_the_schema_comes_from_the_input_model(self) -> None:
        """One declaration, so validation and the advertised schema cannot
        disagree about what the tool accepts."""

        class Input(BaseModel):
            model_config = ConfigDict(extra="forbid")

            container: str
            lines: int = 50

        spec = _Stub(name="logs", input_model=Input).spec

        assert spec.input_schema["properties"].keys() == {"container", "lines"}
        assert spec.input_schema["required"] == ["container"]

    def test_a_no_argument_tool_advertises_an_empty_object(self) -> None:
        schema = _Stub(name="health").spec.input_schema

        assert schema["type"] == "object"
        assert schema.get("properties", {}) == {}


class TestConfirmationPrompts:
    def test_the_prompt_names_what_would_happen(self) -> None:
        spec = ToolSpec(
            name="remove",
            description="Removes a container.",
            group=ToolGroup.DOCKER,
            permission=Permission.DESTRUCTIVE,
            confirmation_prompt="Remove the container “{container}”?",
        )

        assert spec.describe_call({"container": "nova-api"}) == ("Remove the container “nova-api”?")

    def test_a_placeholder_with_no_argument_falls_back(self) -> None:
        """A prompt rendering as "Delete {name}?" looks like a bug at exactly
        the moment somebody is deciding whether to trust NOVA."""
        spec = ToolSpec(
            name="remove",
            description="Removes a container.",
            group=ToolGroup.DOCKER,
            permission=Permission.DESTRUCTIVE,
            confirmation_prompt="Remove {container}?",
        )

        assert spec.describe_call({}) == "Run remove?"


class TestBuilding:
    def test_shell_is_off_by_default(self) -> None:
        registry = build_registry(tools=ToolSettings(), integrations=IntegrationSettings())
        assert not registry.has("execute_shell_command")

    def test_shell_enabled_with_an_empty_allowlist_registers_nothing(self) -> None:
        """A tool that can only ever refuse is worse than no tool.

        The model would be offered a capability and spend turns discovering
        it does not work.
        """
        registry = build_registry(
            tools=ToolSettings(shell_enabled=True, shell_allowlist=[]),
            integrations=IntegrationSettings(),
        )

        assert not registry.has("execute_shell_command")

    def test_shell_with_an_allowlist_registers_as_destructive(self) -> None:
        registry = build_registry(
            tools=ToolSettings(shell_enabled=True, shell_allowlist=["ls"]),
            integrations=IntegrationSettings(),
        )

        spec = registry.get("execute_shell_command").spec
        assert spec.permission is Permission.DESTRUCTIVE
        # The allowlist is in the description, so the model does not waste a
        # turn proposing something that will be refused.
        assert "ls" in spec.description

    def test_github_without_a_token_registers_nothing(self) -> None:
        """Enabled without a credential would register tools that fail on
        every call; declining is the honest outcome."""
        registry = build_registry(
            tools=ToolSettings(github_enabled=True),
            integrations=IntegrationSettings(github_token=None),
        )

        assert not registry.has("github_repositories")

    def test_projects_appear_only_when_configured(self) -> None:
        empty = build_registry(
            tools=ToolSettings(projects_enabled=True), integrations=IntegrationSettings()
        )
        configured = build_registry(
            tools=ToolSettings(projects_enabled=True),
            integrations=IntegrationSettings(
                projects=[ProjectTarget(name="SnapWorth", base_url="http://127.0.0.1:9000")]
            ),
        )

        assert not empty.has("project_health")
        assert configured.has("project_health")

    def test_the_default_registry_is_read_only(self) -> None:
        """Nothing NOVA ships with, out of the box, can change anything.

        Docker is off, shell is off, and what remains only reads. Turning on
        a capability is a deliberate act by the machine's owner.
        """
        registry = build_registry(tools=ToolSettings(), integrations=IntegrationSettings())

        assert registry.specs()
        assert all(spec.permission is Permission.READ for spec in registry.specs())
