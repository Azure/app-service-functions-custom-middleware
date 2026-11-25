<#
.SYNOPSIS
Tests, builds, and validates the package without publishing.

.DESCRIPTION
Run in a Python 3.13+ virtual environment from any directory (the full optional
agents-runtime integration requires Python 3.13):

    .\scripts\build-pypi.ps1

Update the version in pyproject.toml and src\app_service_extensions\__init__.py
before building. Only the newly built wheel and source distribution are left
in dist. No PyPI credentials are used.
#>

[CmdletBinding()]
param(
    [string] $TestResultsFile
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot

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
    $projectVersion = (
        python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the project version"
    }

    $moduleVersion = (
        python -c "import sys; sys.path.insert(0, 'src'); import app_service_extensions; print(app_service_extensions.__version__)"
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the module version"
    }

    if ($projectVersion -ne $moduleVersion) {
        throw "Version mismatch: pyproject.toml is $projectVersion but __version__ is $moduleVersion"
    }

    Write-Host "Preparing app-service-functions-custom-middleware $projectVersion"

    Invoke-Python -Arguments @(
        "-m", "pip", "install", "--upgrade",
        "build", "twine", ".[dev,functions,identity,hosted-skills]"
    )

    $testArguments = @("-m", "pytest")
    if ($TestResultsFile) {
        $testResultsDirectory = Split-Path -Parent $TestResultsFile
        if ($testResultsDirectory) {
            New-Item -ItemType Directory -Path $testResultsDirectory -Force | Out-Null
        }
        $testArguments += "--junitxml=$TestResultsFile"
    }
    Invoke-Python -Arguments $testArguments

    $distDirectory = Join-Path $repositoryRoot "dist"
    if (Test-Path -LiteralPath $distDirectory) {
        Remove-Item -LiteralPath $distDirectory -Recurse -Force
    }
    Invoke-Python -Arguments @("-m", "build")

    $wheels = @(Get-ChildItem -LiteralPath $distDirectory -File -Filter "*.whl")
    $sdists = @(Get-ChildItem -LiteralPath $distDirectory -File -Filter "*.tar.gz")
    if ($wheels.Count -ne 1 -or $sdists.Count -ne 1) {
        throw "Expected one wheel and one source distribution in dist"
    }

    $artifacts = @($wheels[0].FullName, $sdists[0].FullName)
    Invoke-Python -Arguments (@("-m", "twine", "check", "--strict") + $artifacts)
    Write-Host "Validated app-service-functions-custom-middleware $projectVersion in $distDirectory"
}
finally {
    Pop-Location
}
