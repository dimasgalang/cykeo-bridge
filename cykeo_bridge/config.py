"""Konfigurasi bridge agent (JSON di disk).

Catatan keamanan:
- ``api_key`` TIDAK PERNAH ditulis ke log. Gunakan :meth:`Config.safe_dict`
  atau ``redact`` kalau butuh mencetak konfigurasi.
- ``initParam`` Cykeo (hex) ikut disensor agar tidak bocor ke log.
- Nilai/api key boleh dioverride lewat environment variable (lihat
  :data:`ENV_API_KEY` dll) supaya file config tidak perlu berisi rahasia.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "Config",
    "ConfigError",
    "MODE_ZEBRA",
    "MODE_CYKEO",
    "MODE_SIMULATOR",
    "VALID_MODES",
    "DEFAULT_SERVER_URL",
    "DEFAULT_BAUDRATE",
    "INIT_PARAM_RE",
    "INIT_PARAM_DEFAULT",
    "load_config",
    "save_config",
    "config_from_env",
]

MODE_ZEBRA = "zebra"
MODE_CYKEO = "cykeo"
MODE_SIMULATOR = "simulator"
VALID_MODES = (MODE_ZEBRA, MODE_CYKEO, MODE_SIMULATOR)

#: Placeholder — GANTI dengan host server RFID produksi sebelum deploy.
DEFAULT_SERVER_URL = "http://192.168.1.10:8080"
DEFAULT_BAUDRATE = 115200
DEFAULT_BATCH_SIZE = 50
DEFAULT_FLUSH_INTERVAL = 1.0
DEFAULT_COM_PORT = "COM1"

#: `initParam` Cykeo = parameter handshake protokol, BUKAN license key.
#: Nilai ini adalah konstanta contoh resmi yang dipakai di Quick Start kedua
#: dokumen Cykeo (Development Guide & Demo Software Operation Manual) dan
#: dikonfirmasi teknisi SML: tidak ada license key 32 hex per-device.
#: Lihat ``cykeo-docs/FASE4-FINDINGS.md`` §3.
INIT_PARAM_DEFAULT = "E5DC18EDBEEE6EAD954D7332E6D90361"
INIT_PARAM_RE = re.compile(r"^[0-9A-Fa-f]+$")

ENV_PREFIX = "RFID_BRIDGE_"
ENV_SERVER_URL = ENV_PREFIX + "SERVER_URL"
ENV_READER_CODE = ENV_PREFIX + "READER_CODE"
ENV_API_KEY = ENV_PREFIX + "API_KEY"
ENV_COM_PORT = ENV_PREFIX + "COM_PORT"
ENV_BAUDRATE = ENV_PREFIX + "BAUDRATE"
ENV_MODE = ENV_PREFIX + "MODE"
ENV_INIT_PARAM = ENV_PREFIX + "INIT_PARAM"
ENV_QUEUE_PATH = ENV_PREFIX + "QUEUE_PATH"
ENV_LOG_PATH = ENV_PREFIX + "LOG_PATH"

REDACTED = "***REDACTED***"

#: Kunci yang tidak boleh muncul di log / output wizard.
#: `init_param` ikut disensor walau secara teknis bukan rahasia — nilainya
#: parameter handshake unit, dan tidak perlu bocor lewat log/traceback.
SECRET_KEYS = ("api_key", "init_param")


class ConfigError(ValueError):
    """Konfigurasi tidak valid."""


def _default_dir() -> Path:
    """Direktori data agent: %ProgramData% di Windows, ~/.local/share di Linux."""
    if os.name == "nt":
        base = os.environ.get("ProgramData") or "C:\\ProgramData"
        return Path(base) / "CykeoBridge"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "cykeo-bridge"


def default_config_path() -> Path:
    """Lokasi default ``config.json``."""
    env = os.environ.get(ENV_PREFIX + "CONFIG")
    if env:
        return Path(env)
    return _default_dir() / "config.json"


@dataclass
class Config:
    """Konfigurasi runtime bridge agent."""

    server_url: str = DEFAULT_SERVER_URL
    reader_code: str = "CYKEO-01"
    api_key: str = ""
    com_port: str = DEFAULT_COM_PORT
    baudrate: int = DEFAULT_BAUDRATE
    mode: str = MODE_CYKEO
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval: float = DEFAULT_FLUSH_INTERVAL
    log_path: str = ""
    queue_path: str = ""
    #: Parameter handshake protokol Cykeo (hex). BUKAN license key — nilai
    #: default-nya konstanta resmi SDK, lihat FASE4-FINDINGS.md §3.
    init_param: str = INIT_PARAM_DEFAULT
    # Opsional — tidak ada di kontrak payload, hanya untuk adaptasi device.
    antenna: Optional[int] = None
    #: Path eksplisit `cykeo-helper.exe`. Kosong = autodetect.
    helper_path: str = ""
    #: Folder SDK Cykeo (berisi GReaderApi.dll). Kosong = folder helper.
    sdk_dir: str = ""
    poll_interval: float = 0.25
    #: Timeout koneksi reader. Production memanggil
    #: ``OpenSerial(reader, 60)`` (detik) — jangan kurang dari 60, karena
    #: reader CK-D5 butuh lama handshake setelah power-on.
    connect_timeout: float = 60.0
    request_timeout: float = 10.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    simulator_fixture: str = ""
    simulator_loop: bool = True
    simulator_delay: float = 0.05
    dedup_window: float = 0.0  # 0 = tanpa dedup window (dedup hanya per batch)
    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # properties
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        """server_url tanpa trailing slash."""
        return (self.server_url or "").rstrip("/")

    @property
    def events_url(self) -> str:
        """Endpoint kontrak §4 (bentuknya TIDAK boleh diubah)."""
        return f"{self.base_url}/api/v1/rfid/readers/{self.reader_code}/events"

    @property
    def log_file(self) -> Path:
        return Path(self.log_path) if self.log_path else _default_dir() / "logs" / "bridge.log"

    @property
    def queue_file(self) -> Path:
        return Path(self.queue_path) if self.queue_path else _default_dir() / "queue.jsonl"

    @property
    def is_simulated(self) -> bool:
        return self.mode == MODE_SIMULATOR

    @property
    def is_cykeo(self) -> bool:
        return self.mode == MODE_CYKEO

    @property
    def serial_target(self) -> str:
        """Connection string Cykeo, mis. ``COM12:115200`` (guide §3.1)."""
        return f"{self.com_port}:{self.baudrate}"

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    def validate(self, *, strict: bool = False) -> list[str]:
        """Kembalikan daftar pesan error (kosong = valid).

        ``strict=True`` dipakai wizard: api_key kosong dan initParam tidak
        sah dianggap error, bukan sekadar peringatan.
        """
        errors: list[str] = []
        if not self.server_url:
            errors.append("server_url kosong")
        elif not re.match(r"^https?://", self.server_url):
            errors.append("server_url harus diawali http:// atau https://")
        if not self.reader_code:
            errors.append("reader_code kosong")
        elif not re.match(r"^[A-Za-z0-9._-]{1,64}$", self.reader_code):
            errors.append("reader_code hanya boleh huruf/angka/._- (maks 64)")
        if self.mode not in VALID_MODES:
            errors.append(f"mode harus salah satu dari {VALID_MODES}")
        if not isinstance(self.baudrate, int) or self.baudrate <= 0:
            errors.append("baudrate harus bilangan bulat positif")
        if self.batch_size < 1:
            errors.append("batch_size minimal 1")
        if self.flush_interval <= 0:
            errors.append("flush_interval harus > 0")
        if self.mode == MODE_CYKEO:
            if not self.com_port:
                errors.append("com_port kosong")
            # CATATAN: Cykeo TIDAK memakai license key per-device (konfirmasi
            # teknisi SML + nilai contoh identik di kedua dokumen resmi).
            # `init_param` hanya perlu hex; default-nya konstanta resmi SDK.
            if strict and not INIT_PARAM_RE.match(self.init_param or ""):
                errors.append("init_param harus hex non-kosong (lihat FASE4-FINDINGS.md §3)")
        if self.api_key == "" and (strict or self.mode != MODE_SIMULATOR):
            errors.append("api_key kosong (ambil dari server RFID)")
        return errors

    def is_valid(self, *, strict: bool = False) -> bool:
        return not self.validate(strict=strict)

    # ------------------------------------------------------------------ #
    # secrets
    # ------------------------------------------------------------------ #
    def redact(self) -> "Config":
        """Salinan dengan api_key + init_param disamarkan (aman untuk log)."""
        clone = Config.from_dict(self.to_dict())
        for key in SECRET_KEYS:
            if getattr(clone, key):
                setattr(clone, key, REDACTED)
        return clone

    def safe_dict(self) -> Dict[str, Any]:
        """Dict aman untuk dicetak ke log — rahasia disamarkan."""
        return self.redact().to_dict()

    def __repr__(self) -> str:  # noqa: D105
        """Cegah api_key bocor lewat print()/traceback."""
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SECRET_KEYS and value:
                value = REDACTED
            parts.append(f"{f.name}={value!r}")
        return f"Config({', '.join(parts)})"

    __str__ = __repr__

    # ------------------------------------------------------------------ #
    # (de)serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        data.update(extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        kwargs: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key in known:
                kwargs[key] = value
            else:
                extra[key] = value
        # coercion ringan supaya file hasil edit manual tidak bikin crash
        if "baudrate" in kwargs and kwargs["baudrate"] is not None:
            try:
                kwargs["baudrate"] = int(kwargs["baudrate"])
            except (TypeError, ValueError):
                pass
        for int_key in ("batch_size", "max_retries", "antenna"):
            if int_key in kwargs and kwargs[int_key] is not None:
                try:
                    kwargs[int_key] = int(kwargs[int_key])
                except (TypeError, ValueError):
                    kwargs.pop(int_key)
        for float_key in ("flush_interval", "poll_interval", "connect_timeout",
                          "request_timeout", "backoff_base", "backoff_max",
                          "simulator_delay", "dedup_window"):
            if float_key in kwargs and kwargs[float_key] is not None:
                try:
                    kwargs[float_key] = float(kwargs[float_key])
                except (TypeError, ValueError):
                    kwargs.pop(float_key)
        for bool_key in ("simulator_loop", "enabled"):
            if bool_key in kwargs:
                kwargs[bool_key] = _as_bool(kwargs[bool_key])
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)

    def save(self, path: Optional[os.PathLike | str] = None, *, chmod_600: bool = True) -> Path:
        return save_config(self, path, chmod_600=chmod_600)

    @classmethod
    def load(cls, path: os.PathLike | str) -> "Config":
        return load_config(path)


def load_config(path: Optional[os.PathLike | str] = None) -> Config:
    """Baca config dari JSON; env var menutupi nilai di file."""
    target = Path(path) if path else default_config_path()
    data: Dict[str, Any] = {}
    if target.exists():
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Gagal membaca {target}: {exc}") from exc
        raw = raw.strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"JSON tidak valid di {target}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Isi {target} harus object JSON")
    cfg = Config.from_dict(data)
    _apply_env_overrides(cfg)
    return cfg


def _as_bool(value: Any) -> bool:
    """Coercion bool yang aman untuk file config yang diedit manual.

    ``bool("false")`` di Python bernilai ``True`` — jebakan yang membuat
    ``"simulator_loop": "false"`` (string) justru mengaktifkan loop. String
    dan angka juga ditangani di sini.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "aktif")
    return bool(value)


