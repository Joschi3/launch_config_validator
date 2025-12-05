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
    python3 validate_launch_config.py [--isolated-ci] path1 [path2 ...]
"""

import argparse
import sys
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional
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
    from ament_index_python.packages import (
        get_package_prefix,
        get_package_share_directory,
    )
except ImportError:
    get_package_share_directory = None  # type: ignore
    get_package_prefix = None  # type: ignore

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


# --- Duplicate-key-safe YAML loader ----------------------------------------


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys."""

    def construct_mapping(self, node, deep: bool = False):
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


def _parse_package_name(package_xml: Path) -> str:
    """Read the <name> tag from package.xml, falling back to folder name."""
    try:
        import xml.etree.ElementTree as ET

        tag = ET.parse(package_xml).find("name")
        if tag is not None and tag.text:
            return tag.text.strip()
    except Exception:  # noqa: BLE001
        pass
    return package_xml.parent.name


def _find_workspace_root(package_dir: Path) -> Optional[Path]:
    """Return workspace root if it has a src/ containing package_dir."""
    try:
        pkg_dir = package_dir.resolve()
    except OSError:
        return None
    for ancestor in (pkg_dir, *pkg_dir.parents):
        src = ancestor / "src"
        try:
            if src.is_dir() and pkg_dir.is_relative_to(src):
                return ancestor
        except Exception:
            continue
    return None


def _find_local_package_path(pkg: str, current_file: Path) -> Optional[Path]:
    """
    Locate a package in the current workspace without ament_index.
    Returns the package directory if found, otherwise None.
    """
    start = current_file.resolve()
    if start.is_file():
        start = start.parent

    # First, try to find the current package to determine workspace root
    candidate_pkg_dir: Optional[Path] = None
    for ancestor in (start, *start.parents):
        pkg_xml = ancestor / "package.xml"
        try:
            if pkg_xml.is_file():
                candidate_pkg_dir = ancestor
                break
        except OSError:
            continue

    search_root = start
    if candidate_pkg_dir:
        ws_root = _find_workspace_root(candidate_pkg_dir)
        if ws_root:
            search_root = ws_root / "src"
        else:
            search_root = candidate_pkg_dir.parent

    try:
        for xml in search_root.rglob("package.xml"):
            if "build" in xml.parts or "install" in xml.parts:
                continue
            try:
                name = _parse_package_name(xml)
            except Exception:
                name = xml.parent.name
            if name == pkg:
                return xml.parent.resolve()
    except OSError:
        return None

    return None


# --- Issue & file metadata --------------------------------------------------


@dataclass
class Issue:
    path: Path
    message: str
    kind: str = "error"  # could later support "warning"


@dataclass
class FileInfo:
    path: Path
    data: Any
    is_launch: bool
    contains_ros_params: bool
    is_param_config: bool = False  # filled in second pass


@dataclass
class ValidationResult:
    issues: list[Issue]
    num_launch: int
    num_config: int

    @property
    def error_files(self) -> set[Path]:
        return {issue.path for issue in self.issues if issue.kind == "error"}

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.kind == "error")

    @property
    def error_file_count(self) -> int:
        return len(self.error_files)


# --- JSON Schemas -----------------------------------------------------------

# Launch YAML schema
with open(
    Path(__file__).parent / "schemas" / "yaml_launch.json", encoding="utf-8"
) as f:
    LAUNCH_SCHEMA = json.load(f)

# Config YAML schema
with open(
    Path(__file__).parent / "schemas" / "yaml_config.json", encoding="utf-8"
) as f:
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


def _walk_values(obj: Any) -> list[str]:
    """Collect all scalar string values in a nested structure."""
    values: list[str] = []
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


def check_launch_substitutions(path: Path, data: Any) -> list[Issue]:
    """Ensure only allowed launch substitutions are used in string values."""
    issues: list[Issue] = []
    allowed_list = ", ".join(sorted(ALLOWED_LAUNCH_SUBSTITUTIONS))
    for value in _walk_values(data):
        if not isinstance(value, str):
            continue
        for match in SUBSTITUTION_RE.finditer(value):
            name = match.group(1)
            if name not in ALLOWED_LAUNCH_SUBSTITUTIONS:
                issues.append(
                    Issue(
                        path,
                        f"Unknown launch substitution '{name}' used. Allowed substitutions: {allowed_list}",
                    )
                )
    return issues


# --- $(find-pkg-share ...) resolution --------------------------------------


