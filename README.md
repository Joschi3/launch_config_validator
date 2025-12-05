![CI](https://github.com/Joschi3/launch_config_validator/actions/workflows/unittests.yml/badge.svg)
![Lint](https://github.com/Joschi3/launch_config_validator/actions/workflows/lint.yml/badge.svg)
[![codecov](https://codecov.io/gh/Joschi3/launch_config_validator/branch/main/graph/badge.svg)](https://codecov.io/gh/Joschi3/launch_config_validator/)
# ROS 2 YAML Launch & Config Validator

This tool validates ROS 2 YAML **launch files** and **parameter configuration files**.
It performs both structural checks (YAML, schema) and semantic checks (package lookup, referenced file existence).

## What the Validator Checks

### 1. YAML Syntax & Structure

* Parses YAML using a duplicate-key–safe loader (errors on repeated keys).
* Validates **launch files** against `schemas/yaml_launch.json`.
* Validates **ROS parameter config files** against `schemas/yaml_config.json`.


### 2. Resolution of Referenced Files

The validator expands ROS-specific path expressions and verifies that the resulting files exist.

Supported patterns:

* **`$(find-pkg-share pkg)`**
  → resolved via `get_package_share_directory(pkg)` from `ament_index_python`

* **`$(find-pkg-prefix pkg)`**
  → resolved via `get_package_prefix(pkg)` from `ament_index_python`

* **`$(dirname)`** *(discouraged)*
  This expands to the directory of the *launch file currently being validated*.
  It should be avoided in ROS 2 YAML files in favor of `$(find-pkg-share ...)`.



### 3. Classification of ROS 2 Config Files

A YAML file is treated as a **valid ROS 2 parameter config** if **at least one** of the following holds:

1. It contains a top-level `ros__parameters` block.
2. It is referenced in a launch file via a `node.param[].from` entry.



### 4. Error Handling

If any issue is detected—invalid YAML structure, schema violations, unresolved substitutions, missing files—the validator:

* prints a concise, human-readable error report,
* exits with status **1**.


## Usage

Run on one or more files/directories:

```bash
python3 validate_launch_config.py [--isolated-ci] path1 [path2 ...]
````

Examples:

```bash
# Validate all launch/config YAMLs under a package
python3 validate_launch_config.py src/athena_launch

# Validate a single file
python3 validate_launch_config.py src/athena_launch/launch/self_filter_container.launch.yaml
```

The script recursively scans for `*.yml` / `*.yaml` files below directories whose path contains one of:

* `launch`
* `config`
* `configs`
* `test`

### Isolated CI mode

In some CI jobs the full ROS environment (and all dependent packages) is not available.
Use `--isolated-ci` to **avoid failing on missing files and unresolved packages**:

```bash
python3 validate_launch_config.py --isolated-ci src/
```

Use `--verbose` to print every file path being validated:

```bash
python3 validate_launch_config.py --verbose src/athena_launch
```

In this mode:

* Failures to resolve `$(find-pkg-share ...)` **do not produce errors**.
* Missing referenced launch/config YAML files (includes, `param.from`, etc.) **do not produce errors**.
* YAML parsing and JSON Schema validation still run as usual.

---

## Pre-Commit Hook Integration

The repository includes a pre-commit hook that runs this validator automatically on changed YAML files.

`.pre-commit-config.yaml` snippet:

```yaml
  # YAML launch and config files
  - repo: https://github.com/Joschi3/launch_config_validator.git
    rev: v0.1.2
    hooks:
      - id: format-yaml-launch-and-configs
        name: Validate ROS2 launch and config YAML files
```

### How it works

* `entry: launch-config-validator` should point to a console script / wrapper that calls `validate_launch_config.py`.
* On `git commit`, pre-commit runs the validator on all staged `*.yml` / `*.yaml` files.
* If the validator reports any errors (exit code `1`), the commit is **rejected** and the errors are shown in the terminal.

### Enabling the hook

Once `.pre-commit-config.yaml` is present:

```bash
pip install pre-commit          # if not already installed
pre-commit install              # install hooks into .git/hooks
```

After that, every commit will automatically validate launch & config YAMLs, catching:

* YAML syntax errors / duplicate keys
* Schema violations
* Missing included launch/param files (unless `--isolated-ci` is used in the wrapper)

before the changes land in the repo.

---

## YAML Launch File Syntax (Jazzy-style)

Launch files are YAML equivalents of Python launch descriptions.

**Top-level structure**

```yaml
launch:
  - arg: ...
  - let: ...
  - include: ...
  - node: ...
  - group: ...
  - push_ros_namespace: ...
  - set_remap: ...
```

Each list entry is exactly one of these actions.

### 1. Arguments (`arg`)

```yaml
- arg:
    name: "namespace"
    default: "/athena"
    description: "Namespace for all nodes"
```

* Required: `name`
* Optional: `default`, `description`, `if`, `unless`

### 2. Variables (`let`)

```yaml
- let:
    name: "camera_name_fixed"
    value: "$(var camera_name)"
```

* Required: `name`, `value`
* Optional: `if`, `unless`
* Variables can be used with `$(var name)` in later actions.

### 3. Nodes (`node`)

```yaml
- node:
    pkg: "controller_manager"
    exec: "spawner"
    name: "joint_state_broadcaster_spawner"
    namespace: "/athena"
    output: "screen"
    respawn: "true"
    respawn_delay: 5.0        # number or string
    if: "$(var start_controllers)"

    args:
      - "joint_state_broadcaster"
      - "--inactive"

    param:
      - from: "$(find-pkg-share my_pkg)/config/controller.yaml"
        allow_substs: true
      - name: "use_sim_time"
        value: true

    remap:
      - from: "/tf"
        to: "/athena/tf"
      - from: "/tf_static"
        to: "/athena/tf_static"
```

**Supported fields inside `node`:**

* `pkg` (string, required)

* `exec` (string, required)

* `name`, `namespace`, `output` (strings)

* `respawn` (string, e.g. `"true"`)

* `respawn_delay` (number or string)

* `if`, `unless` (string, launch condition expressions)

* `args`:

  * string (single command line) **or**
  * list of strings

* `param`:

  * list of:

    * string (e.g. `my_pkg/config.yaml`) **or**
    * object with arbitrary keys (e.g. `{name, value, from, allow_substs, ...}`)

* `remap`:

  * list of:

    ```yaml
    - from: "old_topic"
      to: "new_topic"
    ```

### 4. Includes (`include`)

```yaml
- include:
    file: "$(find-pkg-share athena_launch)/launch/other.launch.yaml"
    arg:
      - name: "namespace"
        value: "$(var namespace)"
    if: "$(var enable_other)"
```

* Required: `file`
* Optional: `arg` (list of `{name, value}`), `if`, `unless`

### 5. Groups (`group`)

```yaml
- group:
    namespace: "$(var namespace)"
    if: "$(var enable_mapping)"
    actions:
      - node: ...
      - include: ...
```

* Required: `actions` (list of other launch actions)
* Optional: `namespace`, `if`, `unless`

### 6. Namespace and remap actions

```yaml
- push_ros_namespace:
    namespace: "$(var namespace)"
    if: "$(var use_namespace)"

- set_remap:
    from: "/tf"
    to: "/athena/tf"
    unless: "$(var use_global_tf)"
```

### Supported launch substitutions

In ros2 the following substitutions are currently supported:

| Substitution | Description | Example |
| --- | --- | --- |
| `$(find-pkg-share pkg)` | Path to a package's share directory. | `$(find-pkg-share my_robot)/launch/main.launch.yaml` |
| `$(find-pkg-prefix pkg)` | Path to a package's install root. | `$(find-pkg-prefix my_robot)/bin/tool` |
| `$(command 'cmd')` | Execute a shell command; replaced with stdout. | `$(command 'xacro $(dirname)/urdf/robot.urdf.xacro')` |
| `$(var name)` | Value of a declared launch argument. | `$(var use_sim_time)` |
| `$(env name)` | Environment variable (must exist). | `$(env HOSTNAME)` |
| `$(env name default)` | Environment variable with a fallback. | `$(env ROBOT_TYPE standard)` |
| `$(dirname)` | Directory of the current launch file. | `$(dirname)/config/params.yaml` |
| `$(eval 'expr')` | Python expression evaluation. | `$(eval '2 * 3.14')` |
| `$(anon name)` | Generates a unique name. | `$(anon my_node)` |

Any other substitution will cause the launch process to fail.

---

## YAML Parameter Config Syntax

Parameter files are standard ROS 2 YAML parameter configs.

**Single node:**

```yaml
my_node_name:
  ros__parameters:
    use_sim_time: true
    update_rate_hz: 50.0
    frame_id: "base_link"
```

**Multiple nodes in one file:**

```yaml
node_a:
  ros__parameters:
    some_param: 1

node_b:
  ros__parameters:
    some_param: 2
```

The validator treats a YAML file under `config/` or `configs/` as a **parameter config** if:

* it contains at least one `ros__parameters` key, **or**
* it is referenced from a launch file via:

```yaml
- node:
    ...
    param:
      - from: "$(find-pkg-share my_pkg)/config/my_params.yaml"
```

For those files, the tool applies `yaml_config.json` and (in normal mode) checks that any referenced YAML-like paths exist (after resolving `$(find-pkg-share ...)` when possible).
