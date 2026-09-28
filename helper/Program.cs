// Helper .NET untuk reader Cykeo CK-D5.
//
// Meniru alur FlexiAudit.Infrastructure.RfidReader.CykeoReader dari
// aplikasi production SML FlexiAudit:
//
//   1. new GClient()
//   2. client.OpenSerial("<COM>:<baud>", timeout, out status)
//   3. client.OnEncapedTagEpcLog += handler   (setelah Connect sukses)
//   4. SendSynMsg(new MsgBaseGetCapabilities())  -> AntennaCount
//   5. SendSynMsg(new MsgBaseInventoryEpc { AntennaEnable, InventoryMode = 1,
//                                          ReadTid = { Mode = 0, Len = 6 } })
//   6. handler: kalau logBaseEpcInfo.Result == 0 -> kirim Epc, Rssi, AntId
//   7. SendSynMsg(new MsgBaseStop())
//
// Bridge Python mengatur helper lewat stdio JSON (satu request satu baris).
// Helper tidak pernah menulis ke database; hanya forwards ke Python.

using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Ports;
using System.Linq;
using System.Text;

using GDotnet.Reader.Api.DAL;
using GDotnet.Reader.Api.Protocol.Gx;

using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace CykeoBridge.Helper
{

internal static class Program
{
    private const string SdkDirDefault = @"";

    private static GClient _client;
    private static bool _connected;
    private static bool _reading;
    private static int _antennaCount = 1;
    private static StreamWriter _out;
    private static readonly object WriteLock = new();
    private static int _seq;

    private static int Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        _out = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false))
        {
            AutoFlush = true
        };

        string sdkDir = ResolveSdkDir(args);
        if (!string.IsNullOrWhiteSpace(sdkDir))
        {
            try
            {
                Directory.SetCurrentDirectory(sdkDir);
            }
            catch (Exception ex)
            {
                Emit(new JObject
                {
                    ["type"] = "error",
                    ["code"] = "sdk_dir_failed",
                    ["message"] = ex.Message
                });
                return 2;
            }
        }

        Emit(new JObject
        {
            ["type"] = "hello",
            ["helper"] = "cykeo-helper",
            ["version"] = "1.0.0",
            ["sdkDir"] = sdkDir ?? Directory.GetCurrentDirectory()
        });

        string line;
        while ((line = Console.In.ReadLine()) != null)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            JObject req;
            try
            {
                req = JObject.Parse(line);
            }
            catch (Exception ex)
            {
                ReplyError(0, "bad_json", ex.Message);
                continue;
            }

            if (req is null)
            {
                ReplyError(0, "bad_json", "bukan objek JSON");
                continue;
            }

            int id = ReadInt(req, "id", 0);
            string cmd = ReadString(req, "cmd");

            try
            {
                Dispatch(id, cmd, req);
            }
            catch (Exception ex)
            {
                ReplyError(id, "exception", $"{ex.GetType().Name}: {ex.Message}");
            }
        }

        SafeStop();
        SafeClose();
        return 0;
    }

    private static void Dispatch(int id, string cmd, JObject req)
    {
        switch ((cmd ?? string.Empty).Trim().ToLowerInvariant())
        {
            case "detect_ports":
                HandleDetectPorts(id);
                break;

            case "connect":
                HandleConnect(id, req);
                break;

            case "start":
                HandleStart(id, req);
                break;

            case "stop":
                SafeStop();
                ReplyOk(id, new JObject { ["reading"] = _reading });
                break;

            case "disconnect":
                SafeStop();
                SafeClose();
                ReplyOk(id, new JObject { ["connected"] = _connected });
                break;

            case "status":
                ReplyOk(id, new JObject
                {
                    ["connected"] = _connected,
                    ["reading"] = _reading,
                    ["antennaCount"] = _antennaCount
                });
                break;

            case "ping":
                ReplyOk(id, new JObject { ["pong"] = true });
                break;

            case "shutdown":
                SafeStop();
                SafeClose();
                ReplyOk(id, new JObject { ["bye"] = true });
                Environment.Exit(0);
                break;

            default:
                ReplyError(id, "unknown_cmd", $"perintah tidak dikenal: {cmd}");
                break;
        }
    }

    // ---- detect_ports ----------------------------------------------------

    private static void HandleDetectPorts(int id)
    {
        var ports = new JArray();
        try
        {
            foreach (string name in SerialPort.GetPortNames().OrderBy(x => x))
            {
                ports.Add(new JObject
                {
                    ["port"] = name
                });
            }
        }
        catch (Exception ex)
        {
            ReplyError(id, "detect_failed", ex.Message);
            return;
        }

        ReplyOk(id, new JObject { ["ports"] = ports });
    }

    // ---- connect ---------------------------------------------------------

    private static void HandleConnect(int id, JObject req)
    {
        string com = ReadString(req, "com_port");
        if (string.IsNullOrWhiteSpace(com))
        {
            ReplyError(id, "bad_request", "com_port wajib diisi");
            return;
        }

        int baud = ReadInt(req, "baud", 115200);
        if (baud <= 0)
        {
            ReplyError(id, "bad_request", "baud harus positif");
            return;
        }

        int timeout = ReadInt(req, "timeout", 60);
        if (timeout <= 0)
        {
            timeout = 60;
        }

        if (_connected)
        {
            ReplyOk(id, new JObject
            {
                ["connected"] = true,
                ["already"] = true
            });
            return;
        }

        try
        {
            _client = new GClient();

            // Production: OpenSerial(ComPort + ":115200", 60, out status)
            string connString = com + ":" + baud;
            if (!_client.OpenSerial(connString, timeout, out _))
            {
                ReplyError(id, "open_failed", $"gagal membuka {connString}");
                _client = null;
                return;
            }

            // Callback didaftarkan setelah OpenSerial sukses (meniru production)
            _client.OnEncapedTagEpcLog += OnEncapedTagEpcLog;

            _connected = true;
            _antennaCount = ProbeAntennaCount();

            ReplyOk(id, new JObject
            {
                ["connected"] = true,
                ["connectionString"] = connString,
                ["antennaCount"] = _antennaCount
            });
        }
        catch (Exception ex)
        {
            _connected = false;
            _client = null;
            ReplyError(id, "connect_exception", $"{ex.GetType().Name}: {ex.Message}");
        }
    }

    // ---- start -----------------------------------------------------------

    private static void HandleStart(int id, JObject req)
    {
        if (!_connected && _client is null)
        {
            ReplyError(id, "not_connected", "helper belum terhubung; jalankan connect dulu");
            return;
        }

        int want = ReadInt(req, "antenna_count", 0);
        if (want > 0)
        {
            _antennaCount = want;
        }
        else
        {
            _antennaCount = Math.Max(1, _antennaCount);
        }

        int mode = ReadInt(req, "inventory_mode", 1);
        int tidLen = ReadInt(req, "tid_len", 6);
        int tidMode = ReadInt(req, "tid_mode", 0);

        try
        {
            // Production: GetTransmitPowerValues() via MsgBaseGetCapabilities
            int detected = ProbeAntennaCount();
            if (detected > 0)
            {
                _antennaCount = detected;
            }

            uint antennaEnable = GenerateAntenna(_antennaCount);

            var msg = new MsgBaseInventoryEpc
            {
                AntennaEnable = antennaEnable,
                InventoryMode = (byte)mode,
                ReadTid = new ParamEpcReadTid
                {
                    Mode = (byte)tidMode,
                    Len = (byte)tidLen
                }
            };

            _client.SendSynMsg(msg);
            _reading = true;

            ReplyOk(id, new JObject
            {
                ["reading"] = true,
                ["antennaCount"] = _antennaCount,
                ["antennaEnable"] = antennaEnable,
                ["inventoryMode"] = mode
            });
        }
        catch (Exception ex)
        {
            _reading = false;
            ReplyError(id, "start_exception", $"{ex.GetType().Name}: {ex.Message}");
        }
    }

    // ---- stop / close ----------------------------------------------------

    private static void SafeStop()
    {
        if (!_reading || _client is null)
        {
            _reading = false;
            return;
        }

        try
        {
            _client.SendSynMsg(new MsgBaseStop());
        }
        catch (Exception ex)
        {
            Emit(new JObject
            {
                ["type"] = "log",
                ["level"] = "warn",
                ["message"] = "gagal stop inventory: " + ex.Message
            });
        }
        finally
        {
            _reading = false;
        }
    }

    private static void SafeClose()
    {
        if (_client is null)
        {
            _connected = false;
            return;
        }

        try
        {
            if (_connected)
            {
                _client.OnEncapedTagEpcLog -= OnEncapedTagEpcLog;
                _client.Close();
            }
        }
        catch (Exception ex)
        {
            Emit(new JObject
            {
                ["type"] = "log",
                ["level"] = "warn",
                ["message"] = "gagal close: " + ex.Message
            });
        }
        finally
        {
            _connected = false;
            _client = null;
        }
    }

    // ---- tag callback ----------------------------------------------------

    private static void OnEncapedTagEpcLog(EncapedLogBaseEpcInfo msg)
    {
        if (msg?.logBaseEpcInfo is null)
        {
            return;
        }

        try
        {
            // Production: hanya Result == 0 yang diproses
            if (msg.logBaseEpcInfo.Result != 0)
            {
                return;
            }

            string epc = msg.logBaseEpcInfo.Epc;
            if (string.IsNullOrWhiteSpace(epc))
            {
                return;
            }

            Emit(new JObject
            {
                ["type"] = "tag",
                ["epc"] = epc,
                ["rssi"] = msg.logBaseEpcInfo.Rssi,
                ["antId"] = msg.logBaseEpcInfo.AntId,
                ["tid"] = SafeTid(msg.logBaseEpcInfo)
            });
        }
        catch (Exception ex)
        {
            Emit(new JObject
            {
                ["type"] = "log",
                ["level"] = "error",
                ["message"] = "callback tag gagal: " + ex.Message
            });
        }
    }

    private static string SafeTid(LogBaseEpcInfo info)
    {
        try
        {
            return info.Tid ?? string.Empty;
        }
        catch
        {
            return string.Empty;
        }
    }

    // ---- helpers ---------------------------------------------------------

    private static int ProbeAntennaCount()
    {
        if (_client is null)
        {
            return _antennaCount;
        }

        try
        {
            var caps = new MsgBaseGetCapabilities();
            _client.SendSynMsg(caps);
            int n = caps.AntennaCount;
            return n > 0 ? n : _antennaCount;
        }
        catch (Exception ex)
        {
            Emit(new JObject
            {
                ["type"] = "log",
                ["level"] = "warn",
                ["message"] = "get capabilities gagal: " + ex.Message
            });
            return _antennaCount;
        }
    }

    private static uint GenerateAntenna(int n)
    {
        uint mask = 0u;
        for (int i = 0; i < n; i++)
        {
            mask |= (uint)(1 << i);
        }
        return mask;
    }

    private static string ResolveSdkDir(string[] args)
    {
        // 1. argumen pertama
        if (args.Length > 0 && !string.IsNullOrWhiteSpace(args[0]) && Directory.Exists(args[0]))
        {
            return args[0];
        }

        // 2. environment variable
        string env = Environment.GetEnvironmentVariable("CYKEO_SDK_DIR");
        if (!string.IsNullOrWhiteSpace(env) && Directory.Exists(env))
        {
            return env;
        }

        // 3. folder tempat helper berada (sudah disalin SDK di sana)
        string here = AppContext.BaseDirectory;
        if (Directory.Exists(Path.Combine(here, "GReaderApi.dll")))
        {
            return here;
        }

        // 4. lokasi default instalasi vendor
        string guess = SdkDirDefault;
        if (!string.IsNullOrWhiteSpace(guess) && Directory.Exists(guess))
        {
            return guess;
        }

        return null;
    }

    private static string ReadString(JObject obj, string key, string fallback = null)
    {
        JToken node = obj[key];
        if (node != null && node.Type != JTokenType.Null)
        {
            return (string)node;
        }
        return fallback;
    }

    private static int ReadInt(JObject obj, string key, int fallback)
    {
        JToken node = obj[key];
        if (node != null && node.Type != JTokenType.Null)
        {
            try
            {
                return node.Value<int>();
            }
            catch
            {
                return fallback;
            }
        }
        return fallback;
    }

    private static void ReplyOk(int id, JObject data)
    {
        var msg = new JObject
        {
            ["type"] = "reply",
            ["id"] = id,
            ["ok"] = true
        };
        if (data is not null)
        {
            foreach (var kv in data)
            {
                msg[kv.Key] = kv.Value?.DeepClone();
            }
        }
        Emit(msg);
    }

    private static void ReplyError(int id, string code, string message)
    {
        Emit(new JObject
        {
            ["type"] = "reply",
            ["id"] = id,
            ["ok"] = false,
            ["code"] = code,
            ["message"] = message
        });
    }

    private static void Emit(JObject obj)
    {
        lock (WriteLock)
        {
            _out.WriteLine(obj.ToString(Formatting.None));
            _out.Flush();
        }
    }
}

}
