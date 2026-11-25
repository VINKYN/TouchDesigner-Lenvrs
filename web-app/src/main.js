import './style.css';
import { TouchDesignerClient } from './tdClient.js';
import { VNSettingsOverlay, loadLocalPrefs } from './settingsOverlay.js';

const videoEl = document.getElementById('td-video');
const canvas = document.getElementById('td-canvas');
const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
const waitingEl = document.getElementById('td-waiting');
const waitingTitle = document.getElementById('waiting-title');
const waitingMessage = document.getElementById('waiting-message');

const prefs = loadLocalPrefs();
let fitMode = prefs.fitMode || 'cover';

function applyFitMode(mode) {
  fitMode = mode;
  videoEl.classList.toggle('fit-cover', mode !== 'contain');
  videoEl.classList.toggle('fit-contain', mode === 'contain');
}

applyFitMode(fitMode);

function resizeCanvas() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(1, Math.floor(window.innerWidth * dpr));
  const h = Math.max(1, Math.floor(window.innerHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
}

resizeCanvas();
window.addEventListener('resize', resizeCanvas);

function drawJpegFrame(bitmap) {
  videoEl.classList.add('hidden');
  canvas.classList.remove('hidden');
  waitingEl.classList.add('hidden');

  const cw = canvas.width;
  const ch = canvas.height;
  const bw = bitmap.width;
  const bh = bitmap.height;

  const canvasAspect = cw / ch;
  const imgAspect = bw / bh;

  let drawW;
  let drawH;
  if (fitMode === 'contain') {
    if (canvasAspect > imgAspect) {
      drawH = ch;
      drawW = ch * imgAspect;
    } else {
      drawW = cw;
      drawH = cw / imgAspect;
    }
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cw, ch);
  } else {
    if (canvasAspect > imgAspect) {
      drawW = cw;
      drawH = cw / imgAspect;
    } else {
      drawH = ch;
      drawW = ch * imgAspect;
    }
  }

  ctx.drawImage(bitmap, (cw - drawW) * 0.5, (ch - drawH) * 0.5, drawW, drawH);
}

const vnOverlay = new VNSettingsOverlay({
  prefs,
  onCommand: (cmd) => client.send(cmd),
  onFitChange: (newFit) => applyFitMode(newFit),
});

const client = new TouchDesignerClient({
  videoEl,
  onWebRTCStream: (stream) => {
    canvas.classList.add('hidden');
    videoEl.classList.remove('hidden');
    videoEl.srcObject = stream;
    videoEl.play().catch(() => {});
    waitingEl.classList.add('hidden');
  },
  onJpegFrame: (bitmap) => drawJpegFrame(bitmap),
  onState: (state) => vnOverlay.syncFromTDState(state, client.clientFps),
  onConnectionChange: (connected) => {
    if (connected) {
      waitingTitle.textContent = 'Connecté à TouchDesigner';
      waitingMessage.textContent = 'Initialisation du flux WebRTC 60 FPS…';
    } else {
      videoEl.srcObject = null;
      waitingTitle.textContent = 'Connexion à TouchDesigner…';
      waitingMessage.innerHTML =
        'Lance <code>Lancer-App.bat</code> pour afficher ton projet TouchDesigner en direct.';
      waitingEl.classList.remove('hidden');
      vnOverlay.setDisconnected();
    }
  },
});

// Touches 1 et 2 d'origine du projet TouchDesigner (reset particules)
window.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.key === '1' || e.key === '2') {
    client.send({ cmd: 'resetParticles' });
  }
});

client.connect();
