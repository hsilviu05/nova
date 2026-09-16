"""What NOVA can do on this machine, assembled once at startup.

The registry is built from configuration and then frozen. That ordering is
the point: which tools exist is decided by the person who owns the machine,
before any request arrives, and nothing a model or a client says afterwards
can add one. A tool that is not in the registry does not exist as far as the
rest of the application is concerned -- there is no path that constructs one
on demand.

Registration also enforces the invariants that a tool cannot enforce about
itself: unique names, a schema the model can actually be given, and a
confirmation prompt on anything that needs confirming.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.ai.base import EmbeddingProvider
from nova.core.config import AISettings, IntegrationSettings, ToolSettings
from nova.core.logging import get_logger
from nova.tools.base import Permission, Tool, ToolGroup, ToolSpec
from nova.tools.errors import ToolNotFoundError

logger = get_logger(__name__)


class ToolRegistry:
    """The tools available in this process."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add a tool.

        Raises:
            ValueError: if the name is taken, or the tool would need
                confirmation without saying what it is asking. Both are
                programming errors caught at startup rather than conditions
                to handle at runtime.
        """
        spec = tool.spec
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} is already registered.")

        if spec.permission.needs_confirmation and not spec.confirmation_prompt:
            raise ValueError(
                f"Tool {spec.name!r} needs confirmation but has no confirmation_prompt. "
                "Asking someone to approve an action without telling them what it is "
                "is not consent."
            )

        # WRITE runs without asking, so its meaning has to be narrow enough
        # that running without asking is defensible: reversible changes to
        # NOVA's own memory, which the app lists and lets the owner edit.
        # Enforced rather than documented, because the tempting shortcut for
        # a future tool that keeps hitting the confirmation prompt is to
        # relabel it WRITE, and that shortcut should not compile.
        if spec.permission is Permission.WRITE and spec.group is not ToolGroup.KNOWLEDGE:
            raise ValueError(
                f"Tool {spec.name!r} is WRITE but not in the knowledge group. "
                "Only changes to NOVA's own memory may run without confirmation; "
                "anything reaching outside it is DESTRUCTIVE."
            )

        self._tools[spec.name] = tool

    def get(self, name: str) -> Tool:
        """Look a tool up by name.

        Raises:
            ToolNotFoundError: if nothing is registered under that name.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"No tool named {name!r}.")
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def specs(self) -> list[ToolSpec]:
        """Every registered tool, in a stable order for the app's list."""
        return sorted(
            (tool.spec for tool in self._tools.values()),
            key=lambda spec: (spec.group.value, spec.name),
        )

    def groups(self) -> set[ToolGroup]:
        return {tool.spec.group for tool in self._tools.values()}

    def __len__(self) -> int:
        return len(self._tools)


@dataclass(frozen=True, slots=True)
class KnowledgeDependencies:
    """What the memory tools need to reach the store.

    Passed in rather than resolved here because the registry is built once at
    startup, when the session factory and the embedding provider already
    exist, and building them a second time would mean a second connection
    pool.
    """

    session_factory: async_sessionmaker[AsyncSession]
    embeddings: EmbeddingProvider
    ai: AISettings


def build_registry(
    *,
    tools: ToolSettings,
    integrations: IntegrationSettings,
    knowledge: KnowledgeDependencies | None = None,
) -> ToolRegistry:
    """Assemble the registry for this configuration.

    Imports live inside the function rather than at module scope so that a
    disabled group's module is never even loaded. That is mostly hygiene, but
    it also means a broken optional adapter cannot stop the API from starting
    when nobody asked for it.
    """
    registry = ToolRegistry()

    if tools.system_enabled:
        from nova.tools.system import build_system_tools

        for tool in build_system_tools(tools):
            registry.register(tool)

    if tools.docker_enabled:
        from nova.tools.docker import build_docker_tools

        for tool in build_docker_tools(tools):
            registry.register(tool)

    if tools.git_enabled:
        from nova.tools.git import build_git_tools

        for tool in build_git_tools(tools):
            registry.register(tool)

    if tools.github_enabled and integrations.github_token is not None:
        from nova.tools.github import build_github_tools

        for tool in build_github_tools(integrations):
            registry.register(tool)
    elif tools.github_enabled:
        # Enabled without a token would register tools that fail on every
        # call. Declining to register them is the honest outcome: the model
        # is never offered a capability that cannot work.
        logger.warning("github_tools_skipped", reason="no_token")

    if tools.projects_enabled and integrations.projects:
        from nova.tools.projects import build_project_tools

        for tool in build_project_tools(integrations):
            registry.register(tool)

    if tools.memory_tools_enabled and knowledge is not None:
        from nova.tools.knowledge import build_knowledge_tools

        for tool in build_knowledge_tools(
            session_factory=knowledge.session_factory,
            embeddings=knowledge.embeddings,
            settings=knowledge.ai,
        ):
            registry.register(tool)

    if tools.shell_enabled:
        from nova.tools.shell import build_shell_tools

        for tool in build_shell_tools(tools):
            registry.register(tool)

    logger.info(
        "tool_registry_built",
        count=len(registry),
        groups=sorted(group.value for group in registry.groups()),
        shell_enabled=tools.shell_enabled,
    )
    return registry
