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
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace CykeoBridge.Tray
{
    internal static class TrayAgent
    {
        private const string AppName = "Cykeo RFID Bridge";
        private const int RestartDelaySeconds = 10;
        // Backoff maksimum antar percobaan restart bridge. Total 103 restart dalam
        // 6 menit terlihat di tray-agent.log PC teknisi karena _lastStart hanya
        // di-update saat start BERHASIL. Kalau start gagal (mis. package python
        // tidak ada), since selalu ~0 sehingga loop restart berjalan tiap 3 detik.
        // Backoff eskalatif menutup jebakan itu: 10s, 20s, 40s, 60s, 60s...
        private const int MaxRestartDelaySeconds = 60;
        private const int MaxLogBytes = 512 * 1024;

        // ------------------------------------------------------- pemulihan
        // Dua mekanisme supaya tray yang mati tidak-engaja bisa hidup lagi
        // tanpa user melakukan apa-apa:
        //
        //  1. File penanda "exit disengaja" di %ProgramData%\CykeoBridge\.
        //     Exit dari MENU tray menulis file ini, jadi guardian TIDAK
        //     menghidupkan tray lagi. Kalau proses mati karena crash,
        //     taskkill, atau jendela ditutup paksa, file ini tidak pernah
        //     ditulis, jadi guardian BOLEH menghidupkan tray lagi. Tanpa
        //     penanda ini, guardian akan hidup-mati sendiri setiap kali
        //     user menekan Keluar di menu.
        //
        //  2. Mode guardian dari exe yang sama:
        //     "CykeoTrayAgent.exe --watchdog". Guardian tinggal memeriksa
        //     apakah proses tray masih ada (lewat named mutex, bukan nama
        //     proses, karena guardian sendiri bernama CykeoTrayAgent juga).
        //     Kalau tidak ada DAN tidak ada penanda disengaja, guardian
        //     menjalankan tray lagi. Guardian didaftarkan di HKCU Run
        //     supaya ikut hidup setelah login, dan tray memanggilnya
        //     sendiri setiap kali start sebagai jaring pengaman.
        //
        // Mode relay dipakai hanya sebagai cadangan kalau guardian somehow
        // tidak bisa hidup: proses baru menunggu tray yang lama keluar
        // dulu sebelum mengambil alih, supaya tidak bentrok mutex.
        internal const string IntentionalExitFileName = "exit-disengaja.flag";
        // Nama file log tray. Guardian membandingkan stempel waktu file ini
        // sebelum dan sesudah menjalankan tray: kalau tidak berubah, berarti
        // tray start lalu langsung mati, jadi guardian harus BERHENTI
        // me-restart - bukan mengulang dialog "sudah berjalan" tiap tick.
        internal const string TrayLogFileName = "tray-agent.log";
        internal const string WatchdogArg = "--watchdog";
        internal const string RelayArg = "--relay";
        // v1.6.24: mode wizard GUI NATIVE. Exe yang sama, mode berbeda.
        // Tray utama tetap jalan seperti biasa; mode ini cuma membuka
        // form konfigurasi lalu keluar.
        internal const string WizardArg = "--wizard";
        internal const string TrayMutexName = @"Local\CykeoTrayAgentTray";
        internal const string WatchdogMutexName = @"Local\CykeoTrayAgentWatchdog";
        private const int GuardianPollSeconds = 5;
        // Kalau penanda disengaja sudah bertahan selama ini, guardian
        // berhenti supaya tidak ada proses sia-sia menganggur di PC user
        // yang memang sudah tidak memakai bridge.
        private const int GuardianIdleExitSeconds = 30 * 60;

        private static NotifyIcon _tray;
        private static ContextMenuStrip _menu;
        private static Process _bridge;
        private static string _installDir;
        private static string _bridgeExe;
        private static string _helperExe;
        private static string _logDir;
        private static Thread _watchdog;
        private static volatile bool _quitting;
        private static volatile bool _readerTestBusy;
        private static volatile int _lastReaderTestExit = -1;
        private static Form _uiAnchor;
        private static Mutex _trayMutex;
        private static Mutex _guardianMutex;
        private static DateTime _lastStart = DateTime.MinValue;
        private static int _restartCount;

        [STAThread]
        private static void Main(string[] args)
        {
            // Mode wizard GUI NATIVE (v1.6.24). Harus dicek SEBELUM mutex
            // tray: wizard adalah proses terpisah yang hanya membuka form
            // lalu keluar. Kalau mutex dicek lebih dulu, wizard yang dibuka
            // saat tray sedang hidup akan ditolak sebagai "sudah berjalan"
            // dan user tidak pernah melihat form-nya.
            if (HasArg(args, WizardArg))
            {
                int wizardCode = ConfigureDialog.RunWizard(args);
                Environment.Exit(wizardCode);
                return;
            }

            // Catat waktu start proses SEBELUM cabang apa pun. IsIntentionalExit()
            // membandingkan stempel waktu di flag dengan nilai ini untuk
            // membedakan "user menekan Keluar di sesi ini" dari "flag
            // peninggalan logon sebelumnya". Guardian ikut mencatat karena dia
            // yang membaca flag tersebut.
            CatatProsesStart();

            // Mode guardian: proses pengawas kecil. Harus dicek SEBELUM
            // apa pun, termasuk pencarian folder install, karena guardian
            // tidak punya tray dan tidak boleh dianggap sebagai tray yang
            // sedang berjalan.
            if (HasArg(args, WatchdogArg))
            {
                RunGuardian();
                return;
            }

            // Mode relay: proses pengganti yang menunggu tray lama keluar
            // dulu sebelum mengambil alih. Dipakai cuma kalau guardian
            // tidak tersedia (cadangan, bukan jalur utama).
            if (HasArg(args, RelayArg))
                WaitForTrayMutexFree();

            _installDir = FindInstallDir();
            if (string.IsNullOrEmpty(_installDir))
            {
                // pakai MessageBox langsung: form anchor BELUM dibuat di titik
                // ini, jadi ShowResult tidak bisa dipakai.
                MessageBox.Show(
                    "Folder install CykeoRfidBridge tidak ditemukan.\n\n" +
                    "Jalankan install_cykeo.ps1 terlebih dahulu.",
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            _bridgeExe = Path.Combine(_installDir, "cykeo_bridge.exe");
            _helperExe = Path.Combine(_installDir, "cykeo-helper.exe");
            _logDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "CykeoBridge", "logs");

            Directory.CreateDirectory(_logDir);

            // Single instance lewat named mutex, bukan pemindaian nama
            // proses. Guardian juga bernama CykeoTrayAgent.exe, jadi
            // pemindaian nama proses akan selalu menyuruh user "sudah
            // berjalan" begitu guardian hidup - tray jadi tidak bisa start
            // sama sekali. Named mutex juga otomatis tersedia lagi kalau
            // proses sebelumnya mati paksa (crash / taskkill), sedangkan
            // pemindaian nama proses tidak punya konsep itu.
            // Mutex yang dipegang di sesi sebelumnya yang MATI PAKSA (End task di
            // Task Manager, taskkill, blue screen) tidak pernah dilepas dengan
            // rapi. .NET melaporkan itu sebagai "pertama = false" persis
            // seperti ketika tray memang sedang hidup, sehingga setiap start
            // setelahnya memunculkan dialog "sudah berjalan" lalu mati - dan
            // guardian langsung menyalakan tray lagi, jadi dialognya menumpuk
            // terus-menerus. ownerProcessId harus jadi acuan, bukan flag
            // "pertama".
            bool createdNew;
            _trayMutex = new Mutex(false, TrayMutexName, out createdNew);
            bool bisaDiambil;
            try
            {
                bisaDiambil = _trayMutex.WaitOne(0, false);
            }
            catch (AbandonedMutexException)
            {
                // Tray lama mati tanpa melepas mutex. WaitOne sudah memberi
                // mutex ini ke kita, jadi kita BOLEH jadi tray baru.
                bisaDiambil = true;
            }

            if (bisaDiambil)
            {
                // Aman: mutex sekarang milik kita. Catat PID supaya tray lain
                // yang gagal mengambil mutex tahu siapa pemegang yang hidup,
                // lalu keluar dengan tenang tanpa menampilkan apa pun.
                WriteTrayPid();
            }
            else
            {
                // Mutex dipegang proses yang benar-benar hidup. Kalau proses itu
                // sudah tidak ada (mis. baru saja exit), mutex akan di-release
                // oleh OS dalam hitungan milidetik - tunggu sebentar, jangan
                // langsung menyerah dan memunculkan dialog.
                // Sumber PID, diurutkan dari yang paling bisa dipercaya:
                //   1. File PID - ditulis tray yang benar-benar memegang mutex.
                //   2. NtQueryWaitObject - cadangan kalau file PID hilang.
                //
                // Di build 32-bit NtQueryWaitObject sering mengembalikan 0,
                // jadi file PID harus jadi sumber utama, bukan cadangan.
                int holderPid = TrayPidFromFile();
                if (holderPid <= 0) holderPid = MutexOwnerPid(_trayMutex);
                bool holderHidup;
                try
                {
                    // PENTING: harus cek HasExited, BUKAN GetProcessById(pid) != null.
                    // GetProcessById hanya mengembalikan objek handle; objek itu
                    // tidak pernah null dan tidak memberi informasi apa pun soal
                    // prosesnya masih hidup atau sudah mati.
                    //
                    // Di beberapa kondisi Win32, pemanggilan itu justru melempar,
                    // dan catch { } di bawah menganggapnya "pasti hidup" - dialog
                    // "sudah berjalan" muncul tanpa henti. Itu persis bug dialog
                    // bertumpuk yang dikeluhkan user.
                    holderHidup = holderPid > 0 && !Process.GetProcessById(holderPid).HasExited;
                }
                catch (ArgumentException) { holderHidup = false; }
                catch (InvalidOperationException) { holderHidup = false; }
                // Mutex yang DIPEPEGANG proses hidup belum tentu melaporkan PID:
                // pembacaan kernel bisa gagal, atau handle di-close tanpa release.
                // Kasus itu tetap dianggap dipakai, sehingga tray yang baru keluar
                // dengan tenang dan guardian tidak mengira tray itu mati.
                catch { holderHidup = true; }

                if (holderHidup)
                {
                    Log("tray sudah berjalan (pid " + holderPid +
                        "), tidak start dua kali.");
                    // JANGAN tampilkan dialog sama sekali di sini. Dialog
                    // "sudah berjalan" dulu muncul di sini, dan guardian
                    // menyalakan tray lagi setiap 5 detik -> dialognya menumpuk
                    // tanpa henti sampai menutupi seluruh layar. Icon tray yang
                    // sudah ada di system tray sudah cukup memberitahukan user
                    // bahwa bridge hidup, jadi tidak perlu dialog sama sekali.
                    ShowBalloonOnly();
                    return;
                }

                // Pemegang sudah mati: tunggu OS melepas mutex-nya.
                for (int i = 0; i < 40; i++)
                {
                    try
                    {
                        if (_trayMutex.WaitOne(250, false))
                        {
                            bisaDiambil = true;
                            break;
                        }
                    }
                    catch (AbandonedMutexException) { bisaDiambil = true; break; }
                    // Jangan break di sini. Dulu catch { break; } membuat loop
                    // menyerah di iterasi pertama, jadi mutex yang sebenarnya
                    // sedang dilepas OS tidak pernah sempat diambil. Cukup error
                    // tak terduga lain; coba lagi di iterasi berikutnya.
                    catch { }
                }

                if (!bisaDiambil)
                {
                    Log("mutex tray tidak bisa diambil walau pemegang sudah mati " +
                        "(pid " + holderPid + "); tidak menampilkan dialog, keluar.");
                    return;
                }
            }

            // Tray yang hidup berarti user memang ingin tray hidup: dari
            // shortcut Desktop, Start Menu, guardian, atau HKCU Run. Jadi
            // penanda exit disengaja dari sesi sebelumnya harus dibuang,
            // kalau tidak guardian akan mengira user tidak mau tray hidup
            // padahal tray-nya sedang berjalan.
            ClearIntentionalExit();

            BuildTray();
            EnsureGuardian();
            _watchdog = new Thread(Watchdog) { IsBackground = true, Name = "cykeo-watchdog" };
            _watchdog.Start();

            // Jaring pengaman: kalau installer somehow gagal menulis HKCU Run
            // (kategori policy, registry read-only), tray tetap mendaftar
            // sendiri saat start. Idempoten, jadi aman dipanggil tiap login.
            ThreadPool.QueueUserWorkItem(_ => { try { InstallAutostart(); } catch { } });

            // Anchor UI-thread: form disembunyikan, cuma dipakai sebagai owner
            // Invoke/BeginInvoke untuk dialog dari background thread.
            //
            // PENTING: form ini HAKUS disembunyikan eksplisit lewat Hide().
            // Visible=false di object initializer saja tidak cukup:
            // Application.Run() tetap menampilkan form owner-nya, dan karena
            // tidak ada kontrol di dalamnya user melihat kotak putih polos tanpa
            // judul dan tanpa teks (persis laporan bug v1.6.0). Judul juga
            // diset supaya kalau somehow tetap tampil, user bisa mengenali
            // jendela mana ini di task manager.
            _uiAnchor = new Form
            {
                FormBorderStyle = FormBorderStyle.FixedToolWindow,
                ShowInTaskbar = false,
                StartPosition = FormStartPosition.Manual,
                Location = new Point(-32000, -32000),
                Size = new Size(1, 1)
            };
            _uiAnchor.Text = AppName;
            _uiAnchor.Hide();
            Application.Run(_uiAnchor);
        }

        // ---------------------------------------------------------------- tray

        private static void BuildTray()
        {
            _menu = new ContextMenuStrip();
            _menu.Items.Add("Status bridge", null, (s, e) => ShowStatus());
            _menu.Items.Add("Uji Pembaca (baca EPC langsung)", null, (s, e) => RunReaderTest());
            // v1.6.23: menu wizard lama dihapus.
            //
            // Menu itu memanggil install_cykeo.ps1 secara interaktif. Script itu
            // bukan wizard konfigurasi murni: ia menyalin paket, memasang bridge,
            // dan menulis ke %ProgramData%. Dipanggil dari tray (proses biasa,
            // tanpa elevasi) hasilnya error, dan user tidak pernah bisa
            // menyelesaikan wizard lewat menu itu.
            //
            // Sekarang konfigurasi dilakukan wizard GUI NATIVE (WinForms) yang
            // menyatu di tray agent: wizard menulis config.json ke
            // %ProgramData%, jadi harus elevated. Tray memicu dengan verb
            // "runas" supaya Windows menampilkan UAC.
            _menu.Items.Add("Konfigurasi reader (minta izin admin)", null, (s, e) => RunConfigureWizard());
            _menu.Items.Add("Buka folder log", null, (s, e) => OpenFolder(_logDir));
            _menu.Items.Add("Perbaiki auto-start", null, (s, e) => RepairAutostart());
            _menu.Items.Add(new ToolStripSeparator());
            _menu.Items.Add("Restart bridge", null, (s, e) => RestartBridge(true));
            _menu.Items.Add("Hentikan bridge", null, (s, e) => { StopBridge(); SetTrayState(); });
            _menu.Items.Add(new ToolStripSeparator());
            // Jalankan ulang tray: memunculkan proses tray BARU di belakang
            // layar, lalu tray yang ini keluar. Ini cara yang paling aman
            // untuk user yang tray-nya menggantung tapi ikon masih ada:
            // Start-Process dulu (proses baru menunggu tray ini melepas
            // mutex lewat mode --relay), baru keluar.
            _menu.Items.Add("Jalankan ulang tray", null, (s, e) => RelaunchTray());
            _menu.Items.Add("Buka folder install", null, (s, e) => OpenFolder(_installDir));
            // Dua cara keluar, dan bedanya penting:
            //  - "Keluar"        : tray tutup, guardian berhenti setelah
            //                       jeda idle dan TIDAK akan menghidupkan tray lagi.
            //  - "Keluar dan stop guardian": yang di atas, guardian langsung
            //                       dimatikan supaya tidak ada proses pengawas
            //                       yang sama sekali. Untuk user yang memang
            //       tidak mau memakai bridge dalam waktu lama.
            _menu.Items.Add("Keluar", null, (s, e) => Quit());
            _menu.Items.Add("Keluar dan stop guardian", null, (s, e) => QuitAndStopGuardian());

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

        // Notifikasi tanpa jendela: balloon di system tray saja. Dipakai
        // menggantikan MessageBox "sudah berjalan", karena MessageBox modal
        // memblokir proses guardian dan menumpuk setiap kali tray dinyalakan
        // ulang. Balloon tidak pernah butuh klik, jadi tidak bisa menumpuk.
        private static void ShowBalloonOnly()
        {
            try
            {
                NotifyIcon ni = new NotifyIcon();
                ni.Visible = true;
                ni.Icon = SystemIcons.Application;
                ni.BalloonTipTitle = AppName;
                ni.BalloonTipText = "Sudah berjalan. Cari ikonnya di system tray.";
                ni.ShowBalloonTip(2000);
                // Jangan dispose langsung: Windows butuh waktu untuk membaca
                // data balloon. Timer ini melepas handle setelah 3 detik.
                // Nama harus fully qualified: 'Timer' ambigu antara
                // System.Threading.Timer dan System.Windows.Forms.Timer.
                System.Windows.Forms.Timer t = new System.Windows.Forms.Timer();
                t.Interval = 3000;
                t.Tick += delegate
                {
                    t.Stop();
                    ni.Visible = false;
                    ni.Dispose();
                    t.Dispose();
                };
                t.Start();
            }
            catch
            {
                // Notifikasi gagal sama sekali tidak boleh menggagalkan start.
                // Tray yang sudah hidup di system tray tetap jalan.
            }
        }

        // Jeda antar percobaan restart, di-eskalasi setiap kegagalan beruntun.
        // Habis 6 kegagalan (10+20+40+60+60+60 = 250 detik ~4 menit) delay-nya
        // mentok di MaxRestartDelaySeconds. Setelah bridge benar-benar hidup,
        // _restartCount di-reset (lihat MarkBridgeAlive) sehingga delay kembali
        // ke RestartDelaySeconds. Tujuannya: log tetap bisa dibaca manusia dan
        // tidak membanjiri 100+ baris dalam 6 menit.
        private static double CurrentDelaySeconds()
        {
            int attempts = _restartCount < 0 ? 0 : _restartCount;
            double delay = RestartDelaySeconds;
            for (int i = 1; i < attempts && delay < MaxRestartDelaySeconds; i++)
                delay *= 2;
            return delay > MaxRestartDelaySeconds ? MaxRestartDelaySeconds : delay;
        }

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
                        if (since < CurrentDelaySeconds())
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
            StopBridge();

            // Catat waktu percobaan ini SEBELUM menjalankan apa pun, lalu reset
            // _restartCount. Ini menutup jebakan restart-loop: sebelumnya
            // _lastStart hanya di-update di jalur sukses (TryStartBridgeExe /
            // TryStartBridgePython), sehingga saat start GAGAL nilai _lastStart
            // tetap lama, since ~0, dan watchdog memicu restart lagi tiap 3
            // detik. Bukti nyata: 103 restart dalam 6 menit di tray-agent.log.
            _lastStart = DateTime.Now;
            _restartCount = 0;

            // Paket installer v1.6.x TIDAK menyertakan cykeo_bridge.exe (butuh
            // PyInstaller yang harus dibangun di mesin Windows). Kalau tray hanya
            // mencoba .exe, bridge tidak akan PERNAH hidup, tidak ada status di
            // dashboard, dan scan selalu 0 - persis gejala yang dilaporkan.
            // Installer sudah menjanjikan "tray agent memakai Python sebagai
            // bridge", jadi jalankan juga source python yang memang dipaketkan
            // di $InstallDir\cykeo_bridge\.
            var started = TryStartBridgeExe();
            if (!started)
                started = TryStartBridgePython();

            if (!started)
                Log("bridge TIDAK bisa dijalankan: neither " + _bridgeExe +
                    " nor python -m cykeo_bridge tersedia. " +
                    "Tray icon akan tetap ada tapi bridge tidak jalan. " +
                    "Cek: python --version di CMD, dan folder " +
                    "cykeo_bridge di folder install.");
        }

        private static bool TryStartBridgeExe()
        {
            if (string.IsNullOrEmpty(_bridgeExe) || !File.Exists(_bridgeExe))
            {
                Log("cykeo_bridge.exe tidak ada di " + _installDir + " (normal: paket tidak membawa PyInstaller build).");
                return false;
            }

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
                return true;
            }
            catch (Exception ex)
            {
                Log("gagal start bridge exe: " + ex.Message);
                _bridge = null;
                return false;
            }
        }

        private static bool TryStartBridgePython()
        {
            // package python harus di $InstallDir\cykeo_bridge\ LANGSUNG karena
            // 'python -m' mencari module relatif terhadap cwd.
            string packageDir = Path.Combine(_installDir, "cykeo_bridge");
            if (!Directory.Exists(packageDir))
            {
                Log("package python tidak ada di " + packageDir + " - bridge tidak bisa dijalankan.");
                return false;
            }

            var exe = FindPython();
            if (exe == null)
            {
                Log("tidak ada interpreter Python: runtime bawaan paket (python-embed\\python.exe) " +
                    "tidak bisa jalan, dan tidak ada Python di PATH/folder sistem. " +
                    "Install ulang paket ini (v1.6.6+) atau pasang Python 3 lalu " +
                    "coba lagi.");
                return false;
            }

            try
            {
                _bridge = new Process
                {
                    StartInfo = BuildBridgeCommand("run"),
                    EnableRaisingEvents = true
                };
                _bridge.OutputDataReceived += (s, e) => { if (e.Data != null) Log(e.Data); };
                _bridge.ErrorDataReceived += (s, e) => { if (e.Data != null) Log("[err] " + e.Data); };

                _bridge.Start();
                _bridge.BeginOutputReadLine();
                _bridge.BeginErrorReadLine();

                _lastStart = DateTime.Now;
                Log("bridge dijalankan via python (pid " + _bridge.Id + ")");
                return true;
            }
            catch (Exception ex)
            {
                Log("gagal start bridge via python: " + ex.Message);
                _bridge = null;
                return false;
            }
        }

        // Bangun ProcessStartInfo untuk perintah bridge, memakai .exe kalau ada
        // dan jatuh ke python + bridge_launcher.py kalau tidak.
        private static ProcessStartInfo BuildBridgeCommand(string args)
        {
            if (!string.IsNullOrEmpty(_bridgeExe) && File.Exists(_bridgeExe))
            {
                return new ProcessStartInfo(_bridgeExe, args)
                {
                    WorkingDirectory = _installDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
            }

            string packageDir = Path.Combine(_installDir, "cykeo_bridge");
            if (!Directory.Exists(packageDir))
                return null;

            var exe = FindPython();
            if (exe == null)
                return null;

            // WAJIB lewat launcher, BUKAN "-m cykeo_bridge".
            //
            // Interpreter embeddable punya python312._pth, dan begitu file itu
            // ada Python berjalan dalam isolated mode: sys.path dibangun HANYA
            // dari isi _pth, sehingga cwd, PYTHONPATH, dan PYTHONHOME diabaikan.
            // "python -m <module>" bergantung pada sys.path, jadi dalam mode itu
            // selalu gagal dengan "No module named cykeo_bridge" - HAMPIR tanpa
            // kecuali, padahal package-nya ada di folder install.
            //
            // Sebaliknya "python <path-file.py>" dieksekusi langsung tanpa
            // bergantung sys.path. Launcher menambahkan folder install sendiri
            // lalu memanggil module sebagai __main__. Lihat
            // cykeo_bridge/bridge_launcher.py untuk penjelasan lengkap.
            string launcher = Path.Combine(packageDir, "bridge_launcher.py");
            if (!File.Exists(launcher))
            {
                Log("bridge_launcher.py hilang di " + packageDir +
                    " - install ulang paket ini (v1.6.7+).");
                return null;
            }

            return new ProcessStartInfo(exe, "-u \"" + launcher + "\" " + args)
            {
                WorkingDirectory = _installDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
        }

        // Cari interpreter untuk bridge. URUTAN PENTING:
        //   1. runtime mandiri yang ikut dikemas (python-embed\)
        //   2. Python milik sistem
        // Runtime mandiri dicoba lebih dulu karena paket harus bisa jalan di
        // PC yang tidak punya Python sama sekali - dan itulah kondisi nyata
        // di lapangan: bridge restart 100x lebih hanya karena interpreter
        // tidak ketemu, sementara paketnya sendiri sehat.
        private static string FindPython()
        {
            // 1. Runtime mandiri bawaan paket (paling andal)
            var bundled = Path.Combine(_installDir, "python-embed", "python.exe");
            if (File.Exists(bundled))
            {
                if (IsUsablePython(bundled))
                {
                    Log("python: memakai runtime mandiri " + bundled);
                    return bundled;
                }
                Log("runtime mandiri ada tapi tidak bisa jalan, fallback ke Python sistem: " + bundled);
            }

            // 2. Python milik sistem: nama di PATH
            foreach (var name in new[] { "python", "python3", "py" })
            {
                var fromPath = ProbePython(name);
                if (fromPath != null)
                {
                    Log("python: " + fromPath + " (dari PATH)");
                    return fromPath;
                }
            }

            // 3. Python sistem tapi tidak di PATH: lokasi yang paling sering
            //    terlewat. Installer sering hanya menampilkan dialog PATH
            //    sehingga user tidak pernah tahu interpreter-nya sebenarnya ada.
            foreach (var root in new[]
            {
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86)
            })
            {
                if (string.IsNullOrEmpty(root)) continue;
                try
                {
                    foreach (var exe in Directory.GetFiles(root, "python.exe", SearchOption.AllDirectories))
                    {
                        // Tolak stub WindowsApps (App Execution Alias): ukurannya 0
                        // byte dan selalu gagal start dengan exit code 9009.
                        try
                        {
                            if (new FileInfo(exe).Length == 0) continue;
                        }
                        catch { continue; }
                        if (IsUsablePython(exe))
                        {
                            Log("python: " + exe + " (ditemukan lewat pencarian folder)");
                            return exe;
                        }
                    }
                }
                catch { /* folder tidak bisa dibaca - coba root berikutnya */ }
            }
            return null;
        }

        // Buktikan interpreter benar-benar bisa jalan: jalankan probe import,
        // jangan cuma cek file-nya ada. Stub WindowsApps dan exe yang rusak
        // semuanya "ada" tapi tidak bisa dipakai.
        private static bool IsUsablePython(string exe)
        {
            try
            {
                var psi = new ProcessStartInfo(exe,
                    "-c \"import json,ssl,sqlite3,ctypes,struct,socket,urllib.request;print('ok')\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p == null) return false;
                    // Bacalah stderr SEBELUM WaitForExit supaya tidak ada
                    // deadlock pada pipe yang penuh, dan supaya pesan
                    // kegagalan ikut terbaca (BUG v1.6.7: stdout saja
                    // membuat penyebab kegagalan tidak pernah terlihat).
                    string outp = p.StandardOutput.ReadToEnd();
                    string errp = p.StandardError.ReadToEnd();
                    p.WaitForExit(15000);
                    bool ok = p.ExitCode == 0 && outp != null && outp.Trim() == "ok";
                    if (!ok && !string.IsNullOrEmpty(errp))
                    {
                        string first = errp.Trim();
                        int nl = first.IndexOfAny(new[] { '\r', '\n' });
                        if (nl > 0) first = first.Substring(0, nl);
                        Log("[runtime] " + exe + " gagal: " + first);
                    }
                    return ok;
                }
            }
            catch (Exception ex)
            {
                Log("[runtime] " + exe + " gagal: " + ex.Message);
                return false;
            }
        }

        // Jalankan nama python dari PATH, kembalikan sys.executable asli.
        private static string ProbePython(string name)
        {
            try
            {
                var psi = new ProcessStartInfo(name, "-c \"import sys; print(sys.executable)\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p == null) return null;
                    string path = p.StandardOutput.ReadToEnd().Trim();
                    p.WaitForExit(8000);
                    if (path.Length > 0 && File.Exists(path) && IsUsablePython(path))
                        return path;
                }
            }
            catch { /* python tidak ada - coba nama berikutnya */ }
            return null;
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

        // -------------------------------------------------------- uji pembaca

        private static void RunReaderTest()
        {
            if (_readerTestBusy)
            {
                MessageBox.Show("Uji pembaca masih berjalan. Tunggu sampai selesai.",
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            // Reader dipegang helper selama bridge jalan (OpenSerial bersifat
            // eksklusif), jadi bridge harus berhenti lebih dulu. Kalau tidak,
            // ujinya akan gagal membuka port dan teknisi menyalahkan reader
            // yang sebenarnya sehat.
            bool bridgeWasAlive = IsBridgeAlive();
            if (bridgeWasAlive)
            {
                Log("uji pembaca: bridge dihentikan sementara");
                StopBridge();
                SetTrayState();
            }

            _readerTestBusy = true;
            _lastReaderTestExit = -1;   // jangan pakai sisa exit code uji sebelumnya
            ThreadPool.QueueUserWorkItem(_ =>
            {
                string hasil;
                int exitCode;
                try
                {
                    hasil = JalankanTestReader();
                    exitCode = _lastReaderTestExit;
                }
                catch (Exception ex)
                {
                    hasil = "Gagal menjalankan uji: " + ex.Message;
                    exitCode = -1;
                }
                finally
                {
                    _readerTestBusy = false;
                }

                var sb = new StringBuilder();
                sb.AppendLine("Uji Pembaca RFID - hasil");
                sb.AppendLine("========================================");
                sb.AppendLine();
                sb.AppendLine(hasil.TrimEnd());
                sb.AppendLine();
                sb.AppendLine(RingkasanExitCode());
                if (bridgeWasAlive)
                {
                    sb.AppendLine();
                    sb.AppendLine("Bridge dijalankan kembali.");
                }

                Log("uji pembaca selesai: " + hasil.Replace(Environment.NewLine, " | "));

                try
                {
                    if (bridgeWasAlive)
                    {
                        _lastStart = DateTime.MinValue;
                        StartBridge();
                        SetTrayState();
                    }
                }
                catch { }

                try
                {
                    BeginInvokeOnUi(new Action(() =>
                        ShowResult(sb.ToString(),
                            exitCode == 0 ? MessageBoxIcon.Information
                                          : MessageBoxIcon.Warning)));
                }
                catch { }
            });
        }

        private static string JalankanTestReader()
        {
            // Paket tidak membawa cykeo_bridge.exe, jadi Uji Pembaca harus
            // memakai runtime python + bridge_launcher.py. Tanpa fallback ini
            // tombol Uji Pembaca selalu gagal walaupun bridge python-nya hidup.
            var psi = BuildBridgeCommand("test-reader --duration 15");
            if (psi == null)
            {
                return "Tidak bisa menjalankan uji: tidak ada cykeo_bridge.exe, " +
                       "runtime python, maupun bridge_launcher.py." +
                       Environment.NewLine +
                       "Jalankan: " + _installDir + " -> install ulang paket ini " +
                       "(v1.6.7+).";
            }

            using (var p = Process.Start(psi))
            {
                if (p == null)
                {
                    return "Gagal memulai proses uji: " + psi.FileName;
                }

                // Output dibaca asinkron. Kalau stdout di-buffer penuh dan kita
                // blocking ReadToEnd() lebih dulu, proses anak bisa menggantung
                // tanpa pernah Write, WaitForExit tidak akan pernah dipanggil.
                var stdoutTask = p.StandardOutput.ReadToEndAsync();
                var stderrTask = p.StandardError.ReadToEndAsync();

                // Batas keras supaya dialog tidak menggantung selamanya kalau
                // helper menggantung saat handshake reader.
                if (!p.WaitForExit(90 * 1000))
                {
                    try { p.Kill(); } catch { }
                    return "Uji dihentikan paksa setelah 90 detik (helper menggantung).";
                }

                // Beri chance singkat ke task reader menulis sisa output.
                p.WaitForExit();   // pastikan stream benar-benar habis
                _lastReaderTestExit = p.ExitCode;

                string gabung = (stdoutTask.Result ?? string.Empty).Trim();
                string err = (stderrTask.Result ?? string.Empty).Trim();
                if (gabung.Length > 0 && err.Length > 0)
                {
                    gabung += Environment.NewLine + "[err] " + err;
                }
                else if (err.Length > 0)
                {
                    gabung = err;
                }
                return gabung;
            }
        }

        private static string RingkasanExitCode()
        {
            switch (_lastReaderTestExit)
            {
                case 0:
                    return "Kesimpulan: EPC BERHASIL terbaca. Reader dan tag normal.";
                case 1:
                    return "Kesimpulan: reader TIDAK menghasilkan tag. Arahkan tag ke "
                         + "antena reader, atau cek power reader.";
                case 2:
                    return "Kesimpulan: konfigurasi bermasalah. Buka menu tray "
                         + "'Konfigurasi reader (minta izin admin)' lalu ulangi.";
                default:
                    return "Kesimpulan: uji tidak selesai normal. Buka folder log.";
            }
        }

        // ------------------------------------------- wizard konfigurasi (tray)

        /// <summary>
        /// Path exe tray agent yang sedang berjalan.
        ///
        /// v1.6.25: source sebelumnya memakai
        /// Path.Combine(BaseDirectory, "CykeoTrayAgent.exe"). Itu rapuh:
        ///   - kalau tray dijalankan lewat shortcut/scheduled task, nama
        ///     file executable bisa berbeda dari nama asembly, sehingga
        ///     path hasil Combine tidak ada (file not found);
        ///   - kalau file-nya memang ada tapi BaseDirectory kosong/salah,
        ///     wizard gagal.
        ///
        /// Resolver ini mencoba beberapa sumber, dari yang paling exact:
        ///   1. Lokasi assembly yang sedang dieksekusi (paling akurat -
        ///      menunjuk file binary yang benar-benar jalan).
        ///   2. Process.GetCurrentProcess().MainModule.FileName.
        ///   3. Application.ExecutablePath.
        ///   4. BaseDirectory + nama exe.
        ///   5. Pencarian exe di BaseDirectory dan folder install.
        /// </summary>
        private static string ResolveSelfExe()
        {
            var seen = new System.Collections.Generic.List<string>();

            Action<string> add = delegate(string p)
            {
                if (!string.IsNullOrEmpty(p) && !seen.Contains(p))
                    seen.Add(p);
            };

            try
            {
                add(System.Reflection.Assembly.GetExecutingAssembly().Location);
            }
            catch { }
            try
            {
                add(Process.GetCurrentProcess().MainModule.FileName);
            }
            catch { }
            try
            {
                add(Application.ExecutablePath);
            }
            catch { }

            string baseDir = null;
            try { baseDir = AppDomain.CurrentDomain.BaseDirectory; }
            catch { }
            if (!string.IsNullOrEmpty(baseDir))
            {
                add(Path.Combine(baseDir, "CykeoTrayAgent.exe"));
                add(Path.Combine(baseDir, AppName + ".exe"));
            }
            if (!string.IsNullOrEmpty(_installDir))
            {
                add(Path.Combine(_installDir, "CykeoTrayAgent.exe"));
            }

            foreach (string p in seen)
            {
                try
                {
                    if (File.Exists(p))
                        return Path.GetFullPath(p);
                }
                catch { }
            }

            // Terakhir: cari exe pertama yang cocok di folder install /
            // base directory. Ini menutup kasus file tray diganti nama.
            string[] roots = { baseDir, _installDir };
            foreach (string root in roots)
            {
                if (string.IsNullOrEmpty(root) || !Directory.Exists(root))
                    continue;
                try
                {
                    string[] hits = Directory.GetFiles(root, "*.exe");
                    foreach (string hit in hits)
                    {
                        string name = Path.GetFileNameWithoutExtension(hit);
                        if (name.IndexOf("TrayAgent", StringComparison.OrdinalIgnoreCase) >= 0)
                            return Path.GetFullPath(hit);
                    }
                }
                catch { }
            }

            return null;
        }

        private static void RunConfigureWizard()
        {
            // v1.6.24: wizard konfigurasi jadi GUI NATIVE (C#/WinForms),
            // bukan Python (tkinter).
            //
            // SEBELUMNYA (v1.6.23) menu ini menjalankan
            // "python bridge_launcher.py wizard" yang membuka form tkinter.
            // Masalahnya di PC teknisi:
            //   1. Runtime Python embeddable di paket TIDAK selalu punya
            //      tkinter, jadi wizard jatuh ke mode CLI (teks) - teknisi
            //      diminta isi konfigurasi lewat terminal, bukan form.
            //   2. Kalau CLI pun gagal, user tidak pernah bisa menyelesaikan
            //      wizard dari menu tray sama sekali.
            //   3. Butuh interpreter Python hidup untuk hal yang sebenarnya
            //      cuma mengisi beberapa field dan menulis satu file JSON.
            //
            // SEKARANG form WinForms-nya built INTO tray agent sendiri.
            // Tidak butuh Python, tidak butuh tkinter, tidak butuh paket
            // tambahan.
            //
            // Elevasi: config.json ditulis ke %ProgramData%\CykeoBridge\
            // yang butuh hak admin, sedangkan tray jalan sebagai user biasa
            // (tanpa admin). Jadi form dijalankan sebagai PROSES ANAK yang
            // elevated (verb "runas"), persis seperti wizard Python
            // sebelumnya. Tray sendiri TIDAK elevated.
            //
            // Exe yang dijalankan adalah file tray agent yang sedang hidup
            // (Path.GetDirectoryName(Application.ExecutablePath)), dengan
            // argumen --wizard. Jadi wizard tidak butuh file terpisah.
            // v1.6.25: BUG PERBAIKAN. Source sebelumnya menulis:
            //
            //   new ProcessStartInfo("\"selfExe\" --wizard --config ...")
            //
            // Constructor 2-argumen itu menyamakan FileName dengan SELURUH
            // command line. Dengan UseShellExecute=true, ShellExecute
            // memperlakukan seluruh string itu sebagai SATU path file, jadi
            // Windows mencari file bernama
            //   "C:\...\CykeoTrayAgent.exe" --wizard --config "C:\..."
            // yang tidak ada -> ERROR_FILE_NOT_FOUND (2), dan dialog
            // "Gagal menjalankan wizard: The system cannot find the file
            // specified." muncul. Persis yang dilaporkan teknisi.
            //
            // PERBAIKAN: FileName = path exe saja, argumen dipisah lewat
            // property Arguments. ShellExecute lalu benar-benar menjalankan
            // exe dengan argumen tersebut.
            string selfExe = ResolveSelfExe();
            if (selfExe == null)
            {
                MessageBox.Show(
                    "File tray agent tidak ditemukan di:" + Environment.NewLine +
                    "  " + AppDomain.CurrentDomain.BaseDirectory +
                    Environment.NewLine + Environment.NewLine +
                    "Jalankan ulang paket installer untuk memulihkan file tray.",
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            string configPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "CykeoBridge", "config.json");
            string pythonExe = FindPython();

            // Argumen dirakit terpisah. Path yang mengandung spasi tetap
            // aman karena ProcessStartInfo meng-quote sendiri.
            var wizardArgs = new System.Text.StringBuilder();
            wizardArgs.Append("--wizard");
            wizardArgs.Append(" --config \"").Append(configPath).Append("\"");
            if (!string.IsNullOrEmpty(_installDir))
                wizardArgs.Append(" --install-dir \"").Append(_installDir).Append("\"");
            if (!string.IsNullOrEmpty(pythonExe))
                wizardArgs.Append(" --python \"").Append(pythonExe).Append("\"");

            try
            {
                var psi = new ProcessStartInfo(selfExe, wizardArgs.ToString())
                {
                    WorkingDirectory = string.IsNullOrEmpty(_installDir)
                        ? AppDomain.CurrentDomain.BaseDirectory
                        : _installDir,
                    // "runas" = minta elevasi lewat UAC. WAJIB karena wizard
                    // menulis config.json ke %ProgramData%.
                    Verb = "runas",
                    UseShellExecute = true
                };

                Process.Start(psi);
                Log("wizard konfigurasi GUI native dijalankan (elevated): " +
                    selfExe + " " + wizardArgs);
            }
            catch (System.ComponentModel.Win32Exception ex)
            {
                // 1223 = ERROR_CANCELLED: user menekan "No" di dialog UAC.
                // Itu bukan kegagalan, jadi jangan ditakuti dengan pesan error.
                if (ex.NativeErrorCode == 1223)
                {
                    Log("wizard konfigurasi dibatalkan user di dialog UAC.");
                    return;
                }

                MessageBox.Show("Gagal menjalankan wizard:" + Environment.NewLine + ex.Message,
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Gagal menjalankan wizard:" + Environment.NewLine + ex.Message,
                    AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        // -------------------------------------------------------------- status

        private static void ShowStatus()
        {
            bool bridgeAlive = IsBridgeAlive();
            bool helperPresent = File.Exists(_helperExe);
            bool configPresent = File.Exists(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
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
            sb.AppendLine("Guardian: " + (GuardianAlive() ? "aktif (pulihkan tray kalau mati)" : "TIDAK AKTIF"));
            sb.AppendLine("Keluar  : " + (IsIntentionalExit()
                ? " disengaja (guardian tidak akan menghidupkan tray)"
                : " normal"));
            sb.AppendLine();
            sb.AppendLine("Log     : " + _logDir);
            sb.AppendLine();
            sb.AppendLine("Reader CK-D5:");
            sb.AppendLine("  Bridge hanya berhasil baca tag kalau port COM reader terlihat.");
            sb.AppendLine("  Cek Device Manager - Ports (COM & LPT).");
            sb.AppendLine();
            sb.AppendLine("Ikon tray hilang? Cara memulihkannya:");
            sb.AppendLine("  1. Klik dua kali shortcut Desktop 'Cykeo RFID Bridge'.");
            sb.AppendLine("  2. Kalau tidak ada, klik kanan ikon Cykeo di Start Menu");
            sb.AppendLine("     -> folder Programs -> Cykeo RFID Bridge.");
            sb.AppendLine("  3. Kalau keduanya hilang, buka folder install lalu");
            sb.AppendLine("     klik dua kali CykeoTrayAgent.exe.");
            sb.AppendLine("  Tray yang mati sendiri (crash / End task) dipulihkan");
            sb.AppendLine("  otomatis oleh guardian dalam beberapa detik.");

            ShowResult(sb.ToString(),
                bridgeAlive ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        }

// ---------------------------------------------------------- pemulihan tray
        //
        // Semua logika pemulihan hidup di sini. Dua aturan yang harus selalu
        // berpasangan:
        //
        //   * Tray yang DIBUNUH paksa (crash, Task Manager End task, taskkill,
        //     jendela ditutup dari luar) tidak pernah menulis penanda
        //     disengaja, jadi guardian WAJIB menghidupkan tray lagi.
        //
        //   * Tray yang DI-KELUAR dari menu menulis penanda disengaja lebih
        //     dulu, jadi guardian HARUS menahan diri. Tanpa aturan kedua,
        //     guardian akan menghidupkan tray tepat setelah user menekan
        //     Keluar, tray muncul sendiri, dan user tidak pernah bisa
        //     mematikan - lebih buruk dari tidak ada pemulihan sama sekali.
        //
        // Guardian sendiri adalah proses CykeoTrayAgent.exe yang hidup dalam
        // mode --watchdog. Guardian TIDAK memegang TrayMutexName, jadi tidak
        // akan membuat tray baru menyuruh "sudah berjalan" saat start.

        internal static bool HasArg(string[] args, string name)
        {
            if (args == null) return false;
            foreach (var a in args)
            {
                if (string.Equals(a, name, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            return false;
        }

        // Path penanda exit disengaja.
        private static string IntentionalExitPath()
        {
            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "CykeoBridge", IntentionalExitFileName);
        }

        // Stempel waktu proses start, dipakai sebagai batas "sesi".
        //
        // PERBAIKAN v1.6.17. Penanda exit-disengaja.flag ditulis saat user
        // menekan Keluar, lalu TIDAK PERNAH dihapus otomatis. Guardian
        // menghormatinya tanpa batas waktu, jadi begitu user logoff/reboot,
        // guardian di logon berikutnya menerima flag yang dibuat di sesi
        // SEBELUMNYA dan tidak akan pernah menghidupkan tray lagi. Akibatnya
        // ikon tray hilang permanen tanpa indikator apa pun di layar; satu-
        // satu jalan keluar adalah menjalankan exe manual atau menu Start.
        //
        // Flag itu harus tetap bisa menghentikan guardian pada sesi yang
        // SAMA (tekanan Keluar sekali tidak boleh dibalik 5 detik kemudian),
        // tapi tidak boleh bertahan lintas logon. Karena MarkIntentionalExit()
        // sudah menulis timestamp di isi file, kita tidak perlu state tambahan:
        // cukup perlakukan flag yang dibuat sebelum logon berjalan sebagai
        // peninggalan sesi lama.
        private static DateTime _prosesStart = DateTime.MinValue;

        internal static void CatatProsesStart()
        {
            try { _prosesStart = DateTime.Now; }
            catch { _prosesStart = DateTime.MinValue; }
        }

        // Ambil stempel waktu yang tertulis di flag. Formatnya ditulis
        // MarkIntentionalExit(): "yyyy-MM-dd HH:mm:ss".
        internal static bool CobaBacaTandaWaktuFlag(out DateTime waktuDitulis)
        {
            waktuDitulis = DateTime.MinValue;
            try
            {
                string isi = ReadAllTextSafe(IntentionalExitPath());
                if (string.IsNullOrEmpty(isi))
                    return false;

                // Cari 19 karakter pertama: "2026-10-01 12:58:40"
                string depan = isi.Length >= 19 ? isi.Substring(0, 19) : isi.Trim();
                DateTime t;
                if (DateTime.TryParseExact(depan.Trim(),
                        "yyyy-MM-dd HH:mm:ss",
                        System.Globalization.CultureInfo.InvariantCulture,
                        System.Globalization.DateTimeStyles.None, out t))
                {
                    waktuDitulis = t;
                    return true;
                }
            }
            catch { }
            return false;
        }

        private static string ReadAllTextSafe(string path)
        {
            try { return File.Exists(path) ? File.ReadAllText(path) : null; }
            catch { return null; }
        }

        internal static bool IsIntentionalExit()
        {
            try
            {
                string p = IntentionalExitPath();
                if (!File.Exists(p))
                    return false;

                // Flag tanpa stempel waktu yang bisa dibaca: perlakukan
                // sebagai disengaja. Itu perilaku versi lama dan lebih aman
                // daripada membangkitkan tray yang memang tidak diinginkan.
                DateTime ditulis;
                if (!CobaBacaTandaWaktuFlag(out ditulis))
                    return true;

                // Flag dibuat SEBELUM proses ini mulai = tinggalan logon
                // sebelumnya. Guardian harus menghidupkan tray.
                if (_prosesStart == DateTime.MinValue)
                    return true;    // paranoid: tidak tahu, hormati saja
                if (ditulis < _prosesStart)
                {
                    Log("guardian: penanda disengaja dari sesi sebelumnya " +
                        "(" + ditulis.ToString("yyyy-MM-dd HH:mm:ss") +
                        " < start " +
                        _prosesStart.ToString("yyyy-MM-dd HH:mm:ss") +
                        ") diabaikan; tray dihidupkan.");
                    ClearIntentionalExit();
                    return false;
                }

                // Ditulis pada sesi ini: user memang sedang menekan Keluar.
                return true;
            }
            catch { return false; }
        }

        internal static void MarkIntentionalExit()
        {
            try
            {
                string p = IntentionalExitPath();
                Directory.CreateDirectory(Path.GetDirectoryName(p));
                File.WriteAllText(p,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") +
                    " tray keluar atas permintaan user. Guardian tidak akan " +
                    "menghidupkan ulang. Jalankan shortcut Desktop atau menu " +
                    "Start untuk memulai lagi." + Environment.NewLine);
                Log("exit disengaja ditandai; guardian tidak akan menghidupkan tray.");
            }
            catch (Exception ex)
            {
                Log("gagal menulis penanda exit disengaja: " + ex.Message);
            }
        }

        internal static void ClearIntentionalExit()
        {
            try
            {
                string p = IntentionalExitPath();
                if (File.Exists(p))
                {
                    File.Delete(p);
                    Log("penanda exit disengaja dibuang (tray sengaja dijalankan lagi).");
                }
            }
            catch (Exception ex)
            {
                Log("gagal menghapus penanda exit disengaja: " + ex.Message);
            }
        }

        // Guardian dijamin hidup setiap kali tray start. Ini jaring pengaman:
        // kalau HKCU Run tidak berisi entri guardian (installer dijalankan
        // dengan -SkipStartup, atau user pernah mengunci registry), tray
        // tetap menyalakan guardian sendiri.
        private static void EnsureGuardian()
        {
            try
            {
                if (GuardianAlive())
                {
                    Log("guardian: sudah hidup, tidak perlu start lagi.");
                    return;
                }
                string exe = Application.ExecutablePath;
                var psi = new ProcessStartInfo(exe, WatchdogArg)
                {
                    WorkingDirectory = Path.GetDirectoryName(exe),
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                Process p = Process.Start(psi);
                Log("guardian dijalankan (pid " + (p == null ? "?" : p.Id.ToString()) +
                    ") supaya tray bisa dipulihkan kalau mati tidak-engaja.");
            }
            catch (Exception ex)
            {
                Log("guardian gagal dijalankan: " + ex.Message);
            }
        }

        // Guardian hidup = mutex-nya dipegang proses lain. Dilihat lewat mutex
        // named, bukan nama proses, karena nama prosesnya sama persis dengan
        // tray.
        //
        // Nama file yang berisi PID tray yang benar-benar hidup. Tray menulis
        // file ini setiap kali mutex berhasil diambil, lalu menghapusnya saat
        // keluar rapi.
        //
        // Kenapa file, bukan NtQueryWaitObject: di build 32-bit, pembacaan
        // OBJECT_BASIC_INFORMATION lewat NtQueryWaitObject tidak pernah mengisi
        // ProcessId dengan benar sehingga selalu mengembalikan 0. Tray lalu
        // menganggap "pemegang mutex sudah mati" lalu keluar, guardian
        // menyalakannya lagi, dan siklus itu berulang. File PID menutup celah
        // itu tanpa bergantung pada API kernel yang tidak bisa dipercaya.
        internal const string TrayPidFileName = "tray.pid";

        private static string TrayPidPath()
        {
            try
            {
                if (string.IsNullOrEmpty(_logDir)) return string.Empty;
                return Path.Combine(_logDir, TrayPidFileName);
            }
            catch { return string.Empty; }
        }

        // Tulis PID tray ke file. Dipanggil setelah mutex benar-benar dipegang,
        // jadi file ini hanya ada kalau ada satu proses yang hidup dan memegang
        // mutex.
        private static void WriteTrayPid()
        {
            try
            {
                string p = TrayPidPath();
                if (string.IsNullOrEmpty(p)) return;
                File.WriteAllText(p, Process.GetCurrentProcess().Id.ToString());
            }
            catch { }
        }

        private static void RemoveTrayPid()
        {
            try
            {
                string p = TrayPidPath();
                if (string.IsNullOrEmpty(p)) return;
                if (File.Exists(p)) File.Delete(p);
            }
            catch { }
        }

        // PID tray hidup, menurut file PID. Mengembalikan 0 kalau file tidak
        // ada, file rusak, atau prosesnya sudah mati.
        private static int TrayPidFromFile()
        {
            try
            {
                string p = TrayPidPath();
                if (string.IsNullOrEmpty(p)) return 0;
                if (!File.Exists(p)) return 0;
                int pid;
                if (!int.TryParse(File.ReadAllText(p).Trim(), out pid)) return 0;
                if (pid <= 0) return 0;
                if (Process.GetProcessById(pid).HasExited) return 0;
                return pid;
            }
            catch (ArgumentException) { return 0; }
            catch { return 0; }
        }

        // Path log tray, atau string kosong kalau folder log tidak bisa
        // ditentukan. Guardian butuh ini untuk membuktikan tray benar-benar
        // start, bukan cuma launching process yang langsung exit.
        private static string TrayLogPath()
        {
            try
            {
                if (string.IsNullOrEmpty(_logDir)) return string.Empty;
                return Path.Combine(_logDir, TrayLogFileName);
            }
            catch { return string.Empty; }
        }

        // PID pemilik named mutex, dibaca dari kernel (WaitHandle.SafeWaitHandle
        // + GetOwner). Constructor Mutex hanya melaporkan "createdNew" -
        // tidak ada overload yang mengembalikan PID, padahal PID pemilik
        // satu-satunya pembeda antara "tray memang hidup" dan "mutex
        // ditinggalkan proses yang sudah mati".
        private static int MutexOwnerPid(Mutex m)
        {
            try
            {
                if (m == null) return 0;
                if (m.SafeWaitHandle.IsInvalid) return 0;

                const int ObjectBasicInformation = 1;
                WAITORTYPE info;
                int rc = NtQueryWaitObject(
                    m.SafeWaitHandle.DangerousGetHandle(),
                    ObjectBasicInformation,
                    out info,
                    Marshal.SizeOf(typeof(WAITORTYPE)),
                    IntPtr.Zero);
                // ProcessId kini IntPtr, jadi harus dikonversi eksplisit ke
                // int. Dibaca lewat ToInt64 supaya aman di 32-bit maupun 64-bit.
                if (rc == 0) return (int)(info.ProcessId.ToInt64() & 0xFFFFFFFF);
            }
            catch { }
            return 0;
        }

        // Layout OBJECT_BASIC_INFORMATION milik ntdll. Versi sebelumnya
        // memakai int untuk ProcessId, padahal field itu bertipe ULONGLONG
        // (8 byte). Akibatnya setiap field setelahnya bergeser 4 byte: PID
        // terbaca 0, sehingga tray selalu mengira "pemegang mutex sudah
        // mati", menunggu 10 detik, lalu keluar - sementara guardian
        // menyalakannya lagi. Itulah sebabnya dialog lama menumpuk.
        [StructLayout(LayoutKind.Sequential)]
        private struct WAITORTYPE
        {
            public int ObjectType;
            public int ObjectState;
            public IntPtr Handle;
            public IntPtr ProcessId;
            public IntPtr ThreadId;
        }

        [DllImport("ntdll.dll")]
        private static extern int NtQueryWaitObject(
            IntPtr handle,
            int objectInfoClass,
            out WAITORTYPE buffer,
            int bufferLength,
            IntPtr returnLength);

        // Stempel waktu terakhir file log, dipotong ke milidetik supaya
        // perbandingan sebelum dan sesudah start tidak menjadi false positive
        // karena presisi filesystem yang lebih rendah.
        private static DateTime SafeLogStamp(string path)
        {
            try
            {
                if (string.IsNullOrEmpty(path)) return DateTime.MinValue;
                if (!File.Exists(path)) return DateTime.MinValue;
                return new DateTime(File.GetLastWriteTimeUtc(path).Ticks / TimeSpan.TicksPerMillisecond * TimeSpan.TicksPerMillisecond, DateTimeKind.Utc);
            }
            catch { return DateTime.MinValue; }
        }

        // Ringkasan kondisi mutex tray, untuk log guardian. Bedakan dua
        // kondisi yang dari luar terlihat sama:
        //   "bebas"  -> tidak dipegang siapa pun; tray benar-benar mati
        //   "dipegang" -> ada tray jalan, tapi file PID tidak bisa dibaca
        // Guardian harus bisa membedakan keduanya dari log, kalau tidak
        // bug restart berulang akan sulit didiagnosis di lapangan.
        private static string DescribeMutexState()
        {
            Mutex probe = null;
            try
            {
                probe = new Mutex(false, TrayMutexName);
                if (probe.WaitOne(0))
                {
                    probe.ReleaseMutex();
                    return "mutex bebas (tray tidak hidup)";
                }

                int owner = MutexOwnerPid(probe);
                return "mutex dipegang proses hidup (pid kernel " +
                       (owner > 0 ? owner.ToString() : "tidak terbaca") +
                       "); file PID tidak bisa dipakai";
            }
            catch (AbandonedMutexException)
            {
                return "mutex ditinggalkan proses mati (sudah bebas)";
            }
            catch { return "mutex tidak bisa diperiksa"; }
            finally
            {
                if (probe != null) { try { probe.Dispose(); } catch { } }
            }
        }

        private static bool GuardianAlive()
        {
            try
            {
                using (var probe = new Mutex(false, WatchdogMutexName))
                {
                    bool acquired = probe.WaitOne(0);
                    if (acquired) probe.ReleaseMutex();
                    return !acquired;
                }
            }
            catch (AbandonedMutexException)
            {
                // Proses sebelumnya mati tanpa melepas mutex. Mutex sekarang
                // bebas, jadi guardian yang lama sudah tidak hidup.
                return false;
            }
            catch { return false; }
        }

        // Tunggu tray yang sedang jalan benar-benar melepas mutex. Dipakai mode
        // relay: proses pengganti harus tahu tray lama sudah keluar, kalau
        // tidak dia akan mendapat "sudah berjalan" lalu mati juga, dan tray
        // jadi tidak ada sama sekali.
        private static void WaitForTrayMutexFree()
        {
            for (int i = 0; i < 40; i++)
            {
                try
                {
                    using (var probe = new Mutex(false, TrayMutexName))
                    {
                        if (probe.WaitOne(0))
                        {
                            probe.ReleaseMutex();
                            return;
                        }
                    }
                }
                catch (AbandonedMutexException)
                {
                    return;
                }
                catch { return; }
                Thread.Sleep(250);
            }
        }

        // Loop guardian. Satu-satunya tempat yang BOLEH menjalankan tray tanpa
        // ada yang meminta, jadi aturannya harus eksplisit:
        //
        //   tray hidup + penanda disengaja  -> apa-apa (tray sedang berjalan)
        //   tray hidup + tanpa penanda      -> apa-apa
        //   tray mati  + penanda disengaja  -> JANGAN hidupkan (user mau stop)
        //   tray mati  + tanpa penanda      -> hidupkan (crash / taskkill)
        private static void RunGuardian()
        {
            _logDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "CykeoBridge", "logs");
            try { Directory.CreateDirectory(_logDir); } catch { }

            bool acquired;
            _guardianMutex = new Mutex(true, WatchdogMutexName, out acquired);
            if (!acquired)
            {
                // Guardian lain sudah mengawasi. Keluar tanpa log supaya tidak
                // ada dua pengawas yang berebut menghidupkan tray.
                return;
            }

            Log("guardian aktif (mode " + WatchdogArg +
                "): memulihkan tray kalau mati tidak-engaja. Penanda " +
                "disengaja: " + IntentionalExitFileName);
            Log("cara manual: shortcut Desktop '" + AppName +
                "', atau menu Start, atau klik dua kali " +
                "CykeoTrayAgent.exe di folder install.");

            DateTime stopAt = DateTime.MaxValue;
            bool sudahLaporBerhenti = false;

            try
            {
                while (true)
                {
                    Thread.Sleep(GuardianPollSeconds * 1000);

                    if (GuardianAlive())
                        continue;   // mustahil di proses sendiri, tapi aman

                    if (IsIntentionalExit())
                    {
                        // Berhenti setelah batas idle supaya tidak ada proses
                        // sia-sia menganggur di PC yang memang sudah tidak
                        // dipakai. Kalau user menghidupkan tray lagi, guardian
                        // otomatis hidup lagi lewat EnsureGuardian().
                        stopAt = DateTime.Now.AddSeconds(GuardianIdleExitSeconds);
                        if (!sudahLaporBerhenti)
                        {
                            Log("guardian: exit disengaja terdeteksi, guardian " +
                                "akan berhenti dalam " +
                                (GuardianIdleExitSeconds / 60) +
                                " menit kalau tray tidak dijalankan lagi.");
                            sudahLaporBerhenti = true;
                        }
                        if (DateTime.Now < stopAt)
                            continue;

                        Log("guardian: keluar setelah lama tidak dipakai. Tray " +
                            "bisa dijalankan lagi lewat shortcut Desktop atau " +
                            "menu Start.");
                        return;
                    }

                    sudahLaporBerhenti = false;

                    // PERBAIKAN v1.6.16 - akar bug dialog bertumpuk.
                    //
                    // Loop ini TIDAK PERNAH menanyakan "apakah tray sedang
                    // hidup?" sebelum menjalankan tray lagi. Yang dicek cuma
                    // penanda exit disengaja. Padahal di bawah, Start()
                    // dipanggil tanpa syarat apa pun, dan tray yang baru
                    // start itu langsung kena "tray sudah berjalan" lalu
                    // keluar lagi.
                    //
                    // Kalau tray yang SEBENARNYA sedang hidup (bridge jalan,
                    // log berisi batch terkirim), guardian tetap menganggap
                    // "tray tidak hidup": satu-satunya syarat yang ia punya
                    // adalah penanda disengaja, dan penanda itu memang
                    // sengaja dihapus begitu tray hidup. Jadi tiap tick ia
                    // menyalakan satu proses tray baru yang mati dalam
                    // <1 detik. Log dari PC bermasalah: 177 restart dengan
                    // 177 PID berbeda, tiap 10-11 detik, selama 3,5 jam.
                    //
                    // Perbaikan: sebelum Start(), buktikan tray tidak hidup,
                    // lewat file PID yang ditulis tray pemilik mutex.
                    // Kalau ada PID hidup, ini siklus yang sedang berjalan,
                    // bukan crash - jangan sentuh apa pun.
                    // Sumber kebenaran: file PID DAN mutex. Keduanya harus bilang
                    // "tidak ada" sebelum guardian boleh menjalankan tray.
                    //
                    // Kenapa dua sumber? Dari log PC bermasalah, file tray.pid berisi
                    // PID 17532 - dan 17532 justru PID yang DICETAK guardian saat start,
                    // yaitu proses yang dibuat guardian lalu langsung mati karena mutex
                    // sudah dipegang tray asli. File PID bisa usang, jadi tidak boleh
                    // jadi satu-satunya acuan. Kalau mutex dipegang proses hidup, tray
                    // jelas hidup - jangan pernah Start() dalam keadaan itu, itulah
                    // penyebab 177 restart dengan 177 PID berbeda.
                    int trayHidup = TrayPidFromFile();
                    if (trayHidup > 0)
                    {
                        continue;   // ada instance hidup; ini kondisi normal
                    }

                    string mutexState = DescribeMutexState();
                    if (mutexState.IndexOf("dipegang proses hidup", StringComparison.Ordinal) >= 0)
                    {
                        // Mutex dipegang proses yang hidup tapi file PID tidak bisa
                        // dipercaya (usang / hilang). Tetap anggap tray hidup.
                        continue;
                    }

                    // Tidak ada PID hidup dan mutex bebas: tray benar-benar tidak ada.
                    // Dua-duanya sudah dicek, jadi Start() di bawah aman.
                    Log("guardian: tray mati tidak-engaja (" + mutexState +
                        ") -> menjalankan tray.");

                    string dir = FindInstallDir();
                    if (string.IsNullOrEmpty(dir))
                    {
                        Log("guardian: folder install tidak ditemukan, keluar.");
                        return;
                    }

                    string trayExe = Path.Combine(dir, "CykeoTrayAgent.exe");
                    if (!File.Exists(trayExe))
                    {
                        Log("guardian: exe tray hilang di " + trayExe + ", keluar.");
                        return;
                    }

                    try
                    {
                        // Catat waktu sebelum start. Kalau tray benar-benar
                        // berhasil start, ia menulis log dalam hitungan
                        // milidetik. Kalau log itu TIDAK berubah dalam
                        // grace period, berarti tray start lalu langsung mati
                        // (mis. mutex dipegang proses yang tidak bisa
                        // dibaca, atau install dir hilang). Tanpa deteksi ini
                        // guardian akan menyalakan tray lagi setiap tick
                        // selamanya - itulah yang bikin dialog menumpuk.
                        DateTime sebelum = SafeLogStamp(TrayLogPath());

                        var psi = new ProcessStartInfo(trayExe)
                        {
                            WorkingDirectory = dir,
                            UseShellExecute = false,
                            CreateNoWindow = true
                        };
                        Process p = Process.Start(psi);
                        Log("guardian: tray dijalankan ulang (pid " +
                            (p == null ? "?" : p.Id.ToString()) + ").");

                        // Tunggu proses baru selesai start. Kalau dia sudah
                        // exit tanpa jejak di log, jangan coba lagi - tanpa
                        // henti itu, restart akan berjalan selamanya.
                        for (int t = 0; t < 10; t++)
                        {
                            Thread.Sleep(500);
                            if (SafeLogStamp(TrayLogPath()) != sebelum) break;
                            try { if (p != null && p.HasExited) break; } catch { break; }
                        }

                        if (SafeLogStamp(TrayLogPath()) == sebelum)
                        {
                            Log("guardian: tray TIDAK menulis log saat start " +
                                "(langsung keluar). Berhenti restart supaya tidak " +
                                "muncul dialog bertumpuk. Periksa mutex " + TrayMutexName +
                                " atau jalankan tray manual untuk lihat pesan errornya.");
                            return;
                        }

                        // Beri jeda supaya tray sempat mengambil mutex-nya.
                        // Kalau tidak, tick berikutnya masih melihat mutex
                        // kosong dan menyalakan proses kedua.
                        Thread.Sleep(GuardianPollSeconds * 1000);
                    }
                    catch (Exception ex)
                    {
                        Log("guardian: gagal menjalankan tray: " + ex.Message);
                    }
                }
            }
            finally
            {
                Log("guardian berhenti.");
                try { _guardianMutex.ReleaseMutex(); } catch { }
                try { _guardianMutex.Dispose(); } catch { }
            }
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

        // Catatan: single instance TIDAK pakai pemindaian nama proses
        // (seperti dulu) tapi named mutex, lihat Main(). Guardian memakai
        // nama proses yang sama persis dengan tray, jadi pemindaian nama
        // proses akan selalu menganggap "sudah berjalan" begitu guardian
        // hidup - tray tidak akan pernah bisa start.

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

        // Tray tidak punya Form, jadi perlu anchor UI-thread untuk marshal
        // balasan dari background thread (mis. hasil uji pembaca).
        // ------------------------------------------------------------ startup

        // Daftarkan autostart yang tahan restart. HKCU Run dipakai sebagai
        // trigger utama (tidak butuh admin), TAPI user sering mematikan
        // startup lewat Task Manager, jadi kita juga pasang Scheduled Task
        // ber-trigger logon sebagai jaring pengaman. Dua-duanya idempoten.
        internal const string RunValueName = "CykeoRfidBridge";
        internal const string TaskName = "Cykeo RFID Bridge Autostart";

        public static int InstallAutostart()
        {
            int problems = 0;
            string exe = System.Reflection.Assembly.GetExecutingAssembly().Location;

            try
            {
                var runKey = Microsoft.Win32.Registry.CurrentUser
                    .OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true);
                if (runKey == null)
                {
                    runKey = Microsoft.Win32.Registry.CurrentUser
                        .CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
                }
                if (runKey != null)
                {
                    runKey.SetValue(RunValueName, "\"" + exe + "\"");
                    runKey.Close();
                    Log("startup: HKCU Run '" + RunValueName + "' terdaftar.");
                }
            }
            catch (Exception ex)
            {
                Log("startup: gagal menulis HKCU Run: " + ex.Message);
                problems++;
            }

            try
            {
                // schtasks ONLOGON butuh admin; kalau tidak, diamkan saja
                // karena HKCU Run sudah menutupi kebutuhan auto-start.
                var psi = new ProcessStartInfo("schtasks.exe",
                    "/Create /F /SC ONLOGON /TN \"" + TaskName +
                    "\" /TR \"\\\"" + exe + "\\\"\\\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p == null) return problems;
                    string outp = p.StandardOutput.ReadToEnd();
                    string errp = p.StandardError.ReadToEnd();
                    p.WaitForExit(30000);
                    if (p.ExitCode == 0)
                        Log("startup: scheduled task logon terdaftar.");
                    else
                        Log("startup: scheduled task butuh admin, dilewati (" +
                            (errp + outp).Trim() + ")");
                }
            }
            catch (Exception ex)
            {
                Log("startup: schtasks gagal: " + ex.Message);
            }

            return problems;
        }

        // Cek + pasang ulang autostart, lalu laporkan apa adanya ke user
        // supaya jelas apakah tray akan hidup lagi setelah restart.
        private static void RepairAutostart()
        {
            int problems = InstallAutostart();
            bool runOk = IsRunKeyPresent();

            var sb = new StringBuilder();
            sb.AppendLine("Auto-start Cykeo RFID Bridge");
            sb.AppendLine("========================================");
            sb.AppendLine();
            sb.AppendLine("HKCU Run '" + RunValueName + "': " +
                         (runOk ? "TERDAFTAR" : "TIDAK ADA"));
            sb.AppendLine("Scheduled Task logon   : " +
                         (IsTaskPresent() ? "TERDAFTAR" : "tidak ada (butuh admin, opsional)"));
            sb.AppendLine();
            sb.AppendLine("Tray agent akan muncul otomatis di system tray");
            sb.AppendLine("setiap kali Windows dinyalakan.");
            sb.AppendLine();
            sb.AppendLine("Log: " + _logDir);

            ShowResult(sb.ToString(),
                problems == 0 && runOk ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        }

        private static bool IsRunKeyPresent()
        {
            try
            {
                var runKey = Microsoft.Win32.Registry.CurrentUser
                    .OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
                if (runKey == null) return false;
                object v = runKey.GetValue(RunValueName);
                runKey.Close();
                return v != null && v.ToString().Length > 0;
            }
            catch { return false; }
        }

        private static bool IsTaskPresent()
        {
            try
            {
                var psi = new ProcessStartInfo("schtasks.exe", "/Query /TN \"" + TaskName + "\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (var p = Process.Start(psi))
                {
                    if (p == null) return false;
                    p.StandardOutput.ReadToEnd();
                    p.StandardError.ReadToEnd();
                    p.WaitForExit(20000);
                    return p.ExitCode == 0;
                }
            }
            catch { return false; }
        }

        private static void BeginInvokeOnUi(Action action)
        {
            if (_uiAnchor != null && _uiAnchor.InvokeRequired)
            {
                _uiAnchor.BeginInvoke(action);
                return;
            }
            action();
        }

        // Tampilkan teks ke user. MessageBox adalah pilihan utama, tapi kalau
        // form anchor somehow rusak (mis. handle belum siap) MessageBox
        // bisa gagal diam-diam. Fallback ke log + notepad supaya user tetap
        // punya jalur untuk membaca hasilnya.
        private static void ShowResult(string message, MessageBoxIcon icon)
        {
            Log("dialog: " + message.Replace(Environment.NewLine, " | "));
            try
            {
                MessageBox.Show(message, AppName, MessageBoxButtons.OK, icon);
                return;
            }
            catch (Exception ex)
            {
                Log("MessageBox gagal: " + ex.Message);
            }

            // Terakhir: tulis ke file dialog.txt yang tinggal diklik user.
            try
            {
                var file = Path.Combine(_logDir, "dialog-terakhir.txt");
                File.WriteAllText(file, message);
                Process.Start("notepad.exe", "\"" + file + "\"");
            }
            catch { /* tidak ada lagi yang bisa dilakukan */ }
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

        // Keluar atas permintaan user: tray TIDAK boleh dihidupkan lagi oleh
        // guardian. Karena itu penanda disengaja ditulis SEBELUM proses mati.
        // Ini satu-satunya tempat penanda itu dibuat: proses yang mati
        // paksa (crash, taskkill, End task) tidak pernah sampai ke sini,
        // jadi file penanda tidak ada dan guardian akan memulihkan tray.
        private static void Quit()
        {
            Quit(true);
        }

        // disengaja=false dipakai HANYA oleh RelaunchTray: di sana proses
        // tray baru memang sedang diambil alih, jadi menandai disengaja
        // akan membuat guardian mengira user sudah mematikan tray. Itu
        // justru penyebab tray hilang setelah "Jalankan ulang tray".
        private static void Quit(bool disengaja)
        {
            if (disengaja) MarkIntentionalExit();
            _quitting = true;
            Log("tray agent keluar atas permintaan user");
            StopBridge();
            if (_tray != null)
            {
                _tray.Visible = false;
                _tray.Dispose();
            }
            try { if (_trayMutex != null) _trayMutex.ReleaseMutex(); } catch { }
            // Hapus file PID hanya kalau isinya PID kita. Kalau proses ini
            // sebenarnya bukan pemilik mutex, file itu milik tray lain yang
            // sedang hidup dan TIDAK boleh dihapus - kalau dihapus, tray lain
            // akan mengira mutex kosong lalu start dua kali.
            try
            {
                string pidFile = TrayPidPath();
                if (!string.IsNullOrEmpty(pidFile) && File.Exists(pidFile))
                {
                    if (File.ReadAllText(pidFile).Trim() ==
                        Process.GetCurrentProcess().Id.ToString())
                        File.Delete(pidFile);
                }
            }
            catch { }
            Application.Exit();
        }

        // Keluar sekaligus mematikan guardian sepenuhnya. Untuk user yang
        // memang sedang tidak memakai bridge dalam waktu lama dan tidak mau
        // ada proses pengawas yang menganggur.
        private static void QuitAndStopGuardian()
        {
            StopGuardian();
            Quit(true);
        }

        private static void StopGuardian()
        {
            try
            {
                // Nama proses guardian sama dengan tray, jadi harus dibedakan
                // lewat baris perintahnya. Kalau tidak, proses tray ini juga
                // ikut dibunuh - dan user justru kehilangan ikon.
                foreach (var p in Process.GetProcessesByName("CykeoTrayAgent"))
                {
                    try
                    {
                        if (p.Id == Process.GetCurrentProcess().Id) continue;
                        Log("guardian dihentikan (pid " + p.Id + ").");
                        p.Kill();
                    }
                    catch { }
                }
            }
            catch (Exception ex)
            {
                Log("gagal menghentikan guardian: " + ex.Message);
            }
        }

        // Jalankan ulang tray dari menu: proses tray BARU mulai dulu, tray
        // yang ini baru keluar. Urutan ini penting - kalau keluar dulu lalu
        // start, ada jendela waktu di mana tidak ada tray sama sekali dan
        // guardian bisa ikut menyalakan proses kedua.
        //
        // Mode --relay dipakai supaya proses baru tahu harus menunggu tray
        // ini melepas mutex dulu. Tanpa itu, proses baru akan mendapat
        // "sudah berjalan" lalu mati, dan hasil akhirnya tray tidak ada.
        private static void RelaunchTray()
        {
            try
            {
                string exe = Application.ExecutablePath;
                string dir = Path.GetDirectoryName(exe);

                // Buang penanda disengaja: user jelas minta tray hidup lagi.
                ClearIntentionalExit();

                var psi = new ProcessStartInfo(exe, RelayArg)
                {
                    WorkingDirectory = dir,
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                Process p = Process.Start(psi);
                Log("restart tray dari menu: proses baru pid " +
                    (p == null ? "?" : p.Id.ToString()) + " menunggu tray ini keluar.");

                // Beri waktu tray baru mengambil alih mutex sebelum yang
                // lama keluar, supaya user tidak melihat kedipan ikon.
                Thread.Sleep(1500);
                Quit(false);
            }
            catch (Exception ex)
            {
                Log("gagal menjalankan ulang tray: " + ex.Message);
                ShowResult("Gagal menjalankan ulang tray:" + Environment.NewLine +
                           ex.Message, MessageBoxIcon.Warning);
            }
        }
    }
}
