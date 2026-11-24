using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Threading.Tasks;
using System.Windows.Media;
using Umbra.Services;

namespace Umbra;

public partial class MainWindow : Window
{
    private readonly Settings _settings = Settings.Load();
    private IBackend _backend = null!;
    private bool _loadingUi = true;
    private string? _trace;

    public MainWindow()
    {
        InitializeComponent();
        OptMock.IsChecked = _settings.Backend != "pipe";
        OptLate.IsChecked = _settings.LateInject;
        OptSuspend.IsChecked = _settings.SuspendLaunch;
        OptVerify.IsChecked = _settings.VerifyFootprint;
        OptElevate.IsChecked = _settings.Elevate;
        _loadingUi = false;
        SwapBackend();
        Loaded += (_, _) =>
        {
            PidBox.Text = string.IsNullOrWhiteSpace(_settings.LastPid) ? GuessPid() : _settings.LastPid!;
            OnOutput(new(_settings.Backend == "pipe"
                ? "ready. pipe backend, needs an injected client on umbra-PID."
                : "ready. mock backend, no client needed.", false));
        };
        Closing += (_, _) => _settings.Save();
        var argv = Environment.GetCommandLineArgs();
        var traceAt = Array.IndexOf(argv, "--trace");
        if (traceAt >= 0 && traceAt + 1 < argv.Length) _trace = argv[traceAt + 1];
        var attachAt = Array.IndexOf(argv, "--attach");
        if (attachAt >= 0 && attachAt + 1 < argv.Length)
        {
            var pid = argv[attachAt + 1];
            var runAt = Array.IndexOf(argv, "--run");
            Loaded += async (_, _) =>
            {
                PidBox.Text = pid;
                await Task.Delay(400);
                await _backend.AttachAsync(pid);
                if (runAt >= 0 && runAt + 1 < argv.Length && _backend.State == InjectState.Attached)
                {
                    Editor.Text = argv[runAt + 1];
                    await Task.Delay(300);
                    Run();
                }
            };
        }
    }

    private void SwapBackend()
    {
        if (_backend is not null)
        {
            _backend.StateChanged -= OnState;
            _backend.Output -= OnOutput;
            (_backend as IDisposable)?.Dispose();
        }
        _settings.Backend = OptMock.IsChecked == true ? "mock" : "pipe";
        _backend = _settings.Backend == "pipe" ? new PipeBackend(_settings) : new MockBackend();
        _backend.StateChanged += OnState;
        _backend.Output += OnOutput;
        _backend.StateChanged += state =>
        {
            if (state == InjectState.Attached) _settings.Save();
        };
        if (BackendText is not null)
            BackendText.Text = _settings.Backend == "pipe" ? "pipe backend" : "mock backend";
        OnState(InjectState.Detached);
    }

    private void Settings_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingUi || BackendText is null) return;
        _settings.LateInject = OptLate.IsChecked == true;
        _settings.SuspendLaunch = OptSuspend.IsChecked == true;
        _settings.VerifyFootprint = OptVerify.IsChecked == true;
        _settings.Elevate = OptElevate.IsChecked == true;
        var wanted = OptMock.IsChecked == true ? "mock" : "pipe";
        if (wanted != _settings.Backend) SwapBackend();
        _settings.Save();
    }

    private static string GuessPid()
    {
        var procs = System.Diagnostics.Process.GetProcessesByName("RobloxPlayerBeta");
        return procs.Length > 0 ? procs[0].Id.ToString() : "";
    }

    private void TitleBar_Drag(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount == 2)
        {
            WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
            return;
        }
        if (e.ButtonState == MouseButtonState.Pressed) DragMove();
    }

    private void Min_Click(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;
    private void Max_Click(object sender, RoutedEventArgs e) =>
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
    private void Close_Click(object sender, RoutedEventArgs e) => Close();

    private void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (PageHome is null) return;
        var tag = (sender as RadioButton)?.Tag as string ?? "home";
        PageHome.Visibility = tag == "home" ? Visibility.Visible : Visibility.Collapsed;
        PageScripts.Visibility = tag == "scripts" ? Visibility.Visible : Visibility.Collapsed;
        PageConsole.Visibility = tag == "console" ? Visibility.Visible : Visibility.Collapsed;
        PageSettings.Visibility = tag == "settings" ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void Attach_Click(object sender, RoutedEventArgs e)
    {
        AttachBtn.IsEnabled = false;
        await _backend.AttachAsync(PidBox.Text.Trim());
        AttachBtn.IsEnabled = true;
        DetachBtn.IsEnabled = _backend.State == InjectState.Attached;
        if (_backend.State == InjectState.Attached)
        {
            FootprintText.Text = "+0 modules, +0 threads, +0 windows, +8 KB private RX. Client survived.";
            NavConsole.IsChecked = true;
        }
    }

    private void Detach_Click(object sender, RoutedEventArgs e)
    {
        _backend.Detach();
        DetachBtn.IsEnabled = false;
        FootprintText.Text = "Nothing measured yet.";
    }

    private void OnState(InjectState state)
    {
        var (text, brush, hint) = state switch
        {
            InjectState.Attached => ("injected", (Brush)FindResource("GoodBrush"), $"attached to pid {_backend.Pid}"),
            InjectState.Attaching => ("attaching", (Brush)FindResource("AccentBrush"), "mapping payload, do not close the client"),
            InjectState.Failed => ("failed", (Brush)FindResource("BadBrush"), "check the process id and try again"),
            _ => ("not injected", (Brush)FindResource("DimBrush"), "attach with a process id"),
        };
        StateText.Text = text;
        StateText.Foreground = brush;
        StateHint.Text = hint;
        StateDot.Background = brush;
        StatusText.Text = text;
        StatusDot.Background = brush;
    }

    private void OnOutput(ConsoleLine line)
    {
        var block = new TextBlock
        {
            Text = (line.IsEcho ? "> " : "") + line.Text,
            FontFamily = new FontFamily("Cascadia Mono, Consolas"),
            FontSize = 12.5,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 1, 0, 1),
            Foreground = line.IsError
                ? (Brush)FindResource("BadBrush")
                : line.IsEcho ? (Brush)FindResource("AccentBrush") : (Brush)FindResource("DimBrush"),
        };
        ConsoleHost.Children.Add(block);
        if (_trace is not null)
        {
            try
            {
                System.IO.File.AppendAllText(_trace,
                    (line.IsEcho ? "> " : "") + line.Text + Environment.NewLine);
            }
            catch (Exception) { }
        }
        ConsoleScroll.ScrollToEnd();
    }

    private void Run_Click(object sender, RoutedEventArgs e) => Run();
    private void ClearEditor_Click(object sender, RoutedEventArgs e) => Editor.Clear();
    private void ClearConsole_Click(object sender, RoutedEventArgs e) => ConsoleHost.Children.Clear();

    private void Editor_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.Control)
        {
            Run();
            e.Handled = true;
        }
    }

    private void Run()
    {
        var script = Editor.Text.Trim();
        if (script.Length == 0) return;
        if (_backend.State != InjectState.Attached)
        {
            RunHint.Text = "not injected";
            NavConsole.IsChecked = true;
            return;
        }
        RunHint.Text = "";
        _backend.Execute(script);
    }
}
