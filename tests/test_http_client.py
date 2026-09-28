"""Unit test :mod:`cykeo_bridge.http_client` memakai HTTP server dummy lokal.

Tidak ada koneksi ke server RFID produksi — hanya 127.0.0.1.
"""

from __future__ import annotations

import json
import unittest

from cykeo_bridge.config import Config
from cykeo_bridge.http_client import RfidHttpClient, build_payload
from tests.dummy_server import DummyServer

API_KEY = "dummy-key-unit-test"  # placeholder
PATH = "/api/v1/rfid/readers/CYKEO-01/events"


def make_config(base_url: str) -> Config:
    return Config(
        server_url=base_url,
        reader_code="CYKEO-01",
        api_key=API_KEY,
        mode="simulator",
        request_timeout=5.0,
        max_retries=0,
        backoff_base=0.01,
    )


def event(epc: str = "303515C1140E3610A02E4880", antenna: int = 1, rssi: int = -42) -> dict:
    return {"epc": epc, "antenna": antenna, "rssi": rssi, "timestamp": "2026-09-28T04:00:00+00:00"}


class TestBuildPayload(unittest.TestCase):
    def test_shape_matches_contract(self) -> None:
        payload = build_payload([event()])
        self.assertEqual(list(payload.keys()), ["events"])
        self.assertEqual(list(payload["events"][0].keys()), ["epc", "antenna", "rssi", "timestamp"])

    def test_epc_sent_verbatim(self) -> None:
        raw = "  0x303515c1140e3610a02e4880  "
        payload = build_payload([event(epc=raw)])
        self.assertEqual(payload["events"][0]["epc"], raw, "payload TIDAK boleh dinormalisasi")

    def test_extra_keys_dropped(self) -> None:
        payload = build_payload([{**event(), "tid": "SECRET-TID", "internal": 1}])
        self.assertNotIn("tid", payload["events"][0])
        self.assertNotIn("internal", payload["events"][0])

    def test_empty_epc_dropped(self) -> None:
        payload = build_payload([event(epc=""), event(epc="   "), event(epc=None), event()])
        self.assertEqual(len(payload["events"]), 1)

    def test_null_antenna_rssi_kept(self) -> None:
        payload = build_payload([{"epc": "ABC", "antenna": None, "rssi": None,
                                  "timestamp": None}])
        self.assertIsNone(payload["events"][0]["antenna"])
        self.assertIsNone(payload["events"][0]["rssi"])

    def test_empty_input(self) -> None:
        self.assertEqual(build_payload([]), {"events": []})


class TestPostAgainstDummyServer(unittest.TestCase):
    def setUp(self) -> None:
        self.server = DummyServer(expected_path=PATH, expected_api_key=API_KEY)
        self.base = self.server.start()
        self.addCleanup(self.server.stop)
        self.client = RfidHttpClient(make_config(self.base))

    def test_successful_post(self) -> None:
        result = self.client.post_batch([event(), event(epc="303515C1140E3610A02E487C")])
        self.assertTrue(result.ok, result.as_log())
        self.assertEqual(result.status, 200)
        self.assertEqual(result.accepted, 2)
        self.assertEqual(len(self.server.requests), 1)

    def test_request_headers_and_path(self) -> None:
        self.client.post_batch([event()])
        req = self.server.requests[0]
        self.assertEqual(req.path, PATH, "path harus persis kontrak §4")
        self.assertEqual(req.headers.get("X-Reader-Api-Key"), API_KEY)
        self.assertIn("application/json", req.headers.get("Content-Type", ""))

    def test_body_is_events_wrapper(self) -> None:
        self.client.post_batch([event()])
        body = self.server.requests[0].json
        self.assertIsInstance(body, dict)
        self.assertIn("events", body)
        self.assertEqual(body["events"][0]["epc"], "303515C1140E3610A02E4880")

    def test_empty_batch_does_not_hit_server(self) -> None:
        result = self.client.post_batch([])
        self.assertTrue(result.ok)
        self.assertEqual(self.server.requests, [])

    def test_ping(self) -> None:
        result = self.client.ping()
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 405)


