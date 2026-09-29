"""Loop utama bridge agent.

Alur:
1. Baca tag dari adapter (READ-ONLY).
2. Normalisasi EPC (kontrak §2) **hanya untuk dedup lokal** — payload yang
   dikirim ke server tetap EPC MENTAH apa adanya dari device (kontrak §4).
3. Masukkan event ke antrean lokal persisted (JSONL) supaya tidak hilang saat
   server offline.
4. Flush antrean ke server saat ``batch_size`` penuh atau ``flush_interval``
   tercapai. 200 -> commit; 422 -> pindahkan ke file rejected; 401 -> FATAL,
   agent berhenti dengan pesan jelas.
5. Handle SIGINT/SIGTERM -> flush terakhir lalu keluar bersih (exit 0).

Bridge TIDAK membuka port listener; hanya koneksi keluar (kontrak §1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .config import (
    MODE_SIMULATOR,
    Config,
    ConfigError,
    default_config_path,
    load_config,
)
from .devices.base import DeviceAdapter, DeviceError, RawTag
from .devices.factory import build_device
from .epc import normalize
from .http_client import HttpResult, RfidHttpClient
from .logging_setup import register_secret, setup_logging
from .queue import PersistentQueue

__all__ = ["BridgeAgent", "main", "build_arg_parser"]

logger = logging.getLogger("cykeo_bridge.agent")

EXIT_OK = 0
EXIT_FATAL = 2
EXIT_DEVICE = 3
EXIT_CONFIG = 4


class FatalAuthError(RuntimeError):
    """401 — hentikan agent, jangan retry."""


class BridgeAgent:
    """Agent RFID: adapter -> antrean -> HTTP POST."""

    def __init__(self, config: Config, device: Optional[DeviceAdapter] = None,
                 client: Optional[RfidHttpClient] = None,
                 queue: Optional[PersistentQueue] = None) -> None:
        self.config = config
        self.device = device
        self.client = client or RfidHttpClient(config)
        self.queue = queue or PersistentQueue(config.queue_file)
        self._stop = False
        self._shut_done = False
        self._seen_in_batch: Dict[str, float] = {}
        self.stats = {
            "read": 0, "queued": 0, "sent": 0, "rejected": 0, "invalid": 0,
            "deduped": 0, "batches": 0, "failed_batches": 0,
        }

    # ------------------------------------------------------------------ #
    # shutdown
    # ------------------------------------------------------------------ #
    def request_stop(self, signum: Optional[int] = None, _frame: Any = None) -> None:
        if not self._stop:
            logger.info("Shutdown diminta (signal=%s), menyelesaikan flush terakhir...", signum)
        self._stop = True

    @property
    def stopping(self) -> bool:
        return self._stop

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):  # bukan main thread / platform tanpa sinyal
                logger.debug("tidak bisa memasang handler untuk %s", sig)

    # ------------------------------------------------------------------ #
    # intake
    # ------------------------------------------------------------------ #
    def _dedup_key(self, tag: RawTag) -> Optional[str]:
        """Kunci dedup = EPC ternormalisasi. None = tidak bisa dide-dup."""
        return normalize(tag.epc)

    def collect(self, tags: Sequence[RawTag], now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Terima tag dari adapter -> event (dict kontrak) yang layak dikirim.

        - EPC yang gagal dinormalisasi tetap DIKIRIM mentah (server yang
          menormalisasi & menolak), hanya dedup lokal yang dilewati.
        - Duplikat dalam ``dedup_window`` di-drop agar batch tidak membludak.
        """
        now = time.monotonic() if now is None else now
        window = float(self.config.dedup_window or 0.0)
        events: List[Dict[str, Any]] = []
        for tag in tags:
            self.stats["read"] += 1
            key = self._dedup_key(tag)
            if key is None:
                self.stats["invalid"] += 1
                logger.warning("agent: EPC tidak bisa dinormalisasi (tetap dikirim mentah): %r", tag.epc)
            else:
                last = self._seen_in_batch.get(key)
                if last is not None and (window <= 0 or (now - last) < window):
                    self.stats["deduped"] += 1
                    continue
                self._seen_in_batch[key] = now
            # EVENT: payload memakai EPC MENTAH (kontrak §4)
            events.append(tag.to_event())
        if window > 0:
            cutoff = now - window
            self._seen_in_batch = {k: v for k, v in self._seen_in_batch.items() if v >= cutoff}
        if events:
            self.stats["queued"] += len(events)
        return events

    # ------------------------------------------------------------------ #
    # flush
    # ------------------------------------------------------------------ #
    def flush_once(self) -> Optional[HttpResult]:
        """Kirim satu batch dari antrean. Returns None kalau antrean kosong."""
        batch = self.queue.peek(self.config.batch_size)
        if not batch:
            return None
        result = self.client.post_batch(batch)
        self.stats["batches"] += 1
        if result.ok:
            self.queue.commit(len(batch))
            self.stats["sent"] += len(batch)
            logger.info("agent: batch %d event terkirim (accepted=%s, attempts=%d, %.2fs)",
                        len(batch), result.accepted or "?", result.attempts, result.duration_s)
        elif result.fatal:
            self.stats["failed_batches"] += 1
            # event TIDAK di-commit: tetap tersimpan, akan terkirim setelah key diperbaiki
            raise FatalAuthError(
                "API key ditolak server (HTTP 401). Perbaiki api_key (wizard/env "
                f"RFID_BRIDGE_API_KEY), lalu restart agent. Server: {result.message!r}"
            )
        elif result.rejected:
            self.queue.reject(len(batch), result.message)
            self.stats["rejected"] += len(batch)
            logger.warning("agent: batch ditolak server, event dipindah ke file rejected")
        else:
            self.stats["failed_batches"] += 1
            logger.error("agent: batch gagal terkirim (%s), event tetap di antrean", result.as_log())
        self.queue.enforce_max_events()
        return result

    def drain(self, max_batches: int = 100) -> int:
        """Kirim antrean yang tertinggal dari offline sebelumnya."""
        total = 0
        for _ in range(max_batches):
            if self._stop:
                break
            result = self.flush_once()
            if result is None or not result.ok:
                break
            total += result.accepted or 0
        return total

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    def run(self, max_iterations: Optional[int] = None) -> int:
        """Loop utama. Returns exit code."""
        if self.device is None:
            self.device = build_device(self.config)
        logger.info("agent: device=%s mode=%s reader=%s endpoint=%s",
                    self.config.mode, self.config.mode, self.config.reader_code,
                    self.config.events_url)
        logger.info("agent: device siap -> %s", json.dumps(self.device.describe(), default=str))
        stats_q = self.queue.stats()
        if stats_q.pending:
            logger.info("antrean lokal berisi %d event dari sesi sebelumnya; dikirim dulu",
                        stats_q.pending)
            self.drain()

        buffer: List[Dict[str, Any]] = []
        last_flush = time.monotonic()
        iterations = 0
        waiting_logged = False
        # Reader CK-D5 hanya memindai setelah diberi perintah inventory
        # (MsgBaseInventoryEpc). read_tags() sendiri hanya menguras buffer,
        # jadi tanpa baris ini agent akan berjalan seharian dan tetap 0 tag.
        # Dicoba di dalam loop supaya reader yang belum dicolok saat start
        # tetap dipancing begitu siap.
        inventory_dimulai = False
        try:
            while not self._stop:
                iterations += 1
                if max_iterations is not None and iterations > max_iterations:
                    logger.info("agent: mencapai batas iterasi smoke test (%d)", max_iterations)
                    break
                if not inventory_dimulai:
                    try:
                        self.device.begin_inventory()
                        inventory_dimulai = True
                        logger.info("agent: inventory dimulai, reader memindai")
                    except DeviceError as exc:
                        logger.info("agent: inventory belum bisa dimulai (%s), coba lagi", exc)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("agent: begin_inventory dilewati (%s)", exc)
                        inventory_dimulai = True
                try:
                    tags = self.device.read_tags()
                except DeviceError as exc:
                    # Reader belum dicolok / COM port belum muncul itu kondisi
                    # NORMAL di lapangan, bukan kegagalan fatal. Dulu agent langsung
                    # exit 3 sehingga tray agent restart tanpa henti setiap 10 detik.
                    # Sekarang: tunggu reader, log sekali, lalu coba lagi.
                    if self.config.mode != MODE_SIMULATOR:
                        if not waiting_logged:
                            logger.warning(
                                "agent: reader belum siap (%s). Menunggu port COM; "
                                "colok kabel USB reader CK-D5. Bridge tetap hidup.",
                                exc)
                            waiting_logged = True
                        time.sleep(2.0)
                        continue
                    logger.error("agent: device error: %s", exc)
                    raise
                waiting_logged = False
                if tags:
                    buffer.extend(self.collect(tags))
                if len(buffer) >= self.config.batch_size:
                    self.queue.append(buffer)
                    buffer = []
                    last_flush = time.monotonic()
                    self.flush_once()
                elif buffer and (time.monotonic() - last_flush) >= self.config.flush_interval:
                    self.queue.append(buffer)
                    buffer = []
                    last_flush = time.monotonic()
                    self.flush_once()
                if not self._stop:
                    time.sleep(max(0.0, float(self.config.poll_interval)))
        finally:
            self.shutdown()
        return EXIT_OK

    def shutdown(self) -> None:
        """Tutup device, flush sisa buffer, log statistik."""
        if self._shut_done:
            return
        self._shut_done = True
        logger.info("agent: shutting down, stats=%s", json.dumps(self.stats))
        try:
            if self.device is not None:
                # Hentikan inventory dulu supaya reader tidak tertinggal memindai.
                try:
                    self.device.end_inventory()
                except Exception:  # noqa: BLE001, S110
                    pass
                extra = self.device.flush_buffer()
                if extra:
                    buffer_events = self.collect(extra)
                    if buffer_events:
                        self.queue.append(buffer_events)
                self.device.close()
        except Exception:  # noqa: BLE001
            logger.exception("agent: error saat menutup device")
        self.log_stats()

    def log_stats(self) -> None:
        qstats = self.queue.stats()
        logger.info("agent: queue=%s", json.dumps(qstats.as_dict()))
        logger.info("agent: ringkasan=%s", json.dumps(self.stats))

# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cykeo-bridge",
        description="Bridge agent RFID (Windows) — baca reader, POST ke server RFID.",
    )
    parser.add_argument("--version", action="version", version=f"cykeo-bridge {__version__}")

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Jalankan agent (loop baca + kirim)")
    run.add_argument("-c", "--config", default=None, help="path ke config.json")
    run.add_argument("--simulate", action="store_true",
                     help="Paksa mode simulator (fixture bawaan, tanpa hardware)")
    run.add_argument("--fixture", default=None, help="path fixture JSON/JSONL untuk simulator")
    run.add_argument("--server-url", default=None, help="Override server_url")
    run.add_argument("--reader-code", default=None, help="Override reader_code")
    run.add_argument("--api-key", default=None, help="Override api key (hindari; pakai env)")
    run.add_argument("--com-port", default=None, help="Override COM port (mis. COM12)")
    run.add_argument("--baudrate", type=int, default=None, help="Override baudrate")
    run.add_argument("--mode", default=None, help="zebra | cykeo | simulator")
    run.add_argument("--batch-size", type=int, default=None)
    run.add_argument("--flush-interval", type=float, default=None)
    run.add_argument("--max-iterations", type=int, default=None,
                     help="Berhenti setelah N iterasi (untuk smoke test)")
    run.add_argument("--no-log-file", action="store_true", help="Log hanya ke console")
    run.add_argument("--verbose", "-v", action="store_true", help="Level DEBUG")
    run.add_argument("--print-payload", action="store_true",
                     help="Cetak payload JSON tiap batch (tanpa api key)")

    sub.add_parser("check", help="Validasi config + tampilkan status device/SDK")
    check = sub.choices["check"]
    check.add_argument("-c", "--config", default=None)

    # Wizard = first-run configuration yang dipakai installer & user.
    wizard = sub.add_parser("wizard", help="Wizard konfigurasi interaktif (first-run)")
    wizard.add_argument("-c", "--config", default=None, help="path ke config.json")
    wizard.add_argument("--cli", action="store_true",
                        help="Pakai wizard CLI, bukan GUI tkinter")
    wizard.add_argument("--no-autostart", action="store_true",
                        help="Jangan daftarkan startup task saat selesai")

    sub.add_parser("config-path", help="Tampilkan lokasi config default")

    # Uji pembaca lokal: baca EPC langsung dari reader, TIDAK kirim ke server.
    # Dipakai teknisi untuk membuktikan hardware sebelum menyalahkan server.
    uji = sub.add_parser(
        "test-reader",
        help="Uji baca RFID langsung dari reader (TIDAK kirim ke server)",
    )
    uji.add_argument("-c", "--config", default=None, help="path ke config.json")
    uji.add_argument("--duration", type=float, default=None,
                     help="durasi uji dalam detik (default 15)")
    return parser


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if getattr(args, "server_url", None):
        cfg.server_url = args.server_url
    if getattr(args, "reader_code", None):
        cfg.reader_code = args.reader_code
    if getattr(args, "api_key", None):
        cfg.api_key = args.api_key
    if getattr(args, "com_port", None):
        cfg.com_port = args.com_port
    if getattr(args, "baudrate", None):
        cfg.baudrate = args.baudrate
    if getattr(args, "mode", None):
        cfg.mode = args.mode
    if getattr(args, "batch_size", None):
        cfg.batch_size = args.batch_size
    if getattr(args, "flush_interval", None):
        cfg.flush_interval = args.flush_interval
    if getattr(args, "fixture", None):
        cfg.simulator_fixture = args.fixture
    if getattr(args, "simulate", False):
        cfg.mode = MODE_SIMULATOR


