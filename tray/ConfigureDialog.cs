// Cykeo Bridge - wizard konfigurasi GUI NATIVE (C# / WinForms)
//
// v1.6.24: wizard konfigurasi TIDAK lagi pakai Python (tkinter).
//
// SEBELUMNYA (v1.6.23) menu tray "Konfigurasi reader (minta izin admin)"
// menjalankan `python bridge_launcher.py wizard` yang membuka form
// tkinter. Masalahnya di PC teknisi:
//   1. tkinter sering TIDAK ADA di runtime Python embeddable yang kita
//      kirim di paket, jadi wizard jatuh ke mode CLI (teks) - teknisi
//      diminta isi konfigurasi lewat terminal, bukan form.
//   2. Kalau CLI pun gagal, user tidak pernah bisa menyelesaikan wizard
//      dari menu tray sama sekali.
//   3. Tampilan tkinter terlihat seperti tool lain, bukan produk.
//   4. Butuh interpreter Python hidup untuk hal yang sebenarnya cuma
//      mengisi beberapa field dan menulis satu file JSON.
//
// SEKARANG wizard ini form WinForms murni, built into tray agent sendiri.
// Tidak butuh Python, tidak perlu tkinter, tidak perlu paket tambahan.
//
// Elevasi: config.json ditulis ke %ProgramData%\CykeoBridge\config.json
// yang butuh hak admin. Tray berjalan sebagai user biasa (tanpa admin),
// jadi form ini dijalankan sebagai PROSES ANAK yang elevated ("runas"),
// persis seperti wizard Python sebelumnya. Tray sendiri tidak elevated.
//
// Format config HARUS identik dengan yang ditulis wizard Python
// (cykeo_bridge/config.py Config.to_dict) supaya bridge Python membaca
// file yang sama tanpa perubahan. Lihat config.py di repo cykeo-bridge.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace CykeoBridge.Tray
{
    /// <summary>
    /// Form wizard konfigurasi reader. Berjalan sebagai proses anak
    /// elevated (dipicu tray lewat verb "runas").
    /// </summary>
    internal static class ConfigureDialog
    {
        // Konstanta yang HARUS sama dengan cykeo_bridge/config.py.
        // Kalau berubah di sana, ubah juga di sini - dan jangan pernah
        // hardcode ulang nilai yang berbeda.
        internal const string DefaultServerUrl = "http://192.168.1.10:8080";
        internal const string DefaultReaderCode = "CYKEO-01";
        internal const string DefaultComPort = "COM1";
        internal const int DefaultBaudrate = 115200;
        internal const string DefaultInitParam = "E5DC18EDBEEE6EAD954D7332E6D90361";
        internal const int DefaultBatchSize = 50;
        internal const double DefaultFlushInterval = 1.0;
        internal const double DefaultPollInterval = 0.25;
        internal const double DefaultConnectTimeout = 20.0;
        internal const double DefaultRequestTimeout = 10.0;
        internal const int DefaultMaxRetries = 3;
        internal const double DefaultBackoffBase = 1.0;
        internal const double DefaultBackoffMax = 60.0;
        internal const double DefaultSimulatorDelay = 0.05;
        internal const double DefaultDedupWindow = 0.0;
        internal const string ServiceName = "CykeoBridge";

        internal static readonly string[] ValidModes = { "cykeo", "zebra", "simulator" };

        // Argumen baris perintah:
        //   --config <path>     path config.json yang ditulis
        //   --install-dir <dir> folder install (untuk bridge_service.json)
        //   --python <path>    interpreter python (untuk manifest)
        //   --wizard           penanda: jalankan form wizard
        internal const string WizardArg = "--wizard";
        internal const string ConfigArg = "--config";
        internal const string InstallDirArg = "--install-dir";
        internal const string PythonArg = "--python";

        /// <summary>
        /// Entry point wizard. Dipanggil dari TrayAgent.Main ketika exe
        /// ini dijalankan dengan argumen --wizard.
        ///
        /// Sengaja TIDAK punya Main() sendiri: exe ini hanya punya SATU
        /// entry point (TrayAgent.Main). Dua Main di satu assembly = error
        /// compile CS0017, dan installer mengompilasi TrayAgent.cs lewat
        /// csc.exe sebagai file SATU, jadi file tambahan harus ikut
        /// dikompilasi juga - lebih aman menyatukan semuanya.
        /// </summary>
        internal static int RunWizard(string[] args)
        {
            string configPath = null;
            string installDir = null;
            string pythonExe = null;

            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                if (a == ConfigArg && i + 1 < args.Length)
                {
                    configPath = args[++i];
                }
                else if (a == InstallDirArg && i + 1 < args.Length)
                {
                    installDir = args[++i];
                }
                else if (a == PythonArg && i + 1 < args.Length)
                {
                    pythonExe = args[++i];
                }
            }

            if (string.IsNullOrEmpty(configPath))
                configPath = DefaultConfigPath();

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            try
            {
                using (var form = new ConfigureForm(configPath, installDir, pythonExe))
                {
                    Application.Run(form);
                    return form.Saved ? 0 : 2;
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "Wizard konfigurasi gagal:\n\n" + ex.Message,
                    "Cykeo RFID Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        /// <summary>
        /// Lokasi config.json. WAJIB sama dengan
        /// cykeo_bridge.config.default_config_path() di Windows:
        /// %ProgramData%\CykeoBridge\config.json
        /// </summary>
        internal static string DefaultConfigPath()
        {
            string programData = Environment.GetEnvironmentVariable("ProgramData");
            if (string.IsNullOrEmpty(programData))
                programData = @"C:\ProgramData";
            return Path.Combine(programData, "CykeoBridge", "config.json");
        }

        /// <summary>
        /// Lokasi manifest autostart. WAJIB sama dengan
        /// cykeo_bridge.wizard.service_manifest_path():
        /// <folder config>\bridge_service.json
        /// </summary>
        internal static string ServiceManifestPath(string configPath)
        {
            string dir = Path.GetDirectoryName(configPath);
            if (string.IsNullOrEmpty(dir))
                dir = Path.GetDirectoryName(DefaultConfigPath());
            return Path.Combine(dir ?? @"C:\ProgramData\CykeoBridge", "bridge_service.json");
        }
    }

    /// <summary>
    /// Form konfigurasi reader. Field dan validasinya meniru
    /// cykeo_bridge.wizard._run_gui() supaya user tidak kehilangan
    /// apa pun yang dulu bisa diisi.
    /// </summary>
    internal sealed class ConfigureForm : Form
    {
        private readonly string _configPath;
        private readonly string _installDir;
        private readonly string _pythonExe;

        private readonly TextBox _serverUrl = new TextBox();
        private readonly TextBox _readerCode = new TextBox();
        private readonly TextBox _apiKey = new TextBox();
        private readonly TextBox _comPort = new TextBox();
        private readonly TextBox _baudrate = new TextBox();
        private readonly TextBox _initParam = new TextBox();
        private readonly ComboBox _mode = new ComboBox();
        private readonly CheckBox _enabled = new CheckBox();
        private readonly Label _status = new Label();

        /// <summary>True kalau config berhasil disimpan.</summary>
        internal bool Saved { get; private set; }

        internal ConfigureForm(string configPath, string installDir, string pythonExe)
        {
            _configPath = configPath;
            _installDir = installDir;
            _pythonExe = pythonExe;

            BuildUi();
            LoadExisting();
        }

        // ------------------------------------------------------------------ UI

        private void BuildUi()
        {
            Text = "Konfigurasi Reader Cykeo";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(520, 430);
            AutoScaleMode = AutoScaleMode.Font;
            Font = new Font("Segoe UI", 9F);

            int y = 16;
            int labelW = 170;
            int fieldW = 320;
            const int rowH = 30;

            AddRow("Server RFID (host:port)", _serverUrl, ref y, labelW, fieldW, rowH, false);
            AddRow("Reader code", _readerCode, ref y, labelW, fieldW, rowH, false);
            AddRow("API key (dari server RFID)", _apiKey, ref y, labelW, fieldW, rowH, true);
            AddRow("Port COM", _comPort, ref y, labelW, fieldW, rowH, false);
            AddRow("Baudrate", _baudrate, ref y, labelW, fieldW, rowH, false);
            AddRow("initParam Cykeo (hex)", _initParam, ref y, labelW, fieldW, rowH, true);

            // Mode reader
            var modeLabel = new Label
            {
                Text = "Mode reader",
                AutoSize = true,
                Location = new Point(16, y + 4)
            };
            Controls.Add(modeLabel);
            _mode.DropDownStyle = ComboBoxStyle.DropDownList;
            _mode.Items.AddRange(ConfigureDialog.ValidModes);
            _mode.Location = new Point(labelW + 16, y);
            _mode.Width = fieldW;
            Controls.Add(_mode);
            y += rowH;

            _enabled.Text = "Aktifkan autostart saat login Windows";
            _enabled.AutoSize = true;
            _enabled.Location = new Point(16, y + 4);
            Controls.Add(_enabled);
            y += 32;

            _status.AutoSize = false;
            _status.Location = new Point(16, y);
            _status.Size = new Size(488, 34);
            _status.ForeColor = Color.FromArgb(90, 90, 90);
            Controls.Add(_status);
            y += 42;

            // Tombol
            var saveBtn = new Button
            {
                Text = "Simpan",
                Location = new Point(16, y),
                Size = new Size(110, 32),
                DialogResult = DialogResult.None
            };
            saveBtn.Click += OnSave;
            Controls.Add(saveBtn);

            var testBtn = new Button
            {
                Text = "Uji Pembaca",
                Location = new Point(136, y),
                Size = new Size(110, 32),
                DialogResult = DialogResult.None
            };
            testBtn.Click += OnTestReader;
            Controls.Add(testBtn);

            var cancelBtn = new Button
            {
                Text = "Batal",
                Location = new Point(256, y),
                Size = new Size(110, 32),
                DialogResult = DialogResult.Cancel
            };
            Controls.Add(cancelBtn);

            var closeBtn = new Button
            {
                Text = "Tutup",
                Location = new Point(376, y),
                Size = new Size(110, 32),
                DialogResult = DialogResult.OK
            };
            Controls.Add(closeBtn);

            AcceptButton = saveBtn;
            CancelButton = cancelBtn;
        }

        private void AddRow(string label, TextBox box, ref int y,
            int labelW, int fieldW, int rowH, bool secret)
        {
            var lbl = new Label
            {
                Text = label,
                AutoSize = true,
                Location = new Point(16, y + 4)
            };
            Controls.Add(lbl);

            box.Location = new Point(labelW + 16, y);
            box.Width = fieldW;
            // API key dan initParam disamarkan. initParam bukan license key
            // (itu parameter handshake protokol), tapi tidak perlu tampil
            // terbuka di layar yang bisa di-shoulder-surf.
            if (secret)
            {
                box.UseSystemPasswordChar = true;
            }
            Controls.Add(box);
            y += rowH;
        }

        // ------------------------------------------------------- load & simpan

        private void LoadExisting()
        {
            Dictionary<string, string> data = ReadConfig(_configPath);

            _serverUrl.Text = Get(data, "server_url", ConfigureDialog.DefaultServerUrl);
            _readerCode.Text = Get(data, "reader_code", ConfigureDialog.DefaultReaderCode);
            _apiKey.Text = Get(data, "api_key", "");
            _comPort.Text = Get(data, "com_port", ConfigureDialog.DefaultComPort);
            _baudrate.Text = Get(data, "baudrate", ConfigureDialog.DefaultBaudrate.ToString(CultureInfo.InvariantCulture));
            _initParam.Text = Get(data, "init_param", ConfigureDialog.DefaultInitParam);

            string mode = Get(data, "mode", "cykeo");
            if (_mode.Items.Contains(mode))
                _mode.SelectedItem = mode;
            else
                _mode.SelectedIndex = 0;

            _enabled.Checked = Get(data, "enabled", "true").ToLowerInvariant() == "true";

            string sdkNote = "SDK Cykeo: " + DetectSdkStatus();
            _status.Text = sdkNote;
        }

        private static string Get(Dictionary<string, string> data, string key, string fallback)
        {
            string v;
            if (data != null && data.TryGetValue(key, out v) && !string.IsNullOrEmpty(v))
                return v;
            return fallback;
        }

        private void OnSave(object sender, EventArgs e)
        {
            int baud;
            if (!int.TryParse(_baudrate.Text.Trim(), out baud))
            {
                MessageBox.Show("Baudrate harus angka.", "Konfigurasi belum valid",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            string server = _serverUrl.Text.Trim();
            if (server.Length == 0)
            {
                MessageBox.Show("Server RFID tidak boleh kosong.", "Konfigurasi belum valid",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            if (!server.StartsWith("http://", StringComparison.OrdinalIgnoreCase) &&
                !server.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                server = "http://" + server;
            }

            string apiKey = _apiKey.Text.Trim();
            if (apiKey.Length == 0)
            {
                // Ini persis aturan validate(strict=True) di config.py:
                // api_key kosong berarti bridge tidak akan pernah start.
                MessageBox.Show(
                    "API key kosong (ambil dari server RFID).\n\n" +
                    "Buka aplikasi RFID Scanner > Manage API Key, salin key-nya, " +
                    "lalu tempel di sini.",
                    "Konfigurasi belum valid",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            string initParam = _initParam.Text.Trim();
            if (initParam.Length == 0)
                initParam = ConfigureDialog.DefaultInitParam;

            if (!IsHex(initParam))
            {
                MessageBox.Show(
                    "initParam Cykeo harus heksadesimal (0-9, A-F).",
                    "Konfigurasi belum valid",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            string mode = _mode.SelectedItem as string;
            if (string.IsNullOrEmpty(mode))
                mode = "cykeo";

            string json = BuildConfigJson(
                server, _readerCode.Text.Trim(), apiKey,
                _comPort.Text.Trim(), baud, mode, initParam, _enabled.Checked);

            try
            {
                WriteConfig(_configPath, json);
                WriteServiceManifest(_configPath, _installDir, _pythonExe, _enabled.Checked);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "Gagal menyimpan konfigurasi:\n\n" + ex.Message,
                    "Cykeo RFID Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            Saved = true;
            MessageBox.Show(
                "Config ditulis ke:\n" + _configPath + "\n\n" +
                "Bridge akan memakai konfigurasi ini saat restart.",
                "Tersimpan", MessageBoxButtons.OK, MessageBoxIcon.Information);
            Close();
        }

        private void OnTestReader(object sender, EventArgs e)
        {
            // Uji Pembaca butuh bridge berhenti (port COM dipegang helper),
            // jadiujinya dijalankan lewat bridge Python seperti menu tray.
            // Kalau Python tidak ada, tetap perlu tahu itu dari sini.
            string pythonExe = _pythonExe;
            if (string.IsNullOrEmpty(pythonExe) || !File.Exists(pythonExe))
            {
                MessageBox.Show(
                    "Interpreter Python tidak ditemukan, jadi uji pembaca tidak bisa dijalankan.\n\n" +
                    "Simpan konfigurasi dulu, lalu pakai menu tray > Uji Pembaca.",
                    "Uji Pembaca",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            MessageBox.Show(
                "Jalankan uji pembaca lewat menu tray (Uji Pembaca) setelah konfigurasi disimpan.",
                "Uji Pembaca",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        // ------------------------------------------------------ config JSON IO

        /// <summary>
        /// Susun config.json.
        ///
        /// WAJIB punya key yang sama dengan Config.to_dict() di
        /// cykeo_bridge/config.py. Field yang tidak ada di dataclass Config
        /// akan dibaca bridge sebagai "extra" dan tetap diteruskan, tapi
        /// field yang ADA di dataclass WAJIB ada di sini - kalau hilang,
        /// bridge pakai default dan user bingung karena formnya terisi
        /// tapi hasilnya tidak dipakai.
        /// </summary>
        internal static string BuildConfigJson(
            string serverUrl, string readerCode, string apiKey,
            string comPort, int baudrate, string mode, string initParam,
            bool enabled)
        {
            var sb = new StringBuilder();
            sb.Append("{\n");
            sb.Append("  \"server_url\": ").Append(Quote(serverUrl)).Append(",\n");
            sb.Append("  \"reader_code\": ").Append(Quote(readerCode)).Append(",\n");
            sb.Append("  \"api_key\": ").Append(Quote(apiKey)).Append(",\n");
            sb.Append("  \"com_port\": ").Append(Quote(comPort)).Append(",\n");
            sb.Append("  \"baudrate\": ").Append(baudrate.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"mode\": ").Append(Quote(mode)).Append(",\n");
            sb.Append("  \"batch_size\": ").Append(ConfigureDialog.DefaultBatchSize).Append(",\n");
            sb.Append("  \"flush_interval\": ").Append(ConfigureDialog.DefaultFlushInterval.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"log_path\": \"\",\n");
            sb.Append("  \"queue_path\": \"\",\n");
            sb.Append("  \"init_param\": ").Append(Quote(initParam)).Append(",\n");
            sb.Append("  \"antenna\": null,\n");
            sb.Append("  \"helper_path\": \"\",\n");
            sb.Append("  \"sdk_dir\": \"\",\n");
            sb.Append("  \"poll_interval\": ").Append(ConfigureDialog.DefaultPollInterval.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"connect_timeout\": ").Append(ConfigureDialog.DefaultConnectTimeout.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"request_timeout\": ").Append(ConfigureDialog.DefaultRequestTimeout.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"max_retries\": ").Append(ConfigureDialog.DefaultMaxRetries).Append(",\n");
            sb.Append("  \"backoff_base\": ").Append(ConfigureDialog.DefaultBackoffBase.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"backoff_max\": ").Append(ConfigureDialog.DefaultBackoffMax.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"simulator_fixture\": \"\",\n");
            sb.Append("  \"simulator_loop\": true,\n");
            sb.Append("  \"simulator_delay\": ").Append(ConfigureDialog.DefaultSimulatorDelay.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"dedup_window\": ").Append(ConfigureDialog.DefaultDedupWindow.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            sb.Append("  \"enabled\": ").Append(enabled ? "true" : "false").Append("\n");
            sb.Append("}\n");
            return sb.ToString();
        }

        private static string Quote(string value)
        {
            if (value == null)
                value = "";
            var sb = new StringBuilder("\"");
            foreach (char c in value)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        private static bool IsHex(string value)
        {
            if (string.IsNullOrEmpty(value))
                return false;
            foreach (char c in value)
            {
                bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
                if (!hex)
                    return false;
            }
            return true;
        }

        /// <summary>
        /// Baca config.json yang sudah ada. Parser sengaja sederhana:
        /// cukup untuk mengambil nilai string sederhana, dan tidak pernah
        /// melempar exception - file rusak harus tetap bisa dibuka formnya
        /// supaya user bisa mengetik ulang dari nol.
        /// </summary>
        internal static Dictionary<string, string> ReadConfig(string path)
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return result;

            string text;
            try
            {
                // utf-8-sig: Notepad Windows menambah BOM, dan BOM bikin
                // json.loads() Python gagal dengan "Expecting value".
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var sr = new StreamReader(fs, new UTF8Encoding(false), true))
                {
                    text = sr.ReadToEnd();
                }
            }
            catch
            {
                return result;
            }

            if (text.Length > 0 && text[0] == '\uFEFF')
                text = text.Substring(1);

            // Ambil pasangan "kunci": "nilai" dalam satu baris. Bridge
            // menulis JSON indent=2, jadi satu nilai satu baris.
            string[] lines = text.Replace("\r\n", "\n").Split('\n');
            foreach (string raw in lines)
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] != '"')
                    continue;
                int colon = line.IndexOf(':');
                if (colon < 0)
                    continue;
                string key = line.Substring(1, line.IndexOf('"', 1) - 1);
                if (key.Length == 0)
                    continue;
                string rest = line.Substring(colon + 1).Trim();
                if (rest.Length == 0)
                    continue;
                if (rest[0] == '"')
                {
                    int endQuote = rest.IndexOf('"', 1);
                    if (endQuote < 0)
                        continue;
                    string value = rest.Substring(1, endQuote - 1);
                    result[key] = Unescape(value);
                }
                else
                {
                    // angka, true/false, null -> apa adanya
                    result[key] = rest.TrimEnd(',');
                }
            }
            return result;
        }

        private static string Unescape(string value)
        {
            if (value.IndexOf('\\') < 0)
                return value;
            var sb = new StringBuilder(value.Length);
            for (int i = 0; i < value.Length; i++)
            {
                if (value[i] == '\\' && i + 1 < value.Length)
                {
                    char c = value[++i];
                    switch (c)
                    {
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case '"': sb.Append('"'); break;
                        case '\\': sb.Append('\\'); break;
                        case '/': sb.Append('/'); break;
                        case 'u':
                            if (i + 4 < value.Length)
                            {
                                int code;
                                if (int.TryParse(value.Substring(i + 1, 4), NumberStyles.HexNumber,
                                        CultureInfo.InvariantCulture, out code))
                                {
                                    sb.Append((char)code);
                                    i += 4;
                                    break;
                                }
                            }
                            sb.Append(c);
                            break;
                        default: sb.Append(c); break;
                    }
                }
                else
                {
                    sb.Append(value[i]);
                }
            }
            return sb.ToString();
        }

        private static void WriteConfig(string path, string json)
        {
            string dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);

            // Tulis ke file sementara lalu atomic rename, supaya bridge
            // tidak pernah membaca config setengah tertulis (JSON rusak ->
            // bridge restart terus-menerus).
            string tmp = path + ".tmp";
            // UTF8 tanpa BOM: Python json.loads() gagal kalau ada BOM.
            File.WriteAllText(tmp, json, new UTF8Encoding(false));
            if (File.Exists(path))
                File.Delete(path);
            File.Move(tmp, path);
        }

        private static void WriteServiceManifest(string configPath, string installDir,
            string pythonExe, bool enabled)
        {
            string workDir = installDir;
            if (string.IsNullOrEmpty(workDir))
                workDir = Path.GetDirectoryName(Path.GetDirectoryName(configPath));

            var sb = new StringBuilder();
            sb.Append("{\n");
            sb.Append("  \"service_name\": ").Append(Quote(ConfigureDialog.ServiceName)).Append(",\n");
            sb.Append("  \"enabled\": ").Append(enabled ? "true" : "false").Append(",\n");
            sb.Append("  \"install_dir\": ").Append(Quote(workDir ?? "")).Append(",\n");
            sb.Append("  \"config_path\": ").Append(Quote(configPath)).Append(",\n");
            sb.Append("  \"executable\": ").Append(Quote(pythonExe ?? "")).Append(",\n");
            sb.Append("  \"args\": [\n");
            sb.Append("    \"-m\",\n");
            sb.Append("    \"cykeo_bridge\",\n");
            sb.Append("    \"run\",\n");
            sb.Append("    \"--config\",\n");
            sb.Append("    ").Append(Quote(configPath)).Append("\n");
            sb.Append("  ],\n");
            sb.Append("  \"trigger\": \"logon\",\n");
            sb.Append("  \"working_directory\": ").Append(Quote(workDir ?? "")).Append(",\n");
            sb.Append("  \"stop_on_reboot\": false\n");
            sb.Append("}\n");

            string manifestPath = ConfigureDialog.ServiceManifestPath(configPath);
            string dir = Path.GetDirectoryName(manifestPath);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);
            File.WriteAllText(manifestPath, sb.ToString(), new UTF8Encoding(false));
        }

        private static string DetectSdkStatus()
        {
            // Deteksi ringan: GReaderApi.dll ada di folder install berarti
            // SDK ada. Pesan singkat saja - detail lengkap ada di log.
            try
            {
                string installDir = AppDomain.CurrentDomain.BaseDirectory;
                string dll = Path.Combine(installDir, "GReaderApi.dll");
                if (File.Exists(dll))
                    return "GReaderApi.dll ditemukan";
                return "GReaderApi.dll tidak ada di folder aplikasi";
            }
            catch
            {
                return "tidak dapat diperiksa";
            }
        }
    }
}