class TestAuthFailure(unittest.TestCase):
    def test_401_is_fatal_not_retried(self) -> None:
        server = DummyServer(expected_path=PATH, expected_api_key="kunci-yang-sama")
        base = server.start()
        self.addCleanup(server.stop)
        cfg = make_config(base)
        cfg.api_key = "kunci-salah"
        cfg.max_retries = 3
        client = RfidHttpClient(cfg, sleep=lambda _s: None)
        result = client.post_batch([event()])
        self.assertFalse(result.ok)
        self.assertTrue(result.fatal, "401 harus fatal")
        self.assertEqual(result.status, 401)
        self.assertEqual(result.attempts, 1, "401 tidak boleh di-retry")
        self.assertEqual(len(server.requests), 1)


class TestRejectedBatch(unittest.TestCase):
    def test_422_marked_rejected_not_retried(self) -> None:
        server = DummyServer(expected_path=PATH, default_status=422)
        base = server.start()
        self.addCleanup(server.stop)
        cfg = make_config(base)
        cfg.max_retries = 3
        client = RfidHttpClient(cfg, sleep=lambda _s: None)
        result = client.post_batch([event()])
        self.assertFalse(result.ok)
        self.assertTrue(result.rejected)
        self.assertFalse(result.fatal)
        self.assertIn("epc tidak valid", result.message)
        self.assertEqual(result.attempts, 1, "422 tidak boleh di-retry")


class TestRetryAndBackoff(unittest.TestCase):
    def test_500_then_200(self) -> None:
        server = DummyServer(expected_path=PATH, status_sequence=[500, 500, 200])
        base = server.start()
        self.addCleanup(server.stop)
        cfg = make_config(base)
        cfg.max_retries = 3
        delays: list[float] = []
        client = RfidHttpClient(cfg, sleep=delays.append)
        result = client.post_batch([event()])
        self.assertTrue(result.ok, result.as_log())
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(len(delays), 2, "ada 2 jeda sebelum percobaan ke-3")
        self.assertLessEqual(delays[0], delays[1], "backoff harus naik")

    def test_gives_up_after_max_retries(self) -> None:
        server = DummyServer(expected_path=PATH, default_status=503)
        base = server.start()
        self.addCleanup(server.stop)
        cfg = make_config(base)
        cfg.max_retries = 2
        client = RfidHttpClient(cfg, sleep=lambda _s: None)
        result = client.post_batch([event()])
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 3, "1 percobaan + 2 retry")
        self.assertEqual(len(server.requests), 3)

    def test_backoff_is_capped(self) -> None:
        cfg = make_config("http://127.0.0.1:1")
        cfg.backoff_base = 10.0
        cfg.backoff_max = 30.0
        client = RfidHttpClient(cfg, sleep=lambda _s: None)
        for attempt in range(1, 8):
            self.assertLessEqual(client.backoff_delay(attempt), 30.0 * 1.001)

    def test_connection_refused_is_retryable(self) -> None:
        # Port 1 di loopback tidak listening -> URLError
        cfg = make_config("http://127.0.0.1:1")
        cfg.max_retries = 1
        client = RfidHttpClient(cfg, sleep=lambda _s: None)
        result = client.post_batch([event()])
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertIn("URLError", result.error)


class TestNoSecretLeak(unittest.TestCase):
    def test_api_key_not_in_result_log(self) -> None:
        server = DummyServer(expected_path=PATH, default_status=401)
        base = server.start()
        self.addCleanup(server.stop)
        cfg = make_config(base)
        cfg.api_key = "rahasia-yang-tidak-boleh-bocor"
        client = RfidHttpClient(cfg, sleep=lambda _s: None)
        result = client.post_batch([event()])
        self.assertNotIn("rahasia-yang-tidak-boleh-bocor", result.as_log())

    def test_response_parsed_as_json(self) -> None:
        server = DummyServer(expected_path=PATH)
        base = server.start()
        self.addCleanup(server.stop)
        client = RfidHttpClient(make_config(base))
        result = client.post_batch([event()])
        self.assertIsInstance(result.message, str)
        self.assertEqual(result.message, "ok")
        self.assertEqual(json.dumps(result.accepted), "1")


if __name__ == "__main__":
    unittest.main()
