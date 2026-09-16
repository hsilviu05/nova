"""Tools that report on the machine NOVA runs on.

All four are read-only and none of them shells out for the numbers that
Python can read directly. ``os`` and ``shutil`` give load, CPU count and disk
usage without spawning anything, which is both faster and one fewer process
that could hang. Only the process list needs ``ps``, because there is no
portable way to enumerate other processes from the standard library.

Deliberately no psutil. It would give prettier memory figures on Linux, and
it is a compiled dependency for four numbers that ``/proc`` and ``vm_stat``
already publish.
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from nova.core.config import ToolSettings
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
    narrow,
)
from nova.tools.errors import ToolError
from nova.tools.process import resolve_workspace_path, run

# Processes listed at once. A full listing is hundreds of rows of noise; the
# question behind "what's running" is always about the heavy ones.
_PROCESS_LIMIT = 15

# Named rather than inlined so the readers below can be pointed at a fixture.
# These files exist only on Linux, and the parsing is the part worth testing;
# without a seam it could only ever be exercised in CI and never on the Mac
# the code is written on.
_PROC_UPTIME = "/proc/uptime"
_PROC_MEMINFO = "/proc/meminfo"


class _SystemTool(Tool):
    """Shared configuration for the tools that read this machine."""

    def __init__(self, settings: ToolSettings) -> None:
        self._settings = settings


class SystemHealthTool(_SystemTool):
    """Is this machine all right?"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="system_health",
            description=(
                "One-line verdict on the health of the machine NOVA runs on: "
                "load average, memory pressure, and free disk. Use this when "
                "asked whether the server or Mac is okay, rather than reading "
                "each number separately."
            ),
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        load = load_average()
        cpus = os.cpu_count() or 1
        disk = shutil.disk_usage(os.path.expanduser("~"))
        disk_percent = round(disk.used / disk.total * 100, 1)

        # Load per core, not raw load: "4.0" means nothing without knowing
        # how many cores it is spread across.
        load_ratio = load[0] / cpus if load else 0.0
        problems = []
        if load_ratio > 1.5:
            problems.append(f"load is {load_ratio:.1f}x the core count")
        if disk_percent > 90:
            problems.append(f"disk is {disk_percent}% full")

        healthy = not problems
        summary = "Healthy." if healthy else "Under pressure: " + ", ".join(problems) + "."

        data: dict[str, Any] = {
            "healthy": healthy,
            "hostname": platform.node(),
            "platform": f"{platform.system()} {platform.release()}",
            "cpu_count": cpus,
            "load_average": list(load),
            "load_per_core": round(load_ratio, 2),
            "disk_percent_used": disk_percent,
            "uptime_seconds": _uptime_seconds(),
            "problems": problems,
        }
        return ToolResult(
            content=(
                f"{summary} {platform.node()} ({platform.system()} {platform.release()}), "
                f"{cpus} cores, load {load_ratio:.2f} per core, "
                f"disk {disk_percent}% used."
            ),
            data=data,
        )


