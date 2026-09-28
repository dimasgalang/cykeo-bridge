"""Unit test ``cykeo_bridge.epc.normalize`` — vektor sama dengan CONTRACT.md §2.

PENTING: test ini sengaja TIDAK memakai ``normalize`` sebagai acuan (tautologi).
Ekspektasi ditulis literal sesuai tabel kontrak §2, termasuk kasus penolakan.
"""

from __future__ import annotations

import unittest

from cykeo_bridge.epc import is_hex_epc, looks_like_hex, normalize, strip_prefix

# Nilai kanonik yang dipakai berulang di CONTRACT.md §2.
CANON = "303515C1140E3610A02E4880"


class TestContractVectors(unittest.TestCase):
    """Tabel verifikasi yang persis tertulis di CONTRACT.md §2."""

    def test_zebra_upper_prefix(self) -> None:
        # Zebra FX7500: `0X303515C1140E3610A02E4880` -> `303515C1140E3610A02E4880`
        self.assertEqual(normalize("0X303515C1140E3610A02E4880"), CANON)

    def test_zebra_lower_prefix(self) -> None:
        # Zebra FX7500: `0x303515c1140e3610a02e4880` -> `303515C1140E3610A02E4880`
        self.assertEqual(normalize("0x303515c1140e3610a02e4880"), CANON)

    def test_cykeo_plain_hex(self) -> None:
        # Cykeo CK D5: `303515C1140E3610A02E4880` -> `303515C1140E3610A02E4880`
        self.assertEqual(normalize("303515C1140E3610A02E4880"), CANON)

    def test_cykeo_surrounding_whitespace(self) -> None:
        # Cykeo CK D5: ` 303515C1140E3610A02E4880 ` -> `303515C1140E3610A02E4880`
        self.assertEqual(normalize(" 303515C1140E3610A02E4880 "), CANON)

    def test_whitespace_between_every_byte(self) -> None:
        # CONTRACT.md §2 baris "Contoh verifikasi"
        self.assertEqual(normalize("30 35 15 C1 14 0E 36 10 A0 2E 48 80"), CANON)

    def test_reference_rfid_sample_with_prefix(self) -> None:
        # CONTRACT.md §2: `0X303515C1140E3610A02E487C` (contoh reference_rfid)
        self.assertEqual(normalize("0X303515C1140E3610A02E487C"),
                         "303515C1140E3610A02E487C")

    def test_prefix_whitespace_mixed(self) -> None:
        # Gabungan prefix + tab/newline di tepi -> tetap kanonik.
        self.assertEqual(normalize("\t0x303515c1140e3610a02e4880\r\n"), CANON)

    def test_nbsp_inside_is_stripped(self) -> None:
        # NBSP (\xa0) harus dibuang seperti whitespace biasa (langkah 3).
        self.assertEqual(normalize("\xa030\xa03515C1140E3610A02E4880"), CANON)

    def test_bom_is_stripped(self) -> None:
        # BOM (\ufeff) kadang muncul di output reader.
        self.assertEqual(normalize("﻿303515C1140E3610A02E4880﻿"), CANON)

    def test_lowercase_hex_plain_is_uppercased(self) -> None:
        # Tidak ada prefix, semua lowercase -> uppercase (langkah 5).
        self.assertEqual(normalize("303515c1140e3610a02e4880"), CANON)

    def test_result_is_always_uppercase(self) -> None:
        for raw in ("0x303515c1140e3610a02e4880", "0X303515C1140E3610A02E4880",
                    " 30 35 15 c1 14 0e 36 10 a0 2e 48 80 "):
            with self.subTest(raw=raw):
                out = normalize(raw)
                self.assertIsNotNone(out)
                self.assertEqual(out, out.upper())
                self.assertTrue(looks_like_hex(out))

    def test_prefix_removed_only_from_front_two_chars(self) -> None:
        # Aturan 4: hanya 2 char PERTAMA. "xx12" tidak punya prefix -> ditolak.
        self.assertEqual(strip_prefix("0xAB12"), "AB12")
        self.assertEqual(strip_prefix("0Xab12"), "ab12")
        self.assertEqual(strip_prefix("AB0X12"), "AB0X12")
        self.assertIsNone(normalize("AB0X12"))  # 'X' bukan hex -> tolak


