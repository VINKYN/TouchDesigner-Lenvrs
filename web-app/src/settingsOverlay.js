const STORAGE_KEY = 'td_vn_ui_prefs_v2';

export function loadLocalPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { fitMode: 'cover' };
    return { fitMode: 'cover', ...JSON.parse(raw) };
  } catch {
    return { fitMode: 'cover' };
  }
}

export function saveLocalPrefs(prefs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Ignore storage errors
  }
}

/**
 * Gère le raccourci secret "vn" au clavier et le panneau caché pilotant TouchDesigner.
 */
export class VNSettingsOverlay {
  constructor({ prefs, onCommand, onFitChange }) {
    this.prefs = prefs;
    this.onCommand = onCommand;
    this.onFitChange = onFitChange;

    this.isOpen = false;
    this.keyBuffer = '';
    this.lastKeyTime = 0;
    this.lastCamerasSig = '';

    this.overlay = document.getElementById('vn-overlay');
    this.closeBtn = document.getElementById('vn-close-btn');
    this.cameraSelect = document.getElementById('camera-select');
    this.refreshBtn = document.getElementById('refresh-cameras-btn');
    this.resolutionSelect = document.getElementById('resolution-select');
    this.fitSelect = document.getElementById('fit-select');
    this.fpsSelect = document.getElementById('fps-select');
    this.mirrorToggle = document.getElementById('mirror-toggle');
    this.fullscreenBtn = document.getElementById('fullscreen-btn');
    this.fullscreenLabel = document.getElementById('fullscreen-btn-label');
    this.resetParticlesBtn = document.getElementById('reset-particles-btn');
    this.resetMpBtn = document.getElementById('reset-mp-btn');

    this.trackingDot = document.getElementById('tracking-dot');
    this.trackingStatus = document.getElementById('tracking-status');
    this.resolutionReadout = document.getElementById('resolution-readout');
    this.fpsReadout = document.getElementById('fps-readout');

    this.fitSelect.value = this.prefs.fitMode || 'cover';
    this.bindEvents();
  }

  bindEvents() {
    // Détection secrète de "vn" au clavier
    window.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      if (e.key === 'Escape' && this.isOpen) {
        this.close();
        return;
      }

      const tag = document.activeElement?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

      if (e.key.length === 1) {
        const now = performance.now();
        if (now - this.lastKeyTime > 1500) {
          this.keyBuffer = '';
        }
        this.lastKeyTime = now;
        this.keyBuffer = (this.keyBuffer + e.key.toLowerCase()).slice(-4);

        if (this.keyBuffer.endsWith('vn')) {
          this.keyBuffer = '';
          this.toggle();
        }
      }
    });

    this.closeBtn.addEventListener('click', () => this.close());

    this.cameraSelect.addEventListener('change', () => {
      if (this.cameraSelect.value) {
        this.onCommand({ cmd: 'setCamera', value: this.cameraSelect.value });
      }
    });

    this.refreshBtn.addEventListener('click', () => {
      this.onCommand({ cmd: 'refreshState' });
    });

    this.resolutionSelect.addEventListener('change', () => {
      const [w, h] = this.resolutionSelect.value.split('x').map(Number);
      if (w && h) {
        this.onCommand({ cmd: 'setResolution', width: w, height: h });
      }
    });

    this.fitSelect.addEventListener('change', () => {
      this.prefs.fitMode = this.fitSelect.value;
      saveLocalPrefs(this.prefs);
      this.onFitChange(this.prefs.fitMode);
    });

    this.fpsSelect.addEventListener('change', () => {
      const fps = Number(this.fpsSelect.value) || 30;
      this.onCommand({ cmd: 'setStreamFps', fps });
    });

    this.mirrorToggle.addEventListener('change', () => {
      this.onCommand({ cmd: 'setMirror', value: this.mirrorToggle.checked });
    });

    this.resetParticlesBtn.addEventListener('click', () => {
      this.onCommand({ cmd: 'resetParticles' });
    });

    this.resetMpBtn.addEventListener('click', () => {
      this.onCommand({ cmd: 'resetMediaPipe' });
    });

    this.fullscreenBtn.addEventListener('click', async () => {
      try {
        if (!document.fullscreenElement) {
          await document.documentElement.requestFullscreen();
        } else {
          await document.exitFullscreen();
        }
      } catch (err) {
        console.warn('[VNOverlay] Plein écran refusé:', err);
      }
    });

    document.addEventListener('fullscreenchange', () => {
      this.fullscreenLabel.textContent = document.fullscreenElement
        ? 'Quitter plein écran'
        : 'Plein écran';
    });
  }

  open() {
    this.isOpen = true;
    this.overlay.classList.remove('hidden');
    this.overlay.setAttribute('aria-hidden', 'false');
    this.onCommand({ cmd: 'refreshState' });
  }

  close() {
    this.isOpen = false;
    if (this.overlay.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    this.overlay.classList.add('hidden');
    this.overlay.setAttribute('aria-hidden', 'true');
  }

  toggle() {
    if (this.isOpen) this.close();
    else this.open();
  }

  setDisconnected() {
    this.trackingDot.className = 'status-dot error';
    this.trackingStatus.textContent = 'En attente de TouchDesigner…';
    this.fpsReadout.textContent = '-- FPS';
  }

  syncFromTDState(state, clientFps) {
    // Met à jour la liste des caméras uniquement si elle a changé (évite de fermer le dropdown ouvert)
    const cameras = state.cameras || [];
    const sig = JSON.stringify(cameras) + '|' + (state.camera || '');
    if (sig !== this.lastCamerasSig && document.activeElement !== this.cameraSelect) {
      this.lastCamerasSig = sig;
      this.cameraSelect.innerHTML = '';

      if (cameras.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'Aucune caméra détectée dans TD';
        this.cameraSelect.appendChild(opt);
      } else {
        cameras.forEach((cam) => {
          const opt = document.createElement('option');
          opt.value = cam.value;
          opt.textContent = cam.label;
          if (cam.value === state.camera || cam.label === state.camera) {
            opt.selected = true;
          }
          this.cameraSelect.appendChild(opt);
        });
      }
    }

    if (document.activeElement !== this.mirrorToggle) {
      this.mirrorToggle.checked = Boolean(state.mirror);
    }

    if (document.activeElement !== this.resolutionSelect && state.resolution) {
      const exists = Array.from(this.resolutionSelect.options).some(
        (o) => o.value === state.resolution
      );
      if (exists) {
        this.resolutionSelect.value = state.resolution;
      }
    }

    if (document.activeElement !== this.fpsSelect && state.streamFpsTarget) {
      const targetStr = String(state.streamFpsTarget);
      if (Array.from(this.fpsSelect.options).some((o) => o.value === targetStr)) {
        this.fpsSelect.value = targetStr;
      }
    }

    this.trackingDot.className = 'status-dot';
    if (state.gestureMode === 'envers') {
      this.trackingDot.classList.add('envers');
    } else if (state.gestureMode === 'endroit') {
      this.trackingDot.classList.add('endroit');
    } else {
      this.trackingDot.classList.add('ready');
    }

    this.trackingStatus.textContent = `TD actif · ${state.gesture || 'Repos'}`;
    this.resolutionReadout.textContent =
      state.topWidth && state.topHeight ? `${state.topWidth}×${state.topHeight}` : '—';
    const shownFps = Math.round(clientFps || state.streamFps || 0);
    this.fpsReadout.textContent = `${shownFps} FPS`;
  }
}
