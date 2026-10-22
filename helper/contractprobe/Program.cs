// Prober kontrak SDK Cykeo untuk Linux.
//
// DLL GReaderApi.dll production adalah PE32 .NET Framework, jadi tidak
// bisa di-load langsung di Linux (tidak ada runtime .NET Framework).
// Tool ini karena itu memakai MetadataLoadContext: dia membaca METADATA
// dari DLL tanpa menjalankan kodenya, jadi kontrak field/p properti bisa
// dibuktikan di mesin build Linux.
//
// Yang dibuktikan tool ini (sumber kebenaran = DLL production):
//   - MsgBaseSetPower.DicPower      -> Dictionary<Byte,Byte>
//   - MsgBaseSetPower.IsPersistence -> Int32
//   - MsgBaseSetPower.Flag          -> Byte
//   - MsgBaseGetCapabilities.MaxPower -> Byte
//   - MsgBaseGetCapabilities.MinPower -> Byte
//   - MsgBaseGetCapabilities.AntennaCount -> ada
//
// Jalankan:
//   dotnet run --project helper/contractprobe -- <path GReaderApi.dll>

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;

internal static class ContractProbe
{
    private static readonly string[] Wanted =
    {
        "MsgBaseSetPower", "MsgBaseGetPower", "MsgBaseGetCapabilities"
    };

    private static string Sig(Type t)
    {
        if (t == null) return "?";
        if (t.IsByRef) return "ref " + Sig(t.GetElementType());
        if (t.IsArray) return Sig(t.GetElementType()) + "[]";
        if (t.IsGenericType)
        {
            var b = t.GetGenericTypeDefinition().Name;
            if (b == "Nullable`1") return Sig(t.GetGenericArguments()[0]) + "?";
            b = b.Substring(0, b.IndexOf('`'));
            return b + "<" + string.Join(",", t.GetGenericArguments().Select(Sig)) + ">";
        }
        return t.Name;
    }

    private static int Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("usage: contractprobe <GReaderApi.dll>");
            return 2;
        }
        string dll = args[0];
        if (!File.Exists(dll))
        {
            Console.Error.WriteLine("not found: " + dll);
            return 2;
        }

        var paths = new List<string> { dll };
        var runtimeDir = Path.GetDirectoryName(typeof(object).Assembly.Location);
        if (runtimeDir != null) paths.AddRange(Directory.GetFiles(runtimeDir, "*.dll"));

        using var mlc = new MetadataLoadContext(new PathAssemblyResolver(paths));
        Assembly asm;
        try { asm = mlc.LoadFromAssemblyPath(Path.GetFullPath(dll)); }
        catch (Exception ex)
        {
            Console.Error.WriteLine("load error: " + ex.Message);
            return 2;
        }

        int rc = 0;
        Console.WriteLine("dll: " + asm.GetName().Name);

        foreach (var tn in Wanted)
        {
            var t = asm.GetTypes().FirstOrDefault(x => x.Name == tn);
            if (t == null)
            {
                Console.WriteLine("MISSING TYPE: " + tn);
                rc = 1;
                continue;
            }
            Console.WriteLine("TYPE " + t.FullName);
            foreach (var p in t.GetProperties(BindingFlags.Public | BindingFlags.Instance
                                             | BindingFlags.NonPublic))
            {
                var getter = p.GetGetMethod(true);
                var setter = p.GetSetMethod(true);
                string acc = "get:" + (getter == null ? "-" : getter.Name)
                             + " set:" + (setter == null ? "-" : setter.Name);
                Console.WriteLine("  PROP " + p.Name + " : " + Sig(p.PropertyType)
                                  + "  [" + acc + "]");
            }
            foreach (var f in t.GetFields(BindingFlags.Public | BindingFlags.Instance
                                          | BindingFlags.NonPublic))
            {
                Console.WriteLine("  FIELD " + f.Name + " : " + Sig(f.FieldType));
            }
        }

        // --- assertions kontrak yang dipakai helper --------------------
        Console.WriteLine("CHECKS");
        rc |= Check(asm, "MsgBaseSetPower", "DicPower",
                    "Dictionary<Byte,Byte>");
        rc |= Check(asm, "MsgBaseSetPower", "IsPersistence", "Int32");
        rc |= Check(asm, "MsgBaseSetPower", "Flag", "Byte");
        rc |= Check(asm, "MsgBaseGetCapabilities", "MaxPower", "Byte");
        rc |= Check(asm, "MsgBaseGetCapabilities", "MinPower", "Byte");
        rc |= CheckPresent(asm, "MsgBaseGetCapabilities", "AntennaCount");
        rc |= CheckPresent(asm, "MsgBaseGetPower", "DicPower");

        Console.WriteLine(rc == 0 ? "RESULT OK" : "RESULT FAIL");
        return rc;
    }

    private static int Check(Assembly asm, string typeName, string propName,
                             string expected)
    {
        var t = asm.GetTypes().FirstOrDefault(x => x.Name == typeName);
        if (t == null)
        {
            Console.WriteLine("  FAIL " + typeName + " type missing");
            return 1;
        }
        var p = t.GetProperties(BindingFlags.Public | BindingFlags.Instance
                                | BindingFlags.NonPublic)
                .FirstOrDefault(x => x.Name == propName);
        if (p == null)
        {
            Console.WriteLine("  FAIL " + typeName + "." + propName + " missing");
            return 1;
        }
        var actual = Sig(p.PropertyType);
        bool ok = actual == expected;
        Console.WriteLine((ok ? "  OK   " : "  FAIL ") + typeName + "." + propName
                          + " = " + actual + " (expected " + expected + ")");
        return ok ? 0 : 1;
    }

    private static int CheckPresent(Assembly asm, string typeName, string propName)
    {
        var t = asm.GetTypes().FirstOrDefault(x => x.Name == typeName);
        if (t == null)
        {
            Console.WriteLine("  FAIL " + typeName + " type missing");
            return 1;
        }
        var p = t.GetProperties(BindingFlags.Public | BindingFlags.Instance
                                | BindingFlags.NonPublic)
                .FirstOrDefault(x => x.Name == propName);
        if (p == null)
        {
            Console.WriteLine("  FAIL " + typeName + "." + propName + " missing");
            return 1;
        }
        Console.WriteLine("  OK   " + typeName + "." + propName + " present : "
                          + Sig(p.PropertyType));
        return 0;
    }
}
