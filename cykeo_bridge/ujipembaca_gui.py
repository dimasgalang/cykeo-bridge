"""
Dialog "Uji Pembaca" untuk wizard GUI.

Tombolnya membuka jendela terpisah yang menjalankan uji baca lokal, sehingga
teknisi bisa menguji reader LANGSUNG dari installer/wizard — sebelum menjalankan
agent dan sebelum data dikirim ke server.

Kenapa dialog terpisah, bukan popup sederhana:
- Pembacaan COM memblokir selama beberapa detik. Kalau dijalankan di thread UI,
  wizard akan membeku dan terlihat hang — pengguna akan menyimpulkan installer
  rusak. Jadi dijalankan di thread worker, dan UI hanya menampilkan progres.
-毎回 tag baru ditemukan, daftar EPC di-update langsung. Ini memberi
  umpan balik "reader hidup" yang jauh lebih berguna daripada "OK/GAGAL" di
  akhir.
"""

from __future__ import annotations

import queue
import threading
from typing import Optional


def _uji_pembaca_gui(parent) -> None:  # pragma: no cover - GUI
    """Buka dialog uji pembaca. Import tkinter dilakukan lokal."""
    try:
        import tkinter as tk
        from tkinter import scrolledtext, ttk
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[wizard] tkinter tidak tersedia ({exc})", file=sys.stderr)
        return

    from .readertest import jalankan_uji_dari_file

    jendela = tk.Toplevel(parent)
    jendela.title("Uji Pembaca RFID — lokal, tanpa kirim ke server")
    jendela.geometry("640x460")

    tk.Label(
        jendela,
        text=("Arahkan RFID tag ke antena reader. Uji berhenti otomatis "
              "setelah 15 detik.\nTidak ada data yang dikirim ke server."),
        justify="left",
    ).pack(anchor="w", padx=10, pady=(10, 4))

    status = tk.Label(jendela, text="Siap.", anchor="w")
    status.pack(anchor="w", padx=10, pady=(0, 6))

    keluaran = scrolledtext.ScrolledText(jendela, wrap="word", height=18)
    keluaran.pack(fill="both", expand=True, padx=10, pady=(0, 6))

    def tulis(teks: str) -> None:
        keluaran.insert("end", teks)
        keluaran.see("end")

    tombol = ttk.Frame(jendela)
    tombol.pack(fill="x", padx=10, pady=(0, 10))
    tombol_uji = ttk.Button(tombol, text="Mulai Uji", command=lambda: mulai())
    tombol_uji.pack(side="left")
    ttk.Button(tombol, text="Tutup", command=jendela.destroy).pack(side="right")

    #: Antrean dari thread worker -> UI. Tkinter hanya boleh disentuh dari
    #: thread utama, jadi worker tidak boleh langsung menulis ke widget.
    pesan: "queue.Queue[tuple]" = queue.Queue()

    def mulai() -> None:
        keluaran.delete("1.0", "end")
        tombol_uji.config(state="disabled")
        status.config(text="Membaca... arahkan tag ke antena reader.")

        def worker() -> None:
            try:
                hasil = jalankan_uji_dari_file(
                    None,  # pakai config default
                    durasi=15.0,
                    on_tag=lambda epc: pesan.put(("tag", epc)),
                )
                for baris in hasil.as_lines():
                    pesan.put(("teks", baris))
            except Exception as exc:  # noqa: BLE001
                pesan.put(("teks", f"Error tidak terduga: {type(exc).__name__}: {exc}"))
            finally:
                pesan.put(("selesai", ""))

        threading.Thread(target=worker, daemon=True).start()

    def pompa() -> None:
        """Salin pesan dari worker ke UI. Dijadwalkan ulang oleh after()."""
        selesai = False
        while True:
            try:
                jenis, isi = pesan.get_nowait()
            except queue.Empty:
                break
            if jenis == "tag":
                tulis(f"  + {isi}\n")
            elif jenis == "teks":
                tulis(f"{isi}\n")
            else:
                selesai = True
        if selesai:
            status.config(text="Uji selesai. Lihat ringkasan di bawah.")
            tombol_uji.config(state="normal")
            return
        jendela.after(100, pompa)

    def mulai_dan_pompa() -> None:
        mulai()
        pompa()

    # Ganti command tombol setelah semua closure siap.
    tombol_uji.config(command=mulai_dan_pompa)
    pompa()
