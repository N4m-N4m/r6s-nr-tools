# process_all.ps1
# Batch driver: one fresh Blender process per map (memory resets between maps),
# then the Node web-packaging stage.
#
#   .\process_all.ps1 -InDir "D:\maps\raw" -OutDir "D:\maps\web"
#
param(
    [Parameter(Mandatory = $true)] [string] $InDir,
    [Parameter(Mandatory = $true)] [string] $OutDir,
    [string] $Blender   = "blender",          # or full path to blender.exe
    [string] $CleanPy   = ".\r6_map_clean.py",
    [string] $OptimizeJs= ".\optimize.mjs",
    [double] $Angle     = 3.0,                # limited dissolve degrees
    [double] $MergeDist = 0.0005,             # weld distance (scene units)
    [string] $SimplifyError = "0.0005",       # meshopt error budget
    [string[]] $Extensions = @("*.fbx","*.obj","*.glb","*.gltf","*.dae")
)

$ErrorActionPreference = "Stop"
$tmpDir = Join-Path $OutDir "_clean"
New-Item -ItemType Directory -Force -Path $OutDir, $tmpDir | Out-Null

$files = Get-ChildItem -Path $InDir -Include $Extensions -File -Recurse
Write-Host "Found $($files.Count) map file(s)" -ForegroundColor Cyan

$results = @()
foreach ($f in $files) {
    $name   = [IO.Path]::GetFileNameWithoutExtension($f.Name)
    $clean  = Join-Path $tmpDir  "$name.glb"
    $final  = Join-Path $OutDir  "$name.glb"
    $sw = [Diagnostics.Stopwatch]::StartNew()

    Write-Host "`n=== $name ===" -ForegroundColor Yellow

    # Stage 1: Blender headless cleanup (fresh process per map)
    & $Blender --background --factory-startup --python $CleanPy -- `
        --input  $f.FullName `
        --output $clean `
        --angle  $Angle `
        --merge-dist $MergeDist
    if ($LASTEXITCODE -ne 0) { Write-Warning "Blender failed on $name, skipping"; continue }

    # Stage 2: gltf-transform web packaging
    node $OptimizeJs $clean $final --error $SimplifyError
    if ($LASTEXITCODE -ne 0) { Write-Warning "optimize.mjs failed on $name"; continue }

    $sw.Stop()
    $results += [pscustomobject]@{
        Map       = $name
        SourceMB  = [math]::Round($f.Length / 1MB, 1)
        FinalMB   = [math]::Round((Get-Item $final).Length / 1MB, 2)
        Minutes   = [math]::Round($sw.Elapsed.TotalMinutes, 1)
    }
}

Write-Host "`n================ SUMMARY ================" -ForegroundColor Cyan
$results | Format-Table -AutoSize
