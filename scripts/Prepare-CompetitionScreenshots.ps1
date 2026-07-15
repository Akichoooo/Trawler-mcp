# scripts/Prepare-CompetitionScreenshots.ps1
# TRAE Creativity Contest Screenshot Generator
# Generates 3 code screenshots + 1 real pytest result screenshot
# (4th TRAE dialog screenshot: use Capture-TraeDialog.ps1 yourself)

[CmdletBinding()]
param(
    [string]$OutDir = "docs/screenshots",
    [int]$CodeFontSize = 14,
    [int]$TermFontSize = 14
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

# --- Colors (VSCode Dark+ style) ---
$bgColor         = [System.Drawing.Color]::FromArgb(30, 30, 30)
$lineNumBg       = [System.Drawing.Color]::FromArgb(40, 40, 40)
$lineNumFg       = [System.Drawing.Color]::FromArgb(133, 133, 133)
$textFg          = [System.Drawing.Color]::FromArgb(212, 212, 212)
$keywordFg       = [System.Drawing.Color]::FromArgb(86, 156, 214)
$stringFg        = [System.Drawing.Color]::FromArgb(206, 145, 120)
$commentFg       = [System.Drawing.Color]::FromArgb(106, 153, 85)
$titleBg         = [System.Drawing.Color]::FromArgb(0, 122, 204)
$titleFg         = [System.Drawing.Color]::White

# Terminal colors
$termBg          = [System.Drawing.Color]::FromArgb(12, 12, 12)
$termFg          = [System.Drawing.Color]::FromArgb(204, 204, 204)
$termPass        = [System.Drawing.Color]::FromArgb(0, 200, 100)
$termFail        = [System.Drawing.Color]::FromArgb(255, 80, 80)
$termWarn        = [System.Drawing.Color]::FromArgb(229, 187, 100)
$termCyan        = [System.Drawing.Color]::FromArgb(86, 156, 214)
$termPrompt      = [System.Drawing.Color]::FromArgb(0, 200, 100)

# Cascadia Code / Consolas supports CJK glyphs on Win10+
$codeFont   = New-Object System.Drawing.Font("Cascadia Code, Consolas, Courier New", $CodeFontSize, [System.Drawing.FontStyle]::Regular)
$termFont   = New-Object System.Drawing.Font("Cascadia Mono, Consolas, Courier New", $TermFontSize, [System.Drawing.FontStyle]::Regular)
$titleFont  = New-Object System.Drawing.Font("Segoe UI, Microsoft YaHei UI", 14, [System.Drawing.FontStyle]::Bold)
$lineNumFont = New-Object System.Drawing.Font("Cascadia Code, Consolas, Courier New", $CodeFontSize, [System.Drawing.FontStyle]::Regular)

function Get-LineSize([System.Drawing.Graphics]$g, [string]$text, [System.Drawing.Font]$font) {
    $size = $g.MeasureString($text, $font)
    return [int][Math]::Ceiling($size.Width), [int][Math]::Ceiling($size.Height)
}

function Render-CodeScreenshot {
    param(
        [string]$SourceFile,
        [int]$StartLine,
        [int]$EndLine,
        [string]$Title,
        [string]$OutputPath
    )

    if (-not (Test-Path $SourceFile)) {
        Write-Warning "Source file not found: $SourceFile"
        return
    }

    # CRITICAL: -Encoding UTF8 to read CJK code correctly
    $lines = Get-Content $SourceFile -Encoding UTF8
    $codeLines = $lines[($StartLine - 1)..($EndLine - 1)]
    $lineCount = $codeLines.Count

    $maxLineWidth = 0
    $measureG = [System.Drawing.Graphics]::FromImage((New-Object System.Drawing.Bitmap 1,1))
    foreach ($ln in $codeLines) {
        $w, $h = Get-LineSize $measureG $ln $codeFont
        if ($w -gt $maxLineWidth) { $maxLineWidth = $w }
    }
    $measureG.Dispose()

    $lineNumWidth = 60
    $padding = 20
    $titleHeight = 40
    $lineHeight = 22
    $imgWidth  = $padding * 2 + $lineNumWidth + $maxLineWidth + 20
    $imgHeight = $padding * 2 + $titleHeight + ($lineCount * $lineHeight)

    $imgWidth  = [Math]::Max($imgWidth, 1100)
    $imgHeight = [Math]::Max($imgHeight, 400)

    $bmp = New-Object System.Drawing.Bitmap $imgWidth, $imgHeight
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.TextRenderingHint = "ClearTypeGridFit"
    $g.FillRectangle((New-Object System.Drawing.SolidBrush $bgColor), 0, 0, $imgWidth, $imgHeight)

    # Title bar
    $g.FillRectangle((New-Object System.Drawing.SolidBrush $titleBg), 0, 0, $imgWidth, $titleHeight)
    $titleBrush = New-Object System.Drawing.SolidBrush $titleFg
    $g.DrawString($Title, $titleFont, $titleBrush, $padding, 10)

    # Line numbers column
    $g.FillRectangle((New-Object System.Drawing.SolidBrush $lineNumBg), 0, $titleHeight, $lineNumWidth, $imgHeight - $titleHeight)
    $lineNumBrush = New-Object System.Drawing.SolidBrush $lineNumFg

    # Render code
    $codeX = $lineNumWidth + 12
    for ($i = 0; $i -lt $codeLines.Count; $i++) {
        $lineNo = $StartLine + $i
        $lineText = $codeLines[$i]

        $y = $titleHeight + $padding + ($i * $lineHeight)
        $g.DrawString(("{0,4}" -f $lineNo), $lineNumFont, $lineNumBrush, $padding, $y)

        # Simple syntax highlighting
        $color = $textFg
        $trimmed = $lineText.TrimStart()
        if ($trimmed.StartsWith("#")) {
            $color = $commentFg
        } elseif ($lineText -match '^\s*(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|in|not|and|or|lambda|yield|raise|pass|break|continue|global|nonlocal|async|await|True|False|None)\b') {
            $color = $keywordFg
        } elseif ($lineText -match '"""') {
            $color = $stringFg
        }

        $brush = New-Object System.Drawing.SolidBrush $color
        $g.DrawString($lineText, $codeFont, $brush, $codeX, $y)
        $brush.Dispose()
    }

    $g.Dispose()
    $bmp.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host "  - $OutputPath ($imgWidth x $imgHeight)" -ForegroundColor Green
}

function Render-TerminalScreenshot {
    param(
        [string]$OutputPath,
        [string[]]$Lines,
        [string]$Title = "Trawler Test Results"
    )

    $padding = 20
    $titleHeight = 40
    $lineHeight = 22

    $measureG = [System.Drawing.Graphics]::FromImage((New-Object System.Drawing.Bitmap 1,1))
    $maxW = 0
    foreach ($ln in $Lines) {
        $w, $h = Get-LineSize $measureG $ln $termFont
        if ($w -gt $maxW) { $maxW = $w }
    }
    $measureG.Dispose()

    $imgWidth  = [Math]::Max(1100, $padding * 2 + $maxW + 40)
    $imgHeight = $padding * 2 + $titleHeight + ($Lines.Count * $lineHeight) + 40

    $bmp = New-Object System.Drawing.Bitmap $imgWidth, $imgHeight
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.TextRenderingHint = "ClearTypeGridFit"
    $g.FillRectangle((New-Object System.Drawing.SolidBrush $termBg), 0, 0, $imgWidth, $imgHeight)

    $g.FillRectangle((New-Object System.Drawing.SolidBrush $titleBg), 0, 0, $imgWidth, $titleHeight)
    $titleBrush = New-Object System.Drawing.SolidBrush $titleFg
    $g.DrawString($Title, $titleFont, $titleBrush, $padding, 10)

    for ($i = 0; $i -lt $Lines.Count; $i++) {
        $lineText = $Lines[$i]
        $y = $titleHeight + $padding + ($i * $lineHeight)

        $color = $termFg
        if ($lineText -match 'FAILED' -or $lineText -match 'failed' -or $lineText -match 'ERROR') { $color = $termFail }
        elseif ($lineText -match 'WARNING' -or $lineText -match 'warning' -or $lineText -match 'SKIPPED' -or $lineText -match 'skipped') { $color = $termWarn }
        elseif ($lineText -match 'test_' -or $lineText -match '.py::' -or $lineText -match 'collected') { $color = $termCyan }
        elseif ($lineText -match 'PS>') { $color = $termPrompt }
        elseif ($lineText -match 'passed' -or $lineText -match 'PASSED') { $color = $termPass }

        $brush = New-Object System.Drawing.SolidBrush $color
        $g.DrawString($lineText, $termFont, $brush, $padding, $y)
        $brush.Dispose()
    }

    $g.Dispose()
    $bmp.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host "  - $OutputPath ($imgWidth x $imgHeight)" -ForegroundColor Green
}

# ============================================================
# Main flow
# ============================================================
Write-Host ""
Write-Host "=== TRAE Creativity Contest Screenshot Generator ===" -ForegroundColor Cyan
Write-Host ""

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# Img 1: SSRF Guard
Write-Host "[1/4] SSRF Guard code (Chinese)..." -ForegroundColor Yellow
Render-CodeScreenshot `
    -SourceFile "trawler/ssrf.py" `
    -StartLine 142 `
    -EndLine 192 `
    -Title "Trawler-mcp | SSRF Guard - Private/Cloud-metadata CIDR blocklist" `
    -OutputPath "$OutDir/01_ssrf_guard.png"

# Img 2: curl_cffi JA3/JA4 fingerprint
Write-Host "[2/4] curl_cffi JA3/JA4 fingerprint (Chinese)..." -ForegroundColor Yellow
Render-CodeScreenshot `
    -SourceFile "trawler/fetcher/curlcffi_rung.py" `
    -StartLine 1 `
    -EndLine 56 `
    -Title "Trawler-mcp | curl_cffi Rung 0 - JA3/JA4 TLS fingerprint injection" `
    -OutputPath "$OutDir/02_curlcffi_fingerprint.png"

# Img 3: live_browser 13 extract modes
Write-Host "[3/4] Live Browser extract modes (English)..." -ForegroundColor Yellow
Render-CodeScreenshot `
    -SourceFile "trawler/live_browser.py" `
    -StartLine 1 `
    -EndLine 65 `
    -Title "Trawler-mcp | Live Browser - 13 extract modes" `
    -OutputPath "$OutDir/03_live_browser_modes.png"

# Img 4: Real pytest run
Write-Host "[4/4] Running real pytest (this may take 30-60s)..." -ForegroundColor Yellow

# Use venv python directly to bypass uv run wrapper that errors on non-zero exit
$pythonExe = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) { $pythonExe = "python" }
try {
    $pytestRaw = & $pythonExe -m pytest -q --no-header --color=no 2>&1 | Out-String
} catch {
    Write-Warning "pytest errored: $_"
    $pytestRaw = "PS> $pythonExe -m pytest`nERROR: $_"
}
$pytestLines = $pytestRaw -split "`n" | ForEach-Object { $_.TrimEnd() } | Where-Object { $_ -ne "" }

