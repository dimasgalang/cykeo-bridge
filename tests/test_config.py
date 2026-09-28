"""Unit test :class:`cykeo_bridge.config.Config`."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cykeo_bridge.config import (
    ENV_API_KEY,
    ENV_BAUDRATE,
    ENV_MODE,
    ENV_SERVER_URL,
    INIT_PARAM_RE,
    INIT_PARAM_DEFAULT,
    MODE_CYKEO,
    MODE_SIMULATOR,
    MODE_ZEBRA,
    Config,
    ConfigError,
    load_config,
    save_config,
)


def valid_cykeo_config() -> Config:
    return Config(
        server_url="http://10.0.0.5:8080",
        reader_code="CYKEO-01",
        api_key="dummy-api-key-for-test",  # placeholder, bukan key nyata
        com_port="COM12",
        baudrate=115200,
        mode=MODE_CYKEO,
        init_param="A" * 32,
    )


class TestUrlAndPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = valid_cykeo_config()

    def test_events_url_matches_contract(self) -> None:
        # CONTRACT.md §4: POST /api/v1/rfid/readers/{reader_code}/events
        self.assertEqual(self.cfg.events_url,
                         "http://10.0.0.5:8080/api/v1/rfid/readers/CYKEO-01/events")

    def test_trailing_slash_does_not_double(self) -> None:
        self.cfg.server_url = "http://10.0.0.5:8080/"
        self.assertEqual(self.cfg.events_url,
                         "http://10.0.0.5:8080/api/v1/rfid/readers/CYKEO-01/events")

    def test_serial_target_format(self) -> None:
        # guide Cykeo §3.1: "COM1:115200"
        self.cfg.com_port = "COM12"
        self.cfg.baudrate = 115200
        self.assertEqual(self.cfg.serial_target, "COM12:115200")

    def test_default_baudrate_is_115200(self) -> None:
        self.assertEqual(Config().baudrate, 115200)

    def test_queue_and_log_have_defaults(self) -> None:
        cfg = Config()
        self.assertTrue(str(cfg.queue_file).endswith(".jsonl"))
        self.assertTrue(str(cfg.log_file).endswith(".log"))


class TestValidation(unittest.TestCase):
    def test_valid_config(self) -> None:
        self.assertEqual(valid_cykeo_config().validate(), [])

    def test_missing_api_key(self) -> None:
        cfg = valid_cykeo_config()
        cfg.api_key = ""
        self.assertIn("api_key kosong (ambil dari server RFID)", cfg.validate())

    def test_bad_server_url_scheme(self) -> None:
        cfg = valid_cykeo_config()
        cfg.server_url = "10.0.0.5:8080"
        self.assertTrue(any("http" in e for e in cfg.validate()))

    def test_bad_mode(self) -> None:
        cfg = valid_cykeo_config()
        cfg.mode = "fx7500"
        self.assertTrue(any("mode harus" in e for e in cfg.validate()))

    def test_reader_code_charset(self) -> None:
        cfg = valid_cykeo_config()
        cfg.reader_code = "CYKEO 01/../etc"
        self.assertTrue(any("reader_code" in e for e in cfg.validate()))

    def test_cykeo_init_param_required_only_in_strict(self) -> None:
        """initParam wajib hex HANYA saat strict (wizard).

        Regresi: dulu test ini mengasumsikan license key 32 hex. Temuan Fase 4
        membuktikan initParam = parameter handshake, bukan license per-device.
        """
        cfg = valid_cykeo_config()
        cfg.init_param = "NOTHEX"
        self.assertFalse(any("init_param" in e for e in cfg.validate(strict=False)))
        self.assertTrue(any("init_param" in e for e in cfg.validate(strict=True)))

    def test_init_param_regex_accepts_hex_any_length(self) -> None:
        self.assertTrue(INIT_PARAM_RE.match("E5DC18EDBEEE6EAD954D7332E6D90361"))
        self.assertTrue(INIT_PARAM_RE.match("e5dc18edbeee6ead954d7332e6d90361"))
        self.assertTrue(INIT_PARAM_RE.match("AB12"))  # panjang bebas
        self.assertFalse(INIT_PARAM_RE.match("Z5DC18EDBEEE6EAD954D7332E6D90361"))
        self.assertFalse(INIT_PARAM_RE.match(""))

    def test_init_param_default_is_official_sdk_constant(self) -> None:
        """Nilai default harus sama dengan konstanta Quick Start dokumen resmi."""
        self.assertEqual(INIT_PARAM_DEFAULT, "E5DC18EDBEEE6EAD954D7332E6D90361")
        self.assertEqual(Config().init_param, INIT_PARAM_DEFAULT)
        self.assertEqual(Config().to_dict()["init_param"], INIT_PARAM_DEFAULT)

    def test_batch_size_must_be_positive(self) -> None:
        cfg = valid_cykeo_config()
        cfg.batch_size = 0
        self.assertIn("batch_size minimal 1", cfg.validate())

    def test_zebra_mode_needs_no_com(self) -> None:
        cfg = valid_cykeo_config()
        cfg.mode = MODE_ZEBRA
        cfg.com_port = ""
        self.assertEqual(cfg.validate(), [])

    def test_simulator_mode_without_api_key_ok_when_not_strict(self) -> None:
        cfg = Config(mode=MODE_SIMULATOR, api_key="", server_url="http://127.0.0.1:9")
        self.assertEqual(cfg.validate(), [])
        self.assertTrue(cfg.validate(strict=True))


class TestSecretsNeverLeak(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = valid_cykeo_config()
        self.cfg.api_key = "super-secret-api-key-123"
        self.cfg.init_param = "B" * 32

    def test_repr_hides_secrets(self) -> None:
        text = repr(self.cfg)
        self.assertNotIn("super-secret-api-key-123", text)
        self.assertNotIn("B" * 32, text)
        self.assertIn("REDACTED", text)

    def test_str_hides_secrets(self) -> None:
        self.assertNotIn("super-secret-api-key-123", str(self.cfg))

    def test_safe_dict_hides_secrets(self) -> None:
        safe = self.cfg.safe_dict()
        self.assertNotEqual(safe["api_key"], "super-secret-api-key-123")
        self.assertNotEqual(safe["init_param"], "B" * 32)

    def test_to_dict_still_usable_for_saving(self) -> None:
        # to_dict() harus menyimpan nilai asli (dipakai save), bukan disamarkan.
        self.assertEqual(self.cfg.to_dict()["api_key"], "super-secret-api-key-123")


class TestPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip(self) -> None:
        cfg = valid_cykeo_config()
        save_config(cfg, self.path)
        loaded = load_config(self.path)
        self.assertEqual(loaded.server_url, cfg.server_url)
        self.assertEqual(loaded.reader_code, cfg.reader_code)
        self.assertEqual(loaded.com_port, cfg.com_port)
        self.assertEqual(loaded.baudrate, cfg.baudrate)
        self.assertEqual(loaded.mode, cfg.mode)
        self.assertEqual(loaded.init_param, cfg.init_param)
        self.assertEqual(loaded.batch_size, cfg.batch_size)

    def test_saved_file_is_json(self) -> None:
        save_config(valid_cykeo_config(), self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("api_key", data)
        self.assertIn("reader_code", data)

    @unittest.skipIf(os.name == "nt", "chmod 0600 hanya bermakna di POSIX")
    def test_saved_file_permissions_600(self) -> None:
        save_config(valid_cykeo_config(), self.path)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600, "file config berisi api key harus 0600")

    def test_invalid_json_raises_config_error(self) -> None:
        self.path.write_text("{ bukan json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_missing_file_returns_defaults(self) -> None:
        cfg = load_config(self.path)  # belum ada -> default + env
        self.assertIsInstance(cfg, Config)

    def test_string_coercion_from_manual_edit(self) -> None:
        self.path.write_text(json.dumps({
            "reader_code": "CYKEO-01", "baudrate": "9600", "batch_size": "25",
            "flush_interval": "2.5", "simulator_loop": "false",
        }), encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg.baudrate, 9600)
        self.assertEqual(cfg.batch_size, 25)
        self.assertEqual(cfg.flush_interval, 2.5)
        self.assertIs(cfg.simulator_loop, False)

    def test_unknown_keys_go_to_extra(self) -> None:
        self.path.write_text(json.dumps({"reader_code": "X", "mystery": 1}), encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg.extra["mystery"], 1)
        self.assertEqual(cfg.to_dict()["mystery"], 1)

    def test_example_config_is_loadable(self) -> None:
        example = Path(__file__).resolve().parent.parent / "config.example.json"
        cfg = load_config(example)
        self.assertIn(cfg.mode, ("zebra", "cykeo", "simulator"))
        self.assertTrue(cfg.events_url.endswith("/events"))


class TestEnvOverrides(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in
                       (ENV_API_KEY, ENV_SERVER_URL, ENV_BAUDRATE, ENV_MODE)}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_wins_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(valid_cykeo_config(), path)
            os.environ[ENV_API_KEY] = "env-key-placeholder"
            os.environ[ENV_SERVER_URL] = "http://127.0.0.1:9999"
            os.environ[ENV_BAUDRATE] = "9600"
            os.environ[ENV_MODE] = "simulator"
            cfg = load_config(path)
            self.assertEqual(cfg.api_key, "env-key-placeholder")
            self.assertEqual(cfg.server_url, "http://127.0.0.1:9999")
            self.assertEqual(cfg.baudrate, 9600)
            self.assertEqual(cfg.mode, "simulator")


if __name__ == "__main__":
    unittest.main()
