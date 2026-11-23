// Shim de Sky-Claw para patchers de Synthesis (ADR 0019 §2.1/§2.4).
//
// Synthesis.Bethesda.CLI lanza cada patcher de un CliPatcherSettings por su PathToExecutable. La
// Fase 1 de Sky-Claw apunta ese PathToExecutable a una copia de este shim, con un shim.json al
// lado que generó `sky_claw/local/tools/synthesis_shim.py`. El shim:
//   1. valida shim.json contra un esquema CERRADO (claves desconocidas, faltantes o duplicadas
//      fallan),
//   2. verifica el hash del árbol completo del patcher (exe, DLLs, runtimeconfig...),
//   3. lanza el patcher directo o como `dotnet.exe exec <nombre>.dll` (patchers con CETCompat,
//      que bajo usvfs 0.5.6.1 caen si se lanzan por su apphost),
//   4. opcionalmente inyecta --InternalDataFolder/--DefaultDataFolderPath, que CliPatcherRun no
//      pasa (spike S2), y
//   5. devuelve el código de salida del patcher.
// Todo lo que no se puede establecer falla cerrado ANTES de lanzar nada.

using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

internal static class Programa
{
    // Contrato cerrado con `CLAVES_CONFIG_SHIM` de synthesis_shim.py: tests/test_synthesis_shim.py
    // exige que ambos conjuntos sean iguales.
    private static readonly string[] ClavesPermitidas =
    {
        "schema",
        "modo",
        "patcher",
        "dll",
        "dotnet",
        "arbolSha256",
        "internalDataFolder",
        "defaultDataFolderPath",
    };

    private const int EsquemaSoportado = 1;

    // Códigos de salida propios (los del patcher se devuelven tal cual).
    private const int SalidaConfigInvalida = 3;
    private const int SalidaFlagAmbiguo = 4;
    private const int SalidaLanzamientoFallido = 5;
    private const int SalidaPinNoCoincide = 6;

    private static int Fallar(int codigo, string mensaje)
    {
        Console.Error.WriteLine("[SkyClawSynthesisShim] " + mensaje);
        return codigo;
    }

    private static int Main(string[] args)
    {
        Config cfg;
        try
        {
            cfg = Config.Leer(Path.Combine(AppContext.BaseDirectory, "shim.json"));
        }
        catch (Exception e)
        {
            return Fallar(SalidaConfigInvalida, "shim.json invalido: " + e.Message);
        }

        if (cfg.InternalDataFolder is not null || cfg.DefaultDataFolderPath is not null)
        {
            foreach (var a in args)
            {
                if (EsFlagDeDatos(a))
                {
                    return Fallar(SalidaFlagAmbiguo, "el llamador ya paso un flag de datos internos; se rechaza la ambiguedad: " + a);
                }
            }
        }

        var carpetaPatcher = Path.GetDirectoryName(cfg.Patcher)!;
        string arbol;
        try
        {
            arbol = HashArbol(carpetaPatcher);
        }
        catch (Exception e)
        {
            return Fallar(SalidaPinNoCoincide, "no se pudo calcular el hash del arbol del patcher: " + e.Message);
        }
        if (!string.Equals(arbol, cfg.ArbolSha256, StringComparison.Ordinal))
        {
            return Fallar(SalidaPinNoCoincide, "el arbol del patcher no coincide con el fijado en la Fase 1");
        }

        var viaDotnet = cfg.Modo == "via_dotnet";
        var psi = new ProcessStartInfo(viaDotnet ? cfg.Dotnet! : cfg.Patcher)
        {
            UseShellExecute = false,
            WorkingDirectory = carpetaPatcher,
        };
        if (viaDotnet)
        {
            psi.ArgumentList.Add("exec");
            psi.ArgumentList.Add(cfg.Dll!);
        }
        foreach (var a in args)
        {
            psi.ArgumentList.Add(a);
        }
        // Cada flag con su carpeta, por separado, como upstream (ConstructSolutionPatcherRunArgs 0.36.6).
        if (cfg.InternalDataFolder is not null)
        {
            psi.ArgumentList.Add("-i");
            psi.ArgumentList.Add(cfg.InternalDataFolder);
        }
        if (cfg.DefaultDataFolderPath is not null)
        {
            psi.ArgumentList.Add("-f");
            psi.ArgumentList.Add(cfg.DefaultDataFolderPath);
        }

        try
        {
            using var proceso = Process.Start(psi) ?? throw new InvalidOperationException("Process.Start devolvio null");
            proceso.WaitForExit();
            return proceso.ExitCode;
        }
        catch (Exception e)
        {
            return Fallar(SalidaLanzamientoFallido, "no se pudo lanzar el patcher: " + e.GetType().Name + ": " + e.Message);
        }
    }

