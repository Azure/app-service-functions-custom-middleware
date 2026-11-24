<#
.SYNOPSIS
Builds, validates, and publishes this package to PyPI.

.DESCRIPTION
Run from any directory:

    .\scripts\publish-pypi.ps1

This direct-upload helper is retained for compatibility. Microsoft production
releases must use the ESRP release pipeline in .pipelines\release-pypi.yml instead.
For a credential-free build, run .\scripts\build-pypi.ps1.

To save the token for later publishes:

    .\scripts\publish-pypi.ps1 -SaveToken

This command saves the token and exits without publishing. Later runs load the
encrypted token automatically. Remove it with:

    .\scripts\publish-pypi.ps1 -ForgetToken

Before publishing, update the version in both pyproject.toml and
src\app_service_extensions\__init__.py. The script refuses to continue when
they differ, installs the release dependencies, runs tests, recreates dist,
checks the artifacts, and uploads them.

The saved token is encrypted with Windows DPAPI for the current user and
machine, and is stored outside the repository under LocalAppData. Never
hardcode the token in this file or commit it anywhere in the repository.
#>

[CmdletBinding()]
param(
    [string] $TokenFile = (
        Join-Path $env:LOCALAPPDATA "AppExtensionsLibrary\pypi-token.dpapi"
    ),
    [switch] $SaveToken,
    [switch] $ForgetToken
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot

if ($ForgetToken) {
    if (Test-Path -LiteralPath $TokenFile) {
        Remove-Item -LiteralPath $TokenFile -Force
        Write-Host "Removed saved PyPI token from $TokenFile"
    }
    else {
        Write-Host "No saved PyPI token exists at $TokenFile"
    }
    return
}

if ($SaveToken) {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw "-SaveToken requires Windows DPAPI"
    }

    $secureToken = Read-Host "PyPI API token" -AsSecureString
    try {
        $tokenDirectory = Split-Path -Parent $TokenFile
        if ($tokenDirectory) {
            New-Item -ItemType Directory -Path $tokenDirectory -Force | Out-Null
        }

        ConvertFrom-SecureString $secureToken |
            Set-Content -LiteralPath $TokenFile -NoNewline
        Write-Host "Saved encrypted PyPI token to $TokenFile"
    }
    finally {
        $secureToken.Dispose()
    }
    return
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments
    )

    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

Push-Location $repositoryRoot
try {
    & (Join-Path $PSScriptRoot "build-pypi.ps1")

    $projectVersion = (
        python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the project version"
    }

    $artifacts = @(
        Get-ChildItem .\dist -File |
            Where-Object { $_.Name -like "*.whl" -or $_.Name -like "*.tar.gz" } |
            Sort-Object Name |
            ForEach-Object { $_.FullName }
    )
    if ($artifacts.Count -eq 0) {
        throw "The build produced no wheel or source distribution"
    }

    # Twine reads these standard variables in its child process. Restore any
    # pre-existing values immediately after the upload attempt.
    if (Test-Path -LiteralPath $TokenFile) {
        try {
            $encryptedToken = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
            $secureToken = ConvertTo-SecureString $encryptedToken
        }
        catch {
            throw "Could not decrypt the saved PyPI token at $TokenFile. Run with -ForgetToken, then -SaveToken."
        }
        Write-Host "Using saved PyPI token from $TokenFile"
    }
    else {
        $secureToken = Read-Host "PyPI API token" -AsSecureString
    }

    $credential = [System.Net.NetworkCredential]::new("", $secureToken)
    $previousUsername = $env:TWINE_USERNAME
    $previousPassword = $env:TWINE_PASSWORD

    try {
        $env:TWINE_USERNAME = "__token__"
        $env:TWINE_PASSWORD = $credential.Password
        Invoke-Python -Arguments (
            @("-m", "twine", "upload", "--repository", "pypi") + $artifacts
        )
    }
    finally {
        if ($null -eq $previousUsername) {
            Remove-Item Env:\TWINE_USERNAME -ErrorAction SilentlyContinue
        }
        else {
            $env:TWINE_USERNAME = $previousUsername
        }

        if ($null -eq $previousPassword) {
            Remove-Item Env:\TWINE_PASSWORD -ErrorAction SilentlyContinue
        }
        else {
            $env:TWINE_PASSWORD = $previousPassword
        }

        $credential = $null
        $secureToken.Dispose()
    }

    Write-Host "Published app-service-functions-custom-middleware $projectVersion"
}
finally {
    Pop-Location
}
