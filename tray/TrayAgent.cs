// Cykeo Bridge Tray Agent
//
// Menjalankan cykeo_bridge.exe sebagai child process di background dan
// menampilkan status lewat NotifyIcon (system tray). Kalau bridge mati, agent
// me-restart otomatis dengan backoff.
//
// Build (Windows, .NET Framework 4.8, 32-bit):
//   C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe /target:winexe
//     /platform:x86 /out:CykeoTrayAgent.exe /reference:System.Windows.Forms.dll
//     /reference:System.Drawing.dll TrayAgent.cs
//
// Build harus 32-bit karena SDK production GReaderApi.dll adalah PE32.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace CykeoBridge.Tray
{
    internal static class TrayAgent
    {
        private const string AppName = "Cykeo RFID Bridge";
        private const int RestartDelaySeconds = 10;
        private const int MaxLogBytes = 512 * 1024;

        private static NotifyIcon _tray;
        private static ContextMenuStrip _menu;
        private static Process _bridge;
        private static string _installDir;
        private static string _bridgeExe;
        private static string _helperExe;
        private static string _logDir;
        private static Thread _watchdog;
        private static volatile bool _quitting;
        private static DateTime _lastStart = DateTime.MinValue;
        private static int _restartCount;

        [STAThread]
        private static void Main()
        {
            _installDir = FindInstallDir();
            if (string.IsNullOrEmpty(_installDir))
            {
                MessageBox.Show(
                    "Folder install CykeoRfidBridge tidak ditemukan.\n\n" +
                    "Jalankan install_cykeo.ps1 terlebih dahulu.",
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            _bridgeExe = Path.Combine(_installDir, "cykeo_bridge.exe");
            _helperExe = Path.Combine(_installDir, "cykeo-helper.exe");
            _logDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "CykeoBridge", "logs");

            Directory.CreateDirectory(_logDir);

            if (IsAlreadyRunning())
            {
                MessageBox.Show(
                    AppName + " sudah berjalan.\nCari ikonnya di system tray " +
                    "(area panah kecil di dekat jam).",
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            BuildTray();
            _watchdog = new Thread(Watchdog) { IsBackground = true, Name = "cykeo-watchdog" };
            _watchdog.Start();

            Application.Run();
        }

        // ---------------------------------------------------------------- tray

        private static void BuildTray()
        {
            _menu = new ContextMenuStrip();
            _menu.Items.Add("Status bridge", null, (s, e) => ShowStatus());
            _menu.Items.Add("Jalankan wizard konfigurasi", null, (s, e) => RunWizard());
            _menu.Items.Add("Buka folder log", null, (s, e) => OpenFolder(_logDir));
            _menu.Items.Add(new ToolStripSeparator());
            _menu.Items.Add("Restart bridge", null, (s, e) => RestartBridge(true));
            _menu.Items.Add("Mulai ulang bridge", null, (s, e) => { StartBridge(); SetTrayState(); });
            _menu.Items.Add("Hentikan bridge", null, (s, e) => { StopBridge(); SetTrayState(); });
            _menu.Items.Add(new ToolStripSeparator());
            _menu.Items.Add("Buka folder install", null, (s, e) => OpenFolder(_installDir));
            _menu.Items.Add("Keluar", null, (s, e) => Quit());

            _tray = new NotifyIcon
            {
                Icon = SystemIcons.Application,
                Text = AppName,
                ContextMenuStrip = _menu,
                Visible = true
            };
            _tray.DoubleClick += (s, e) => ShowStatus();
            SetTrayState();
        }

        private static void SetTrayState()
        {
            bool alive = IsBridgeAlive();
            _tray.Text = Trim(alive
                ? AppName + " - berjalan"
                : AppName + " - stopped");

            _tray.Icon = alive ? SystemIcons.Information : SystemIcons.Warning;

            var item = _menu.Items["Status bridge"] as ToolStripMenuItem;
            if (item != null)
            {
                item.Text = alive ? "Status bridge (running)" : "Status bridge (stopped)";
            }
        }

        /// <summary>NotifyIcon.Text dibatasi 63 karakter oleh Windows.</summary>
        private static string Trim(string s)
        {
            return s != null && s.Length > 63 ? s.Substring(0, 60) + "..." : s;
        }

        // ------------------------------------------------------------ watchdog

        private static void Watchdog()
        {
            while (!_quitting)
            {
                try
                {
                    if (!IsBridgeAlive())
                    {
                        // Jangan restart terlalu cepat.
                        double since = (DateTime.Now - _lastStart).TotalSeconds;
                        if (since < RestartDelaySeconds)
                        {
                            Thread.Sleep(1000);
                            continue;
                        }
                        // Bridge keluar karena reader/COM belum siap (exit 3) atau
                        // config belum ada (exit 4) — restart hanya akan-loop
                        // forever. Kondisi lapangan yang normal, bukan crash yang
                        // harus di-retry. Restart otomatis dihentikan; operator
                        // tekan menu "Restart bridge" setelah memperbaiki.
                        int code = WaitForExitCode();
                        if (code == 3 || code == 4)
                        {
                            _restartCount = 0;
                            Log("bridge berhenti dengan exit " + code
                                + " (reader belum siap / config belum ada). "
                                + "Restart otomatis DINONAKTIFKAN supaya tidak loop. "
                                + "Perbaiki dulu, lalu menu 'Restart bridge'.");
                            SetTrayState();
                            Thread.Sleep(5000);
                            continue;
                        }
                        _restartCount++;
                        Log("bridge tidak hidup, restart #" + _restartCount);
                        StartBridge();
                    }

                    Thread.Sleep(3000);
                }
                catch (Exception ex)
                {
                    Log("watchdog error: " + ex.Message);
                    Thread.Sleep(5000);
                }
            }
        }

        private static void StartBridge()
        {
            // Prioritas 1: exe beku (PyInstaller) kalau ada.
            // Prioritas 2: fallback ke Python. Bridge ini murni standard library,
            // jadi tidak butuh pip install apa pun selama ada interpreter.
            string exe = null;
            string args = "run";

            if (File.Exists(_bridgeExe))
            {
                exe = _bridgeExe;
            }
            else
            {
                string py = FindPython();
                if (py == null)
                {
                    Log("bridge tidak bisa dijalankan: tidak ada cykeo_bridge.exe " +
                        "dan tidak ada python di PATH");
                    return;
                }
                exe = py;
                // -u: log langsung ke pipe tanpa buffering, supaya tray log bisa
                // menampilkan progress. -m: pakai package cykeo_bridge dari
                // folder install (WorkingDirectory di bawah).
                args = "-u -m cykeo_bridge run";
                Log("cykeo_bridge.exe tidak ada; fallback ke " + py);
            }

            StopBridge();

            try
            {
                var psi = new ProcessStartInfo(exe, args)
                {
                    WorkingDirectory = _installDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };

                // Catatan: event Exited dipakai untuk MEREKAM exit code. Bridge yang
                // keluar karena reader belum siap (exit 3) atau config belum ada
                // (exit 4) tidak boleh di-restart otomatis, kalau tidak akan loop
                // setiap 10 detik selamanya. Watchdog IsBridgeAlive() tetap
                // meng-poll HasExited sebagai pemicu aksi.
                _lastExitCode = -1;
                _bridge = new Process { StartInfo = psi, EnableRaisingEvents = true };
                _bridge.OutputDataReceived += (s, e) => { if (e.Data != null) Log(e.Data); };
                _bridge.ErrorDataReceived += (s, e) => { if (e.Data != null) Log("[err] " + e.Data); };
                _bridge.Exited += (s, e) =>
                {
                    var proc = s as Process;
                    if (proc == null) return;
                    try { _lastExitCode = proc.ExitCode; }
                    catch { /* proses sudah di-dispose */ }
                };

                _bridge.Start();
                _bridge.BeginOutputReadLine();
                _bridge.BeginErrorReadLine();

                _lastStart = DateTime.Now;
                Log("bridge dijalankan (pid " + _bridge.Id + ")");
            }
            catch (Exception ex)
            {
                Log("gagal start bridge: " + ex.Message);
            }
        }

        /// <summary>
        /// Exit code bridge terakhir, dibaca di event Exited SEBELUM proses di-
        /// dispose. -1 = belum pernah jalan / tidak diketahui.
        /// </summary>
        private static int _lastExitCode = -1;

        /// <summary>
        /// Kode keluar bridge terakhir, atau -1 kalau bridge masih hidup /
        /// belum pernah ada yang keluar.
        /// </summary>
        private static int WaitForExitCode()
        {
            return _lastExitCode;
        }

        private static void StopBridge()
        {
            if (_bridge == null) return;
            try
            {
                if (!_bridge.HasExited)
                {
                    _bridge.Kill();
                    _bridge.WaitForExit(5000);
                }
            }
            catch { }
            finally
            {
                _bridge.Dispose();
                _bridge = null;
            }
            // Helper biasanya ikut mati; paksa bersih kalau masih nyangkut.
            KillByName("cykeo-helper");
            // Kalau bridge jalan via python -m, prosesnya bernama python, bukan
            // cykeo_bridge - jadi jangan tebak nama proses python: user bisa punya
            // python lain yang tidak ada hubungannya. Child process sudah di-Kill
            // di atas; helper juga sudah membereskan dirinya sendiri.
        }

        private static void RestartBridge(bool manual)
        {
            if (manual) Log("restart manual dari system tray");
            StopBridge();
            _lastStart = DateTime.MinValue;
            StartBridge();
            SetTrayState();
        }

        // -------------------------------------------------------------- wizard

        /// <summary>
        /// Jalankan wizard konfigurasi lewat bridge yang tersedia. Dipakai kalau
        /// config belum ada — tanpa ini operator harus edit JSON manual.
        /// </summary>
        private static void RunWizard()
        {
            string exe;
            string args;
            if (File.Exists(_bridgeExe))
            {
                exe = _bridgeExe;
                args = "wizard";
            }
            else
            {
                string py = FindPython();
                if (py == null)
                {
                    MessageBox.Show(
                        "Wizard tidak bisa dijalankan.\n\n" +
                        "Tidak ada cykeo_bridge.exe dan tidak ada Python di PATH.\n\n" +
                        "Pasang Python 3.11 (tandai 'Add to PATH'), lalu buka menu ini lagi.",
                        AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                exe = py;
                args = "-m cykeo_bridge wizard";
            }

            try
            {
                Log("wizard konfigurasi dijalankan dari tray");
                // Wizard butuh interaksi user (console/GUI), jadi TIDAK
                // CreateNoWindow: operator harus bisa melihat dan mengisi.
                var psi = new ProcessStartInfo(exe, args)
                {
                    WorkingDirectory = _installDir,
                    UseShellExecute = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p != null) p.WaitForExit(600000);
                }
            }
            catch (Exception ex)
            {
                Log("gagal jalankan wizard: " + ex.Message);
                MessageBox.Show("Gagal menjalankan wizard: " + ex.Message,
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            StartBridge();
            SetTrayState();
        }

        // -------------------------------------------------------------- status

        private static void ShowStatus()
        {
            bool bridgeAlive = IsBridgeAlive();
            bool helperPresent = File.Exists(_helperExe);
            bool configPresent = File.Exists(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "CykeoBridge", "config.json"));

            var sb = new StringBuilder();
            sb.AppendLine(AppName);
            sb.AppendLine("========================================");
            sb.AppendLine();
            sb.AppendLine("Folder install : " + _installDir);
            sb.AppendLine();
            sb.AppendLine("Bridge  : " + (bridgeAlive
                ? "BERJALAN (pid " + SafePid() + ")"
                : "BERHENTI"));
            sb.AppendLine("Helper  : " + (helperPresent
                ? "ada - " + SafeHelperSize() + " byte"
                : "HILANG (kemungkinan dikarantina antivirus)"));
            sb.AppendLine("Config  : " + (configPresent ? "ada" : "belum dibuat"));
            sb.AppendLine("Restart : " + _restartCount + " kali");
            sb.AppendLine();
            sb.AppendLine("Log     : " + _logDir);
            sb.AppendLine();

            // Kalau bridge mati, jelaskan SEBABnya. Tanpa ini operator cuma
            // lihat "BERHENTI" lalu menebak-nebak.
            if (!bridgeAlive)
            {
                sb.AppendLine("Kenapa bridge tidak berjalan:");
                sb.AppendLine();
                if (File.Exists(_bridgeExe))
                {
                    sb.AppendLine("  cykeo_bridge.exe ADA tapi prosesnya mati.");
                    sb.AppendLine("  Cek log bridge di folder Log di atas.");
                }
                else
                {
                    string py = FindPython();
                    if (py != null)
                        sb.AppendLine("  Paket bridge: Python fallback (" + py + ")");
                    else
                        sb.AppendLine("  Paket bridge: TIDAK ADA");
                    sb.AppendLine();
                    // Pakai exit code yang SEBENARNYA, bukan tebakan. Config ada
                    // tapi reader belum dicolok = exit 3; config belum ada = exit 4.
                    // Dulu dua-duanya dilabeli "dua kemungkinan" walau penyebabnya
                    // sudah pasti, jadi operatormenebak tanpa bukti.
                    if (_lastExitCode == 3)
                    {
                        sb.AppendLine("  Penyebab: reader belum siap (exit 3).");
                        sb.AppendLine("  Config SUDAH ada, jadi ini bukan masalah config.");
                        sb.AppendLine("  Cek Device Manager - Ports (COM & LPT), lalu colok");
                        sb.AppendLine("  kabel USB reader CK-D5.");
                        sb.AppendLine("  Setelah port muncul: menu 'Restart bridge'.");
                    }
                    else if (_lastExitCode == 4)
                    {
                        sb.AppendLine("  Penyebab: config belum ada (exit 4).");
                        sb.AppendLine("  Solusi: menu 'Jalankan wizard konfigurasi'.");
                    }
                    else
                    {
                        sb.AppendLine("  Bridge berhenti tanpa kode yang jelas.");
                        sb.AppendLine("  Cek log bridge di folder Log di atas.");
                    }
                }
                sb.AppendLine();
            }

            sb.AppendLine("Reader CK-D5:");
            sb.AppendLine("  Bridge hanya berhasil baca tag kalau port COM reader terlihat.");
            sb.AppendLine("  Cek Device Manager - Ports (COM & LPT).");

            MessageBox.Show(sb.ToString(), AppName,
                MessageBoxButtons.OK,
                bridgeAlive ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        }

        // -------------------------------------------------------------- helpers

        /// <summary>
        /// True kalau ada bridge yang bisa dijalankan: exe beku (PyInstaller)
        /// ATAU interpretor Python dengan package cykeo_bridge.
        /// </summary>
        private static bool BridgeAvailable()
        {
            if (File.Exists(_bridgeExe)) return true;
            return FindPython() != null;
        }

        /// <summary>
        /// Cari python yang bisa menjalankan -m cykeo_bridge. WindowsApp alias
        /// (Store stub) sengaja diabaikan: stub itu exit dengan error, bukan
        /// menjalankan bridge, jadi StartupsWith di bawah menyaringnya.
        /// </summary>
        private static string FindPython()
        {
            var names = new[] { "python.exe", "python3.exe" };
            var pathDirs = (Environment.GetEnvironmentVariable("PATH") ?? "")
                .Split(Path.PathSeparator);

            foreach (var dir in pathDirs)
            {
                if (string.IsNullOrWhiteSpace(dir)) continue;
                foreach (var n in names)
                {
                    try
                    {
                        var full = Path.Combine(dir.Trim(), n);
                        if (!File.Exists(full)) continue;
                        if (IsWindowsAppAlias(full)) continue;
                        if (IsRealPython(full)) return full;
                    }
                    catch { }
                }
            }
            return null;
        }

        private static bool IsWindowsAppAlias(string exe)
        {
            var name = Path.GetFileName(exe) ?? "";
            if (!name.StartsWith("python", StringComparison.OrdinalIgnoreCase)) return false;
            var dir = (Path.GetDirectoryName(exe) ?? "").ToLowerInvariant();
            return dir.Contains("microsoft\\windowsapps")
                || dir.Contains("microsoft/windowsapps");
        }

        /// <summary>Jalankan -c import; hanya Python asli yang keluar 0.</summary>
        private static bool IsRealPython(string exe)
        {
            try
            {
                var psi = new ProcessStartInfo(exe, "-c \"import sys; sys.exit(0)\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p == null) return false;
                    if (!p.WaitForExit(8000)) { try { p.Kill(); } catch { } return false; }
                    p.StandardOutput.ReadToEnd();
                    p.StandardError.ReadToEnd();
                    return p.ExitCode == 0;
                }
            }
            catch { return false; }
        }

        private static bool IsBridgeAlive()
        {
            return _bridge != null && !_bridge.HasExited;
        }

        private static string SafePid()
        {
            try { return _bridge.Id.ToString(); } catch { return "?"; }
        }

        private static string SafeHelperSize()
        {
            try
            {
                return new FileInfo(_helperExe).Length.ToString("N0");
            }
            catch { return "?"; }
        }

        private static bool IsAlreadyRunning()
        {
            string me = Process.GetCurrentProcess().ProcessName;
            var meId = Process.GetCurrentProcess().Id;
            foreach (var p in Process.GetProcesses())
            {
                try
                {
                    if (p.ProcessName == me && p.Id != meId) return true;
                }
                catch { }
            }
            return false;
        }

        private static void KillByName(string name)
        {
            foreach (var p in Process.GetProcesses())
            {
                try
                {
                    if (p.ProcessName == name) p.Kill();
                }
                catch { }
            }
        }

        private static string FindInstallDir()
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string[] candidates =
            {
                Path.Combine(local, "CykeoRfidBridge"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                             "CykeoRfidBridge"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonProgramFiles),
                             "CykeoRfidBridge")
            };
            foreach (var c in candidates)
            {
                if (Directory.Exists(c)) return c;
            }
            return null;
        }

        private static void OpenFolder(string path)
        {
            try
            {
                if (Directory.Exists(path)) Process.Start("explorer.exe", "\"" + path + "\"");
                else MessageBox.Show("Folder tidak ada: " + path, AppName,
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Gagal buka folder: " + ex.Message, AppName,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private static void Log(string message)
        {
            try
            {
                string stamp = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
                string path = Path.Combine(_logDir, "tray-agent.log");
                File.AppendAllText(path, "[" + stamp + "] " + message + Environment.NewLine);

                var fi = new FileInfo(path);
                if (fi.Length > MaxLogBytes)
                {
                    // Potong separuh terakhir file, sisakan header_timestamp.
                    string content = File.ReadAllText(path);
                    int cut = content.Length / 2;
                    string tail = content.Substring(cut);
                    File.WriteAllText(path,
                        "[log dipotong " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "]" +
                        Environment.NewLine + tail);
                }
            }
            catch { }
        }

        private static void Quit()
        {
            _quitting = true;
            Log("tray agent keluar");
            StopBridge();
            if (_tray != null)
            {
                _tray.Visible = false;
                _tray.Dispose();
            }
            Application.Exit();
        }
    }
}
