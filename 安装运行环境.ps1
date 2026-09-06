$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot ".venv-workflow"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"

Write-Host "正在准备独立运行环境，请稍候..." -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    py -3.12 -m venv $venvPath
}

# 在包含中文的 Windows 路径中，虚拟环境自身的 pip 可能在构建依赖时长时间挂起。
# 使用系统 Python 将完全相同的依赖定向安装到该环境，可避开这个问题。
$sitePackages = Join-Path $venvPath "Lib\site-packages"
py -3.12 -m pip install --disable-pip-version-check --upgrade --target $sitePackages -r (Join-Path $projectRoot "requirements.txt")
& $pythonPath -c "from workflow_core import ensure_depth_model; print('深度模型：', ensure_depth_model())"
& $pythonPath -m unittest discover -s (Join-Path $projectRoot "tests") -v

Write-Host "安装与自检完成。现在可以双击“启动自动流程.bat”。" -ForegroundColor Green
