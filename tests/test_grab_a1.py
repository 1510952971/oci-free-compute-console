import threading
import unittest
from unittest import mock

import grab_a1


class PlanValidationTests(unittest.TestCase):
    def test_all_presets_fit_current_free_limits(self):
        for name, preset in grab_a1.PRESETS.items():
            with self.subTest(name=name):
                self.assertTrue(grab_a1.normalize_items(preset["items"]))

    def test_old_four_ocpu_plan_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ARM OCPU"):
            grab_a1.normalize_items([
                {"shape": "arm", "count": 1, "ocpus": 4, "memory_gbs": 24, "boot_gbs": 50}
            ])

    def test_mixed_plan_expands_to_stable_unique_names(self):
        items = grab_a1.normalize_items(grab_a1.PRESETS["mixed"]["items"])
        targets = grab_a1.expand_targets(items, "free-test")
        self.assertEqual(
            [target["name"] for target in targets],
            ["free-test-arm-1", "free-test-micro-1", "free-test-micro-2"],
        )

    def test_existing_resources_are_included_in_projection(self):
        usage = {
            "arm_ocpus": 1, "arm_memory_gbs": 6,
            "micro_count": 0, "boot_gbs": 150,
        }
        missing = [{
            "shape": "arm", "ocpus": 2, "memory_gbs": 12,
            "boot_gbs": 50, "name": "free-test-arm-1",
        }]
        with self.assertRaisesRegex(ValueError, "现有资源"):
            grab_a1.assert_within_account_limits(usage, missing)

    def test_existing_target_is_not_added_twice(self):
        usage = {
            "arm_ocpus": 2, "arm_memory_gbs": 12,
            "micro_count": 0, "boot_gbs": 50,
        }
        grab_a1.assert_within_account_limits(usage, [])

    def test_micro_values_are_fixed_by_server(self):
        result = grab_a1.normalize_items([
            {"shape": "micro", "count": 1, "ocpus": 9, "memory_gbs": 99, "boot_gbs": 50}
        ])
        self.assertEqual(result[0]["ocpus"], 1)
        self.assertEqual(result[0]["memory_gbs"], 1)

    def test_closed_output_pipe_does_not_kill_logging(self):
        engine = grab_a1.GrabEngine.__new__(grab_a1.GrabEngine)
        engine.lock = threading.RLock()
        engine.status_data = {"history": [], "message": ""}
        with mock.patch("builtins.print", side_effect=BrokenPipeError):
            engine.log("still running")
        self.assertEqual(engine.status_data["message"], "still running")
        self.assertEqual(engine.status_data["history"][0]["message"], "still running")

    def test_watchdog_restarts_a_missing_worker(self):
        engine = grab_a1.GrabEngine.__new__(grab_a1.GrabEngine)
        engine.lock = threading.RLock()
        engine.status_data = {"history": [], "message": ""}
        engine.saved = {"active_job": {"preset": "arm_full", "items": [{"shape": "arm"}]}}
        engine.worker = None
        engine.next_watchdog_retry = 0
        engine.start = mock.Mock(return_value={"ok": True})
        engine.watchdog()
        engine.start.assert_called_once_with([{"shape": "arm"}], "arm_full")
        self.assertEqual(engine.status_data["phase"], "recovering")

    def test_watchdog_ignores_idle_service(self):
        engine = grab_a1.GrabEngine.__new__(grab_a1.GrabEngine)
        engine.saved = {"active_job": None}
        engine.start = mock.Mock()
        engine.watchdog()
        engine.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