def _wizard_hint() -> str:
    """Petunjuk menjalankan wizard yang BENAR-BENAR bisa jalan.

    Di interpreter embeddable, python312._pth mengaktifkan isolated mode:
    sys.path hanya berisi isi _pth, sehingga cwd dan PYTHONPATH diabaikan.
    Karena itu "python -m cykeo_bridge wizard" gagal dengan
    "No module named cykeo_bridge" - persis yang terjadi di produksi
    (29 Sep 2026: bridge start 100x lebih, module tidak pernah ketemu).

    bridge_launcher.py dipanggil sebagai path file (bukan -m), jadi tidak
    bergantung sys.path. Kalau file itu ada, sarankan itu; kalau tidak,
    baru jatuh ke -m untuk instalasi python biasa.
    """
    launcher = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "bridge_launcher.py"
    )
    if os.path.isfile(launcher):
        return 'Jalankan wizard: "%s" "%s" wizard' % (sys.executable, launcher)
    return "Jalankan wizard: python -m cykeo_bridge wizard"


def cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[config] ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    _apply_overrides(cfg, args)

    errors = cfg.validate(strict=False)
    if errors and cfg.mode != MODE_SIMULATOR:
        print("[config] ERROR:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(_wizard_hint(), file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(None if args.no_log_file else cfg.log_file,
                  level=logging.DEBUG if args.verbose else logging.INFO,
                  console=True,
                  secrets=(cfg.api_key, cfg.init_param))
    register_secret(cfg.api_key, cfg.init_param)
    # Catat config tanpa membocorkan rahasia
    logger.info("config: %s", json.dumps(cfg.safe_dict(), indent=2, default=str))

    agent = BridgeAgent(cfg)
    agent.install_signal_handlers()
    if args.print_payload:
        _install_payload_logger(agent)
    try:
        return agent.run(max_iterations=args.max_iterations)
    except FatalAuthError as exc:
        logger.critical("FATAL: %s", exc)
        return EXIT_FATAL
    except DeviceError as exc:
        logger.critical("FATAL: device tidak bisa dipakai: %s", exc)
        return EXIT_DEVICE
    except KeyboardInterrupt:  # pragma: no cover
        agent.request_stop()
        agent.shutdown()
        return EXIT_OK


