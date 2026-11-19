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
        $executable = Get-Item -LiteralPath $blender
        $productVersion = $executable.VersionInfo.ProductVersionRaw
        if ($productVersion -and $productVersion.Major -ge 5) {
            $bundledPython = Join-Path $executable.DirectoryName ($productVersion.ToString(2) + '\python\bin\python.exe')
            if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
                $python = $bundledPython
            }
        }
    }
}
if (!$python) {
    $python = (Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
}
if (!$python) {
    throw 'Set BLENDERCTL_PYTHON to Python 3.13 or use a Blender executable with bundled Python.'
}
& $python -B $entry @args
exit $LASTEXITCODE
