$ErrorActionPreference = 'Stop'
$root = 'D:\IT\program\python-program\FoodRecommendationPlatform'
$src  = Join-Path $root 'docs\寻味美食推荐平台-第十组-项目汇报.pptx'
$tmp  = Join-Path $root 'build_ppt\tmp_deck.pptx'
$out  = Join-Path $root 'build_ppt\preview'

Write-Output "exists=$(Test-Path -LiteralPath $src) size=$((Get-Item -LiteralPath $src).Length)"
Copy-Item -LiteralPath $src -Destination $tmp -Force
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out | Out-Null

$app = New-Object -ComObject PowerPoint.Application
$pres = $app.Presentations.Open($tmp, $true, $false)
$i = 0
foreach ($slide in $pres.Slides) {
    $i++
    $f = Join-Path $out ("slide-{0:D2}.png" -f $i)
    $slide.Export($f, 'PNG', 1600, 900)
}
$pres.Close()
$app.Quit()
Write-Output "exported $i slides"
