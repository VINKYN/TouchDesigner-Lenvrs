# TouchDesigner — Lenvrs (Caméra WebRTC Interactive)

Application web plein écran affichant en direct le rendu réel du projet TouchDesigner (`je ne sais pas pour qui je me prend.18.toe`) via **WebRTC 60 FPS haute qualité**, avec un panneau de paramètres caché accessible au clavier.

## GitHub Pages

- **Page en ligne** : https://vinkyn.github.io/TouchDesigner-Lenvrs/
- La page web se connecte automatiquement à ton instance locale TouchDesigner sur `ws://127.0.0.1:9980/` (il suffit que TouchDesigner soit lancé en local via `Lancer-App.bat`).

## Lancement rapide (Windows)

1. Installe les dépendances une première fois dans `web-app/` :
   ```bash
   cd web-app
   npm install
   npm run build
   ```
2. Double-clique sur **`Lancer-App.bat`** :
   - génère/synchronise `web-app/td/project_live.toe` depuis `je ne sais pas pour qui je me prend.18.toe`,
   - lance TouchDesigner avec le pont WebRTC 60 FPS (`web-app/td/td_bridge.py`),
   - ouvre automatiquement l'application dans ton navigateur (`http://127.0.0.1:9980`, ou utilise la GitHub Page).

## Paramètres cachés (`vn`)

Tape **`vn`** au clavier n'importe où sur la page pour afficher ou masquer le panneau de paramètres (`Échap` le ferme également) :

- **Source caméra** : sélection en direct parmi les caméras détectées par `MediaPipe` dans TouchDesigner
- **Résolution caméra** (`1280×720`, `1920×1080`, `640×480`)
- **Miroir horizontal** (`MediaPipe.par.Wflip`)
- **Cadrage** (`Cover` / `Contain`), **Fluidité flux** (`60` / `45` / `30 FPS`), **Plein écran**
- **Reset particules** (touches `1` et `2` également conservées) & **Relancer MediaPipe**
