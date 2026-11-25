$p = Start-Process -FilePath "dist\test\RobloxPlayerBeta.exe" -PassThru -RedirectStandardOutput dist\test\run6.log -RedirectStandardError dist\test\run6.err -NoNewWindow
Start-Sleep -Seconds 6
$target = [IntPtr]0
if (Test-Path dist\test\RobloxPlayerBeta.crash.log) {
  $rip = (Get-Content dist\test\RobloxPlayerBeta.crash.log)[1]
  "$rip"
  $hex = ($rip -split '0x')[1]
  $target = [IntPtr][Convert]::ToInt64($hex,16)
}
try { $mods = $p.Modules } catch { $mods = @() }
foreach ($m in $mods) {
  $start = [Int64]$m.BaseAddress
  $end = $start + $m.ModuleMemorySize
  if ($target.ToInt64() -ge $start -and $target.ToInt64() -lt $end) { "HIT " + $m.ModuleName + " +" + ($target.ToInt64()-$start).ToString("x") }
}
"modules=" + $mods.Count
