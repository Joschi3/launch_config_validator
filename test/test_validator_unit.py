import json
import tempfile
from pathlib import Path
from unittest import mock, TestCase

import yaml

import launch_config_validator.validate_launch_config as val


class ValidatorUnitTests(TestCase):
    def test_duplicate_keys_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dup = Path(tmp) / "dup.yaml"
            dup.write_text("a: 1\na: 2\n", encoding="utf-8")
            with self.assertRaises(yaml.constructor.ConstructorError):
                val.load_yaml(dup)

    def test_resolve_path_substitutions_missing_pkg_resolver(self) -> None:
        dummy = Path("dummy.yaml")
        with (
            mock.patch.object(val, "get_package_share_directory", None),
            mock.patch.object(val, "get_package_prefix", None),
        ):
            resolved, issues = val.resolve_path_substitutions(
                "$(find-pkg-share demo_pkg)", dummy, isolated_ci=False
            )
        self.assertEqual("$(find-pkg-share demo_pkg)", resolved)
        self.assertEqual(1, len(issues))
        self.assertIn("ament_index_python not available", issues[0].message)

    def test_resolve_path_substitutions_with_failure_and_var(self) -> None:
        def failing_resolver(_: str) -> str:
            raise RuntimeError("boom")

        dummy = Path("dummy.yaml")
        with (
            mock.patch.object(val, "get_package_share_directory", failing_resolver),
            mock.patch.object(val, "get_package_prefix", failing_resolver),
        ):
            resolved, issues = val.resolve_path_substitutions(
                "$(find-pkg-prefix badpkg)", dummy, isolated_ci=False
            )
            var_resolved, var_issues = val.resolve_path_substitutions(
                "$(find-pkg-share $(var my_pkg))", dummy, isolated_ci=False
            )

        self.assertEqual("$(find-pkg-prefix badpkg)", resolved)
        self.assertEqual(1, len(issues))
        self.assertIn("Cannot resolve package 'badpkg'", issues[0].message)
        self.assertTrue(var_resolved.startswith("$(var ..."))
        self.assertFalse(var_issues)

    def test_suggest_similar_path_and_missing_file_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            launch_dir = base / "launch"
            launch_dir.mkdir()
            (launch_dir / "foo_found.launch.yaml").write_text(
                "launch: []", encoding="utf-8"
            )
            main_path = launch_dir / "main.launch.yaml"
            main_path.write_text("launch: []", encoding="utf-8")

            data = {
                "launch": [
                    {"include": {"file": "foo_missing.launch.yaml"}},
                    {
                        "node": {
                            "pkg": "demo_pkg",
                            "exec": "exec",
                            "param": [{"from": "config/missing.yaml"}],
                        }
                    },
                ]
            }

            issues = val.check_launch_semantics(main_path, data, isolated_ci=False)
            self.assertTrue(any("closest match" in issue.message for issue in issues))
            self.assertTrue(
                any(
                    "Parameter file does not exist" in issue.message for issue in issues
                )
            )

            suggestion = val.suggest_similar_path(
                launch_dir / "foo_missing.launch.yaml"
            )
            self.assertEqual(launch_dir / "foo_found.launch.yaml", suggestion)

    def test_check_launch_substitutions_allows_known_names(self) -> None:
        data = {
            "launch": [
                {
                    "include": {
                        "file": "$(find-pkg-share demo)/launch/child.launch.yaml"
                    }
                },
                {
                    "node": {
                        "pkg": "demo",
                        "exec": "node",
                        "param": [{"from": "$(dirname)/cfg.yaml"}],
                    }
                },
            ]
        }
        issues = val.check_launch_substitutions(Path("main.launch.yaml"), data)
        self.assertFalse(issues)

    def test_check_config_semantics_reports_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(json.dumps({"a": "missing.yaml"}), encoding="utf-8")
            issues = val.check_config_semantics(
                cfg, {"a": "missing.yaml"}, isolated_ci=False
            )
            self.assertEqual(1, len(issues))
            self.assertIn("Referenced file does not exist", issues[0].message)

        issues = val.check_config_semantics(
            Path("dummy"), {"a": "$(var cfg)"}, isolated_ci=False
        )
        self.assertFalse(issues)

    def test_classify_files_and_empty_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            empty = config_dir / "empty.yaml"
            empty.write_text("", encoding="utf-8")
            files = [empty]
            infos, refs, issues = val.classify_files(files)

            self.assertEqual(0, len(refs))
            self.assertEqual(1, len(issues))
            self.assertIn("YAML file is empty", issues[0].message)
            self.assertFalse(infos)  # empty file is skipped from infos

    def test_classify_files_with_invalid_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            bad = config_dir / "bad.yaml"
            bad.write_text("a: [", encoding="utf-8")
            infos, refs, issues = val.classify_files([bad])
            self.assertFalse(infos)
            self.assertFalse(refs)
            self.assertEqual(1, len(issues))
            self.assertIn("YAML syntax error", issues[0].message)

    def test_is_config_file_detection(self) -> None:
        launch_path = Path("foo.launch.yaml")
        config_path = Path("params.yaml")
        self.assertFalse(val.is_config_file(launch_path, {"launch": []}))
        self.assertTrue(val.is_config_file(config_path, {"some": "data"}))

    def test_collect_files_filters_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "launch").mkdir()
            (root / "other").mkdir()
            yaml_a = root / "config" / "a.yaml"
            yaml_b = root / "launch" / "b.yml"
            ignored = root / "other" / "c.yaml"
            yaml_a.write_text("{}", encoding="utf-8")
            yaml_b.write_text("{}", encoding="utf-8")
            ignored.write_text("{}", encoding="utf-8")

            collected = val.collect_files([str(root)])
            self.assertEqual({yaml_a, yaml_b}, set(collected))

            # Passing a single file path exercises the file branch.
            collected_single = val.collect_files([str(yaml_a)])
            self.assertEqual([yaml_a], collected_single)

    def test_collect_config_references_from_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launch_file = Path(tmp) / "launch" / "main.launch.yaml"
            launch_file.parent.mkdir()
            launch_file.write_text("launch: []", encoding="utf-8")

            data = {
                "launch": [
                    {
                        "node": {
                            "pkg": "demo_pkg",
                            "exec": "demo",
                            "param": [{"from": "../config/params.yaml"}],
                        }
                    }
                ]
            }
            refs = val.collect_config_references_from_launch(launch_file, data)
            expected = (launch_file.parent / "../config/params.yaml").resolve()
            self.assertEqual({expected}, refs)

    def test_make_path_relative_to_file_absolute_passthrough(self) -> None:
        absolute = Path("/tmp/example.yaml")
        current = Path("/tmp/current/launch.yaml")
        self.assertEqual(
            absolute, val.make_path_relative_to_file(str(absolute), current)
        )

    def test_iter_launch_entries_handles_non_launch(self) -> None:
        data = {"launch": ["not-a-dict", {"node": {"pkg": "a", "exec": "b"}}]}
        entries = list(val.iter_launch_entries(data))
        self.assertEqual(1, len(entries))
        self.assertEqual({"pkg": "a", "exec": "b"}, entries[0][1])

    def test_iter_launch_entries_with_non_dict_input(self) -> None:
        self.assertEqual([], list(val.iter_launch_entries("not-dict")))
        self.assertEqual([], list(val.iter_launch_entries({"launch": {"not": "list"}})))

    def test_validate_with_schema_error_path_added(self) -> None:
        issues = val.validate_with_schema(
            data={"launch": "not-a-list"},
            schema=val.LAUNCH_SCHEMA,
            path=Path("bad.launch.yaml"),
            schema_name="launch-schema",
        )
        self.assertEqual(1, len(issues))
        self.assertIn("at ['launch']", issues[0].message)

    def test_check_files_no_input(self) -> None:
        exit_code = val.check_files([], verbose=True)
        self.assertEqual(0, exit_code)

    def test_check_files_success_and_failure(self) -> None:
        ok_files = val.collect_files(["test/examples/correct"])
        self.assertEqual(0, val.check_files(ok_files, isolated_ci=True, verbose=True))

        bad_files = val.collect_files(["test/examples/incorrect"])
        self.assertEqual(1, val.check_files(bad_files, isolated_ci=True, verbose=False))

    def test_parse_args_and_main_roundtrip(self) -> None:
        paths, isolated_ci, verbose = val.parse_args(
            ["foo", "--isolated-ci", "--verbose"]
        )
        self.assertEqual(["foo"], paths)
        self.assertTrue(isolated_ci)
        self.assertTrue(verbose)

        exit_code = val.main(["--isolated-ci", "test/examples/correct"])
        self.assertEqual(0, exit_code)
