<#
.SYNOPSIS
    Store DevBrain's API keys as Windows user environment variables.

.DESCRIPTION
    DevBrain reads all configuration from application (environment) variables.
    This script prompts for each secret and saves it to your Windows *user*
    environment, so it persists across reboots and is available to every
    terminal, VS Code and Docker Compose — without ever writing a .env file.

    Secrets are entered as masked input and are never echoed to the console,
    never written to disk by this script, and never stored in your PowerShell
    history.

    Scope is "User", not "Machine", so no administrator rights are needed and
    the values are not exposed to other accounts on this computer.

.EXAMPLE
    .\set-env.ps1
    Prompts for every value, leaving anything already set untouched if you
    press Enter.

.EXAMPLE
    .\set-env.ps1 -Show
    Lists which variables are currently set, without revealing their values.

.EXAMPLE
    .\set-env.ps1 -Clear
    Removes every DevBrain variable from your user environment.
#>
[CmdletBinding()]
param(
    [switch]$Show,
    [switch]$Clear
)

$ErrorActionPreference = 'Stop'

# Secrets are prompted with masked input; everything else is plain text.
$Secrets = @(
    @{ Name = 'OPENAI_API_KEY';    Hint = 'https://platform.openai.com/api-keys  (starts sk-)' }
    @{ Name = 'COHERE_API_KEY';    Hint = 'https://dashboard.cohere.com/api-keys' }
    @{ Name = 'LANGSMITH_API_KEY'; Hint = 'https://smith.langchain.com -> Settings -> API Keys (optional)' }
    @{ Name = 'ADMIN_PASSWORD';    Hint = 'password for the seeded administrator account' }
)

$Plain = @(
    @{ Name = 'OPENAI_MODEL';       Default = 'gpt-4o-mini' }
    @{ Name = 'COHERE_EMBED_MODEL'; Default = 'embed-english-v3.0' }
    @{ Name = 'LANGSMITH_TRACING';  Default = 'true' }
    @{ Name = 'LANGSMITH_PROJECT';  Default = 'devbrain' }
    @{ Name = 'LANGSMITH_ENDPOINT'; Default = 'https://api.smith.langchain.com'
       Hint = 'use https://eu.api.smith.langchain.com if your LangSmith account is in the EU' }
    @{ Name = 'DATABASE_URL';       Default = 'postgresql+psycopg://devbrain:devbrain_pass@localhost:5433/devbrain' }
    @{ Name = 'ADMIN_USERNAME';     Default = 'Shailendra.KS' }
)

$AllNames = ($Secrets.Name + $Plain.Name)

function Get-UserVar([string]$Name) {
    [Environment]::GetEnvironmentVariable($Name, 'User')
}

function Set-UserVar([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    # Also set it in THIS session so you can use it without reopening a terminal.
    Set-Item -Path "Env:$Name" -Value $Value
}

# ---------------------------------------------------------------- -Show
if ($Show) {
    Write-Host ''
    Write-Host 'DevBrain user environment variables' -ForegroundColor Cyan
    Write-Host ('-' * 62)
    foreach ($name in $AllNames) {
        $value = Get-UserVar $name
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Host ('  {0,-20} not set' -f $name) -ForegroundColor DarkGray
        }
        elseif ($Secrets.Name -contains $name) {
            # Never print a secret. Length + first 4 chars is enough to identify it.
            $preview = if ($value.Length -gt 4) { $value.Substring(0, 4) } else { '' }
            Write-Host ('  {0,-20} set ({1} chars, starts "{2}...")' -f $name, $value.Length, $preview) -ForegroundColor Green
        }
        else {
            Write-Host ('  {0,-20} {1}' -f $name, $value) -ForegroundColor Green
        }
    }
    Write-Host ''
    return
}

# ---------------------------------------------------------------- -Clear
if ($Clear) {
    Write-Host ''
    Write-Warning 'This removes every DevBrain variable from your USER environment.'
    $confirm = Read-Host 'Type "yes" to continue'
    if ($confirm -ne 'yes') { Write-Host 'Cancelled.' -ForegroundColor Yellow; return }

    foreach ($name in $AllNames) {
        [Environment]::SetEnvironmentVariable($name, $null, 'User')
        if (Test-Path "Env:$name") { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    }
    Write-Host 'All DevBrain variables removed.' -ForegroundColor Green
    Write-Host ''
    return
}

# ---------------------------------------------------------------- set
Write-Host ''
Write-Host 'DevBrain — store configuration as Windows user variables' -ForegroundColor Cyan
Write-Host ('-' * 62)
Write-Host 'Press Enter to keep the current value (or accept the default).'
Write-Host 'Secrets are masked as you type and are never displayed.'
Write-Host ''

foreach ($item in $Secrets) {
    $name    = $item.Name
    $current = Get-UserVar $name
    $status  = if ([string]::IsNullOrWhiteSpace($current)) { 'not set' } else { "already set ($($current.Length) chars)" }

    Write-Host ("{0}  [{1}]" -f $name, $status) -ForegroundColor Yellow
    if ($item.Hint) { Write-Host ("  {0}" -f $item.Hint) -ForegroundColor DarkGray }

    $secure = Read-Host '  value' -AsSecureString
    $plain  = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                 [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))

    if ([string]::IsNullOrWhiteSpace($plain)) {
        Write-Host '  kept unchanged' -ForegroundColor DarkGray
    }
    else {
        Set-UserVar $name $plain.Trim()
        Write-Host '  saved' -ForegroundColor Green
    }
    Write-Host ''
}

foreach ($item in $Plain) {
    $name    = $item.Name
    $current = Get-UserVar $name
    $shown   = if ([string]::IsNullOrWhiteSpace($current)) { $item.Default } else { $current }

    Write-Host ("{0}" -f $name) -ForegroundColor Yellow
    if ($item.Hint) { Write-Host ("  {0}" -f $item.Hint) -ForegroundColor DarkGray }

    $entered = Read-Host ("  value [{0}]" -f $shown)
    $value   = if ([string]::IsNullOrWhiteSpace($entered)) { $shown } else { $entered.Trim() }

    Set-UserVar $name $value
    Write-Host '  saved' -ForegroundColor Green
    Write-Host ''
}

Write-Host ('-' * 62)
Write-Host 'Done.' -ForegroundColor Green
Write-Host ''
Write-Host 'IMPORTANT: already-open terminals, VS Code windows and Docker Desktop'
Write-Host 'still hold the OLD environment. Restart them to pick these up.'
Write-Host ''
Write-Host 'Verify with:  .\set-env.ps1 -Show'
Write-Host 'Then run   :  uvicorn app:app --reload --port 8001'
Write-Host ''
Write-Host 'You can now delete .env if you no longer want secrets on disk.' -ForegroundColor Cyan
Write-Host ''
