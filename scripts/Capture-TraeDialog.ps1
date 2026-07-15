# scripts/Capture-TraeDialog.ps1
# One-click helper to capture the TRAE IDE dialog screenshot
# Run this with the TRAE dialog you want to screenshot already visible

[CmdletBinding()]
param(
    [string]$OutputPath = "docs/screenshots/05_trae_dialog.png"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

Write-Host ""
Write-Host "=== TRAE Dialog Screenshot Helper ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script will:" -ForegroundColor Gray
Write-Host "    1. Minimize this PowerShell window" -ForegroundColor Gray
Write-Host "    2. Show a 3-second countdown" -ForegroundColor Gray
Write-Host "    3. After 3 seconds, you'll be prompted to draw a rectangle" -ForegroundColor Gray
Write-Host "    4. Drag to select the TRAE dialog area" -ForegroundColor Gray
Write-Host "    5. Save to: $OutputPath" -ForegroundColor Gray
Write-Host ""
Write-Host "  TIP: Switch to TRAE IDE first and bring the dialog to the front." -ForegroundColor Yellow
Write-Host ""
$confirm = Read-Host "Press Enter to start (Ctrl+C to cancel)"

# Minimize this window
$consolePtr = (Get-Process -Id $PID).MainWindowHandle
[void][System.Windows.Forms.SendKeys]::SendWait("{F11}")

Start-Sleep -Seconds 3

# Capture selected region using .NET Graphics.CopyFromScreen
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Full screen capture as fallback
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)

# Create a form to let user crop
$cropForm = New-Object System.Windows.Forms.Form
$cropForm.Text = "Drag to select TRAE dialog area (ESC to use full screen)"
$cropForm.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$cropForm.Opacity = 0.3
$cropForm.BackColor = [System.Drawing.Color]::Black
$cropForm.Bounds = $bounds
$cropForm.TopMost = $true
$cropForm.Cursor = [System.Windows.Forms.Cursors]::Cross

$start = [System.Drawing.Point]::Empty
$end = [System.Drawing.Point]::Empty
$isDragging = $false

$cropForm.Add_MouseDown({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $script:start = $e.Location
        $script:isDragging = $true
    }
})

$cropForm.Add_MouseMove({
    param($s, $e)
    if ($script:isDragging) {
        $script:end = $e.Location
        $cropForm.Invalidate()
    }
})

$cropForm.Add_MouseUp({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $script:end = $e.Location
        $script:isDragging = $false
        $cropForm.Close()
    }
})

$cropForm.Add_KeyDown({
    param($s, $e)
    if ($e.KeyCode -eq [System.Windows.Forms.Keys]::Escape) {
        $script:start = [System.Drawing.Point]::Empty
        $script:end = [System.Drawing.Point]::Empty
        $cropForm.Close()
    }
})

$cropForm.Add_Paint({
    param($s, $e)
    if ($script:isDragging -and $script:start -ne [System.Drawing.Point]::Empty) {
        $x = [Math]::Min($script:start.X, $script:end.X)
        $y = [Math]::Min($script:start.Y, $script:end.Y)
        $w = [Math]::Abs($script:end.X - $script:start.X)
        $h = [Math]::Abs($script:end.Y - $script:start.Y)
        $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::Red), 2
        $e.Graphics.DrawRectangle($pen, $x, $y, $w, $h)
    }
})

[void]$cropForm.ShowDialog()
$g.Dispose()

# Crop or use full
if ($start -ne [System.Drawing.Point]::Empty -and $end -ne [System.Drawing.Point]::Empty -and $start -ne $end) {
    $x = [Math]::Min($start.X, $end.X)
    $y = [Math]::Min($start.Y, $end.Y)
    $w = [Math]::Abs($end.X - $start.X)
    $h = [Math]::Abs($end.Y - $start.Y)
    $cropRect = New-Object System.Drawing.Rectangle($x, $y, $w, $h)
    $cropped = $bmp.Clone($cropRect, $bmp.PixelFormat)
    $bmp.Dispose()
    $bmp = $cropped
    Write-Host "  - Cropped to ${w}x${h} at (${x},${y})" -ForegroundColor Green
} else {
    Write-Host "  - Using full screen" -ForegroundColor Green
}

$dir = Split-Path $OutputPath -Parent
if ($dir -and -not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
$bmp.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host "  - Saved to: $OutputPath" -ForegroundColor Green
Write-Host ""
