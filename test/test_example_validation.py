from pathlib import Path
import unittest


from launch_config_validator.validate_launch_config import collect_files, validate_files

# run with python -m unittest discover test/


EXAMPLES_DIR = Path(__file__).parent / "examples"


def _collect_example_files(folder_name: str) -> tuple[Path, list[Path]]:
    base_dir = EXAMPLES_DIR / folder_name
    files = collect_files([str(base_dir)])
    return base_dir, files


class ExampleValidationTests(unittest.TestCase):
    def test_correct_examples_have_no_errors(self) -> None:
        base_dir, files = _collect_example_files("correct")
        self.assertTrue(files, f"Expected at least one example file in {base_dir}")

        result = validate_files(files, isolated_ci=True)

        self.assertEqual(set(), {path.resolve() for path in result.error_files})

    def test_incorrect_examples_report_errors(self) -> None:
        base_dir, files = _collect_example_files("incorrect")
        self.assertTrue(files, f"Expected at least one example file in {base_dir}")

        result = validate_files(files, isolated_ci=True)
        expected_error_files = {path.resolve() for path in files}

        self.assertEqual(
            expected_error_files, {path.resolve() for path in result.error_files}
        )


if __name__ == "__main__":
    unittest.main()
