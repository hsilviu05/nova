"""The tool abstraction.

A tool is a named, schema-described action NOVA can take on the machine it
runs on. Everything NOVA can do to a system goes through this interface, for
one reason: it is the only place a permission can be checked, an argument can
be validated, and an invocation can be recorded. A capability added anywhere
else is a capability nobody is auditing.

Three things are deliberately *not* in the model's view of a tool:

* what it does on the machine -- the model gets a description and a schema;
* its permission level -- the model must not be able to reason about how to
  get a higher-privileged call past the gate;
* any credential it uses -- tokens are unwrapped inside adapters and never
  appear in a definition, an argument, or a result.

Inputs are declared as Pydantic models rather than hand-written JSON Schema.
One declaration then serves three purposes that would otherwise drift apart:
the schema shown to the model, the validation applied before execution, and
the field list the app renders. A tool whose schema and validation disagree
is a tool that accepts arguments the model was told not to send.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from nova.ai.base import ToolDefinition


class NoArguments(BaseModel):
    """The input model for a tool that takes nothing.

    ``extra="ignore"`` here, where every tool that declares real parameters
    uses ``extra="forbid"``. The difference is deliberate and narrow.

    Forbidding extras on a tool with parameters is what stops a model
    inventing an argument that quietly changes what runs. A tool that
    declares *no* parameters has nothing an argument could change, so the
    only thing a refusal buys is a wasted round trip -- and small local
    models, which are the point of running NOVA on your own machine, emit a
    junk argument for a no-argument tool routinely. llama3.2 calling
    ``system_health`` with an invented key turned "is this machine healthy"
    into "System health check failed", which is both wrong and alarming.

    ``json_schema_extra`` restores ``additionalProperties: false`` to the
    published schema, which ``extra="ignore"`` would otherwise drop: a model
    that reads the schema is still told to send nothing, and this only
    decides what happens when one does anyway.

    It also replaces the description. This schema is sent to the model on
    every turn, so the note you are reading would otherwise be spent from
    the context window several times over.
    """

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "description": "Takes no arguments.",
            "additionalProperties": False,
        },
    )


class Permission(enum.StrEnum):
    """How much damage a tool could do.

    Three levels, and the line between the last two is where the security
    model actually lives:

    ``READ``
        No side effects. Runs whenever it is asked for, including from the
        model mid-reply.

    ``WRITE``
        Changes something *NOVA itself owns*, reversibly, where the person
        can see the result and undo it in the app. In practice that means
        memory, and only memory -- which
        :class:`~nova.tools.registry.ToolRegistry` enforces rather than
        leaves as a convention. Runs without confirmation, because NOVA
        already writes memories on its own from ordinary conversation, and a
        prompt for the explicit "remember this" but not the implicit one
        would be theatre.

    ``DESTRUCTIVE``
        Everything else: anything that reaches outside NOVA's own data,
        anything irreversible, anything touching infrastructure. Always
        requires a person to say yes to that specific call, whoever asked
        for it. "Restart the container" belongs here as much as "delete it"
        does -- the test is whether being wrong costs something that cannot
        be taken back, and a restart in the middle of a deploy does.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def rank(self) -> int:
        return _PERMISSION_RANK[self.value]

    @property
    def needs_confirmation(self) -> bool:
        """Whether a person has to say yes to this specific call first."""
        return self is Permission.DESTRUCTIVE


_PERMISSION_RANK = {"read": 0, "write": 1, "destructive": 2}