class SystemResourcesTool(_SystemTool):
    """CPU and memory, in numbers."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="system_resources",
            description=(
                "Current CPU load and memory usage for the machine NOVA runs "
                "on, as numbers rather than a verdict."
            ),
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        load = load_average()
        cpus = os.cpu_count() or 1
        memory = await memory_usage()

        lines = [
            f"CPU: {cpus} cores, load {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}"
            if load
            else f"CPU: {cpus} cores, load unavailable",
        ]
        if memory:
            lines.append(
                f"Memory: {_gib(memory['used_bytes'])} of {_gib(memory['total_bytes'])} used "
                f"({memory['percent_used']}%)"
            )
        else:
            lines.append("Memory: not readable on this platform")

        return ToolResult(
            content="\n".join(lines),
            data={
                "cpu_count": cpus,
                "load_average": list(load),
                "memory": memory,
            },
        )


class DiskUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Annotated[
        str | None,
        Field(
            default=None,
            max_length=512,
            description=(
                "Directory to measure. Must be inside a directory NOVA is "
                "configured to look at; omit for the default project root."
            ),
        ),
    ] = None


class DiskUsageTool(_SystemTool):
    """How much room is left."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="disk_usage",
            description=(
                "Disk space on the volume holding a given directory: total, "
                "used, free, and percentage."
            ),
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
            input_model=DiskUsageInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, DiskUsageInput)

        # Resolved against the workspace roots even though reading usage is
        # harmless, so that the path in the answer is one NOVA was allowed to
        # name -- and so the model cannot use this to probe for directories.
        target = (
            resolve_workspace_path(payload.path, self._settings.workspace_roots)
            if self._settings.workspace_roots or payload.path
            else None
        )
        measured = str(target) if target else os.path.expanduser("~")

        usage = shutil.disk_usage(measured)
        percent = round(usage.used / usage.total * 100, 1)

        return ToolResult(
            content=(
                f"{measured}: {_gib(usage.used)} used of {_gib(usage.total)} "
                f"({percent}%), {_gib(usage.free)} free."
            ),
            data={
                "path": measured,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent_used": percent,
            },
        )


class RunningProcessesTool(_SystemTool):
    """What is using the machine."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="running_processes",
            description=(
                f"The {_PROCESS_LIMIT} processes using the most CPU right now, "
                "with their memory share. Use this when asked what is running "
                "or what is making the machine slow."
            ),
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        # A fixed argv with no interpolation: there is nothing here a caller
        # can influence, which is the easiest kind of command to be sure of.
        #
        # `-A -o` only, and the sort done in Python. BSD `ps` spells
        # "sort by CPU" as `-r` and procps spells it `--sort=-pcpu`; using
        # either makes this tool silently return nothing on the other
        # platform, which is how it first shipped -- working on the Mac it
        # was written on and failing on the Linux host the deployment guide
        # describes.
        result = await run(
            ["ps", "-Ao", "pid,pcpu,pmem,comm"],
            timeout_seconds=self._settings.command_timeout_seconds,
            max_output_bytes=self._settings.max_output_bytes,
        )
        if not result.ok:
            return ToolResult.failure("Could not list processes on this machine.")

        rows = _parse_ps(result.stdout, limit=_PROCESS_LIMIT)
        if not rows:
            return ToolResult(content="No processes reported.", data={"processes": []})

        lines = [f"{'PID':>7}  {'CPU%':>5}  {'MEM%':>5}  COMMAND"]
        lines += [
            f"{row['pid']:>7}  {row['cpu_percent']:>5}  {row['memory_percent']:>5}  {row['name']}"
            for row in rows
        ]
        return ToolResult(content="\n".join(lines), data={"processes": rows})


# -- helpers ------------------------------------------------------------------


def load_average() -> tuple[float, float, float]:
    try:
        return os.getloadavg()
    except OSError:  # pragma: no cover - not available on every platform
        return (0.0, 0.0, 0.0)


def _uptime_seconds() -> int | None:
    """Seconds since boot, where the platform makes it readable.

    Linux publishes it in ``/proc``. macOS does not, and the alternative is
    parsing ``sysctl``, which is not worth a subprocess for one number that
    the caller can live without.
    """
    try:
        with open(_PROC_UPTIME) as handle:
            return int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


async def memory_usage() -> dict[str, Any] | None:
    """Memory usage, or ``None`` where the platform will not say.

    Async because macOS will not report it without asking ``vm_stat``. Both
    callers are already async, and the alternative -- returning nothing on
    macOS -- is what this did until it was noticed that the memory reading
    was blank on exactly the machine NOVA is meant to run on.
    """
    if reading := _linuxmemory_usage():
        return reading
    return await _macosmemory_usage()


def _linuxmemory_usage() -> dict[str, Any] | None:
    try:
        values: dict[str, int] = {}
        with open(_PROC_MEMINFO) as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return None

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None

    return _reading(total=total, available=available)


# The page classes ``vm_stat`` reports that are available to a process that
# asks for memory: free pages, the inactive and speculative caches, and
# pages that can be reclaimed on demand. Anything else -- active, wired,
# compressed -- is in use.
_RECLAIMABLE = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")


async def _macosmemory_usage() -> dict[str, Any] | None:
    """Physical memory and what is reclaimable, via ``vm_stat``.

    ``sysconf`` gives the total but not the free count: macOS does not
    implement ``SC_AVPHYS_PAGES``, and asking for it raises. ``vm_stat`` is
    the documented way to get the rest, and it goes through the same bounded
    runner as every other command -- argv, no shell, no inherited
    environment.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total_pages = os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):  # pragma: no cover - platform dependent
        return None

    if total_pages <= 0 or page_size <= 0:  # pragma: no cover - platform dependent
        return None

    try:
        result = await run(["vm_stat"], timeout_seconds=5, max_output_bytes=8192)
    except ToolError:
        # No vm_stat on this machine, or it did not finish. Either way there
        # is no reading to report, and a missing memory figure must not take
        # down the dashboard that was only asking for one -- `run` raises for
        # a program it cannot find rather than returning a failed result.
        return None

    if not result.ok:
        return None

    pages = _parse_vm_stat(result.stdout)
    if not pages:
        return None

    total = total_pages * page_size
    available = min(sum(pages.get(name, 0) for name in _RECLAIMABLE) * page_size, total)
    return _reading(total=total, available=available)


