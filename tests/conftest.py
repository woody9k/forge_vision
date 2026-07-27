import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_vision.server.runtime import Runtime  # noqa: E402


@pytest.fixture
def runtime(tmp_path):
    return Runtime(data_dir=str(tmp_path / "data"))


@pytest.fixture
def armed_runtime(runtime):
    """Runtime with a connected sim device and armed TX interlock."""
    runtime.connect("sim-pluto-0")
    runtime.safety.arm("test-operator", "bench, cabled, attenuated — authorized")
    return runtime