def save_config(cfg: Config, path: Optional[os.PathLike | str] = None, *,
                chmod_600: bool = True) -> Path:
    """Simpan config ke JSON (dibuat 0600 di platform POSIX)."""
    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.to_dict()
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if chmod_600 and os.name != "nt":
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target


def _apply_env_overrides(cfg: Config) -> None:
    """Environment variable menang atas file — dipakai untuk Rahasia."""
    mapping = {
        ENV_SERVER_URL: ("server_url", str),
        ENV_READER_CODE: ("reader_code", str),
        ENV_API_KEY: ("api_key", str),
        ENV_COM_PORT: ("com_port", str),
        ENV_BAUDRATE: ("baudrate", int),
        ENV_MODE: ("mode", str),
        ENV_INIT_PARAM: ("init_param", str),
        ENV_QUEUE_PATH: ("queue_path", str),
        ENV_LOG_PATH: ("log_path", str),
    }
    for env_name, (attr, caster) in mapping.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        try:
            setattr(cfg, attr, caster(value))
        except (TypeError, ValueError):
            continue


def config_from_env(**defaults: Any) -> Config:
    """Buat Config dari environment saja (dipakai smoke test / CI)."""
    cfg = Config(**defaults)
    _apply_env_overrides(cfg)
    return cfg