# Desensitize local absolute paths for public screenshots
$localPath1 = [regex]::Escape('D:\devloop\workSpace\app_ZCode\Trawler-mcp')
$localPath2 = [regex]::Escape('D:/devloop/workSpace/app_ZCode/Trawler-mcp')
$localPath3 = [regex]::Escape('D:\devloop\workSpace\app_ZCode\Trawler-mcp\.venv\Lib\site-packages\')
$desensitized = New-Object System.Collections.Generic.List[string]
foreach ($ln in $pytestLines) {
    $tmp = $ln -replace $localPath1, '<repo>'
    $tmp = $tmp -replace $localPath2, '<repo>'
    $tmp = $tmp -replace $localPath3, '<venv>/'
    [void]$desensitized.Add($tmp)
}
$pytestLines = $desensitized.ToArray()

if ($pytestLines.Count -gt 30) {
    $pytestLines = $pytestLines[($pytestLines.Count - 30)..($pytestLines.Count - 1)]
}

$headerLine = "PS> python -m pytest -q --no-header --color=no"
$pytestLines = @($headerLine) + @($pytestLines)

Render-TerminalScreenshot `
    -OutputPath "$OutDir/04_pytest_results.png" `
    -Lines $pytestLines `
    -Title ("Trawler-mcp | pytest -- " + (Get-Date -Format 'yyyy-MM-dd HH:mm'))

Write-Host ""
Write-Host "=== Done: 3 PNGs generated ===" -ForegroundColor Green
Write-Host "  Location: $OutDir/" -ForegroundColor Cyan
Write-Host "  Remaining: 1 TRAE dialog screenshot (use Capture-TraeDialog.ps1)" -ForegroundColor Cyan
Write-Host ""
