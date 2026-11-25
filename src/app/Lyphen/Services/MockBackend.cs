using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace Lyphen.Services;

public enum InjectState { Detached, Attaching, Attached, Failed }

public sealed record ConsoleLine(string Text, bool IsError = false, bool IsEcho = false);

/// <summary>
/// Everything the window needs from a client. The mock lets the whole panel be
/// built and demoed with no Roblox running; the pipe backend implements the
/// same surface once M2 lands.
/// </summary>
public interface IBackend
{
    InjectState State { get; }
    int Pid { get; }
    string Build { get; }
    event Action<InjectState>? StateChanged;
    event Action<ConsoleLine>? Output;

    Task<bool> AttachAsync(string? pid = null);
    void Detach();
    void Execute(string script);
}

public sealed class MockBackend : IBackend
{
    private readonly Random _rng = new();
    private readonly List<string> _fakeScripts = new()
    {
        "print(\"hello from \" .. game.PlaceId)",
        "game.Players.LocalPlayer.Character.Humanoid.Health = 100",
        "for _, p in ipairs(game.Players:GetPlayers()) do print(p.Name) end",
    };

    public InjectState State { get; private set; } = InjectState.Detached;
    public int Pid { get; private set; }
    public string Build => "0.1.0-dev (mock)";
    public event Action<InjectState>? StateChanged;
    public event Action<ConsoleLine>? Output;

    public async Task<bool> AttachAsync(string? pid = null)
    {
        if (State is InjectState.Attached or InjectState.Attaching) return false;
        Set(InjectState.Attaching);
        Output?.Invoke(new("locating RobloxPlayerBeta.exe", false));
        await Task.Delay(700);
        if (!int.TryParse(pid, out var parsed) || parsed <= 0)
        {
            Set(InjectState.Failed);
            Output?.Invoke(new("client not found", true));
            return false;
        }
        Pid = parsed;
        Output?.Invoke(new($"pid {Pid}: mapping 24 KB payload"));
        await Task.Delay(450);
        Output?.Invoke(new("payload mapped, entry returned, client stable"));
        Output?.Invoke(new("0 modules, 0 threads, 0 windows added"));
        Set(InjectState.Attached);
        return true;
    }

    public void Detach()
    {
        Pid = 0;
        Set(InjectState.Detached);
        Output?.Invoke(new("detached"));
    }

    public void Execute(string script)
    {
        if (State != InjectState.Attached)
        {
            Output?.Invoke(new("not injected", true));
            return;
        }
        Output?.Invoke(new(script, false, true));
        var delay = _rng.Next(80, 300);
        Task.Delay(delay).ContinueWith(_ =>
        {
            if (_rng.Next(6) == 0)
                Output?.Invoke(new("script error: attempt to index nil with 'Character'", true));
            else
                Output?.Invoke(new($"done in {delay} ms"));
        });
    }

    private void Set(InjectState state)
    {
        State = state;
        StateChanged?.Invoke(state);
    }
}
