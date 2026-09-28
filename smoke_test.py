#!/usr/bin/env python3
"""Smoke test end-to-end: simulator -> queue -> http_client -> server dummy.

TIDAK menyentuh server RFID produksi. Hanya loopback 127.0.0.1.
Jalankan: python3 smoke_test.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.dummy_server import DummyServer
from cykeo_bridge.config import Config
from cykeo_bridge.devices.simulator import SimulatedDevice
from cykeo_bridge.http_client import RfidHttpClient
from cykeo_bridge.queue import PersistentQueue

API_KEY = "dummy-key-for-local-smoke-test"
READER = "CYKEO-01"

failures = []


def check(label, condition, detail=""):
    status = "OK " if condition else "GAGAL"
    print(f"  [{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


with DummyServer(expected_api_key=API_KEY) as server:
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(
        server_url=server.url,
        reader_code=READER,
        api_key=API_KEY,
        mode="simulator",
        batch_size=4,
        queue_path=str(tmp / "queue.jsonl"),
        log_path=str(tmp / "bridge.log"),
    )

    device = SimulatedDevice(loop=False)
    queue = PersistentQueue(cfg.queue_path)
    client = RfidHttpClient(cfg)

    # 1) Baca semua tag dari simulator.
    raw_tags = []
    while True:
        batch = device.read_tags()
        if not batch:
            break
        raw_tags.extend(batch)
    print(f"\n1) simulator menghasilkan {len(raw_tags)} tag mentah")

    # 2) Masukkan ke antrean (dict event, EPC mentah).
    events = [
        {"epc": t.epc, "antenna": t.antenna, "rssi": t.rssi, "timestamp": t.timestamp}
        for t in raw_tags
    ]
    written = queue.append(events)
    print(f"2) antrean berisi {queue.count()} event (ditulis {written})")
    check("semua event masuk antrean", queue.count() == len(events),
          f"{queue.count()} dari {len(events)}")

    # 3) Kirim per batch.
    sent = 0
    while queue.count() > 0:
        batch = queue.peek(cfg.batch_size)
        result = client.post_batch(batch)
        if result.ok:
            queue.commit(len(batch))
            sent += len(batch)
        else:
            print(f"   GAGAL kirim: {result.as_log()}")
            break
    print(f"3) total terkirim: {sent}; request diterima: {len(server.requests)}")

    check("semua event terkirim", sent == len(events), f"{sent} dari {len(events)}")
    check("antrean kosong setelah commit", queue.count() == 0)
    check("server menerima request", len(server.requests) >= 1)

    if server.requests:
        req = server.requests[0]
        print("\n--- PATH ---")
        print(req.path)
        print("--- AUTH HEADER ---")
        print("X-Reader-Api-Key benar" if req.headers.get("X-Reader-Api-Key") == API_KEY else "SALAH")
        print("--- PAYLOAD (EPC harus MENTAH, apa adanya) ---")
        print(json.dumps(req.json, indent=2)[:1200])

        check("path endpoint benar",
              req.path == f"/api/v1/rfid/readers/{READER}/events", req.path)
        check("auth header benar", req.headers.get("X-Reader-Api-Key") == API_KEY)
        check("payload punya key 'events'",
              isinstance(req.json, dict) and "events" in req.json)
        check("tiada event tanpa 'epc'",
              all("epc" in e for e in req.json.get("events", [])))
        # EPC harus dikirim apa adanya: prefix 0X & whitespace harus masih ada
        # di payload (normalisasi Happens di server, bukan di agent).
        sent_epcs = [e["epc"] for e in req.json["events"]]
        check("EPC dikirim mentah (prefix 0X dipertahankan)",
              any(e.startswith("0X") for e in sent_epcs))
        check("path konsisten di semua request",
              all(r.path == f"/api/v1/rfid/readers/{READER}/events" for r in server.requests))

print("\n" + "=" * 60)
if failures:
    print(f"[GAGAL] {len(failures)} pemeriksaan gagal: {failures}")
    sys.exit(1)
print("[OK] smoke test end-to-end LULUS")
