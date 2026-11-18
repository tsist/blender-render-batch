$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot 'batch_cli.py'
$python = $env:BLENDERCTL_PYTHON
if (!$python) {
    $blender = $env:BLENDER_EXECUTABLE
    for ($index = 0; $index -lt $args.Count; $index++) {
        $argument = [string]$args[$index]
        if ($argument -ceq '--blender' -and ($index + 1) -lt $args.Count) {
            $blender = [string]$args[$index + 1]
        } elseif ($argument.StartsWith('--blender=', [StringComparison]::Ordinal)) {
            $blender = $argument.Substring(10)
        }
    }
    if (!$blender) {
        $blender = (Get-Command blender.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    }
    if ($blender -and (Test-Path -LiteralPath $blender -PathType Leaf)) {
        $blenderDirectory = Split-Path (Resolve-Path -LiteralPath $blender).Path
        $bundledPython = Join-Path $blenderDirectory '5.2\python\bin\python.exe'
        if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
            $python = $bundledPython
        }
    }
}
if (!$python) {
    $python = (Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
}
if (!$python) {
    throw 'Set BLENDERCTL_PYTHON to the Python executable included in Blender 5.2.'
}
& $python -B $entry @args
exit $LASTEXITCODE
