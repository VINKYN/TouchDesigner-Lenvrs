@echo off
title TouchDesigner — Application Web
cd /d "%~dp0web-app"

echo [1/3] Verification de la synchronisation avec ton fichier .toe...
node td\build-live-toe.mjs
if errorlevel 1 (
  echo Erreur lors de la preparation du fichier .toe.
  pause
  exit /b 1
)

echo [2/3] Lancement de TouchDesigner avec ton vrai projet .toe...
start "" "C:\Program Files\Derivative\TouchDesigner\bin\TouchDesigner.exe" "%~dp0web-app\td\project_live.toe"

echo [3/3] Ouverture de l'application web sur http://127.0.0.1:9980 ...
echo Astuce : tape "vn" au clavier dans le navigateur pour ouvrir les parametres caches.
powershell -NoProfile -Command "for ($i=0; $i -lt 45; $i++) { try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:9980/api/state' -TimeoutSec 2; if ($r.StatusCode -eq 200) { break } } catch {}; Start-Sleep -Seconds 1 }"
start "" "http://127.0.0.1:9980"