def _install_payload_logger(agent: BridgeAgent) -> None:
    """Cetak payload tiap batch (untuk verifikasi bentuk JSON, tanpa api key)."""
    original = agent.client.post_batch

    def wrapped(events):  # type: ignore[no-untyped-def]
        from .http_client import build_payload
        print("[payload] " + json.dumps(build_payload(events), ensure_ascii=False),
              flush=True)
        return original(events)

    agent.client.post_batch = wrapped  # type: ignore[assignment]


def cmd_check(args: argparse.Namespace) -> int:
    from .devices.factory import sdk_status

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[config] ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"config path : {args.config or default_config_path()}")
    print(f"mode        : {cfg.mode}")
    print(f"reader_code : {cfg.reader_code}")
    print(f"server_url  : {cfg.server_url}")
    print(f"endpoint    : {cfg.events_url}")
    print(f"com/baud    : {cfg.com_port} @ {cfg.baudrate}")
    print(f"api_key     : {'TERISI (disamarkan)' if cfg.api_key else 'KOSONG'}")
    print(f"initParam  : {'TERISI (disamarkan)' if cfg.init_param else 'KOSONG'}")
    print(f"log         : {cfg.log_file}")
    print(f"queue       : {cfg.queue_file}")
    status = sdk_status()
    print(f"sdk cykeo   : {'TERSEDIA' if status['available'] else 'TIDAK TERSEDIA'} — {status['reason']}")
    errors = cfg.validate(strict=False)
    if errors:
        print("validasi    : PERLU DIPERBAIKI")
        for err in errors:
            print(f"  - {err}")
        return EXIT_CONFIG
    print("validasi    : OK")
    return EXIT_OK


