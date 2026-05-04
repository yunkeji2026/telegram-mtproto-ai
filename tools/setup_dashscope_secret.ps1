param(
    [string]$ApiKey = "",
    [string]$Region = "cn"
)

$ErrorActionPreference = "Stop"

if (-not $ApiKey.Trim()) {
    $secure = Read-Host "DASHSCOPE_API_KEY" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $ApiKey = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not $ApiKey.Trim()) {
    throw "DASHSCOPE_API_KEY is empty"
}

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envPath = Join-Path $root ".env.local"

@(
    "DASHSCOPE_API_KEY=$ApiKey"
    "DASHSCOPE_REGION=$Region"
) | Set-Content -Encoding UTF8 -Path $envPath

Write-Host "Saved local DashScope secret:"
Write-Host $envPath
Write-Host "This file is ignored by git."
