param([string]$Path, [int]$RepeatSeconds = 6)
Add-Type -AssemblyName System.Media
$player = New-Object System.Media.SoundPlayer($Path)
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $RepeatSeconds) {
    $player.PlaySync()
}
