"""EPC yang sama harus terkirim ULANG pada batch berikutnya.

Bug di lapangan (2026-09-29): dengan default ``dedup_window = 0.0`` tabel
``_seen_in_batch`` tidak pernah di-reset, sehingga setiap EPC yang sudah
pernah dikirim di-drop selamanya selama hidup proses. Log agent jadi:

    stats={"read": 4004, "queued": 9, "sent": 9, "deduped": 3995}

Artinya reader membaca ~4000 tag tapi hanya 9 yang terkirim. Scan kedua —
misalnya Reader Test yang dijalankan dari UI web — selalu sees 0 EPC,
padahal tag-nya masih menempel di reader.

Perbaikan: ``dedup_window = 0`` = dedup hanya di dalam satu batch baca,
lalu tabel dibersihkan sebelum batch berikutnya.
"""
import sys
import unittest

sys.path.insert(0, "/home/hermes/workspace/cykeo-bridge")

from cykeo_bridge.config import Config
from cykeo_bridge.devices.base import RawTag
from cykeo_bridge.main import BridgeAgent


def _tag(epc):
    return RawTag(epc=epc, antenna=1, rssi=70, timestamp="2026-09-29T00:00:00+00:00")


def _prune_now():
    """Waktu monotolik yang pasti > window dari entri pertama."""
    import time as _time
    return _time.monotonic() + 100.0


class TestDedupPerBatch(unittest.TestCase):
    """EPC sama di dua batch berbeda harus tetap terkirim dua kali."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.batch_size = 50
        # default produksi: dedup_window == 0.0
        self.assertEqual(self.cfg.dedup_window, 0.0)
        self.agent = BridgeAgent(self.cfg)
        self.epc = "303515C10C41B211602DD1FE"

    def test_epc_across_batches_is_resent(self):
        """Inti bug: 2 batch dengan EPC sama harus menghasilkan 2 batch event."""
        first = self.agent.collect([_tag(self.epc)])
        self.assertEqual(len(first), 1, "batch pertama harus mengirim 1 event")

        second = self.agent.collect([_tag(self.epc)])
        self.assertEqual(
            len(second), 1,
            "batch kedua harus mengirim EPC yang sama lagi; "
            "kalau 0, berarti dedup_window=0 tidak me-reset tabel dedup",
        )
        self.assertEqual(second[0]["epc"], self.epc)

    def test_dedup_still_works_inside_one_batch(self):
        """Duplikat dalam satu panggilan collect tetap di-drop (anti-bludak)."""
        events = self.agent.collect([_tag(self.epc), _tag(self.epc), _tag(self.epc)])
        self.assertEqual(len(events), 1, "duplikat dalam 1 batch harus tetap di-dedup")
        self.assertEqual(self.agent.stats["deduped"], 2)

    def test_repeated_scans_all_report_events(self):
        """Reader memindai tag sama terus-menerus -> tiap batch tetap ada event.

        Ini skenario lapangan: tag tetap menempel di reader, agent berjalan
        ribuan iterasi.
        """
        total_sent = 0
        for _ in range(5):
            total_sent += len(self.agent.collect([_tag(self.epc)]))
        self.assertEqual(
            total_sent, 5,
            "setiap putaran baca harus menghasilkan event; "
            f"hanya {total_sent}/5 terkirim",
        )

    def test_seen_table_does_not_grow_without_bound(self):
        """Tabel dedup tidak boleh tumbuh melebihi satu batch saat window=0.

        Setelah tiap collect, tabel boleh memuat entri batch yang SEDANG
        diproses (itu memang gunanya dedup per-batch). Yang tidak boleh
        terjadi: entri dari ribuan batch sebelumnya ikut menumpuk.
        """
        for _ in range(50):
            self.agent.collect([_tag(self.epc), _tag("E3008298000000000000000A")])
        self.assertLessEqual(
            len(self.agent._seen_in_batch), 2,
            "tabel dedup harus dibatasi per batch; "
            f"iskannya {len(self.agent._seen_in_batch)} entri setelah 50 batch",
        )

    def test_old_batches_are_forgotten(self):
        """EPC dari batch LAMA harus dilupakan, bukan di-dedup selamanya."""
        self.agent.collect([_tag("E3008298000000000000000A")])
        # batch berikutnya membawa EPC yang SAMA dan satu EPC baru
        events = self.agent.collect([_tag("E3008298000000000000000A")])
        self.assertEqual(
            len(events), 1,
            "EPC dari batch sebelumnya tidak boleh memblokir batch baru",
        )

    def test_dedup_window_positive_still_prunes(self):
        """dedup_window > 0 tetap prune berbasis waktu (perilaku lama utuh)."""
        self.cfg.dedup_window = 30.0
        agent = BridgeAgent(self.cfg)
        agent.collect([_tag(self.epc)])
        self.assertIn("303515C10C41B211602DD1FE", agent._seen_in_batch)
        # entri yang lebih tua dari window harus ter-prune
        agent.collect([_tag("E3008298000000000000000A")], now=_prune_now())
        self.assertNotIn("303515C10C41B211602DD1FE", agent._seen_in_batch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