FIND_PKG_RE = re.compile(r"\$\(\s*(find-pkg-share|find-pkg-prefix)\s+([^)]+?)\s*\)")
DIRNAME_RE = re.compile(r"\$\(\s*dirname\s*\)")
SUBSTITUTION_RE = re.compile(r"\$\(\s*([A-Za-z0-9_-]+)")

ALLOWED_LAUNCH_SUBSTITUTIONS = {
    "find-pkg-share",
    "find-pkg-prefix",
    "command",
    "var",
    "env",
    "dirname",
    "eval",
    "anon",
}


def resolve_path_substitutions(
    expr: str, current_file: Path, isolated_ci: bool = False
) -> tuple[str, list[Issue]]:
    """
    Replace known path substitutions:
    - $(find-pkg-share pkg)
    - $(find-pkg-prefix pkg)
    - $(dirname)
    Returns (resolved_string, issues).

    If isolated_ci is True, failures to resolve packages do NOT produce issues.
    """
    issues: list[Issue] = []

    def repl_pkg(match: re.Match) -> str:
        kind = match.group(1)
        pkg = match.group(2).strip()
        resolver = (
            get_package_share_directory
            if kind == "find-pkg-share"
            else get_package_prefix
        )
        if "$" in pkg:
            return "$(var ...)"

        if resolver is None:
            fallback_path = _find_local_package_path(pkg, current_file)
            # ament_index not available; only use local package discovery
            return str(fallback_path) if fallback_path else match.group(0)

        try:
            path = resolver(pkg)
            return path
        except Exception:  # noqa: BLE001
            fallback_path = _find_local_package_path(pkg, current_file)
            if fallback_path:
                return str(fallback_path)
            if not isolated_ci:
                issues.append(
                    Issue(
                        current_file,
                        f"Cannot resolve package '{pkg}' in {kind}: ",
                    )
                )
            return match.group(0)

    resolved = FIND_PKG_RE.sub(repl_pkg, expr)

    if DIRNAME_RE.search(resolved):
        resolved = DIRNAME_RE.sub(str(current_file.parent), resolved)

    return resolved, issues


def looks_like_path(value: str) -> bool:
    if any(
        marker in value
        for marker in ("$(find-pkg-share", "$(find-pkg-prefix", "$(dirname)")
    ):
        return True
    if any(value.endswith(suf) for suf in [".yaml", ".yml"]):
        return True
    return False


def make_path_relative_to_file(resolved: str, current_file: Path) -> Path:
    p = Path(resolved)
    if p.is_absolute():
        return p
    return (current_file.parent / p).resolve()


SIMILARITY_THRESHOLD = 0.5


