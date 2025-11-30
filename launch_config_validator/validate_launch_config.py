#!/usr/bin/env python3
"""
ROS 2 YAML validator for launch & config files.

Features:
- YAML parsing with duplicate-key detection
- JSON Schema validation (shape / required fields)
- Semantic checks:
  - resolve $(find-pkg-share pkg)
  - verify included launch/config/BT files exist
- Extra rule for YAMLs in config/configs:
  - Each YAML in a config/ or configs/ folder must either
    * contain ros__parameters (i.e., be a ROS 2 parameter config), or
    * be referenced from some launch file via node.param[].from

Usage:
    python3 ros2_yaml_validator.py path1 [path2 ...]
"""

import argparse
import sys
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Tuple, Dict, Set
import json

# --- Dependencies -----------------------------------------------------------

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

try:
    from jsonschema import validate as jsonschema_validate, ValidationError
except ImportError:
    print("ERROR: jsonschema is required (pip install jsonschema)", file=sys.stderr)
    sys.exit(2)

try:
    from ament_index_python.packages import get_package_share_directory
except ImportError:
    get_package_share_directory = None  # type: ignore


# --- Duplicate-key-safe YAML loader ----------------------------------------


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key ({key!r})",
                    key_node.start_mark,
                )
            value = self.construct_object(value_node, deep=deep)
            mapping[key] = value
        return mapping


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f, Loader=UniqueKeyLoader)


# --- Issue representation ---------------------------------------------------


@dataclass
class Issue:
    path: Path
    message: str
    kind: str = "error"  # could later support "warning"


# --- JSON Schemas -----------------------------------------------------------

# Launch YAML schema
with open(Path(__file__).parent / "schemas" / "yaml_launch.json", "r", encoding="utf-8") as f:
    LAUNCH_SCHEMA = json.load(f)

# Config YAML schema
with open(Path(__file__).parent / "schemas" / "yaml_config.json", "r", encoding="utf-8") as f:
    CONFIG_SCHEMA = json.load(f)

# --- Helpers for classification & traversal ---------------------------------


def is_launch_data(data: Any) -> bool:
    return isinstance(data, dict) and "launch" in data


def is_launch_file(path: Path, data: Any) -> bool:
    # Heuristic: name contains "launch" or content has "launch" key
    if "launch" in path.name:
        return True
    return is_launch_data(data)


def is_config_file(path: Path, data: Any) -> bool:
    # Anything YAML that is not a launch file we treat as "config-like"
    return not is_launch_file(path, data)


def is_in_config_dir(path: Path) -> bool:
    """True if any part of the path is 'config' or 'configs'."""
    return any(part in ("config", "configs") for part in path.parts)


