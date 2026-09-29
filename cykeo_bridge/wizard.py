"""Wizard konfigurasi bridge agent.

Dua mode:
- **GUI tkinter** (default di Windows dengan desktop): form dengan IP/port
  server, reader code, API key, mode reader, COM, baud, initParam, dan
  checkbox "aktifkan service".
- **CLI interaktif** (fallback, dipakai juga di server/headless).

Wizard juga menulis file ``bridge_service.json`` yang dibaca installer/Task
Scheduler untuk mendaftarkan agent sebagai startup task Windows.

Tidak pernah mencetak api key / initParam; hanya menampilkan "TERISI".
"""

from __future__ import annotations

import json
import sys
from getpass import getpass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import (
    DEFAULT_BAUDRATE,
    DEFAULT_COM_PORT,
    DEFAULT_SERVER_URL,
    INIT_PARAM_RE,
    MODE_CYKEO,
    MODE_SIMULATOR,
    MODE_ZEBRA,
    VALID_MODES,
    Config,
    default_config_path,
    load_config,
    save_config,
)

__all__ = ["run_wizard", "collect_cli", "write_service_manifest", "service_manifest_path"]

SERVICE_NAME = "CykeoBridge"
MODE_LABELS = {
    MODE_CYKEO: "cykeo  (CK-D5 via native GReader.dll, butuh DLL Cykeo)",
    MODE_ZEBRA: "zebra  (FX7500 pasif — konfigurasi reader TIDAK diubah)",
    MODE_SIMULATOR: "simulator  (fixture, tanpa hardware — untuk uji coba)",
}


def service_manifest_path(config_path: Optional[Path] = None) -> Path:
    """Lokasi manifest service (dipakai installer & Task Scheduler)."""
    base = Path(config_path).parent if config_path else default_config_path().parent
    return base / "bridge_service.json"


def write_service_manifest(enabled: bool, config_path: Path, *,
                           work_dir: Optional[Path] = None) -> Path:
    """Tulis manifest autostart (startup task Windows)."""
    manifest: Dict[str, Any] = {
        "service_name": SERVICE_NAME,
        "enabled": bool(enabled),
        "install_dir": str(work_dir or Path(__file__).resolve().parent.parent),
        "config_path": str(config_path),
        "executable": sys.executable,
        "args": ["-m", "cykeo_bridge", "run", "--config", str(config_path)],
        "trigger": "logon",  # Startup Task; installer bisa ubah ke boot
        "working_directory": str(work_dir or Path(__file__).resolve().parent.parent),
        "stop_on_reboot": False,
    }
    path = service_manifest_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------- #
# CLI interaktif
# ---------------------------------------------------------------------- #
def _ask(prompt: str, default: str = "", *, secret: bool = False,
         reader: Callable[[str], str] = input) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    label = prompt + suffix + ": "
    # Rahasia: pakai getpass (tidak echoed). Saat test memakai reader() kustom,
    # input tetap lewat reader supaya bisa diotomasi.
    if secret and reader is input:
        value = getpass.getpass(label)
    else:
        value = reader(label)
    value = value.strip()
    return value or default


def _ask_int(prompt: str, default: int, *, reader: Callable[[str], str] = input) -> int:
    while True:
        value = _ask(prompt, str(default), reader=reader)
        try:
            return int(value)
        except ValueError:
            print("  ! Masukkan angka bulat.")


def _ask_choice(prompt: str, options: list[str], default: str, *,
                reader: Callable[[str], str] = input) -> str:
    print(prompt)
    for idx, option in enumerate(options, 1):
        marker = "*" if option == default else " "
        print(f"   {marker} {idx}. {MODE_LABELS.get(option, option)}")
    while True:
        value = _ask("Pilih nomor", str(options.index(default) + 1), reader=reader)
        try:
            idx = int(value) - 1
        except ValueError:
            print("  ! Masukkan nomor.")
            continue
        if 0 <= idx < len(options):
            return options[idx]
        print("  ! Nomor di luar range.")


