# ROS 2 YAML Launch & Config Validator

This tool validates ROS 2 YAML **launch files** and **parameter configs**.

It checks:

- **YAML syntax**
  - using jsonschema to validate the yaml syntax of launch and confg files
- **Semantic rules**:
  - Resolves `$(find-pkg-share pkg)` using `ament_index_python`.
  - Verifies that included launch/config files actually exist.

If any error is found, the script exits with status `1` and prints a human-readable message.


## Usage

Run on one or more files/directories:

```bash
python3 validate_launch_config.py path1 [path2 ...]
```

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

## Pre-Commit Hook Integration

The repository includes a pre-commit hook that runs this validator automatically on changed YAML files.

`.pre-commit-config.yaml` snippet:

```yaml
-   id: format-yaml-launch-and-configs
    name: Format YAML launch and config files
    description: Format YAML launch and config files
    entry: launch-config-validator
    language: python
    files: \.(ya?ml)$
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
* Missing included launch/param files

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

For those files, the tool applies `yaml_config.json` and checks that any referenced YAML-like paths exist (after resolving `$(find-pkg-share ...)` when possible).