def _best_match_in_dir(
    directory: Path, target_name: str, require_dir: bool
) -> Optional[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    target_lower = target_name.lower()
    best_score = 0.0
    best_candidate: Optional[Path] = None
    for candidate in entries:
        try:
            if require_dir and not candidate.is_dir():
                continue
            if not require_dir and not candidate.is_file():
                continue
        except OSError:
            continue
        score = SequenceMatcher(None, target_lower, candidate.name.lower()).ratio()
        if score > best_score:
            best_score = score
            best_candidate = candidate
    if best_candidate and best_score >= SIMILARITY_THRESHOLD:
        return best_candidate
    return None


def suggest_similar_path(target: Path) -> Optional[Path]:
    missing_parts: list[str] = []
    current = target
    while True:
        if current.exists() and current.is_dir():
            break
        parent = current.parent
        if parent == current:
            return None
        missing_parts.append(current.name)
        current = parent
    if not missing_parts:
        return None
    missing_parts.reverse()
    candidate = current
    for idx, part in enumerate(missing_parts):
        if not candidate.is_dir():
            return None
        require_dir = idx < len(missing_parts) - 1
        match = _best_match_in_dir(candidate, part, require_dir)
        if match is None:
            return None
        candidate = match
    return candidate


# --- JSON Schema validation wrappers ----------------------------------------


def validate_with_schema(
    data: Any, schema: dict, path: Path, schema_name: str
) -> list[Issue]:
    issues: list[Issue] = []
    try:
        jsonschema_validate(instance=data, schema=schema)
    except ValidationError as e:
        msg = f"{schema_name} validation error: {e.message}"
        if e.path:
            msg += f" (at {list(e.path)})"
        issues.append(Issue(path, msg))
    return issues


# --- Launch traversal helpers ----------------------------------------------


def iter_launch_entries(data: Any):
    """
    Yield (entry, node_data, include_data) for each launch array item.
    """
    if not isinstance(data, dict):
        return
    launch_list = data.get("launch", [])
    if not isinstance(launch_list, list):
        return
    for entry in launch_list:
        if not isinstance(entry, dict):
            continue
        node_data = entry.get("node")
        include_data = entry.get("include")
        yield entry, node_data, include_data


# --- Semantic checks & reference collection ---------------------------------


def collect_config_references_from_launch(path: Path, data: Any) -> set[Path]:
    """
    Collect config file paths referenced from a launch file via node.param[].from.
    This is used only to classify config files (whether they're referenced at all).
    """
    refs: set[Path] = set()

    for _entry, node_data, _include_data in iter_launch_entries(data):
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
            resolved, _ = resolve_path_substitutions(from_value, path, isolated_ci=True)
            # For classification we don't care if resolution fails; we still
            # get a path relative to the launch file.
            if "$(var" in resolved or "$(" in resolved:
                continue
            cfg_path = make_path_relative_to_file(resolved, path)
            refs.add(cfg_path.resolve())

    return refs


def check_launch_semantics(path: Path, data: Any, isolated_ci: bool) -> list[Issue]:
    """
    Semantic checks for launch YAML:
    - included launch files exist
    - param.from files exist
    - only allowed launch substitutions are used

    When isolated_ci is True, missing file checks are skipped and
    find-pkg-share resolution failures do not produce errors.
    """
    issues: list[Issue] = []

    issues.extend(check_launch_substitutions(path, data))

    for _entry, node_data, include_data in iter_launch_entries(data):
        # include.file
        if isinstance(include_data, dict):
            file_value = include_data.get("file")
            if isinstance(file_value, str):
                resolved, extra = resolve_path_substitutions(
                    file_value, path, isolated_ci=isolated_ci
                )
                issues.extend(extra)

                # Skip if other substitutions remain (e.g. $(var ...))
                if "$(var" in resolved or "$(" in resolved:
                    continue

                inc_path = make_path_relative_to_file(resolved, path)
                if not inc_path.is_file() and not isolated_ci:
                    suggestion = suggest_similar_path(inc_path)
                    hint = f" (closest match: {suggestion})" if suggestion else ""
                    message = f"Included launch file does not exist: {inc_path}{hint}"
                    issues.append(Issue(path, message))

        # node.param.from
        if isinstance(node_data, dict):
            params = node_data.get("param")
            if isinstance(params, list):
                for param in params:
                    if not isinstance(param, dict):
                        continue
                    from_value = param.get("from")
                    if isinstance(from_value, str):
                        resolved, extra = resolve_path_substitutions(
                            from_value, path, isolated_ci=isolated_ci
                        )
                        issues.extend(extra)
                        if "$(var" in resolved or "$(" in resolved:
                            continue
                        cfg_path = make_path_relative_to_file(resolved, path)
                        if not cfg_path.is_file() and not isolated_ci:
                            suggestion = suggest_similar_path(cfg_path)
                            hint = (
                                f" (closest match: {suggestion})" if suggestion else ""
                            )
                            issues.append(
                                Issue(
                                    path,
                                    f"Parameter file does not exist: {cfg_path}{hint}",
                                )
                            )

    return issues


def check_config_semantics(path: Path, data: Any, isolated_ci: bool) -> list[Issue]:
    """
    Semantic checks for config YAML (only for files classified as ROS 2 param configs):
    - referenced files that look like YAML paths must exist

    When isolated_ci is True, missing file checks are skipped and
    find-pkg-share resolution failures do not produce errors.
    """
    issues: list[Issue] = []

    for value in _walk_values(data):
        if not looks_like_path(value):
            continue
        resolved, extra = resolve_path_substitutions(
            value, path, isolated_ci=isolated_ci
        )
        issues.extend(extra)

        # Skip if other substitutions remain (e.g. $(var ...))
        if "$(var" in resolved or "$(" in resolved:
            continue

        cfg_path = make_path_relative_to_file(resolved, path)
        if not cfg_path.is_file() and not isolated_ci:
            issues.append(Issue(path, f"Referenced file does not exist: {cfg_path}"))

    return issues


# --- File collection --------------------------------------------------------


SCAN_DIR_NAMES = ("launch", "test", "config", "configs")


def collect_files(paths: list[str]) -> list[Path]:
    result: list[Path] = []

    def is_launch_or_config_file(p: Path) -> bool:
        # either a parent (recursive) directory is a launch/config/configs/test directory
        return any(part in SCAN_DIR_NAMES for part in p.parts)

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


# --- Classification (first pass) -------------------------------------------


def classify_files(files: list[Path]) -> tuple[list[FileInfo], set[Path], list[Issue]]:
    """
    Load YAML, classify as launch/non-launch, record ros__parameters presence,
    and collect referenced config paths from launch files.
    """
    infos: list[FileInfo] = []
    referenced_config_paths: set[Path] = set()
    issues: list[Issue] = []

    for path in files:
        try:
            data = load_yaml(path)
        except Exception as e:  # noqa: BLE001
            issues.append(Issue(path, f"YAML syntax error: {e}"))
            continue

        if data is None:
            issues.append(Issue(path, "YAML file is empty"))
            continue

        is_launch = is_launch_file(path, data)
        info = FileInfo(
            path=path,
            data=data,
            is_launch=is_launch,
            contains_ros_params=contains_ros_parameters(data),
        )
        infos.append(info)

        if is_launch:
            referenced_config_paths |= collect_config_references_from_launch(path, data)

    return infos, referenced_config_paths, issues


# --- Main check logic (second pass) ----------------------------------------


def check_files(
    files: list[Path], isolated_ci: bool = False, verbose: bool = False
) -> int:
    if not files:
        print("No YAML files found.", file=sys.stderr)
        return 0

    if verbose:
        print(f"Checking {len(files)} YAML files:")
        for path in files:
            print(f"- {path}")
        print()

    result = validate_files(files, isolated_ci=isolated_ci)

    for issue in result.issues:
        print(f"{issue.path}: {issue.kind}: {issue.message}", file=sys.stderr)

    summary = (
        f"Checked {result.num_launch} launch files and {result.num_config} config files. "
        f"Found {result.error_count} errors in {result.error_file_count} files."
    )

    if result.error_count:
        print(f"{RED}{summary}{RESET}", file=sys.stderr)
    else:
        print(f"{GREEN}{summary}{RESET} All good!")

    return 1 if result.error_count else 0


def validate_files(files: list[Path], isolated_ci: bool = False) -> ValidationResult:
    """Run validation without printing, returning a result object for reuse in tests."""
    all_issues: list[Issue] = []

    # First pass: load + classify + collect references
    infos, referenced_config_paths, first_pass_issues = classify_files(files)
    all_issues.extend(first_pass_issues)

    # Second pass: schema + semantics + "config-folder classification" rule
    for info in infos:
        path = info.path
        data = info.data

        if info.is_launch:
            # Launch file
            all_issues.extend(
                validate_with_schema(data, LAUNCH_SCHEMA, path, "launch-schema")
            )
            all_issues.extend(check_launch_semantics(path, data, isolated_ci))
        else:
            # Non-launch YAML
            in_config_dir = is_in_config_dir(path)
            abs_path = path.resolve()

            # Decide if this should be treated as a ROS 2 parameter config
            info.is_param_config = in_config_dir and (
                info.contains_ros_params or abs_path in referenced_config_paths
            )

            if info.is_param_config:
                # This is a ROS 2 param config: enforce config schema + semantic path checks
                all_issues.extend(
                    validate_with_schema(data, CONFIG_SCHEMA, path, "config-schema")
                )
                all_issues.extend(check_config_semantics(path, data, isolated_ci))

    num_launch = sum(1 for info in infos if info.is_launch)
    num_config = sum(1 for info in infos if not info.is_launch)

    return ValidationResult(
        issues=all_issues,
        num_launch=num_launch,
        num_config=num_config,
    )


# --- CLI --------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> tuple[list[str], bool, bool]:
    parser = argparse.ArgumentParser(
        description="Validate ROS 2 YAML launch and config files using JSON Schema "
        "and semantic checks (file existence)."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="YAML files or directories containing YAML files",
    )
    parser.add_argument(
        "--isolated-ci",
        action="store_true",
        help=(
            "Skip missing-file errors and suppress errors from find-pkg-share "
            "resolution (useful when running in isolated CI without a full ROS setup)."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every file path being validated.",
    )
    args = parser.parse_args(argv)
    return args.paths, args.isolated_ci, args.verbose


def main(argv: Optional[list[str]] = None) -> int:
    paths, isolated_ci, verbose = parse_args(argv)
    files = collect_files(paths)
    return check_files(files, isolated_ci=isolated_ci, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())