    // Flag de datos internos en cualquiera de sus formas. El parser de Synthesis acepta la corta
    // separada (-i valor), con el valor pegado (-ivalor) y con '=' (-i=valor): una sola rayita
    // seguida de 'i' o 'f' ya es el flag; "--..." es forma larga y se compara aparte.
    internal static bool EsFlagDeDatos(string argumento)
    {
        var l = argumento.ToLowerInvariant();
        if (l.Length >= 2 && l[0] == '-' && (l[1] == 'i' || l[1] == 'f'))
        {
            return true;
        }
        return l.StartsWith("--internaldatafolder") || l.StartsWith("--defaultdatafolderpath");
    }

    // Hash de árbol canónico, igual que `hash_arbol` de synthesis_shim.py: sha256 sobre las líneas
    // "ruta_relativa_posix\0sha256_del_archivo\n" ordenadas ordinalmente. Rechaza cualquier punto
    // de reanálisis (symlink, junction) y nombres con caracteres fuera del BMP, para los que el
    // orden UTF-16 de C# y el de code points de Python difieren.
    internal static string HashArbol(string raiz)
    {
        var archivos = new List<(string Rel, string Ruta)>();
        var pendientes = new Stack<DirectoryInfo>();
        var dirRaiz = new DirectoryInfo(raiz);
        if ((dirRaiz.Attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidDataException("la carpeta del patcher es un punto de reanalisis");
        }
        pendientes.Push(dirRaiz);
        while (pendientes.Count > 0)
        {
            foreach (var entrada in pendientes.Pop().EnumerateFileSystemInfos())
            {
                var rel = Path.GetRelativePath(raiz, entrada.FullName).Replace('\\', '/');
                if (rel.Any(char.IsSurrogate))
                {
                    throw new InvalidDataException("nombre fuera del BMP en el arbol: " + rel);
                }
                if ((entrada.Attributes & FileAttributes.ReparsePoint) != 0)
                {
                    throw new InvalidDataException("punto de reanalisis en el arbol: " + rel);
                }
                if (entrada is DirectoryInfo dir)
                {
                    pendientes.Push(dir);
                }
                else
                {
                    archivos.Add((rel, entrada.FullName));
                }
            }
        }
        archivos.Sort((a, b) => string.CompareOrdinal(a.Rel, b.Rel));
        using var acumulado = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var (rel, ruta) in archivos)
        {
            using var flujo = File.OpenRead(ruta);
            var digesto = Convert.ToHexString(SHA256.HashData(flujo)).ToLowerInvariant();
            acumulado.AppendData(Encoding.UTF8.GetBytes(rel + "\0" + digesto + "\n"));
        }
        return Convert.ToHexString(acumulado.GetHashAndReset()).ToLowerInvariant();
    }

