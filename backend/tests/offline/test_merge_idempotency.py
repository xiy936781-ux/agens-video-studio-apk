"""Offline tests for merge idempotency + task-lock registry.

Uses isolated temp dirs and mocks; no network, no real video API, no writes
to production jobs.json / config.json / outputs.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Isolated env BEFORE importing server so its DATA/OUT module-level vars
# point at our temp tree, not the production one.
_TMP = Path(tempfile.mkdtemp(prefix="agnes_merge_idem_test_"))
os.environ["AGNES_DATA_DIR"] = str(_TMP / "data")
os.environ["AGNES_OUTPUT_DIR"] = str(_TMP / "outputs")
os.environ.pop("AGNES_API_TOKEN", None)

import server  # noqa: E402
from server import (  # noqa: E402
    _is_valid_merged_file,
    _fresh_merge_output_path,
    _merge_task_lock,
    _merge_release_task_lock,
    _jobs_lock,
    jobs_load,
    jobs_save,
)


def _mk_data_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="agnes_idem_data_"))
    d.mkdir(parents=True, exist_ok=True)
    return d


class _JobsDataIsolation(unittest.TestCase):
    """Mixin: point server.DATA at a fresh temp dir for jobs_load/jobs_save."""

    def setUp(self):
        super().setUp()
        self.data = _mk_data_dir()
        p = patch.object(server, "DATA", self.data)
        p.start()
        self._data_patch = p

    def tearDown(self):
        self._data_patch.stop()
        super().tearDown()

    def write_jobs(self, jobs: dict) -> None:
        (self.data / "jobs.json").write_text(
            json.dumps(jobs, ensure_ascii=False, indent=2)
        )

    def read_jobs(self) -> dict:
        p = self.data / "jobs.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text())


class HelperTests(_JobsDataIsolation, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp(prefix="agnes_idem_out_"))
        self._out_patch = patch.object(server, "OUT", self.out)
        self._out_patch.start()

    def tearDown(self):
        self._out_patch.stop()
        super().tearDown()

    # ---- valid merged file ------------------------------------------------
    def test_valid_merged_file_inside_out(self):
        f = self.out / "lv_x_merged_abc.mp4"
        f.write_bytes(b"video")
        self.assertTrue(_is_valid_merged_file(str(f)))

    def test_reject_outside_out_path(self):
        outside = Path(tempfile.mkdtemp(prefix="agnes_idem_outside_")) / "x.mp4"
        outside.write_bytes(b"x")
        self.assertFalse(_is_valid_merged_file(str(outside)))

    def test_reject_empty_file_inside_out(self):
        f = self.out / "empty.mp4"
        f.write_bytes(b"")
        self.assertFalse(_is_valid_merged_file(str(f)))

    def test_reject_missing_file_inside_out(self):
        self.assertFalse(_is_valid_merged_file(str(self.out / "nope.mp4")))

    def test_reject_relative_escape_path(self):
        # A relative path that resolves outside OUT must be rejected
        self.assertFalse(_is_valid_merged_file("../escape.mp4"))

    def test_reject_non_string(self):
        self.assertFalse(_is_valid_merged_file(None))
        self.assertFalse(_is_valid_merged_file(123))

    # ---- UUID output naming ----------------------------------------------
    def test_fresh_output_path_unique_and_no_second_timestamp(self):
        a = _fresh_merge_output_path("job1")
        b = _fresh_merge_output_path("job1")
        self.assertNotEqual(a, b)
        self.assertTrue(a.name.startswith("lv_job1_merged_"))
        self.assertTrue(a.name.endswith(".mp4"))
        suffix = a.name[len("lv_job1_merged_"):-len(".mp4")]
        self.assertEqual(len(suffix), 32)          # UUID4 hex
        self.assertNotRegex(suffix, r"^\d{10}$")    # not a second-level ts

    # ---- task-lock semantics ----------------------------------------------
    def test_same_task_serialized(self):
        """Two threads on the same jid must never hold the lock at the same time."""
        first_entered = threading.Event()
        second_attempting = threading.Event()
        allow_first_release = threading.Event()
        second_entered = threading.Event()
        order = []
        order_lock = threading.Lock()
        active_locks = [0]
        active_lock = threading.Lock()
        violations = []

        def worker(tag: str):
            lock = _merge_task_lock("jobA")
            if tag == "second":
                second_attempting.set()
            lock.acquire()
            try:
                with active_lock:
                    active_locks[0] += 1
                    if active_locks[0] > 1:
                        violations.append("same task lock held concurrently")
                with order_lock:
                    order.append(tag)
                if tag == "first":
                    first_entered.set()
                    allow_first_release.wait(timeout=5)
                else:
                    second_entered.set()
            finally:
                with active_lock:
                    active_locks[0] -= 1
                lock.release()

        t1 = threading.Thread(target=worker, args=("first",))
        t2 = threading.Thread(target=worker, args=("second",))
        t1.start()
        t2.start()

        self.assertTrue(first_entered.wait(timeout=5), "first thread never entered")
        self.assertTrue(second_attempting.wait(timeout=5), "second thread never began acquisition")
        self.assertFalse(second_entered.is_set(), "second thread entered before first released")
        allow_first_release.set()
        self.assertTrue(second_entered.wait(timeout=5), "second thread did not enter after release")
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(order, ["first", "second"], "same task lock was not serialized")
        self.assertFalse(violations, "concurrent critical section detected")

    def test_different_tasks_do_not_block_each_other(self):
        """Threads on different jids can hold their locks in parallel."""
        a_held = threading.Event()
        b_held = threading.Event()
        both_held = threading.Event()
        release = threading.Event()

        def worker(jid: str, held_evt: threading.Event, other_evt: threading.Event):
            lock = _merge_task_lock(jid)
            lock.acquire()
            try:
                held_evt.set()
                other_evt.wait(timeout=5)   # wait for the other thread to also hold
                release.wait(timeout=5)     # wait for main to signal both are held
            finally:
                lock.release()

        t1 = threading.Thread(target=worker, args=("jobA", a_held, b_held))
        t2 = threading.Thread(target=worker, args=("jobB", b_held, a_held))
        t1.start()
        t2.start()
        # Both threads must hold their locks concurrently
        self.assertTrue(a_held.wait(timeout=5), "jobA never acquired")
        self.assertTrue(b_held.wait(timeout=5), "jobB never acquired")
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

    def test_registry_keeps_stable_lock_object(self):
        """After _merge_release_task_lock the registry must still map jid to
        the SAME lock, so a waiting thread never deadlocks on a new lock."""
        lock1 = _merge_task_lock("jobX")
        _merge_release_task_lock("jobX")
        lock2 = _merge_task_lock("jobX")
        self.assertIs(lock1, lock2, "registry must not swap lock instances")

    def test_locks_not_held_under_jobs_lock(self):
        """A thread holding the FFmpeg path (i.e. the task lock) must not
        block a concurrent jobs.json reader.  Verify by acquiring the task
        lock on one thread, then acquiring _jobs_lock from the test thread."""
        task_lock = _merge_task_lock("jobY")
        task_lock.acquire()
        acquired = threading.Event()

        def reader():
            try:
                with _jobs_lock:
                    jobs_load()
                acquired.set()
            except Exception:
                pass

        t = threading.Thread(target=reader)
        t.start()
        self.assertTrue(acquired.wait(timeout=5),
                        "_jobs_lock was blocked while task lock held")
        task_lock.release()
        t.join(timeout=5)


class JobsSaveIsolationTests(_JobsDataIsolation, unittest.TestCase):

    def test_jobs_save_success_updates_file(self):
        self.write_jobs({"job1": {"long": True, "segments": []}})
        with _jobs_lock:
            j = jobs_load()
            j["job1"]["merge_status"] = "merged"
            j["job1"]["merged_file"] = "/some/out.mp4"
            jobs_save(j)
        persisted = self.read_jobs()
        self.assertEqual(persisted["job1"]["merge_status"], "merged")
        self.assertEqual(persisted["job1"]["merged_file"], "/some/out.mp4")

    def test_jobs_save_failure_preserves_original_file(self):
        self.write_jobs({"job1": {"long": True, "segments": []}})
        original = (self.data / "jobs.json").read_text()
        with patch.object(server, "jobs_save", side_effect=OSError("disk full")):
            with _jobs_lock:
                try:
                    server.jobs_save(server.jobs_load())
                    self.fail("expected OSError")
                except OSError:
                    pass
        self.assertEqual((self.data / "jobs.json").read_text(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
