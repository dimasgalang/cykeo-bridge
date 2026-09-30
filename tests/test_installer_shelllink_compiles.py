"""Buktikan C# ShellLink di install_cykeo.ps1 benar-benar bisa dikompilasi.

v1.6.11 gagal di PC test dengan COMPILER_ERRORS, karena ada dua tipe bernama
CykeoShellLink di satu compilation unit (CS0101). Test lama hanya mengecek
string, jadi tidak pernah menangkap ini.

Test iniMENYUSUN blok MemberDefinition apa adanya lalu memanggil Roslyn
sebagai proses terpisah. Kalau dotnet tidak ada, test di-skip - bukan lulus
diam-diam.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_installer_shortcut_guardian import _find_repo, INSTALLER  # noqa: E402

# Add-Type otomatis menyuntik using ini sebelum kompilasi. Tanpa itu,
# kompilasi gagal karena attribute marshalling dan tipe fundamental tidak
# ditemukan - itu artefak harness, bukan bug di sumber.
PRELUDE = (
    "using System;\n"
    "using System.Runtime.InteropServices;\n"
    "using System.Text;\n"
)

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <OutputType>Library</OutputType>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <NoWarn>CS0649;CS0169</NoWarn>
    <AssemblyName>ShellLinkCheck</AssemblyName>
  </PropertyGroup>
  <ItemGroup><Compile Include="ShellLink.cs" /></ItemGroup>
</Project>
"""


def extract_member_definition(ps1: str) -> str:
    """Ambil isi here-string MemberDefinition Add-Type, apa adanya."""
    m = re.search(r"-MemberDefinition\s+@'\r?\n(.*?)\r?\n'@", ps1, re.S)
    if not m:
        raise AssertionError("blok MemberDefinition Add-Type tidak ditemukan")
    return m.group(1)


class TestShellLinkCompiles(unittest.TestCase):
    """C# yang di-Add-Type harus benar-benar bisa dikompilasi."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = _find_repo()
        cls.ps1 = (cls.repo / INSTALLER).read_text(encoding="utf-8")
        cls.dotnet = shutil.which("dotnet") or os.path.expanduser("~/.dotnet/dotnet")
        if not Path(cls.dotnet).exists() and not shutil.which("dotnet"):
            raise unittest.SkipTest("dotnet tidak tersedia, kompilasi tidak diuji")

    def _build(self, source: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "ShellLink.cs").write_text(PRELUDE + source, encoding="utf-8")
            (work / "Proj.csproj").write_text(CSPROJ, encoding="utf-8")
            proc = subprocess.run(
                [self.dotnet, "build", "Proj.csproj", "-v", "q", "--nologo"],
                cwd=td,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_blok_member_definition_kompilasi_bersih(self) -> None:
        """Blok C# di installer harus compile tanpa error sama sekali."""
        source = extract_member_definition(self.ps1)
        rc, out = self._build(source)
        errors = [ln for ln in out.splitlines() if "error CS" in ln]
        self.assertEqual(
            rc,
            0,
            "blok ShellLink di %s gagal dikompilasi:\n%s"
            % (INSTALLER, "\n".join(errors) or out),
        )

    def test_tidak_ada_nama_kelas_duplikat(self) -> None:
        """Dua tipe dengan nama sama di satu unit = CS0101.

        Ini persis penyebab v1.6.11 gagal total di PC test. Nama kelas COM
        dan kelas factory harus berbeda.
        """
        source = extract_member_definition(self.ps1)
        nama = re.findall(r"^\s*(?:public\s+)?(?:static\s+)?class\s+(\w+)", source, re.M)
        dup = {n for n in nama if nama.count(n) > 1}
        self.assertEqual(
            dup,
            set(),
            "nama kelas duplikat di blok ShellLink, ini memicu CS0101: %s" % dup,
        )

    def test_kelas_factory_berbeda_dari_kelas_com(self) -> None:
        """Kelas factory harus dipanggil lewat nama yang benar di PowerShell.

        Kalau nama C# diubah tapi pemanggilan PowerShell tidak, hasilnya
        'Unable to find type' - shortcut hilang tanpa error yang jelas.
        """
        source = extract_member_definition(self.ps1)
        factory = re.search(r"static\s+class\s+(\w+)\s*\{", source)
        self.assertIsNotNone(factory, "tidak ada static class factory di blok ShellLink")
        nama_factory = factory.group(1)
        self.assertIn(
            "[Cykeo.CykeoShellLinkFactory]::Create",
            self.ps1,
            "PowerShell masih memanggil CykeoShellLink::Create, tapi kelas "
            "C# bernama %s" % nama_factory,
        )


if __name__ == "__main__":
    unittest.main()
