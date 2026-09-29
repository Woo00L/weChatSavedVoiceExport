param([string]$Output = (Join-Path $PSScriptRoot '..\wechat-favorites-tool-source.zip'))

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$outputPath = [System.IO.Path]::GetFullPath($Output)
if ($outputPath.StartsWith($root + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '源码包必须放在项目目录之外。'
}
if (Test-Path -LiteralPath $outputPath) { throw "目标文件已存在：$outputPath" }

$files = @(
    '.gitignore', 'LICENSE', 'README.md', 'CONTRIBUTING.md', '使用说明.md',
    'requirements.txt', 'main.py', 'build.ps1', 'package_source.ps1',
    '.github/workflows/tests.yml',
    'favorite_tool/__init__.py', 'favorite_tool/audio.py',
    'favorite_tool/bridge.py', 'favorite_tool/common.py',
    'favorite_tool/database.py', 'favorite_tool/gui.py',
    'favorite_tool/service.py',
    'tests/test_audio.py', 'tests/test_database.py', 'tests/test_service.py'
)

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ('wechat-favorites-source-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    foreach ($relative in $files) {
        $source = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "缺少发布文件：$relative" }
        $target = Join-Path $stage $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $source -Destination $target
    }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $outputPath -CompressionLevel Optimal
    Write-Output "源码包：$outputPath"
    $files | Sort-Object | ForEach-Object { Write-Output "  $_" }
}
finally {
    if ((Test-Path -LiteralPath $stage) -and
        $stage.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