def _walk_values(obj: Any) -> List[str]:
    """Collect all scalar string values in a nested structure."""
    values: List[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            values.extend(_walk_values(v))
    elif isinstance(obj, list):
        for v in obj:
            values.extend(_walk_values(v))
    elif isinstance(obj, str):
        values.append(obj)
    return values


def contains_ros_parameters(obj: Any) -> bool:
    """Return True if any mapping in the structure has a 'ros__parameters' key."""
    if isinstance(obj, dict):
        if "ros__parameters" in obj:
            return True
        return any(contains_ros_parameters(v) for v in obj.values())
    if isinstance(obj, list):
        return any(contains_ros_parameters(v) for v in obj)
    return False


# --- $(find-pkg-share ...) resolution --------------------------------------


FIND_PKG_SHARE_RE = re.compile(r"\$\(\s*find-pkg-share\s+([^)]+?)\s*\)")


def resolve_find_pkg_share(expr: str, current_file: Path) -> Tuple[str, List[Issue]]:
    """
    Replace $(find-pkg-share pkg_name) with the actual share directory.
    Returns (resolved_string, issues).
    """
    issues: List[Issue] = []

    def repl(match: re.Match) -> str:
        pkg = match.group(1).strip()
        if get_package_share_directory is None:
            issues.append(
                Issue(
                    current_file,
                    f"Cannot resolve find-pkg-share('{pkg}'): "
                    "ament_index_python not available (ROS env not sourced?)",
                )
            )
            # keep original; caller will see unresolved '$(' and skip existence check
            return match.group(0)
        try:
            share = get_package_share_directory(pkg)
        except Exception as e:  # noqa: BLE001
            issues.append(
                Issue(
                    current_file,
                    f"Cannot resolve package '{pkg}' in find-pkg-share: {e}",
                )
            )
            return match.group(0)
        return share

    resolved = FIND_PKG_SHARE_RE.sub(repl, expr)
    return resolved, issues


def looks_like_path(value: str) -> bool:
    if "$(find-pkg-share" in value:
        return True
    if any(value.endswith(suf) for suf in [".yaml", ".yml"]):
        return True
    return False


def make_path_relative_to_file(resolved: str, current_file: Path) -> Path:
    p = Path(resolved)
    if p.is_absolute():
        return p
    return (current_file.parent / p).resolve()


# --- JSON Schema validation wrappers ----------------------------------------


def validate_with_schema(
    data: Any, schema: dict, path: Path, schema_name: str
) -> List[Issue]:
    issues: List[Issue] = []
    try:
        jsonschema_validate(instance=data, schema=schema)
    except ValidationError as e:
        msg = f"{schema_name} validation error: {e.message}"
        if e.path:
            msg += f" (at {list(e.path)})"
        issues.append(Issue(path, msg))
    return issues


# --- Semantic checks & reference collection ---------------------------------


def collect_config_references_from_launch(path: Path, data: Any) -> Set[Path]:
    """
    Collect config file paths referenced from a launch file via node.param[].from.
    This is used only to classify config files (whether they're referenced at all).
    """
    refs: Set[Path] = set()
    launch_list = data.get("launch", [])
    if not isinstance(launch_list, list):
        return refs

    for entry in launch_list:
        if not isinstance(entry, dict):
            continue
        node_data = entry.get("node")
        if not isinstance(node_data, dict):
            continue
        params = node_data.get("param")
        if not isinstance(params, list):
            continue

        for param in params:
            if not isinstance(param, dict):
                continue
            from_value = param.get("from")
            if not isinstance(from_value, str):
                continue
            resolved, _ = resolve_find_pkg_share(from_value, path)
            # For classification we don't care if resolution fails; we still
            # get a path relative to the launch file.
            if "$(var" in resolved or "$(" in resolved:
                continue
            cfg_path = make_path_relative_to_file(resolved, path)
            refs.add(cfg_path.resolve())

    return refs


def check_launch_semantics(path: Path, data: Any) -> List[Issue]:
    """
    Semantic checks for launch YAML:
    - included launch files exist
    - param.from files exist
    """
    issues: List[Issue] = []
    launch_list = data.get("launch", [])
    if not isinstance(launch_list, list):
        return issues

    for entry in launch_list:
        if not isinstance(entry, dict):
            continue

        # include.file
        include_data = entry.get("include")
        if isinstance(include_data, dict):
            file_value = include_data.get("file")
            if isinstance(file_value, str):
                resolved, extra = resolve_find_pkg_share(file_value, path)
                issues.extend(extra)

                # Skip if other substitutions remain (e.g. $(var ...))
                if "$(var" in resolved or "$(" in resolved:
                    continue

                inc_path = make_path_relative_to_file(resolved, path)
                if not inc_path.is_file():
                    issues.append(
                        Issue(
                            path,
                            f"Included launch file does not exist: {inc_path}",
                        )
                    )

        # node.param.from
        node_data = entry.get("node")
        if isinstance(node_data, dict):
            params = node_data.get("param")
            if isinstance(params, list):
                for param in params:
                    if not isinstance(param, dict):
                        continue
                    from_value = param.get("from")
                    if isinstance(from_value, str):
                        resolved, extra = resolve_find_pkg_share(from_value, path)
                        issues.extend(extra)
                        if "$(var" in resolved or "$(" in resolved:
                            continue
                        cfg_path = make_path_relative_to_file(resolved, path)
                        if not cfg_path.is_file():
                            issues.append(
                                Issue(
                                    path,
                                    f"Parameter file does not exist: {cfg_path}",
                                )
                            )

    return issues


def check_config_semantics(path: Path, data: Any) -> List[Issue]:
    """
    Semantic checks for config YAML (only for files classified as ROS 2 param configs):
    - referenced files that look like YAML paths must exist
    """
    issues: List[Issue] = []

    for value in _walk_values(data):
        if not looks_like_path(value):
            continue
        resolved, extra = resolve_find_pkg_share(value, path)
        issues.extend(extra)

        # Skip if other substitutions remain (e.g. $(var ...))
        if "$(var" in resolved or "$(" in resolved:
            continue

        cfg_path = make_path_relative_to_file(resolved, path)
        if not cfg_path.is_file():
            issues.append(
                Issue(path, f"Referenced file does not exist: {cfg_path}")
            )

    return issues


# --- File collection --------------------------------------------------


def collect_files(paths: List[str]) -> List[Path]:
    result: List[Path] = []

    def is_launch_or_config_file(p: Path) -> bool:
        # either a parent (recursive) directory is a launch/config/configs/test directory
        for part in p.parts:
            if part in ("launch", "test", "config", "configs"):
                return True
        return False

    for p in paths:
        path = Path(p)
        if path.is_dir():
            for sub in path.rglob("*.yaml"):
                if is_launch_or_config_file(sub):
                    result.append(sub)
            for sub in path.rglob("*.yml"):
                if is_launch_or_config_file(sub):
                    result.append(sub)
        elif path.is_file():
            if is_launch_or_config_file(path):
                result.append(path)
    # De-duplicate
    return sorted(set(result))


# --- Main check logic (two-pass) -------------------------------------------


def check_files(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Validate ROS 2 YAML launch and config files using JSON Schema "
        "and semantic checks (file existence)."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="YAML files or directories containing YAML files",
    )
    args = parser.parse_args(argv)

    files = collect_files(args.paths)
    if not files:
        print("No YAML files found.", file=sys.stderr)
        return 0

    all_issues: List[Issue] = []

    # First pass: load + classify + collect references
    file_data: Dict[Path, Any] = {}
    file_is_launch: Dict[Path, bool] = {}
    file_contains_ros_params: Dict[Path, bool] = {}
    referenced_config_paths: Set[Path] = set()

    for path in files:
        try:
            data = load_yaml(path)
        except Exception as e:  # noqa: BLE001
            all_issues.append(Issue(path, f"YAML syntax error: {e}"))
            continue

        if data is None:
            all_issues.append(Issue(path, "YAML file is empty"))
            continue

        file_data[path] = data
        is_launch = is_launch_file(path, data)
        file_is_launch[path] = is_launch
        file_contains_ros_params[path] = contains_ros_parameters(data)

        if is_launch:
            referenced_config_paths |= collect_config_references_from_launch(path, data)

    # Second pass: schema + semantics + "config-folder classification" rule
    for path in files:
        data = file_data.get(path)
        if data is None:
            # Already had syntax/empty issue in pass 1
            continue

        if file_is_launch.get(path, False):
            # Launch file
            all_issues.extend(
                validate_with_schema(data, LAUNCH_SCHEMA, path, "launch-schema")
            )
            all_issues.extend(check_launch_semantics(path, data))
        else:
            # Non-launch YAML
            in_config_dir = is_in_config_dir(path)
            abs_path = path.resolve()

            # Decide if this should be treated as a ROS 2 parameter config
            is_param_config = in_config_dir and (
                file_contains_ros_params.get(path, False)
                or abs_path in referenced_config_paths
            )

            if is_param_config:
                # This is a ROS 2 param config: enforce config schema + semantic path checks
                all_issues.extend(
                    validate_with_schema(data, CONFIG_SCHEMA, path, "config-schema")
                )
                all_issues.extend(check_config_semantics(path, data))

    for issue in all_issues:
        print(f"{issue.path}: {issue.kind}: {issue.message}", file=sys.stderr)

    return 1 if any(i.kind == "error" for i in all_issues) else 0


def parse_args() -> List[str]:
    parser = argparse.ArgumentParser(
        description="Validate ROS 2 YAML launch and config files using JSON Schema "
        "and semantic checks (file existence)."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="YAML files or directories containing YAML files",
    )
    args = parser.parse_args()
    return args.paths


def main() -> int:
    # paths = parse_args()
    paths = ["/home/aljoscha-schmidt/hector/src/athena_launch"]
    return check_files(paths)


if __name__ == "__main__":
    raise SystemExit(main())
