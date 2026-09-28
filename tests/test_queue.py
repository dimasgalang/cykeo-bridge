"""Unit test :class:`cykeo_bridge.queue.PersistentQueue` (JSONL persisted)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cykeo_bridge.queue import PersistentQueue


def event(epc: str, rssi: int = -42, antenna: int = 1) -> dict:
    return {"epc": epc, "antenna": antenna, "rssi": rssi, "timestamp": "2026-09-28T04:00:00+00:00"}


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "queue.jsonl"
        self.q = PersistentQueue(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestAppendAndRead(QueueTestCase):
    def test_append_and_count(self) -> None:
        self.assertEqual(self.q.append([event("AAA"), event("BBB")]), 2)
        self.assertEqual(self.q.count(), 2)
        self.assertEqual(len(self.q), 2)

    def test_empty_append_is_noop(self) -> None:
        self.assertEqual(self.q.append([]), 0)
        self.assertEqual(self.q.append([None, {}]), 0)
        self.assertEqual(self.q.count(), 0)

    def test_peek_does_not_remove(self) -> None:
        self.q.append([event("AAA"), event("BBB")])
        batch = self.q.peek(1)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["epc"], "AAA")
        self.assertEqual(self.q.count(), 2, "peek tidak boleh menghapus")

    def test_file_is_jsonl(self) -> None:
        self.q.append([event("AAA"), event("BBB")])
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("epc", json.loads(line))

    def test_survives_reopen(self) -> None:
        self.q.append([event("AAA")])
        reopened = PersistentQueue(self.path)
        self.assertEqual(reopened.count(), 1)
        self.assertEqual(reopened.peek()[0]["epc"], "AAA")

    def test_epc_stored_verbatim(self) -> None:
        # Antrean tidak boleh menormalisasi EPC (payload tetap mentah).
        raw = "  30 35 15 c1 14 0e 36 10 a0 2e 48 80  "
        self.q.append([event(raw)])
        self.assertEqual(self.q.peek()[0]["epc"], raw)


class TestCommit(QueueTestCase):
    def test_commit_removes_front(self) -> None:
        self.q.append([event("A"), event("B"), event("C")])
        removed = self.q.commit(2)
        self.assertEqual(removed, 2)
        self.assertEqual([e["epc"] for e in self.q.peek()], ["C"])

    def test_commit_zero_is_noop(self) -> None:
        self.q.append([event("A")])
        self.assertEqual(self.q.commit(0), 0)
        self.assertEqual(self.q.count(), 1)

    def test_commit_more_than_available(self) -> None:
        self.q.append([event("A")])
        self.assertEqual(self.q.commit(99), 1)
        self.assertEqual(self.q.count(), 0)

    def test_commit_is_durable(self) -> None:
        self.q.append([event("A"), event("B")])
        self.q.commit(1)
        reopened = PersistentQueue(self.path)
        self.assertEqual([e["epc"] for e in reopened.peek()], ["B"])

    def test_commit_leaves_no_temp_files(self) -> None:
        self.q.append([event("A"), event("B")])
        self.q.commit(1)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"file tmp tertinggal: {leftovers}")


class TestReject(QueueTestCase):
    def test_reject_moves_to_sidecar(self) -> None:
        self.q.append([event("A"), event("B"), event("C")])
        moved = self.q.reject(1, "422 payload salah")
        self.assertEqual(moved, 1)
        self.assertEqual([e["epc"] for e in self.q.peek()], ["B", "C"])
        rejected_lines = self.q.rejected_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rejected_lines), 1)
        self.assertEqual(json.loads(rejected_lines[0])["epc"], "A")

    def test_reject_appends_across_batches(self) -> None:
        self.q.append([event("A")])
        self.q.reject(1, "422")
        self.q.append([event("B")])
        self.q.reject(1, "422")
        self.assertEqual(len(self.q.rejected_path.read_text(encoding="utf-8").strip().splitlines()), 2)


class TestRobustness(QueueTestCase):
    def test_corrupt_line_is_quarantined(self) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("{bukan json}\n")
        self.q.append([event("A")])
        self.assertEqual([e["epc"] for e in self.q.peek()], ["A"])
        self.assertTrue(self.q.corrupt_path.exists())
        self.assertIn("{bukan json}", self.q.corrupt_path.read_text(encoding="utf-8"))

    def test_non_dict_json_line_skipped(self) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("[1,2,3]\n")
        self.q.append([event("A")])
        self.assertEqual(self.q.count(), 1)

    def test_blank_lines_ignored(self) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n   \n")
        self.q.append([event("A")])
        self.assertEqual(self.q.count(), 1)

    def test_enforce_max_events_drops_oldest(self) -> None:
        q = PersistentQueue(self.path, max_events=3)
        q.append([event(str(i)) for i in range(6)])
        dropped = q.enforce_max_events()
        self.assertEqual(dropped, 3)
        self.assertEqual([e["epc"] for e in q.peek()], ["3", "4", "5"])

    def test_enforce_max_events_noop_below_limit(self) -> None:
        q = PersistentQueue(self.path, max_events=10)
        q.append([event("A")])
        self.assertEqual(q.enforce_max_events(), 0)

    def test_requeue_front(self) -> None:
        self.q.append([event("A")])
        self.q.requeue_front([event("Z")])
        self.assertEqual([e["epc"] for e in self.q.peek()], ["Z", "A"])

    def test_split_batch(self) -> None:
        self.q.append([event(str(i)) for i in range(5)])
        batch, has_more = self.q.split_batch(2)
        self.assertEqual(len(batch), 2)
        self.assertTrue(has_more)

    def test_stats(self) -> None:
        self.q.append([event("A"), event("B")])
        self.q.reject(1, "422")
        stats = self.q.stats().as_dict()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["corrupt"], 0)

    def test_clear(self) -> None:
        self.q.append([event("A"), event("B")])
        self.assertEqual(self.q.clear(), 2)
        self.assertEqual(self.q.count(), 0)

    def test_empty_queue_peek(self) -> None:
        self.assertEqual(self.q.peek(10), [])
        self.assertEqual(self.q.count(), 0)


if __name__ == "__main__":
    unittest.main()
