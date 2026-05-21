import importlib.util
import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
XIANGSHAN_PY = REPO_ROOT / "scripts" / "xiangshan.py"

spec = importlib.util.spec_from_file_location("xiangshan_script", XIANGSHAN_PY)
xiangshan_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xiangshan_script)


class FakeProcess:
    def __init__(self, affinity, status="running"):
        self.info = {
            "cpu_affinity": affinity,
            "status": status,
        }


class GetUnsetCoresTest(unittest.TestCase):
    def unset_cores_for_affinity(
        self,
        affinity,
        *,
        cpu_count=128,
        reserved_width_limit=32,
        status="running",
    ):
        process = FakeProcess(affinity, status=status)
        return xiangshan_script._get_unset_cores(
            cpu_count=cpu_count,
            core_usage=[0] * cpu_count,
            process_iter=lambda attrs: [process],
            reserved_width_limit=reserved_width_limit,
        )

    def test_ignores_broad_affinity_masks_when_reserving_cores(self):
        unset_cores = self.unset_cores_for_affinity(list(range(16, 121)))

        self.assertTrue(set(range(16, 32)).issubset(unset_cores))

    def test_ignores_affinity_masks_much_wider_than_reserved_window(self):
        unset_cores = self.unset_cores_for_affinity(list(range(32, 96)))

        self.assertTrue(set(range(32, 48)).issubset(unset_cores))

    def test_reserves_narrow_affinity_masks_for_existing_bound_jobs(self):
        unset_cores = self.unset_cores_for_affinity(list(range(32, 48)))

        self.assertFalse(set(range(32, 48)).intersection(unset_cores))

    def test_reserves_wider_explicit_affinity_masks_for_existing_bound_jobs(self):
        unset_cores = self.unset_cores_for_affinity(
            list(range(32, 56)),
            reserved_width_limit=16,
        )

        self.assertFalse(set(range(32, 56)).intersection(unset_cores))

    def test_reserves_three_quarter_machine_affinity_as_explicit_binding(self):
        unset_cores = self.unset_cores_for_affinity(
            list(range(0, 24)),
            cpu_count=32,
            reserved_width_limit=16,
        )

        self.assertFalse(set(range(0, 24)).intersection(unset_cores))


if __name__ == "__main__":
    unittest.main()