def collect_cli(config_path: Optional[Path] = None, *,
                reader: Callable[[str], str] = input,
                printer: Callable[[str], None] = print) -> Config:
    """Kumpulkan konfigurasi lewat CLI interaktif."""
    existing = Config()
    if config_path and Path(config_path).exists():
        try:
            existing = load_config(config_path)
            printer(f"Config lama ditemukan: {existing.safe_dict()['reader_code']} @ {existing.base_url}")
        except Exception:  # noqa: BLE001
            printer("Config lama tidak terbaca, mulai dari default.")

    printer("\n=== Wizard Konfigurasi Cykeo RFID Bridge ===")
    printer("(Ctrl+C untuk batal; nilai kosong = pakai default)\n")

    server_url = _ask("IP/host server RFID (tanpa http:// ikut?) — contoh 192.168.1.10:8080",
                      existing.server_url, reader=reader)
    if not server_url.startswith(("http://", "https://")):
        server_url = f"http://{server_url}"
    reader_code = _ask("Reader code", existing.reader_code or "CYKEO-01", reader=reader)
    api_key = _ask("API key reader (dari server RFID)", existing.api_key, secret=True, reader=reader)
    mode = _ask_choice("Mode reader:", list(VALID_MODES), existing.mode or MODE_CYKEO, reader=reader)

    com_port = existing.com_port or DEFAULT_COM_PORT
    baudrate = int(existing.baudrate or DEFAULT_BAUDRATE)
    init_param = existing.init_param or INIT_PARAM_DEFAULT
    if mode == MODE_CYKEO:
        com_port = _ask("Port COM", com_port, reader=reader)
        baudrate = _ask_int("Baudrate", baudrate, reader=reader)
        helper_path = _ask("Path cykeo-helper.exe (kosong = ikut folder bridge)",
                           existing.helper_path or "", reader=reader)
        sdk_dir = _ask("Folder SDK vendor (GReaderApi.dll, kosong = ikut bridge)",
                       existing.sdk_dir or "", reader=reader)
        if not helper_path and not sdk_dir:
            printer("  ! Helper .NET dan SDK vendor tidak ditemukan — install ulang")
            printer("    dengan install_cykeo.ps1, atau isi path manual di bawah.")
        init_param = _ask("initParam Cykeo (hex, opsional — default resmi OK)",
                          init_param, secret=True, reader=reader)
        if init_param and not INIT_PARAM_RE.match(init_param):
            printer("  ! initParam bukan 32 hex — nilai handshake protokol, bukan license key.")
            printer("    Biarkan kosong/default kalau tidak yakin.")
    else:
        printer("  (mode ini tidak memakai COM/helper)")

    batch_size = _ask_int("Ukuran batch", existing.batch_size or 50, reader=reader)
    flush_interval = str(existing.flush_interval or 1.0)
    flush_interval = float(_ask("Interval flush (detik)", flush_interval, reader=reader) or 1.0)

    if mode == MODE_CYKEO:
        from .devices.factory import helper_status
        status = helper_status(helper_path=helper_path, sdk_dir=sdk_dir)
        printer(f"  * Status helper: {status['reason']}")
        if not status["available"]:
            printer("    Saran: install ulang dengan install_cykeo.ps1, atau pakai")
            printer("    mode simulator dulu untuk uji pipeline tanpa reader.")

    enable_service = _ask("Aktifkan autostart saat login Windows? (y/n)", "n", reader=reader).lower()
    enabled = enable_service.startswith("y")

    cfg = Config(
        server_url=server_url,
        reader_code=reader_code,
        api_key=api_key,
        com_port=com_port,
        baudrate=baudrate,
        mode=mode,
        init_param=init_param,
        helper_path=helper_path,
        sdk_dir=sdk_dir,
        batch_size=batch_size,
        flush_interval=flush_interval,
        enabled=enabled,
    )
    return cfg


def _buka_uji_pembaca(parent) -> None:  # pragma: no cover - GUI
    """Buka dialog uji pembaca; jatuh ke CLI kalau tkinter tidak ada."""
    from .ujipembaca_gui import _uji_pembaca_gui
    _uji_pembaca_gui(parent)


