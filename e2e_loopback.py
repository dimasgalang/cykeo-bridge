"""Loopback E2E: bridge agent (simulator) -> dummy server -> verifikasi event.

Tujuan: membuktikan pipeline bridge masih utuh SETELAH mode cykeo diarahkan
ke helper .NET. Mode simulator dipakai karena Linux tidak punya reader.

BUKAN pengganti smoke test CK-D5 fisik di PC Test.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.dummy_server import DummyServer  # noqa: E402

from cykeo_bridge.config import Config  # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cykeo-e2e-")
    log_path = os.path.join(tmp, "bridge.log")
    queue_path = os.path.join(tmp, "queue.jsonl")

    e2e_key = "e2e-local-key"
    e2e_reader = "CYKEO-01"

    with DummyServer(expected_api_key=e2e_key) as server:
        base = server.url
        print("dummy server  :", base)
        print("reader code   :", e2e_reader)
        print("api key       : (disensor)")

        cfg = Config(
            server_url=base,
            reader_code=e2e_reader,
            api_key=e2e_key,
            mode="simulator",
            simulator_loop=True,
            simulator_delay=0.01,
            batch_size=10,
            flush_interval=0.3,
            poll_interval=0.05,
            request_timeout=5.0,
            max_retries=2,
            log_path=log_path,
            queue_path=queue_path,
        )

        from cykeo_bridge.devices.factory import build_device
        from cykeo_bridge.queue import PersistentQueue
        from cykeo_bridge.http_client import RfidHttpClient

        device = build_device(cfg)
        queue = PersistentQueue(cfg.queue_path)
        client = RfidHttpClient(cfg)

        print("adapter       :", device.describe().get("adapter"))
        print("deskripsi     :", json.dumps(device.describe(), ensure_ascii=False, default=str))

        sent_total = 0
        deadline = time.time() + 6.0
        try:
            while time.time() < deadline:
                tags = device.read_tags()
                if tags:
                    queue.append([t.to_event() for t in tags])
                pending = queue.peek(cfg.batch_size)
                if pending:
                    res = client.post_batch(pending)
                    if res.ok:
                        queue.commit(len(pending))
                        sent_total += len(pending)
                    else:
                        print("HTTP gagal    :", res.status, res.message[:120])
                time.sleep(0.1)
        finally:
            device.close()

        requests_seen = getattr(server, "requests", None) or []
        received = []
        for req in requests_seen:
            received.extend(req.events)

        print("")
        print("=== HASIL E2E ===")
        print("event dikirim :", sent_total)
        print("event diterima:", len(received))
        print("queue tersisa :", len(queue))

        if received:
            sample = received[0]
            print("contoh payload:", json.dumps(sample, ensure_ascii=False)[:220])
            keys = set(sample.keys())
            expected = {"epc", "antenna", "rssi", "timestamp"}
            missing = expected - keys
            extra = keys - expected
            print("key payload   :", sorted(keys))
            if missing:
                print("[X] key kurang :", sorted(missing))
            if extra:
                print("[!] key tambahan:", sorted(extra))
            if not missing and not extra:
                print("[OK] bentuk payload sesuai kontrak")

        ok = len(received) > 0 and sent_total == len(received)
        print("")
        print("VERDICT:", "LULUS" if ok else "GAGAL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
