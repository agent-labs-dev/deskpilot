import pytest

from deskpilot import session


@pytest.mark.parametrize("limited_group", ["", "runner", "runner/job"])
def test_available_memory_honors_every_ancestor(tmp_path, limited_group):
    proc, cgroups = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text("MemAvailable: 16777216 kB\n")
    (proc / "self" / "cgroup").write_text("0::/runner/job/session\n")
    for relative in ("", "runner", "runner/job", "runner/job/session"):
        group = cgroups / relative
        group.mkdir(parents=True, exist_ok=True)
        (group / "memory.max").write_text(str(4 * 2**30) if relative == limited_group else "max")
        # Includes other processes in the limiting ancestor, leaving only 1 GiB.
        (group / "memory.current").write_text(str(3 * 2**30) if relative == limited_group else "0")
    assert session.available_mb(proc, cgroups) == 1024
