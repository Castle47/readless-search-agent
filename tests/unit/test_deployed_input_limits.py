from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
import yaml


spec = importlib.util.spec_from_file_location(
    "input_limits_overlay", Path(__file__).parents[2] / "deploy/input-limits/apply.py"
)
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


def test_input_overlay_preserves_the_deployed_versions_other_settings(settings):
    base = settings.defaults()
    base["model"]["roles"]["understand"]["max_input"] = 3000
    base["model"]["roles"]["planner"]["max_input"] = 4000
    before = deepcopy(base)
    result = overlay.apply_limits(base, {"understand": 6000, "planner": 8000})
    expected = deepcopy(base)
    expected["model"]["roles"]["understand"]["max_input"] = 6000
    expected["model"]["roles"]["planner"]["max_input"] = 8000
    assert result == expected and base == before
    assert result["model"]["finalization_split"] == before["model"]["finalization_split"]
    assert result["research"]["total_tokens"] == before["research"]["total_tokens"]


@pytest.mark.parametrize(
    "bad", [{"writer": 24000}, {"understand": True, "planner": 8000}, {"understand": 6000, "planner": -1}]
)
def test_overlay_rejects_unapproved_roles_and_invalid_values(settings, bad):
    with pytest.raises(ValueError):
        overlay.apply_limits(settings.defaults(), bad)


def test_runtime_overlay_and_development_defaults_use_the_same_approved_caps():
    root = Path(__file__).parents[2]
    limits = yaml.safe_load((root / "deploy/input-limits/limits.yaml").read_text())
    defaults = yaml.safe_load((root / "configs/default.yaml").read_text())
    assert limits == {"understand": 6000, "planner": 8000}
    assert all(defaults["model"]["roles"][role]["max_input"] == value for role, value in limits.items())
