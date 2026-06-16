# create-distros.ps1 — vytvoří 3 samostatná WSL prostředí pro Kuchařku.
# Spusť v PowerShellu na Windows. Předpokládá nainstalované základní "Ubuntu"
# (jinak: wsl --install -d Ubuntu a jednou ho otevři, ať se dokončí setup).

$ErrorActionPreference = "Stop"
$Base    = "Ubuntu"
$Root    = "C:\wsl"
$Tar     = "$Root\ubuntu-base.tar"
$Distros = @("kucharka-db", "kucharka-api", "kucharka-web")

New-Item -ItemType Directory -Force -Path $Root | Out-Null

Write-Host "Exportuji základní distro '$Base'…"
wsl --export $Base $Tar

foreach ($d in $Distros) {
    $dir = "$Root\$d"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Write-Host "Vytvářím $d…"
    wsl --import $d $dir $Tar --version 2
}

Write-Host ""
Write-Host "Hotovo. Prostředí:"
wsl --list --verbose
Write-Host ""
Write-Host "Pozn.: importovaná distra se přihlašují jako root (pro vývoj OK)."
Write-Host "Spustíš je: wsl -d kucharka-db   (resp. -api / -web)"