class ToolGroup(enum.StrEnum):
    """Which capability a tool belongs to, and therefore which switch turns it on."""

    SYSTEM = "system"
    DOCKER = "docker"
    GIT = "git"
    GITHUB = "github"
    PROJECTS = "projects"
    KNOWLEDGE = "knowledge"
    DEVELOPER = "developer"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything about a tool that is not its behaviour."""

    name: str
    description: str
    group: ToolGroup
    permission: Permission
    input_model: type[BaseModel] = NoArguments
    # Shown to the person in the confirmation prompt, with ``{argument}``
    # placeholders filled from the call. Required for anything that needs
    # confirming; :class:`~nova.tools.registry.ToolRegistry` enforces that,
    # because an unlabelled "Are you sure?" is not informed consent.
    confirmation_prompt: str | None = None

    @property
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the arguments."""
        return self.input_model.model_json_schema()

    def as_definition(self) -> ToolDefinition:
        """The tool as the model sees it."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    def describe_call(self, arguments: dict[str, Any]) -> str:
        """Render the confirmation prompt for one proposed call.

        Falls back to the tool's description when a placeholder refers to an
        argument that is not there. A prompt that renders as ``Delete {name}?``
        is worse than a generic one: it looks like a bug at the moment
        somebody is deciding whether to trust NOVA with their containers.
        """
        template = self.confirmation_prompt or f"Run {self.name}?"
        try:
            return template.format(**arguments)
        except (KeyError, IndexError):
            return f"Run {self.name}?"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What running a tool produced.

    ``content`` is text, because that is what goes back to the model.
    ``data`` is the same information structured, because that is what the app
    renders. Tools fill both rather than having the app parse the text.
    """

    content: str
    data: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    # Set when output was cut. Visible rather than silent, so neither the
    # model nor the person mistakes a truncated listing for a complete one.
    truncated: bool = False

    @classmethod
    def failure(cls, message: str, *, code: str = "tool_failed") -> ToolResult:
        return cls(content=message, data={"error": code}, is_error=True)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is running a tool, and on whose behalf.

    Passed to every ``execute`` rather than captured at construction, because
    a tool is a process-lifetime object while a caller is a request.
    """

    user_id: Any
    request_id: str | None = None
    conversation_id: Any = None
    # True when the call came from the model rather than from a person
    # tapping a button. Tools do not branch on it; the audit log does, and
    # knowing which is which is the point of recording it.
    initiated_by_model: bool = False


def narrow[T: BaseModel](arguments: BaseModel, model: type[T]) -> T:
    """Narrow validated arguments to a tool's own input model.

    :class:`~nova.services.tools.ToolService` validates against
    ``spec.input_model`` before calling ``execute``, so this is a type
    narrowing rather than a check. It raises rather than coercing, because a
    mismatch means the registry and the tool disagree about the tool's own
    schema -- a wiring bug, not a bad request.
    """
    if not isinstance(arguments, model):  # pragma: no cover - wiring error, not input
        raise TypeError(f"Expected {model.__name__}, got {type(arguments).__name__}")
    return arguments


class Tool(ABC):
    """One thing NOVA can do.

    Subclasses implement :attr:`spec` and :meth:`execute` and nothing else.
    Permission checks, schema validation, timeouts, output truncation,
    redaction and audit logging all happen in
    :class:`~nova.services.tools.ToolService`, so a tool cannot forget to do
    them and a new tool inherits them by existing.
    """

    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """Describe this tool."""

    async def describe(self, arguments: BaseModel, context: ToolContext) -> str | None:
        """A better confirmation prompt for one specific call, if there is one.

        The default renders ``spec.confirmation_prompt`` from the arguments,
        which is enough when the arguments *are* the thing -- "Remove the
        container nova-api?" reads correctly. It is not enough when an
        argument is an opaque id: nobody can consent to forgetting
        ``a3f1c8…``. A tool with such an argument overrides this and looks
        the subject up, which is why the context comes with it.

        Returning ``None`` means "use the template". A failure here must not
        block the confirmation, so an implementation that cannot look
        something up returns ``None`` rather than raising.
        """
        return None

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        """Run the tool.

        ``arguments`` is an instance of the spec's ``input_model``, already
        validated, so an implementation reads typed attributes rather than
        digging through a dict. It must still treat *values* as untrusted: a
        validated string is still a string somebody chose.

        Raises:
            ToolError: for a failure the caller should see. Anything else
                escaping is caught by the service and reported as an
                unexpected failure without its detail, which can disclose
                paths and internals.
        """
