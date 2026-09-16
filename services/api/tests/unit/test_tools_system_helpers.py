"""The system tools' parsers and their platform-dependent readers.

The companion integration test runs these tools against this actual machine,
which is the only way to find out that the numbers are real. It cannot cover
the branches that matter most when something is wrong: a machine under load,
a full disk, a ``ps`` that failed, a ``/proc`` that is not there. Those are
here, driven by fixtures.

``/proc/uptime`` and ``/proc/meminfo`` exist only on Linux. The readers take
their paths from module constants so the parsing can be exercised on a Mac,
rather than being a branch that only CI ever runs.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from nova.core.config import ToolSettings
from nova.tools import system as system_module
from nova.tools.base import Permission, ToolContext
from nova.tools.errors import ToolError
from nova.tools.process import CommandResult
from nova.tools.system import (
    DiskUsageInput,
    _as_float,
    _gib,
    _linuxmemory_usage,
    _macosmemory_usage,
    _parse_ps,
    _parse_vm_stat,
    _uptime_seconds,
    build_system_tools,
    memory_usage,
)


@pytest.fixture
def tools() -> dict[str, Any]:
    return {tool.spec.name: tool for tool in build_system_tools(ToolSettings())}


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(user_id=uuid.uuid4())


class TestUptime:
    def test_it_is_read_and_truncated_to_whole_seconds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "uptime"
        proc.write_text("350735.47 234388.90\n")
        monkeypatch.setattr(system_module, "_PROC_UPTIME", str(proc))

        assert _uptime_seconds() == 350735

    def test_a_missing_file_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS has no /proc. Uptime is a nice-to-have, not a reason to fail
        the whole health check."""
        monkeypatch.setattr(system_module, "_PROC_UPTIME", "/nonexistent/uptime")

        assert _uptime_seconds() is None

    @pytest.mark.parametrize("contents", ["", "not-a-number rest\n", "\n"])
    def test_output_that_does_not_parse_is_not_an_error(
        self, contents: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "uptime"
        proc.write_text(contents)
        monkeypatch.setattr(system_module, "_PROC_UPTIME", str(proc))

        assert _uptime_seconds() is None


class TestLinuxMemory:
    def test_meminfo_is_parsed_into_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """meminfo is in kibibytes, and the tools report bytes.

        MemAvailable rather than MemFree: free memory on a busy Linux box is
        close to zero by design, and reporting it would say every machine is
        out of memory.
        """
        proc = tmp_path / "meminfo"
        proc.write_text(
            "MemTotal:       16384000 kB\n"
            "MemFree:          512000 kB\n"
            "MemAvailable:    4096000 kB\n"
            "Buffers:          128000 kB\n"
        )
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", str(proc))

        reading = _linuxmemory_usage()

        assert reading == {
            "total_bytes": 16384000 * 1024,
            "used_bytes": (16384000 - 4096000) * 1024,
            "available_bytes": 4096000 * 1024,
            "percent_used": 75.0,
        }

    def test_a_line_with_no_value_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "meminfo"
        proc.write_text("HugePages_Total:\nMemTotal: 1000 kB\nMemAvailable: 400 kB\n")
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", str(proc))

        reading = _linuxmemory_usage()

        assert reading is not None
        assert reading["percent_used"] == 60.0

    @pytest.mark.parametrize(
        "contents",
        [
            "MemAvailable: 400 kB\n",  # no total
            "MemTotal: 1000 kB\n",  # no available
            "MemTotal: 0 kB\nMemAvailable: 0 kB\n",  # a total of zero would divide by zero
        ],
    )
    def test_an_incomplete_reading_is_no_reading(
        self, contents: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "meminfo"
        proc.write_text(contents)
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", str(proc))

        assert _linuxmemory_usage() is None

    def test_a_value_that_is_not_a_number_abandons_the_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "meminfo"
        proc.write_text("MemTotal: lots kB\n")
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", str(proc))

        assert _linuxmemory_usage() is None

    def test_no_proc_at_all_is_no_reading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", "/nonexistent/meminfo")

        assert _linuxmemory_usage() is None

    async def test_linux_is_preferred_when_both_could_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/proc is the accurate one; vm_stat is the fallback.

        Reaching the fallback would also mean running a subprocess, which is
        worth avoiding on the platform that does not need one.
        """
        proc = tmp_path / "meminfo"
        proc.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", str(proc))

        async def refuse(argv: list[str], **kwargs: Any) -> CommandResult:  # pragma: no cover
            raise AssertionError("vm_stat should not be run when /proc answered")

        monkeypatch.setattr("nova.tools.system.run", refuse)

        assert await memory_usage() == _linuxmemory_usage()

    async def test_without_proc_it_falls_back_rather_than_giving_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(system_module, "_PROC_MEMINFO", "/nonexistent/meminfo")

        async def fallback() -> dict[str, Any]:
            return {"percent_used": 42.0}

        monkeypatch.setattr(system_module, "_macosmemory_usage", fallback)

        assert await memory_usage() == {"percent_used": 42.0}


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                 6602.
Pages active:                             376725.
Pages inactive:                           373227.
Pages speculative:                          2781.
Pages throttled:                               0.
Pages wired down:                         249002.
Pages purgeable:                           21539.
"Translation faults":                  912345678.
File-backed pages:                        123456.
Swapins:                                       0.
"""


class TestMacOSMemory:
    """The fallback for platforms with no /proc.

    This is the path that matters most in practice: the MacBook is the
    machine NOVA is designed to run on. It reported nothing at all until it
    was noticed that macOS does not implement ``SC_AVPHYS_PAGES`` -- the
    lookup raised, the reader returned None, and the dashboard's memory card
    was simply blank with no error anywhere.
    """

    async def test_it_reports_a_plausible_reading_of_this_machine(self) -> None:
        """Against the real ``vm_stat``, not a fixture.

        A parser that agrees with a fixture and disagrees with the tool is
        exactly the failure this is here to catch.
        """
        reading = await _macosmemory_usage()

        assert reading is not None
        assert reading["total_bytes"] > 0
        assert reading["used_bytes"] + reading["available_bytes"] == reading["total_bytes"]
        assert 0 <= reading["percent_used"] <= 100

    async def test_the_reclaimable_classes_are_what_counts_as_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Free pages alone would say every Mac is out of memory.

        macOS keeps almost nothing free by design; the inactive and
        speculative caches are handed back the moment something asks.
        """
        monkeypatch.setattr(
            system_module.os,
            "sysconf",
            lambda name: {"SC_PAGE_SIZE": 16384, "SC_PHYS_PAGES": 1_572_864}[name],
        )

        async def vm_stat(argv: list[str], **kwargs: Any) -> CommandResult:
            assert argv == ["vm_stat"]
            return CommandResult(exit_code=0, stdout=VM_STAT, stderr="", truncated=False)

        monkeypatch.setattr("nova.tools.system.run", vm_stat)

        reading = await _macosmemory_usage()

        assert reading is not None
        assert reading["available_bytes"] == (6602 + 373227 + 2781 + 21539) * 16384
        assert reading["total_bytes"] == 1_572_864 * 16384

    async def test_a_reclaimable_count_larger_than_the_machine_is_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The page classes overlap in some macOS versions.

        Summing them can exceed physical memory, and an available figure
        above the total would produce negative usage.
        """
        monkeypatch.setattr(
            system_module.os,
            "sysconf",
            lambda name: {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 10}[name],
        )

        async def vm_stat(argv: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(
                exit_code=0, stdout="Pages free: 9999.\n", stderr="", truncated=False
            )

        monkeypatch.setattr("nova.tools.system.run", vm_stat)

        reading = await _macosmemory_usage()

        assert reading is not None
        assert reading["used_bytes"] == 0
        assert reading["percent_used"] == 0.0

    @pytest.mark.parametrize(
        ("exit_code", "stdout"),
        [
            (1, ""),  # vm_stat is not there, or refused
            (0, "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"),  # no page lines
            (0, ""),
        ],
    )
    async def test_a_reading_that_cannot_be_trusted_is_no_reading(
        self, exit_code: int, stdout: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def vm_stat(argv: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(exit_code=exit_code, stdout=stdout, stderr="", truncated=False)

        monkeypatch.setattr("nova.tools.system.run", vm_stat)

        assert await _macosmemory_usage() is None

    def test_only_page_count_lines_are_read(self) -> None:
        """The header carries a byte count and the trailing lines carry
        totals; neither is a page class, and reading one as such would put a
        nonsense number in the answer."""
        pages = _parse_vm_stat(VM_STAT)

        assert pages["Pages free"] == 6602
        assert "Mach Virtual Memory Statistics" not in pages
        assert '"Translation faults"' not in pages
        assert "File-backed pages" not in pages

    def test_a_count_that_is_not_a_number_is_skipped(self) -> None:
        assert _parse_vm_stat("Pages free: many.\nPages active: 12.\n") == {"Pages active": 12}


class TestParsingPs:
    OUTPUT = (
        "  PID %CPU %MEM COMM\n"
        "    1  0.1  0.2 /sbin/launchd\n"
        "  412 91.5  8.1 /Applications/Ollama.app/Contents/MacOS/ollama\n"
        "  913  4.0  1.1 /usr/bin/python3\n"
    )

    def test_the_busiest_come_first_regardless_of_the_order_ps_gave(self) -> None:
        """The reason sorting happens here.

        ``ps`` is asked for the table unsorted, because the flag that sorts
        it is spelled differently on macOS and Linux and using either makes
        the tool return nothing on the other platform.
        """
        rows = _parse_ps(self.OUTPUT, limit=10)

        assert [row["pid"] for row in rows] == [412, 913, 1]

    def test_the_limit_takes_the_busiest_not_the_first_listed(self) -> None:
        rows = _parse_ps(self.OUTPUT, limit=1)

        assert [row["pid"] for row in rows] == [412]

    def test_the_command_is_shown_by_its_basename(self) -> None:
        """comm is a full path on Linux; the basename is what a person
        recognises in a list on a phone."""
        rows = _parse_ps(self.OUTPUT, limit=10)

        assert rows[0]["name"] == "ollama"

    def test_the_header_is_not_a_process(self) -> None:
        rows = _parse_ps(self.OUTPUT, limit=10)

        assert all(isinstance(row["pid"], int) for row in rows)
        assert len(rows) == 3

    @pytest.mark.parametrize(
        "line",
        [
            "  412 91.5",  # too few fields
            "notapid 1.0 1.0 thing",  # pid is not a number
            "",
        ],
    )
    def test_a_row_that_does_not_parse_is_skipped(self, line: str) -> None:
        rows = _parse_ps(f"PID %CPU %MEM COMM\n{line}\n", limit=10)

        assert rows == []

    def test_a_process_whose_usage_is_unreadable_does_not_lose_the_listing(self) -> None:
        """Some platforms print "-" rather than a number. Sorting on that
        would raise and take every other row down with it."""
        rows = _parse_ps("PID %CPU %MEM COMM\n1 - - thing\n2 5.0 1.0 other\n", limit=10)

        assert [row["pid"] for row in rows] == [2, 1]

    @pytest.mark.parametrize(("value", "expected"), [("4.5", 4.5), ("-", 0.0), (None, 0.0)])
    def test_a_percentage_that_is_not_a_number_reads_as_zero(
        self, value: Any, expected: float
    ) -> None:
        assert _as_float(value) == expected


class TestRunningProcesses:
    async def test_a_ps_that_failed_is_reported_rather_than_faked(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def failing(argv: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(
                exit_code=1, stdout="", stderr="ps: illegal option", truncated=False
            )

        monkeypatch.setattr("nova.tools.system.run", failing)

        result = await tools["running_processes"].execute(DiskUsageInput(), context)

        assert result.is_error is True

    async def test_a_table_with_no_rows_says_so(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def empty(argv: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(
                exit_code=0, stdout="PID %CPU %MEM COMM\n", stderr="", truncated=False
            )

        monkeypatch.setattr("nova.tools.system.run", empty)

        result = await tools["running_processes"].execute(DiskUsageInput(), context)

        assert result.data == {"processes": []}

    async def test_the_listing_is_a_table_a_person_can_read(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def listing(argv: list[str], **kwargs: Any) -> CommandResult:
            return CommandResult(
                exit_code=0,
                stdout="  PID %CPU %MEM COMM\n  412 91.5  8.1 /usr/local/bin/ollama\n",
                stderr="",
                truncated=False,
            )

        monkeypatch.setattr("nova.tools.system.run", listing)

        result = await tools["running_processes"].execute(DiskUsageInput(), context)

        lines = result.content.splitlines()
        assert lines[0].split() == ["PID", "CPU%", "MEM%", "COMMAND"]
        assert lines[1].split() == ["412", "91.5", "8.1", "ollama"]
        assert result.data["processes"][0]["pid"] == 412

    async def test_ps_is_invoked_portably(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No BSD-only flags.

        This shipped once with ``-Aco ... -r``, which macOS accepts and
        Linux does not -- so the tool returned nothing at all on the host the
        deployment guide describes, and the failure was silent.
        """
        seen: list[list[str]] = []

        async def record(argv: list[str], **kwargs: Any) -> CommandResult:
            seen.append(argv)
            return CommandResult(exit_code=0, stdout="", stderr="", truncated=False)

        monkeypatch.setattr("nova.tools.system.run", record)
        await tools["running_processes"].execute(DiskUsageInput(), context)

        assert seen == [["ps", "-Ao", "pid,pcpu,pmem,comm"]]


class TestHealthUnderPressure:
    async def test_a_loaded_machine_is_reported_as_under_pressure(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Load per core, not raw load.

        "16.0" is idle on a 32-core machine and dying on a 4-core one, so the
        verdict is the ratio.
        """
        monkeypatch.setattr(system_module, "load_average", lambda: (16.0, 15.0, 14.0))
        monkeypatch.setattr(system_module.os, "cpu_count", lambda: 4)

        result = await tools["system_health"].execute(DiskUsageInput(), context)

        assert result.data["healthy"] is False
        assert result.data["load_per_core"] == 4.0
        assert "load is 4.0x the core count" in result.content

    async def test_a_full_disk_is_reported_as_under_pressure(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        monkeypatch.setattr(
            system_module.shutil,
            "disk_usage",
            lambda _: shutil._ntuple_diskusage(total=1000, used=960, free=40),
        )

        result = await tools["system_health"].execute(DiskUsageInput(), context)

        assert result.data["healthy"] is False
        assert result.data["disk_percent_used"] == 96.0
        assert "disk is 96.0% full" in result.content

    async def test_an_idle_machine_with_room_is_healthy(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        monkeypatch.setattr(system_module, "load_average", lambda: (0.4, 0.3, 0.2))
        monkeypatch.setattr(system_module.os, "cpu_count", lambda: 8)
        monkeypatch.setattr(
            system_module.shutil,
            "disk_usage",
            lambda _: shutil._ntuple_diskusage(total=1000, used=100, free=900),
        )

        result = await tools["system_health"].execute(DiskUsageInput(), context)

        assert result.data["healthy"] is True
        assert result.content.startswith("Healthy.")

    async def test_a_machine_with_no_readable_cpu_count_still_answers(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(system_module.os, "cpu_count", lambda: None)

        result = await tools["system_health"].execute(DiskUsageInput(), context)

        assert result.data["cpu_count"] == 1


class TestResources:
    async def test_memory_that_cannot_be_read_says_so_rather_than_reporting_zero(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero would be read as "no memory in use", which is a worse answer
        than "I cannot tell"."""

        async def unreadable() -> None:
            return None

        monkeypatch.setattr(system_module, "memory_usage", unreadable)

        result = await tools["system_resources"].execute(DiskUsageInput(), context)

        assert "not readable on this platform" in result.content
        assert result.data["memory"] is None

    async def test_memory_that_can_be_read_is_reported_in_gibibytes(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def reading() -> dict[str, Any]:
            return {
                "total_bytes": 16 * 1024**3,
                "used_bytes": 12 * 1024**3,
                "available_bytes": 4 * 1024**3,
                "percent_used": 75.0,
            }

        monkeypatch.setattr(system_module, "memory_usage", reading)

        result = await tools["system_resources"].execute(DiskUsageInput(), context)

        assert "Memory: 12.0 GiB of 16.0 GiB used (75.0%)" in result.content

    def test_gibibytes_are_rounded_for_reading_not_for_arithmetic(self) -> None:
        assert _gib(0) == "0.0 GiB"
        assert _gib(1536 * 1024**2) == "1.5 GiB"


class TestDiskUsage:
    async def test_with_no_roots_configured_it_measures_the_home_directory(
        self, context: ToolContext
    ) -> None:
        """The default when NOVA has not been pointed at anything.

        Measuring a volume is harmless; the path resolution exists so the
        *answer* names a directory NOVA was allowed to name.
        """
        tool = {t.spec.name: t for t in build_system_tools(ToolSettings(workspace_roots=[]))}[
            "disk_usage"
        ]

        result = await tool.execute(DiskUsageInput(), context)

        import os

        assert result.data["path"] == os.path.expanduser("~")
        assert result.data["percent_used"] >= 0

    async def test_a_path_outside_the_roots_is_refused(
        self, tmp_path: Path, context: ToolContext
    ) -> None:
        """Not because measuring it would be dangerous, but because answering
        tells the model whether it exists."""
        tool = {
            t.spec.name: t
            for t in build_system_tools(ToolSettings(workspace_roots=[str(tmp_path)]))
        }["disk_usage"]

        with pytest.raises(ToolError):
            await tool.execute(DiskUsageInput(path="/etc"), context)

    async def test_a_path_inside_a_root_is_measured_and_named(
        self, tmp_path: Path, context: ToolContext
    ) -> None:
        inside = tmp_path / "project"
        inside.mkdir()
        tool = {
            t.spec.name: t
            for t in build_system_tools(ToolSettings(workspace_roots=[str(tmp_path)]))
        }["disk_usage"]

        result = await tool.execute(DiskUsageInput(path=str(inside)), context)

        assert result.data["path"] == str(inside.resolve())
        assert "GiB" in result.content

    def test_a_path_longer_than_the_bound_is_refused_at_the_schema(self) -> None:
        with pytest.raises(ValidationError):
            DiskUsageInput(path="/" + "a" * 512)


class TestTheGroup:
    def test_every_system_tool_is_read_only(self, tools: dict[str, Any]) -> None:
        """Nothing here changes the machine, so nothing here can be reached
        by a model without a person in the loop having any effect."""
        assert all(tool.spec.permission is Permission.READ for tool in tools.values())

    def test_the_group_is_the_four_documented_tools(self, tools: dict[str, Any]) -> None:
        assert sorted(tools) == [
            "disk_usage",
            "running_processes",
            "system_health",
            "system_resources",
        ]
