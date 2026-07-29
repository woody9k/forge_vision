import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_vision.server.runtime import Runtime  # noqa: E402


@pytest.fixture
def runtime(tmp_path):
    return Runtime(data_dir=str(tmp_path / "data"))


def complete_checklist(runtime):
    """Confirm every required pre-transmit check (FR-SAF-009)."""
    for item in runtime.safety.checklist:
        if item["required"]:
            runtime.safety.confirm_checklist_item(item["id"], True)


@pytest.fixture
def armed_runtime(runtime):
    """Runtime with a connected sim device and armed TX interlock."""
    runtime.connect("sim-pluto-0")
    complete_checklist(runtime)
    runtime.safety.arm("test-operator", "bench, cabled, attenuated — authorized")
    return runtime
