$ErrorActionPreference = "Stop"

$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$NkHome = if ($env:NK_HOME) { $env:NK_HOME } else { Join-Path $HOME ".nk" }
$Stage = Join-Path $NkHome (".install." + $PID)
$Revision = (git -C $SourceRoot rev-parse HEAD).Trim()
if (git -C $SourceRoot status --porcelain --untracked-files=all) {
    $Revision += "-dirty"
}

try {
    New-Item -ItemType Directory -Force -Path $NkHome, (Join-Path $NkHome "clusters"), (Join-Path $Stage "app"), (Join-Path $Stage "bin"), (Join-Path $Stage "skills") | Out-Null
    Copy-Item -Recurse (Join-Path $SourceRoot "nk") (Join-Path $Stage "app\nk")
    Copy-Item -Recurse (Join-Path $SourceRoot "entrypoints") (Join-Path $Stage "app\entrypoints")
    Copy-Item -Recurse (Join-Path $SourceRoot "prompts") (Join-Path $Stage "app\prompts")
    Set-Content -Encoding ascii (Join-Path $Stage "app\REVISION") $Revision
    Copy-Item (Join-Path $SourceRoot "bin\nk.cmd") (Join-Path $Stage "bin\nk.cmd")
    Copy-Item -Recurse (Join-Path $SourceRoot "skills\*") (Join-Path $Stage "skills")

    foreach ($Name in "app", "bin", "skills") {
        $Destination = Join-Path $NkHome $Name
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Destination
        Move-Item (Join-Path $Stage $Name) $Destination
    }
    $Discoveries = @(
        (Join-Path $HOME ".agents\skills"),
        (Join-Path $HOME ".claude\skills")
    )
    New-Item -ItemType Directory -Force -Path $Discoveries | Out-Null
    Get-ChildItem (Join-Path $NkHome "skills") -Directory | ForEach-Object {
        foreach ($Discovery in $Discoveries) {
            $Destination = Join-Path $Discovery $_.Name
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Destination
            Copy-Item -Recurse $_.FullName $Destination
        }
    }
    Write-Output "installed nk at $NkHome; add $NkHome\bin to PATH"
}
finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Stage
}
