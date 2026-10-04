$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$mutex = New-Object System.Threading.Mutex($false, 'Local\TraceFangApplication')
$owned = $false
try { $owned = $mutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) {
    [System.Windows.MessageBox]::Show('TraceFang is already open. Use its existing window.', 'TraceFang') > $null
    $mutex.Dispose()
    exit 0
}
[xml]$layout = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
 Title="TraceFang" Width="510" Height="285" ResizeMode="CanMinimize" WindowStartupLocation="CenterScreen">
 <StackPanel Margin="28">
  <TextBlock Text="TraceFang" FontSize="29" FontWeight="SemiBold" Foreground="#172B4D" Margin="0,0,0,20"/>
  <TextBlock Name="Status" Text="Starting project services..." FontSize="14" Margin="0,0,0,16"/>
  <TextBlock Text="Closing this window stops acquisition and project services.&#10;Saved data is retained. The browser only displays the interface."
   TextWrapping="Wrap" Foreground="#667085" Margin="0,0,0,20"/>
  <StackPanel Orientation="Horizontal">
   <Button Name="Open" Content="Open interface" IsEnabled="False" Padding="14,6" Margin="0,0,12,0"/>
   <Button Name="Stop" Content="Stop and exit" Padding="14,6"/>
  </StackPanel>
 </StackPanel>
</Window>
'@
$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $layout))
$status = $window.FindName('Status')
$open = $window.FindName('Open')
$stop = $window.FindName('Stop')
$script:worker = $null
$script:line = $null
$script:closing = $false
$script:canClose = $false

function Start-Worker([string]$command) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = "-u -m tracefang.service $command"
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.EnvironmentVariables['PYTHONPATH'] = Join-Path $root 'src'
    $script:worker = [System.Diagnostics.Process]::Start($info)
    $script:errors = $script:worker.StandardError.ReadToEndAsync()
    $script:line = $script:worker.StandardOutput.ReadLineAsync()
    if ($command -ne 'session') { $script:worker.StandardInput.Close() }
}
function Request-Stop {
    if ($script:closing) { return }
    $script:closing = $true
    $status.Text = 'Stopping services. Please wait...'
    $open.IsEnabled = $false
    $stop.IsEnabled = $false
    if ($null -ne $script:worker -and -not $script:worker.HasExited) {
        $script:worker.StandardInput.Close()
    } else {
        try { Start-Worker 'stop-app' }
        catch {
            $script:closing = $false
            $status.Text = 'Cannot stop services. Check the project installation.'
            $stop.IsEnabled = $true
        }
    }
}
$open.Add_Click({ Start-Process 'http://127.0.0.1:8000' })
$stop.Add_Click({ if ($script:canClose) { $window.Close() } else { Request-Stop } })
$window.Add_Closing({
    param($sender, $eventArgs)
    if (-not $script:canClose) { $eventArgs.Cancel = $true; Request-Stop }
})
$timer = New-Object Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(250)
$timer.Add_Tick({
    if ($null -eq $script:worker) { return }
    while ($null -ne $script:line -and $script:line.IsCompleted) {
        $text = $script:line.Result
        if ($null -eq $text) { $script:line = $null; break }
        if ($text -eq 'TRACEFANG_APP_READY' -and -not $script:closing) {
            $status.Text = 'Project services are running'
            $open.IsEnabled = $true
            Start-Process 'http://127.0.0.1:8000'
        }
        $script:line = $script:worker.StandardOutput.ReadLineAsync()
    }
    if ($script:worker.HasExited) {
        $code = $script:worker.ExitCode
        $script:worker.Dispose()
        $script:worker = $null
        if ($code -eq 3) {
            $script:canClose = $true
            $script:closing = $false
            $open.IsEnabled = $false
            $status.Text = 'Another application owns the services. Use its existing window.'
            $stop.Content = 'Close this window'
            $stop.IsEnabled = $true
        } elseif ($script:closing -and $code -eq 0) {
            $script:canClose = $true
            $window.Close()
        } else {
            $script:closing = $false
            $open.IsEnabled = $false
            $status.Text = 'Startup or shutdown failed. Check Docker and the project configuration.'
            $stop.Content = 'Retry stop and exit'
            $stop.IsEnabled = $true
        }
    }
})
try {
    try { Start-Worker 'session' }
    catch {
        $script:canClose = $true
        $status.Text = 'Run setup.cmd before opening TraceFang.'
        $stop.Content = 'Close'
    }
    $timer.Start()
    $window.ShowDialog() > $null
} finally {
    $timer.Stop()
    if ($null -ne $script:worker -and -not $script:worker.HasExited) {
        $script:worker.StandardInput.Close()
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
