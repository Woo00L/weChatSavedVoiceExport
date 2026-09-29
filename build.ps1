param([string]$Python = "$PSScriptRoot\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Python)) { throw '请先建立 .venv 并安装 requirements.txt。' }
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw '测试失败，停止打包。' }
    & $Python main.py --self-test
    if ($LASTEXITCODE -ne 0) { throw '运行环境自检失败，停止打包。' }
    & $Python -m PyInstaller --noconfirm --onefile --windowed --name '微信收藏语音工具' --distpath dist --workpath build main.py
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    Copy-Item -LiteralPath '使用说明.md' -Destination 'dist\使用说明.md'
    Copy-Item -LiteralPath 'LICENSE' -Destination 'dist\LICENSE'
    Write-Output '打包完成：dist\微信收藏语音工具.exe'
}
finally { Pop-Location }
