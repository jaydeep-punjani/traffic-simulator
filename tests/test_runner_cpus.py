"""Default worker count must respect container CPU limits (cgroup v2 cpu.max)."""

import pytest

from trafficsim import runner
from trafficsim.config import load_scenario


@pytest.fixture
def cpu_max(tmp_path, monkeypatch):
    path = tmp_path / "cpu.max"
    monkeypatch.setattr(runner, "CGROUP_CPU_MAX", path)
    return path


def test_cgroup_quota_caps_cpus(cpu_max):
    host = runner.available_cpus()  # file absent: affinity / cpu_count only
    cpu_max.write_text("250000 100000\n")  # --cpus 2.5 rounds up to 3
    assert runner.available_cpus() == min(host, 3)


def test_unlimited_or_missing_cgroup_uses_host_cpus(cpu_max):
    cpu_max.write_text("max 100000\n")
    unlimited = runner.available_cpus()
    cpu_max.unlink()
    assert runner.available_cpus() == unlimited >= 1


def test_tiny_quota_still_gives_one_worker(cpu_max):
    cpu_max.write_text("10000 100000\n")  # --cpus 0.1
    assert runner.available_cpus() == 1


def test_default_workers_respects_quota(cpu_max):
    cpu_max.write_text("200000 100000\n")
    scenario = load_scenario("stress_100k")
    scenario.workers = None
    assert runner.default_workers(scenario, 100_000) <= 2