def _register_windows_startup(config_path: Optional[str]) -> None:
    """Daftarkan startup task Windows (per-user, ONLOGON) bila di Windows.

    Di platform lain (Linux/macOS, mis. saat QA) fungsi ini no-op supaya
    wizard tetap bisa dipakai untuk uji coba tanpa efek samping.
    """
    if os.name != "nt":
        logger.info("startup task: dilewati (platform bukan Windows)")
        return

    from .wizard import write_service_manifest
    path = Path(config_path) if config_path else default_config_path()
    manifest = write_service_manifest(True, path)
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = f'"{exe}" run --config "{path}"'
    else:
        command = f'"{exe}" -m cykeo_bridge run --config "{path}"'

    task_name = "CykeoRfidBridge"
    result = subprocess.run(
        ["schtasks.exe", "/Create", "/F", "/SC", "ONLOGON", "/TN", task_name, "/TR", command],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info("startup task %s dibuat; manifest: %s", task_name, manifest)
    else:
        logger.warning("gagal membuat startup task (exit=%s): %s",
                       result.returncode, (result.stderr or result.stdout).strip())


def _cmd_test_reader(args: argparse.Namespace) -> int:
    """``cykeo_bridge test-reader`` — uji baca lokal, tanpa kirim ke server.

    Exit code sengaja dipisah supaya installer/skrip bisa membedakan
    "reader terbukti salah" dari "reader tidak bisa dibuka sama sekali":
      0 = tag terbaca
      1 = tidak ada tag / reader tidak hidup
      2 = konfigurasi bermasalah
    """
    from .readertest import jalankan_uji_dari_file

    durasi = getattr(args, "duration", None)

    def tampilkan(epc: str) -> None:
        print(f"  + {epc}", flush=True)

    kwargs = {"on_tag": tampilkan}
    if durasi:
        kwargs["durasi"] = float(durasi)

    print("Memulai uji pembaca lokal (tidak ada data yang dikirim ke server)...")
    print("Arahkan RFID tag ke antena reader. Menutup otomatis.\n")

    hasil = jalankan_uji_dari_file(getattr(args, "config", None), **kwargs)

    print()
    for baris in hasil.as_lines():
        print(baris)

    if not hasil.ok:
        if hasil.error and hasil.error.startswith("CONFIG_TIDAK_ADA"):
            return 2
        return 1
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = args.command or "run"
    if command == "run":
        return cmd_run(args)
    if command == "check":
        return cmd_check(args)
    if command == "config-path":
        print(default_config_path())
        return EXIT_OK
    if command == "wizard":
        from .wizard import run_wizard
        config = getattr(args, "config", None)
        # --cli memaksa wizard teks (dipakai installer/script non-interaktif),
        # --no-autostart menahan pendaftaran startup task.
        result = run_wizard(config, prefer_gui=not getattr(args, "cli", False))
        if result == EXIT_OK and not getattr(args, "no_autostart", False):
            _register_windows_startup(config)
        return result
    if command == "test-reader":
        return _cmd_test_reader(args)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