def _parse_vm_stat(output: str) -> dict[str, int]:
    """The ``Pages <class>:  <count>.`` lines, as counts.

    Lines that are not in that shape -- the header, and the byte-valued
    totals at the end -- are skipped rather than guessed at.
    """
    pages: dict[str, int] = {}
    for line in output.splitlines():
        key, separator, rest = line.partition(":")
        if not separator or not key.startswith("Pages "):
            continue
        value = rest.strip().rstrip(".")
        if value.isdigit():
            pages[key.strip()] = int(value)
    return pages


def _reading(*, total: int, available: int) -> dict[str, Any]:
    used = total - available
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent_used": round(used / total * 100, 1),
    }


def _parse_ps(output: str, *, limit: int) -> list[dict[str, Any]]:
    """Turn ``ps`` output into the busiest rows, skipping what does not parse.

    Sorted here rather than by ``ps`` so the command stays portable -- see
    the note in :class:`RunningProcessesTool`. The whole table is parsed
    first because the limit applies to the busiest processes, not to
    whichever ones the kernel happened to list first.
    """
    rows: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, memory, name = parts
        if not pid.isdigit():
            continue

        rows.append(
            {
                "pid": int(pid),
                "cpu_percent": cpu,
                "memory_percent": memory,
                # `comm` is a full path on Linux and on macOS without -c.
                # The basename is what a person recognises.
                "name": name.strip().rsplit("/", 1)[-1],
            }
        )

    rows.sort(key=lambda row: _as_float(row["cpu_percent"]), reverse=True)
    return rows[:limit]


def _as_float(value: Any) -> float:
    """A percentage from ``ps``, or 0 when it is not a number.

    Some platforms print "-" for a process whose usage cannot be read, and a
    sort that raised on one of those would lose the whole listing.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def build_system_tools(settings: ToolSettings) -> list[Tool]:
    """Every system tool, for the registry."""
    return [
        SystemHealthTool(settings),
        SystemResourcesTool(settings),
        DiskUsageTool(settings),
        RunningProcessesTool(settings),
    ]


__all__ = [
    "DiskUsageTool",
    "RunningProcessesTool",
    "SystemHealthTool",
    "SystemResourcesTool",
    "build_system_tools",
    "load_average",
    "memory_usage",
]
