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
            _tray.Text = alive
                ? AppName + " - berjalan"
                : AppName + " - stopped";

            _tray.Icon = alive ? SystemIcons.Information : SystemIcons.Warning;

            var item = _menu.Items["Status bridge"] as ToolStripMenuItem;
            if (item != null)
            {
                item.Text = alive ? "Status bridge (running)" : "Status bridge (stopped)";
            }
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
            if (!File.Exists(_bridgeExe))
            {
                Log("cykeo_bridge.exe tidak ada di " + _bridgeExe);
                return;
            }

            StopBridge();

            try
            {
                var psi = new ProcessStartInfo(_bridgeExe, "run")
                {
                    WorkingDirectory = _installDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };

                // Catatan: tidak memasang handler Exited. Watchdog IsBridgeAlive()
                // sudah mem-poll HasExited, jadi event ini hanya duplikat.
                _bridge = new Process { StartInfo = psi, EnableRaisingEvents = true };
                _bridge.OutputDataReceived += (s, e) => { if (e.Data != null) Log(e.Data); };
                _bridge.ErrorDataReceived += (s, e) => { if (e.Data != null) Log("[err] " + e.Data); };

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
        }

        private static void RestartBridge(bool manual)
        {
            if (manual) Log("restart manual dari system tray");
            StopBridge();
            _lastStart = DateTime.MinValue;
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
            sb.AppendLine("Reader CK-D5:");
            sb.AppendLine("  Bridge hanya 성공 baca tag kalau port COM reader terlihat.");
            sb.AppendLine("  Cek Device Manager - Ports (COM & LPT).");

            MessageBox.Show(sb.ToString(), AppName,
                MessageBoxButtons.OK,
                bridgeAlive ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        }

        // -------------------------------------------------------------- helpers

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
