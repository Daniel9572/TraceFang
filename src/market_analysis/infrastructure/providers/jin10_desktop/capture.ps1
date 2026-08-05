param(
    [ValidateSet("XAUUSD", "XAGUSD")]
    [string]$Symbol,
    [switch]$ProbeOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Result {
    param([hashtable]$Value, [int]$ExitCode = 0)
    $Value | ConvertTo-Json -Compress -Depth 5
    exit $ExitCode
}

try {
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class MarketWindowApi {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr destination, uint flags);

    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
}
"@

    $processes = @(Get-Process -Name "JinShiShuJu" -ErrorAction SilentlyContinue)
    if ($processes.Count -eq 0) {
        Write-Result @{
            success = $false
            available = $false
            state = "process_not_running"
            error = "Jin10 desktop process is not running"
            checked_at = [DateTime]::UtcNow.ToString("o")
        } 2
    }

    $processIds = @($processes | ForEach-Object { [uint32]$_.Id })
    $windows = [System.Collections.Generic.List[object]]::new()
    [MarketWindowApi]::EnumWindows({
        param([IntPtr]$Handle, [IntPtr]$State)
        $processId = [uint32]0
        [MarketWindowApi]::GetWindowThreadProcessId($Handle, [ref]$processId) | Out-Null
        if ($processIds -contains $processId) {
            $className = New-Object System.Text.StringBuilder 256
            [MarketWindowApi]::GetClassName($Handle, $className, 256) | Out-Null
            if ($className.ToString() -eq "FLUTTER_RUNNER_WIN32_WINDOW") {
                $windows.Add([pscustomobject]@{
                    Handle = $Handle
                    Visible = [MarketWindowApi]::IsWindowVisible($Handle)
                    Minimized = [MarketWindowApi]::IsIconic($Handle)
                })
            }
        }
        return $true
    }, [IntPtr]::Zero) | Out-Null

    if ($windows.Count -eq 0) {
        Write-Result @{
            success = $false
            available = $false
            state = "market_window_not_found"
            error = "Jin10 market window was not found; open the market page"
            checked_at = [DateTime]::UtcNow.ToString("o")
        } 2
    }

    $window = $windows | Where-Object { $_.Visible } | Select-Object -First 1
    if ($null -eq $window) {
        $window = $windows | Select-Object -First 1
    }
    if ($window.Minimized) {
        Write-Result @{
            success = $false
            available = $false
            state = "window_minimized"
            error = "Jin10 market window is minimized; restore it before capture"
            checked_at = [DateTime]::UtcNow.ToString("o")
        } 2
    }

    if ($ProbeOnly) {
        Write-Result @{
            success = $true
            available = $true
            state = "ready"
            error = $null
            checked_at = [DateTime]::UtcNow.ToString("o")
        }
    }

    if ([string]::IsNullOrWhiteSpace($Symbol)) {
        throw "Symbol is required"
    }

    $rect = New-Object MarketWindowApi+RECT
    if (-not [MarketWindowApi]::GetWindowRect($window.Handle, [ref]$rect)) {
        throw "Cannot read the Jin10 market window dimensions"
    }
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -lt 1000 -or $height -lt 700) {
        throw "Jin10 market window is too small; restore or maximize it"
    }

    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $deviceContext = $graphics.GetHdc()
    try {
        $captured = [MarketWindowApi]::PrintWindow($window.Handle, $deviceContext, 2)
    }
    finally {
        $graphics.ReleaseHdc($deviceContext)
        $graphics.Dispose()
    }
    if (-not $captured) {
        $bitmap.Dispose()
        throw "Jin10 market window capture failed"
    }
    if (-not [string]::IsNullOrWhiteSpace($env:MARKET_ANALYSIS_DESKTOP_FULL_DEBUG)) {
        $bitmap.Save(
            $env:MARKET_ANALYSIS_DESKTOP_FULL_DEBUG,
            [System.Drawing.Imaging.ImageFormat]::Png
        )
    }

    # Flutter keeps the market-watch column at fixed logical coordinates. Windows
    # PowerShell 5.1 is DPI-virtualized, so proportional crops drift into the name
    # column on 150% displays; these logical-pixel regions remain stable.
    $sourceX = 490
    $sourceY = if ($Symbol -eq "XAUUSD") { 330 } else { 440 }
    $sourceWidth = 175
    $sourceHeight = 90
    $scale = 3
    $scaledWidth = [int]($sourceWidth * $scale)
    $scaledHeight = [int]($sourceHeight * $scale)
    $crop = New-Object System.Drawing.Bitmap($scaledWidth, $scaledHeight)
    $cropGraphics = [System.Drawing.Graphics]::FromImage($crop)
    try {
        $cropGraphics.Clear([System.Drawing.Color]::White)
        $cropGraphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $destination = New-Object System.Drawing.Rectangle(0, 0, $scaledWidth, $scaledHeight)
        $source = New-Object System.Drawing.Rectangle(
            $sourceX, $sourceY, $sourceWidth, $sourceHeight
        )
        $cropGraphics.DrawImage(
            $bitmap,
            $destination,
            $source,
            [System.Drawing.GraphicsUnit]::Pixel
        )
    }
    finally {
        $cropGraphics.Dispose()
        $bitmap.Dispose()
    }

    $temporaryPath = Join-Path ([IO.Path]::GetTempPath()) (
        "market-analysis-jin10-" + [Guid]::NewGuid().ToString("N") + ".png"
    )
    try {
        $crop.Save($temporaryPath, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $crop.Dispose()
    }
    if (-not [string]::IsNullOrWhiteSpace($env:MARKET_ANALYSIS_DESKTOP_CAPTURE_DEBUG)) {
        Copy-Item -LiteralPath $temporaryPath -Destination $env:MARKET_ANALYSIS_DESKTOP_CAPTURE_DEBUG -Force
    }

    try {
        $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
        $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
        $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
        $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
        $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
        })[0]
        function Await-WindowsRuntime {
            param($Operation, [Type]$ResultType)
            $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
            $task.Wait()
            return $task.Result
        }
        $file = Await-WindowsRuntime (
            [Windows.Storage.StorageFile]::GetFileFromPathAsync($temporaryPath)
        ) ([Windows.Storage.StorageFile])
        $stream = Await-WindowsRuntime (
            $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
        ) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-WindowsRuntime (
            [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        ) ([Windows.Graphics.Imaging.BitmapDecoder])
        $softwareBitmap = Await-WindowsRuntime (
            $decoder.GetSoftwareBitmapAsync()
        ) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) {
            throw "Windows OCR is unavailable; install OCR language support"
        }
        $ocrResult = Await-WindowsRuntime (
            $engine.RecognizeAsync($softwareBitmap)
        ) ([Windows.Media.Ocr.OcrResult])
        $rawPrice = $ocrResult.Text
    }
    finally {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }

    if ([string]::IsNullOrWhiteSpace($rawPrice)) {
        throw "Quote recognition failed; keep the Jin10 market page in its default layout"
    }
    Write-Result @{
        success = $true
        available = $true
        state = "ready"
        symbol = $Symbol
        raw_price = $rawPrice
        captured_at = [DateTime]::UtcNow.ToString("o")
        capture_method = "print_window_windows_ocr"
        capture_width = $width
        capture_height = $height
    }
}
catch {
    Write-Result @{
        success = $false
        available = $false
        state = "capture_failed"
        error = $_.Exception.Message
        error_line = $_.InvocationInfo.ScriptLineNumber
        checked_at = [DateTime]::UtcNow.ToString("o")
    } 2
}
