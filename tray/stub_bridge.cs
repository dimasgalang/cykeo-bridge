// Stub cykeo_bridge.exe - hanya untuk menguji supervision tray agent.
// Berhenti sendiri setelah N detik supaya bisa diuji proses kill + restart.
using System;
using System.Threading;

class Stub
{
    static int Main(string[] args)
    {
        int life = 20;
        foreach (var a in args)
        {
            if (a.StartsWith("--life="))
                int.TryParse(a.Substring(7), out life);
        }

        // Env dipakai tray agent? Tidak - tray agent selalu memanggil "run".
        // Jadi life diambil dari env yang diwarisi agar tes bisa mengatur umur.
        string envLife = Environment.GetEnvironmentVariable("CYKEO_STUB_LIFE");
        if (!string.IsNullOrEmpty(envLife))
            int.TryParse(envLife, out life);
        Console.WriteLine("stub bridge start, hidup " + life + " detik");
        Console.Out.Flush();
        Thread.Sleep(life * 1000);
        Console.WriteLine("stub bridge keluar normal");
        return 0;
    }
}
