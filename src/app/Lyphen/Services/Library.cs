using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace Lyphen.Services;

public sealed class LibraryScript
{
    public string Name { get; init; } = "";
    public string Description { get; init; } = "";
    public string Source { get; init; } = "";
}

/// <summary>
/// The scripts that ship with the build. Same files the in-game panel carries, so a user
/// with the executor gets a one-click list instead of a blank editor. Read from the
/// install's library folder, then from the repo while developing.
/// </summary>
public static class Library
{
    private static IEnumerable<string> Candidates()
    {
        yield return Path.Combine(AppContext.BaseDirectory, "library");
        yield return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Lyphen", "library");
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var depth = 0; depth < 6 && dir is not null; depth++, dir = dir.Parent)
        {
            var guess = Path.Combine(dir.FullName, "src", "luau", "library");
            if (Directory.Exists(guess)) yield return guess;
        }
    }

    public static List<LibraryScript> Load()
    {
        foreach (var dir in Candidates())
        {
            try
            {
                if (!Directory.Exists(dir)) continue;
                var files = Directory.GetFiles(dir, "*.luau").OrderBy(f => f).ToList();
                if (files.Count == 0) continue;
                var items = new List<LibraryScript>();
                foreach (var file in files)
                {
                    var lines = File.ReadAllLines(file);
                    var name = lines.FirstOrDefault(l => l.StartsWith("-- name:"))?[7..].Trim()
                               ?? Path.GetFileNameWithoutExtension(file);
                    var description = lines.FirstOrDefault(l => l.StartsWith("-- desc:"))?[7..].Trim() ?? "";
                    var source = string.Join(Environment.NewLine, lines
                        .SkipWhile(l => l.StartsWith("--") || l.Trim().Length == 0));
                    items.Add(new LibraryScript { Name = name, Description = description, Source = source });
                }
                return items;
            }
            catch (Exception)
            {
            }
        }
        return new List<LibraryScript>();
    }
}
