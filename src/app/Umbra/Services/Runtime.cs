using System;
using System.IO;

namespace Umbra.Services;

/// <summary>
/// Files the injected payload reads at runtime. The core loads runner.luau and api.luau from
/// %LOCALAPPDATA%\Umbra so a Luau change needs no re-injection, which means those files have to
/// be staged before an inject and kept current afterwards.
/// </summary>
public static class Runtime
{
    public static string Root { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Umbra");

    public static string Workspace => Path.Combine(Root, "workspace");

    public static string PayloadDll => Path.Combine(AppContext.BaseDirectory, "payload", "core.dll");

    /// <summary>Copies the shipped Luau and makes the folders the payload expects.</summary>
    public static void Ensure()
    {
        Directory.CreateDirectory(Root);
        Directory.CreateDirectory(Workspace);
        Directory.CreateDirectory(Path.Combine(Root, "profiles"));

        var source = Path.Combine(AppContext.BaseDirectory, "luau");
        foreach (var name in new[] { "runner.luau", "api.luau" })
        {
            var from = Path.Combine(source, name);
            if (File.Exists(from)) File.Copy(from, Path.Combine(Root, name), overwrite: true);
        }
    }

    /// <summary>True when the payload and both chunks are all present.</summary>
    public static bool Ready =>
        File.Exists(PayloadDll) &&
        File.Exists(Path.Combine(Root, "runner.luau")) &&
        File.Exists(Path.Combine(Root, "api.luau"));
}