    private sealed record Config(
        string Modo,
        string Patcher,
        string? Dll,
        string? Dotnet,
        string ArbolSha256,
        string? InternalDataFolder,
        string? DefaultDataFolderPath)
    {
        public static Config Leer(string ruta)
        {
            if (!File.Exists(ruta))
            {
                throw new InvalidDataException("falta shim.json junto al shim");
            }
            using var doc = JsonDocument.Parse(File.ReadAllText(ruta, Encoding.UTF8));
            var raiz = doc.RootElement;
            if (raiz.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException("la raiz no es un objeto");
            }
            var vistas = new HashSet<string>(StringComparer.Ordinal);
            foreach (var propiedad in raiz.EnumerateObject())
            {
                if (!ClavesPermitidas.Contains(propiedad.Name))
                {
                    throw new InvalidDataException("clave desconocida: " + propiedad.Name);
                }
                if (!vistas.Add(propiedad.Name))
                {
                    throw new InvalidDataException("clave duplicada: " + propiedad.Name);
                }
            }
            if (vistas.Count != ClavesPermitidas.Length)
            {
                throw new InvalidDataException("faltan claves: " + string.Join(", ", ClavesPermitidas.Where(c => !vistas.Contains(c))));
            }

            var esquema = raiz.GetProperty("schema");
            if (esquema.ValueKind != JsonValueKind.Number || !esquema.TryGetInt32(out var version) || version != EsquemaSoportado)
            {
                throw new InvalidDataException("schema no soportado");
            }
            var modo = Texto(raiz, "modo");
            var patcher = RutaAbsoluta(raiz, "patcher");
            var dll = RutaOpcional(raiz, "dll");
            var dotnet = RutaOpcional(raiz, "dotnet");
            var arbol = Texto(raiz, "arbolSha256");
            var internalData = RutaOpcional(raiz, "internalDataFolder");
            var defaultData = RutaOpcional(raiz, "defaultDataFolderPath");

            if (!File.Exists(patcher))
            {
                throw new InvalidDataException("patcher inexistente: " + patcher);
            }
            if (arbol.Length != 64 || !arbol.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f'))
            {
                throw new InvalidDataException("arbolSha256 no es un sha256 hexadecimal en minusculas");
            }
            switch (modo)
            {
                case "directo":
                    if (dll is not null || dotnet is not null)
                    {
                        throw new InvalidDataException("modo directo con dll o dotnet");
                    }
                    break;
                case "via_dotnet":
                    if (dll is null || dotnet is null)
                    {
                        throw new InvalidDataException("modo via_dotnet sin dll o sin dotnet");
                    }
                    var esperado = Path.Combine(Path.GetDirectoryName(patcher)!, Path.GetFileNameWithoutExtension(patcher) + ".dll");
                    if (!string.Equals(Path.GetFullPath(dll), Path.GetFullPath(esperado), StringComparison.OrdinalIgnoreCase))
                    {
                        throw new InvalidDataException("la dll no es la del patcher: " + dll);
                    }
                    if (!File.Exists(dll) || !File.Exists(dotnet))
                    {
                        throw new InvalidDataException("dll o dotnet inexistente");
                    }
                    if (!string.Equals(Path.GetFileName(dotnet), "dotnet.exe", StringComparison.OrdinalIgnoreCase))
                    {
                        throw new InvalidDataException("dotnet no apunta a dotnet.exe: " + dotnet);
                    }
                    break;
                default:
                    throw new InvalidDataException("modo desconocido: " + modo);
            }
            // Independientes, como upstream: cada una, si viene, tiene que existir.
            if (internalData is not null && !Directory.Exists(internalData))
            {
                throw new InvalidDataException("internalDataFolder inexistente: " + internalData);
            }
            if (defaultData is not null && !Directory.Exists(defaultData))
            {
                throw new InvalidDataException("defaultDataFolderPath inexistente: " + defaultData);
            }
            return new Config(modo, patcher, dll, dotnet, arbol, internalData, defaultData);
        }

        private static string Texto(JsonElement raiz, string clave)
        {
            var valor = raiz.GetProperty(clave);
            if (valor.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(valor.GetString()))
            {
                throw new InvalidDataException(clave + " debe ser un texto no vacio");
            }
            return valor.GetString()!;
        }

        private static string RutaAbsoluta(JsonElement raiz, string clave)
        {
            var ruta = Texto(raiz, clave);
            if (!Path.IsPathFullyQualified(ruta))
            {
                throw new InvalidDataException(clave + " debe ser una ruta absoluta");
            }
            return ruta;
        }

        private static string? RutaOpcional(JsonElement raiz, string clave)
        {
            return raiz.GetProperty(clave).ValueKind == JsonValueKind.Null ? null : RutaAbsoluta(raiz, clave);
        }
    }
}
