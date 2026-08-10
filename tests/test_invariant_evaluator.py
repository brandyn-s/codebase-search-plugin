import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "invariant_evaluator.py"
spec = importlib.util.spec_from_file_location("invariant_evaluator", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def _bundle(subjects):
    return {
        "schema_version": 1,
        "invariant": {
            "id": "SEC-AUTH-001",
            "description": "All administrative routes require authorization.",
            "assertion": "all_subjects_satisfy",
        },
        "subjects": subjects,
    }


class InvariantEvaluatorTests(unittest.TestCase):
    def test_all_subjects_pass(self):
        result = module.evaluate(
            _bundle(
                [
                    {
                        "id": "route:/admin/users",
                        "status": "pass",
                        "observation_ids": ["obs:v1:" + "a" * 64],
                    },
                    {
                        "id": "route:/admin/config",
                        "status": "pass",
                        "observation_ids": ["obs:v1:" + "b" * 64],
                    },
                ]
            )
        )
        self.assertEqual(
            result["invariant"],
            {
                "id": "SEC-AUTH-001",
                "status": "pass",
                "checked": 2,
                "violations": 0,
                "unresolved": 0,
            },
        )
        self.assertEqual(
            len(result["details"]["supporting_observation_ids"]),
            2,
        )

    def test_any_counterexample_fails(self):
        result = module.evaluate(
            _bundle(
                [
                    {
                        "id": "route:/admin/users",
                        "status": "pass",
                        "observation_ids": ["obs:v1:" + "a" * 64],
                    },
                    {
                        "id": "route:/admin/debug",
                        "status": "fail",
                        "observation_ids": ["obs:v1:" + "c" * 64],
                    },
                ]
            )
        )
        self.assertEqual(result["invariant"]["status"], "fail")
        self.assertEqual(result["invariant"]["violations"], 1)
        self.assertEqual(
            result["details"]["violating_subject_ids"],
            ["route:/admin/debug"],
        )
        self.assertEqual(
            result["details"]["contradicting_observation_ids"],
            ["obs:v1:" + "c" * 64],
        )

    def test_unresolved_subject_prevents_pass(self):
        result = module.evaluate(
            _bundle(
                [
                    {
                        "id": "route:/admin/dynamic",
                        "status": "unresolved",
                        "observation_ids": [],
                    }
                ]
            )
        )
        self.assertEqual(result["invariant"]["status"], "unresolved")
        self.assertEqual(result["invariant"]["unresolved"], 1)

    def test_empty_subject_set_is_unresolved_not_vacuously_true(self):
        result = module.evaluate(_bundle([]))
        self.assertEqual(result["invariant"]["status"], "unresolved")

    def test_passing_subject_requires_supporting_observation(self):
        with self.assertRaisesRegex(
            module.InvariantInputError,
            "pass subject requires at least one observation",
        ):
            module.evaluate(
                _bundle(
                    [
                        {
                            "id": "route:/admin/users",
                            "status": "pass",
                            "observation_ids": [],
                        }
                    ]
                )
            )

    def test_duplicate_subject_is_rejected(self):
        subjects = [
            {
                "id": "route:/admin",
                "status": "pass",
                "observation_ids": ["obs:v1:" + "a" * 64],
            },
            {
                "id": "route:/admin",
                "status": "pass",
                "observation_ids": ["obs:v1:" + "b" * 64],
            },
        ]
        with self.assertRaisesRegex(
            module.InvariantInputError,
            "duplicate subject",
        ):
            module.evaluate(_bundle(subjects))

    def test_noncanonical_observation_reference_is_rejected(self):
        with self.assertRaisesRegex(module.InvariantInputError, "obs:v1"):
            module.evaluate(
                _bundle(
                    [
                        {
                            "id": "route:/admin",
                            "status": "pass",
                            "observation_ids": ["not-an-observation"],
                        }
                    ]
                )
            )


if __name__ == "__main__":
    unittest.main()
