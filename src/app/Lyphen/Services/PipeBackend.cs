using System;
using System.Collections.Concurrent;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Lyphen.Services;

/// <summary>
/// Talks to the injected DLL over \\.\pipe\lyphen-&lt;pid&gt;, newline-delimited JSON.
///
/// Wire format, both directions, one object per line:
///   {"op":"hello","proto":1,"build":"0.1.0"}
///   {"op":"log","kind":"print"|"warn"|"error"|"info","script":7,"text":"..."}
///   {"op":"exec","id":7,"code":"print(1)"}
///   {"op":"done","id":7,"ms":12}            script finished
///   {"op":"fail","id":7,"text":"..."}       script raised
///   {"op":"ready","pid":1234,"build":"2.748.1526.0"}     handshake answer, runner is live
///   {"op":"state","state":"attached"|"detached","modules":0,"threads":0,"windows":0,"kb":8}
///   {"op":"bye"}
///
/// The socket is the source of truth for state: EOF means detached, never the UI.
/// </summary>
public sealed class PipeBackend : IBackend, IDisposable
{
    private readonly Settings _settings;
    private NamedPipeClientStream? _pipe;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private CancellationTokenSource? _pump;
    private int _nextId;
    private readonly ConcurrentDictionary<int, DateTime> _running = new();

    public PipeBackend(Settings settings) => _settings = settings;

    public InjectState State { get; private set; } = InjectState.Detached;
    public int Pid { get; private set; }
    public string Build { get; private set; } = "unknown";
    public event Action<InjectState>? StateChanged;
    public event Action<ConsoleLine>? Output;

    public async Task<bool> AttachAsync(string? pid = null)
    {
        if (State is InjectState.Attached or InjectState.Attaching) return false;
        if (!int.TryParse(pid, out var parsed) || parsed <= 0)
        {
            Set(InjectState.Failed);
            Output?.Invoke(new("give a process id", true));
            return false;
        }

        Set(InjectState.Attaching);
        Pid = parsed;
        var pipe = new NamedPipeClientStream(".", $"lyphen-{parsed}", PipeDirection.InOut,
            PipeOptions.Asynchronous);
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await pipe.ConnectAsync(timeout.Token);
        }
        catch (Exception)
        {
            pipe.Dispose();
            Set(InjectState.Failed);
            Output?.Invoke(new($"no pipe lyphen-{parsed} (is the DLL injected?)", true));
            return false;
        }

        _pipe = pipe;
        _reader = new StreamReader(pipe, new UTF8Encoding(false));
        _writer = new StreamWriter(pipe, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" };
        await Send(new { op = "hello", proto = 1, build = "0.1.0" });
        _pump = new CancellationTokenSource();
        _ = Task.Run(() => PumpAsync(_pump.Token));
        Set(InjectState.Attached);
        return true;
    }

    private async Task PumpAsync(CancellationToken token)
    {
        var reader = _reader;
        if (reader is null) return;
        try
        {
            while (!token.IsCancellationRequested)
            {
                var line = await reader.ReadLineAsync(token);
                if (line is null) break;
                Handle(line);
            }
        }
        catch (Exception ex)
        {
            Output?.Invoke(new("pump error: " + ex.GetType().Name + " " + ex.Message, true));
        }
        Output?.Invoke(new("pump ended", false));
        if (State == InjectState.Attached)
        {
            Output?.Invoke(new("pipe closed", true));
            Set(InjectState.Detached);
        }
    }

    private void Handle(string line)
    {
        JsonElement root;
        try
        {
            using var doc = JsonDocument.Parse(line);
            root = doc.RootElement.Clone();
        }
        catch (JsonException)
        {
            Output?.Invoke(new(line, true));
            return;
        }

        var op = root.TryGetProperty("op", out var opValue) ? opValue.GetString() : null;
        switch (op)
        {
            case "log":
                var kind = root.TryGetProperty("kind", out var k) ? k.GetString() : "print";
                var text = root.TryGetProperty("text", out var t) ? t.GetString() : "";
                Output?.Invoke(new(text ?? "", kind is "error" or "fail"));
                break;
            case "done":
                if (root.TryGetProperty("id", out var idDone)) _running.TryRemove(idDone.GetInt32(), out _);
                Output?.Invoke(new($"done in {root.GetProperty("ms").GetInt32()} ms"));
                break;
            case "fail":
                if (root.TryGetProperty("id", out var idFail)) _running.TryRemove(idFail.GetInt32(), out _);
                Output?.Invoke(new(root.TryGetProperty("text", out var ft) ? ft.GetString() ?? "script failed"
                    : "script failed", true));
                break;
            case "ready":
                /* the core answers this to hello: it is the only proof the runner exists */
                if (root.TryGetProperty("build", out var build)) Build = build.GetString() ?? Build;
                Output?.Invoke(new($"core up, client {Build}", false));
                break;
            case "state":
                if (root.TryGetProperty("build", out var b)) Build = b.GetString() ?? Build;
                var modules = root.TryGetProperty("modules", out var m) ? m.GetInt32() : 0;
                var threads = root.TryGetProperty("threads", out var th) ? th.GetInt32() : 0;
                var windows = root.TryGetProperty("windows", out var w) ? w.GetInt32() : 0;
                var kb = root.TryGetProperty("kb", out var kbv) ? kbv.GetInt32() : 0;
                Output?.Invoke(new($"footprint: +{modules} modules, +{threads} threads, " +
                                   $"+{windows} windows, {kb} KB private", false));
                break;
            case "bye":
                Output?.Invoke(new("host said bye"));
                break;
        }
    }

    public void Execute(string script)
    {
        if (State != InjectState.Attached)
        {
            Output?.Invoke(new("not injected", true));
            return;
        }
        var id = Interlocked.Increment(ref _nextId);
        _running[id] = DateTime.UtcNow;
        Output?.Invoke(new(script, false, true));
        _ = Send(new { op = "exec", id, code = script });
    }

    public void Detach()
    {
        _ = Send(new { op = "bye" });
        try { _pump?.Cancel(); } catch (Exception) { }
        _reader?.Dispose();
        _writer?.Dispose();
        _pipe?.Dispose();
        _pipe = null;
        Pid = 0;
        Set(InjectState.Detached);
    }

    private async Task Send(object payload)
    {
        var writer = _writer;
        if (writer is null) return;
        try
        {
            await writer.WriteLineAsync(JsonSerializer.Serialize(payload));
        }
        catch (Exception)
        {
        }
    }

    private void Set(InjectState state)
    {
        State = state;
        _settings.LastPid = Pid > 0 ? Pid.ToString() : _settings.LastPid;
        StateChanged?.Invoke(state);
    }

    public void Dispose()
    {
        try { Detach(); } catch (Exception) { }
    }
}