class TestRejection(unittest.TestCase):
    """Langkah 2 & 6: input kosong / non-hex harus DITOLAK (None)."""

    def test_empty_string(self) -> None:
        self.assertIsNone(normalize(""))

    def test_none(self) -> None:
        self.assertIsNone(normalize(None))

    def test_whitespace_only(self) -> None:
        for raw in ("   ", "\t", "\n", "\r\n", " \t \n "):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(normalize(raw))

    def test_prefix_only(self) -> None:
        self.assertIsNone(normalize("0X"))
        self.assertIsNone(normalize("0x"))
        self.assertIsNone(normalize(" 0x "))

    def test_non_hex_characters(self) -> None:
        for raw in ("XYZ", "0xZZZ", "NOT-A-HEX", "303515C1140E3610A02E488G",
                    "0x3035-15C1", "E2801130200020190FFD019B!!"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize(raw))

    def test_0x_prefixed_non_hex(self) -> None:
        # Setelah prefix dibuang, "12G4" tetap bukan hex.
        self.assertIsNone(normalize("0x12G4"))

    def test_double_prefix_rejected(self) -> None:
        # Aturan 4 hanya sekali: "0x0x12" -> "0X12" -> mengandung 'X' -> tolak.
        self.assertIsNone(normalize("0x0x12"))

    def test_decimal_points_and_signs(self) -> None:
        self.assertIsNone(normalize("+303515C1140E3610A02E4880"))
        self.assertIsNone(normalize("0.5E10"))

    def test_wrong_type(self) -> None:
        self.assertIsNone(normalize(12345))
        self.assertIsNone(normalize(["a"]))


class TestIdempotence(unittest.TestCase):
    """Normalisasi harus idempoten: EPC kanonik -> EPC kanonik."""

    def test_idempotent_on_all_valid_vectors(self) -> None:
        raws = [
            "0X303515C1140E3610A02E4880", "0x303515c1140e3610a02e4880",
            "303515C1140E3610A02E4880", " 303515C1140E3610A02E4880 ",
            "30 35 15 C1 14 0E 36 10 A0 2E 48 80",
            "\t\r\n 0x 30 35 15 c1 14 0e 36 10 a0 2e 48 80 \n",
        ]
        for raw in raws:
            with self.subTest(raw=raw):
                once = normalize(raw)
                self.assertIsNotNone(once)
                self.assertEqual(normalize(once), once)
                # prefix 0X tidak boleh muncul di hasil
                self.assertFalse(once.startswith("0X"))
                self.assertFalse(once.startswith("0x"))


class TestZebraAndCykeoAgree(unittest.TestCase):
    """Tag yang sama dari 2 device harus jadi EPC kanonik yang sama."""

    def test_same_tag_two_devices(self) -> None:
        zebra = normalize("0X303515C1140E3610A02E4880")
        cykeo = normalize(" 303515C1140E3610A02E4880 ")
        self.assertEqual(zebra, cykeo)
        self.assertEqual(zebra, CANON)

    def test_all_606_style_variants_collapse(self) -> None:
        raw = "0X303515C1140E3610A02E487C"
        variants = [raw, raw.lower().replace("0x", "0X"), CANON.replace("4880", "487C"),
                    " 303515C1140E3610A02E487C ",
                    "30 35 15 C1 14 0E 36 10 A0 2E 48 7C"]
        outputs = {normalize(v) for v in variants}
        self.assertEqual(len(outputs), 1, f"varian harus collapse ke 1 nilai, dapat {outputs}")
        self.assertEqual(outputs.pop(), "303515C1140E3610A02E487C")


class TestBytesInput(unittest.TestCase):
    """Adapter ctypes bisa memberi bytes; normalisasi harus menanganinya."""

    def test_ascii_bytes(self) -> None:
        self.assertEqual(normalize(b"0x303515c1140e3610a02e4880"), CANON)

    def test_non_ascii_bytes_rejected(self) -> None:
        self.assertIsNone(normalize(b"\xff\xfe\x30\x31"))

    def test_is_hex_epc(self) -> None:
        self.assertTrue(is_hex_epc("0x" + CANON.lower()))
        self.assertFalse(is_hex_epc("zzz"))
        self.assertFalse(is_hex_epc(""))


if __name__ == "__main__":
    unittest.main()
