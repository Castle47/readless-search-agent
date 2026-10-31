"""Overlay approved input ceilings on the running image's own defaults."""

import argparse
from copy import deepcopy
from pathlib import Path

import yaml

from research_agent.config import validate_defaults


def apply_limits(defaults, overrides):
    if not isinstance(overrides, dict) or set(overrides) != {"understand", "planner"}:
        raise ValueError("Expected only understand and planner input ceilings")
    result = deepcopy(defaults)
    for role, value in overrides.items():
        if type(value) is not int or value <= 0:
            raise ValueError("Input ceiling must be a positive integer")
        result["model"]["roles"][role]["max_input"] = value
    validate_defaults(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path("/app/configs/default.yaml"))
    parser.add_argument("--limits", type=Path, default=Path("/run/readless-input-limits/limits.yaml"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/readless-defaults.yaml"))
    args = parser.parse_args()
    value = apply_limits(yaml.safe_load(args.base.read_text()), yaml.safe_load(args.limits.read_text()))
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