# ---------------------------------------------------------------------- #
# GUI tkinter
# ---------------------------------------------------------------------- #
def _run_gui(config_path: Optional[Path]) -> Optional[Config]:  # pragma: no cover - GUI
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:  # noqa: BLE001
        print(f"[wizard] tkinter tidak tersedia ({exc}); pakai mode CLI.", file=sys.stderr)
        return None

    existing = load_config(config_path) if config_path and Path(config_path).exists() else Config()

    root = tk.Tk()
    root.title("Wizard Konfigurasi Cykeo RFID Bridge")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=12)
    frame.grid()

    fields: Dict[str, Any] = {}

    def add_row(row: int, label: str, key: str, default: str = "", secret: bool = False) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=default)
        show = "*" if secret else ""
        ttk.Entry(frame, textvariable=var, width=42, show=show).grid(row=row, column=1, pady=3)
        fields[key] = var

    add_row(0, "Server RFID (host:port)", "server_url", existing.server_url or DEFAULT_SERVER_URL)
    add_row(1, "Reader code", "reader_code", existing.reader_code or "CYKEO-01")
    add_row(2, "API key reader", "api_key", existing.api_key or "", secret=True)
    add_row(3, "Port COM", "com_port", existing.com_port or DEFAULT_COM_PORT)
    add_row(4, "Baudrate", "baudrate", str(existing.baudrate or DEFAULT_BAUDRATE))
    add_row(5, "initParam Cykeo (hex)", "init_param",
            existing.init_param or INIT_PARAM_DEFAULT, secret=True)

    mode_var = tk.StringVar(value=existing.mode or MODE_CYKEO)
    ttk.Label(frame, text="Mode reader").grid(row=6, column=0, sticky="w", pady=3)
    ttk.Combobox(frame, textvariable=mode_var, values=list(VALID_MODES), state="readonly",
                 width=39).grid(row=6, column=1, pady=3)

    enable_var = tk.BooleanVar(value=bool(existing.enabled))
    ttk.Checkbutton(frame, text="Aktifkan autostart saat login Windows",
                    variable=enable_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=6)

    status = ttk.Label(frame, text="", foreground="#555")
    status.grid(row=8, column=0, columnspan=2, sticky="w")

    from .devices.factory import sdk_status
    sdk = sdk_status()
    status.config(text=("SDK: " + sdk["reason"])[:120])

    def on_save() -> None:
        try:
            baud = int(fields["baudrate"].get() or DEFAULT_BAUDRATE)
        except ValueError:
            messagebox.showerror("Input salah", "Baudrate harus angka.")
            return
        server = fields["server_url"].get().strip()
        if not server.startswith(("http://", "https://")):
            server = f"http://{server}"
        cfg = Config(
            server_url=server,
            reader_code=fields["reader_code"].get().strip(),
            api_key=fields["api_key"].get().strip(),
            com_port=fields["com_port"].get().strip(),
            baudrate=baud,
            mode=mode_var.get(),
            init_param=(fields["init_param"].get().strip() or INIT_PARAM_DEFAULT),
            enabled=bool(enable_var.get()),
        )
        errors = cfg.validate(strict=True)
        if errors:
            messagebox.showerror("Konfigurasi belum valid", "\n".join(f"- {e}" for e in errors))
            return
        target = Path(config_path) if config_path else default_config_path()
        save_config(cfg, target)
        write_service_manifest(bool(enable_var.get()), target)
        messagebox.showinfo("Tersimpan", f"Config ditulis ke:\n{target}\n\n"
                                         "Jalankan agent dengan:\n  "
                                         f"{sys.executable} -m cykeo_bridge run --config {target}")
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=9, column=0, columnspan=2, pady=(10, 0))
    ttk.Button(buttons, text="Simpan", command=on_save).pack(side="left", padx=6)
    ttk.Button(buttons, text="Uji Pembaca",
               command=lambda: _buka_uji_pembaca(root)).pack(side="left", padx=6)
    ttk.Button(buttons, text="Batal", command=root.destroy).pack(side="left", padx=6)
    root.bind("<Return>", lambda _e: on_save())
    root.mainloop()
    return None  # config disimpan di dalam on_save


# ---------------------------------------------------------------------- #
def run_wizard(config_path: Optional[str] = None, *, prefer_gui: bool = True) -> int:
    """Jalankan wizard. Returns exit code."""
    path = Path(config_path) if config_path else default_config_path()
    if prefer_gui and sys.platform == "win32":
        if _run_gui(path) is None:
            return 0
    # fallback CLI
    try:
        cfg = collect_cli(path)
    except (KeyboardInterrupt, EOFError):
        print("\nWizard dibatalkan.")
        return 1
    errors = cfg.validate(strict=False)
    if errors:
        print("\nKonfigurasi belum lengkap:")
        for err in errors:
            print(f"  - {err}")
        if cfg.mode == MODE_CYKEO and any("init_param" in e for e in errors):
            print("\nSaran: jalankan wizard lagi dan pilih mode 'simulator' untuk uji coba.")
        again = input("Simpan trotz itu? (y/N): ").strip().lower()
        if not again.startswith("y"):
            return 1
    save_config(cfg, path)
    manifest = write_service_manifest(cfg.enabled, path)
    print(f"\nConfig tersimpan : {path}")
    print(f"Manifest service : {manifest}")
    print(f"Uji pipeline tanpa hardware:")
    print(f"  {sys.executable} -m cykeo_bridge run --config {path} --simulate --max-iterations 5 "
          f"--print-payload")
    print(f"Status konfigurasi:")
    print(f"  {sys.executable} -m cykeo_bridge check --config {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_wizard(sys.argv[1] if len(sys.argv) > 1 else None))
