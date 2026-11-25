/**
 * Client temps réel vers TouchDesigner (/project1/web_bridge/ws).
 * - Établit une connexion WebRTC directe GPU 60 FPS (latence minimale, décodage matériel <video>).
 * - Bascule automatiquement sur un flux JPEG asynchrone si WebRTC ne s'établit pas sous 3,5 s.
 */
export class TouchDesignerClient {
  constructor({ videoEl, onWebRTCStream, onJpegFrame, onState, onConnectionChange }) {
    this.videoEl = videoEl;
    this.onWebRTCStream = onWebRTCStream;
    this.onJpegFrame = onJpegFrame;
    this.onState = onState;
    this.onConnectionChange = onConnectionChange;

    this.ws = null;
    this.pc = null;
    this.connected = false;
    this.reconnectTimer = null;
    this.webrtcTimeout = null;
    this.transport = 'webrtc'; // 'webrtc' | 'jpeg'

    // Fallback JPEG
    this.decoding = false;
    this.pendingBlob = null;

    // Mesure FPS réel décodé côté navigateur
    this.frameCount = 0;
    this.fpsWindowStart = performance.now();
    this.clientFps = 0;

    this.startVideoFpsMonitor();
  }

  startVideoFpsMonitor() {
    if (!this.videoEl || typeof this.videoEl.requestVideoFrameCallback !== 'function') return;
    const onVideoFrame = (now) => {
      if (this.transport === 'webrtc' && this.videoEl.readyState >= 2) {
        this.frameCount++;
        const elapsed = (now - this.fpsWindowStart) / 1000;
        if (elapsed >= 0.75) {
          this.clientFps = this.frameCount / elapsed;
          this.frameCount = 0;
          this.fpsWindowStart = now;
        }
      }
      this.videoEl.requestVideoFrameCallback(onVideoFrame);
    };
    this.videoEl.requestVideoFrameCallback(onVideoFrame);
  }

  getWebSocketUrl() {
    const params = new URLSearchParams(window.location.search);
    const customTd = params.get('td');
    if (customTd) {
      return customTd;
    }

    const host = window.location.hostname || '127.0.0.1';
    const isLocalHost =
      host === '127.0.0.1' ||
      host === 'localhost' ||
      host === '[::1]' ||
      /^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host);

    if (window.location.port === '9980' && isLocalHost) {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${proto}//${window.location.host}/`;
    }

    // Depuis GitHub Pages (HTTPS) ou Vite dev, on cible TouchDesigner en local sur 127.0.0.1:9980
    return `ws://${isLocalHost ? host : '127.0.0.1'}:9980/`;
  }

  closePeerConnection() {
    if (this.webrtcTimeout) {
      clearTimeout(this.webrtcTimeout);
      this.webrtcTimeout = null;
    }
    if (this.pc) {
      try {
        this.pc.close();
      } catch {
        // ignore
      }
      this.pc = null;
    }
  }

  connect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.closePeerConnection();

    const url = this.getWebSocketUrl();
    try {
      this.ws = new WebSocket(url);
      this.ws.binaryType = 'blob';
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.addEventListener('open', () => {
      this.connected = true;
      this.transport = 'webrtc';
      this.onConnectionChange(true);

      // Démarre la négociation WebRTC 60 FPS dès la connexion WebSocket
      this.send({ cmd: 'webrtcStart' });

      // Si aucune piste vidéo WebRTC n'arrive sous 4s, bascule sur JPEG asynchrone
      this.webrtcTimeout = setTimeout(() => {
        if (!this.videoEl?.srcObject || this.videoEl.readyState < 2) {
          console.warn('[TDClient] WebRTC lent/indisponible -> bascule JPEG asynchrone');
          this.transport = 'jpeg';
          this.closePeerConnection();
          this.send({ cmd: 'fallbackJpeg' });
        }
      }, 4000);
    });

    this.ws.addEventListener('close', () => {
      this.connected = false;
      this.closePeerConnection();
      this.onConnectionChange(false);
      this.scheduleReconnect();
    });

    this.ws.addEventListener('error', () => {});

    this.ws.addEventListener('message', (event) => {
      if (typeof event.data === 'string') {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }
        if (msg.type === 'state') {
          this.onState(msg);
        } else if (msg.type === 'webrtc_offer') {
          this.handleWebRTCOffer(msg.sdp);
        } else if (msg.type === 'webrtc_ice') {
          this.handleWebRTCIce(msg);
        }
        return;
      }

      if (event.data instanceof Blob) {
        this.handleBinaryFrame(event.data);
      }
    });
  }

  async handleWebRTCOffer(sdp) {
    try {
      this.closePeerConnection();
      // Localhost 127.0.0.1 : pas besoin de STUN externe -> connexion directe < 10ms
      const pc = new RTCPeerConnection({ iceServers: [] });
      this.pc = pc;

      pc.ontrack = (event) => {
        if (this.webrtcTimeout) {
          clearTimeout(this.webrtcTimeout);
          this.webrtcTimeout = null;
        }
        this.transport = 'webrtc';
        const stream =
          event.streams && event.streams[0]
            ? event.streams[0]
            : new MediaStream([event.track]);
        this.onWebRTCStream(stream);
      };

      pc.onicecandidate = (event) => {
        if (event.candidate && event.candidate.candidate) {
          this.send({
            cmd: 'webrtcIce',
            candidate: event.candidate.candidate,
            sdpMLineIndex: event.candidate.sdpMLineIndex ?? 0,
            sdpMid: event.candidate.sdpMid ?? '',
          });
        }
      };

      pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'failed') {
          this.transport = 'jpeg';
          this.closePeerConnection();
          this.send({ cmd: 'fallbackJpeg' });
        }
      };

      await pc.setRemoteDescription({ type: 'offer', sdp });
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      this.send({ cmd: 'webrtcAnswer', sdp: answer.sdp });
    } catch (err) {
      console.warn('[TDClient] Erreur WebRTC offer -> bascule JPEG:', err);
      this.transport = 'jpeg';
      this.closePeerConnection();
      this.send({ cmd: 'fallbackJpeg' });
    }
  }

  async handleWebRTCIce(msg) {
    if (!this.pc || !msg.candidate) return;
    try {
      await this.pc.addIceCandidate({
        candidate: msg.candidate,
        sdpMLineIndex: msg.sdpMLineIndex ?? 0,
        sdpMid: msg.sdpMid ?? '',
      });
    } catch {
      // Ignore late/duplicate candidates
    }
  }

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 1000);
  }

  send(payload) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }

  async handleBinaryFrame(blob) {
    if (this.decoding) {
      this.pendingBlob = blob;
      this.send({ cmd: 'ack' });
      return;
    }

    this.decoding = true;
    try {
      const bitmap = await createImageBitmap(blob);
      this.onJpegFrame(bitmap);
      bitmap.close();

      this.frameCount++;
      const now = performance.now();
      const elapsed = (now - this.fpsWindowStart) / 1000;
      if (elapsed >= 0.75) {
        this.clientFps = this.frameCount / elapsed;
        this.frameCount = 0;
        this.fpsWindowStart = now;
      }
    } catch {
      // Ignore transient frame decode error
    } finally {
      this.decoding = false;
      this.send({ cmd: 'ack' });

      if (this.pendingBlob) {
        const next = this.pendingBlob;
        this.pendingBlob = null;
        this.handleBinaryFrame(next);
      }
    }
  }
}
