"""
Pont Web temps réel haute performance exécuté dans TouchDesigner.
- Diffuse /project1/comp1 en WebRTC natif GPU (60 FPS, latence minimale, 0 blocage CPU).
- Inclut un fallback JPEG asynchrone (numpyArray(delayed=True) + cv2 thread) si WebRTC échoue.
- Sert l'application web compilée (web-app/dist) sur http://127.0.0.1:9980.
- Pilote les paramètres réels de /project1/MediaPipe, /project1/particle1 et /project1/feedback.
"""

import json
import mimetypes
import queue
import threading
import time
import traceback
from pathlib import Path

import td
from td import *

try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

BRIDGE_PORT = 9980
BASE_DIR = Path(__file__).resolve().parent
WEB_APP_DIR = BASE_DIR.parent
DIST_DIR = WEB_APP_DIR / "dist"
LOG_PATH = BASE_DIR / "bridge.log"

# État partagé du bridge
_clients = {}  # ws_client -> {"mode": "webrtc"|"jpeg", "conn_id": str|None, "in_flight": int, "last_send": float}
_conn_to_client = {}  # webrtc connectionId -> ws_client
_last_state_sent = 0.0
_stream_fps_target = 60.0

# Fallback JPEG asynchrone (actif uniquement si un client bascule en mode "jpeg")
_jpeg_in_queue = queue.Queue(maxsize=1)
_jpeg_out_queue = queue.Queue(maxsize=2)
_jpeg_worker_started = False
_last_jpeg_grab = 0.0


def _log(msg):
    try:
        ts = time.strftime("%H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _get_mediapipe():
    return td.op("/project1/MediaPipe")


def _get_output_top():
    comp = td.op("/project1/comp1")
    if comp is not None:
        return comp
    return td.op("/project1/out1")


def _get_bridge():
    return td.op("/project1/web_bridge")


def _get_ws():
    return td.op("/project1/web_bridge/ws")


def _get_webrtc():
    return td.op("/project1/web_bridge/webrtc1")


def _get_vstream():
    return td.op("/project1/web_bridge/vstream")


def _safe_chop_val(op_path, chan_name="ty", default=0.0):
    try:
        ch = td.op(op_path)
        if ch is None:
            return default
        c = ch[chan_name]
        if c is not None:
            return float(c.eval())
        if ch.numChans > 0:
            return float(ch[0].eval())
    except Exception:
        pass
    return default


def _get_cameras(mp):
    cameras = []
    current = ""
    if mp is None:
        return cameras, current

    try:
        par = mp.par.Webcam
        names = list(par.menuNames) if par.menuNames else []
        labels = list(par.menuLabels) if par.menuLabels else []
        current = str(par.eval())

        seen = set()
        for i, name in enumerate(names):
            lbl = labels[i] if i < len(labels) else name
            if not name and not lbl:
                continue
            key = str(name)
            if key in seen:
                continue
            seen.add(key)
            cameras.append({"value": key, "label": str(lbl)})

        if not cameras:
            wl = mp.op("webcam_list")
            if wl is not None and wl.text:
                raw = json.loads(wl.text)
                if isinstance(raw, list):
                    for item in raw:
                        lbl = item.get("label") if isinstance(item, dict) else str(item)
                        if lbl and lbl not in seen:
                            seen.add(lbl)
                            cameras.append({"value": lbl, "label": lbl})
    except Exception as e:
        _log(f"Erreur lecture caméras: {e}")

    return cameras, current


def build_state_payload():
    mp = _get_mediapipe()
    out_top = _get_output_top()
    cameras, current_cam = _get_cameras(mp)

    mirror = False
    res_w = 1280
    res_h = 720
    if mp is not None:
        try:
            mirror = bool(mp.par.Wflip.eval())
        except Exception:
            pass
        try:
            res_w = int(mp.par.Resolutionw.eval())
            res_h = int(mp.par.Resolutionh.eval())
        except Exception:
            pass

    lag_endroit = _safe_chop_val("/project1/lag_tache", "ty", 0.0)
    lag_envers = _safe_chop_val("/project1/lag_tache1", "ty", 0.0)
    if lag_envers > 0.25:
        gesture = "L à l'envers (Couleur)"
        gesture_mode = "envers"
    elif lag_endroit > 0.25:
        gesture = "L à l'endroit (N&B)"
        gesture_mode = "endroit"
    else:
        gesture = "Repos"
        gesture_mode = "idle"

    td_fps = 60.0
    try:
        td_fps = float(td.project.cookRate)
    except Exception:
        pass

    top_w = int(out_top.width) if out_top is not None else 1280
    top_h = int(out_top.height) if out_top is not None else 720

    return {
        "type": "state",
        "connected": True,
        "cameras": cameras,
        "camera": current_cam,
        "mirror": mirror,
        "resolution": f"{res_w}x{res_h}",
        "streamFpsTarget": int(_stream_fps_target),
        "tdFps": round(td_fps, 1),
        "topWidth": top_w,
        "topHeight": top_h,
        "gesture": gesture,
        "gestureMode": gesture_mode,
        "lagEndroit": round(lag_endroit, 3),
        "lagEnvers": round(lag_envers, 3),
    }


def _broadcast_state(ws_dat):
    if not _clients or ws_dat is None:
        return
    try:
        payload = json.dumps(build_state_payload())
        for cid in list(_clients.keys()):
            try:
                ws_dat.webSocketSendText(cid, payload)
            except Exception:
                _cleanup_client(cid)
    except Exception as e:
        _log(f"Erreur broadcast state: {e}")


# ==============================================================================
# WebRTC Natif GPU (Video Stream Out TOP + WebRTC DAT)
# ==============================================================================

def _close_webrtc_conn(conn_id):
    if not conn_id:
        return
    _conn_to_client.pop(conn_id, None)
    try:
        wrtc = _get_webrtc()
        if wrtc is not None:
            wrtc.closeConnection(conn_id)
    except Exception as e:
        _log(f"closeConnection({conn_id}) warn: {e}")


def _start_webrtc_for_client(ws_client):
    wrtc = _get_webrtc()
    vstream = _get_vstream()
    if wrtc is None or vstream is None:
        _log("WebRTC indisponible, bascule JPEG")
        return False

    try:
        # Ferme toute ancienne connexion WebRTC pour garder un seul flux GPU propre et rapide
        for old_conn in list(_conn_to_client.keys()):
            _close_webrtc_conn(old_conn)

        conn_id = wrtc.openConnection()
        if not conn_id:
            _log("openConnection() a retourné vide")
            return False

        _conn_to_client[conn_id] = ws_client
        info = _clients.get(ws_client)
        if info is not None:
            info["conn_id"] = conn_id
            info["mode"] = "webrtc"

        # Ajoute la piste vidéo sortante et relie Video Stream Out TOP dessus
        wrtc.addTrack(conn_id, "TDVideo", "video")

        vstream.par.active = False
        vstream.par.mode = "webrtc"
        vstream.par.fps = int(_stream_fps_target)
        vstream.par.webrtc = "webrtc1"
        vstream.par.webrtcconnection = conn_id
        vstream.par.webrtcvideotrack = "TDVideo"
        vstream.par.active = True

        wrtc.createOffer(conn_id)
        _log(f"WebRTC offer lancée pour {ws_client} (conn={conn_id})")
        return True
    except Exception:
        _log("Erreur _start_webrtc_for_client:\n" + traceback.format_exc())
        return False


def _boost_sdp_bitrate(sdp, min_kbps=15000, start_kbps=30000, max_kbps=50000):
    """
    Augmente fortement le bitrate WebRTC (15–50 Mbps sur localhost 127.0.0.1)
    uniquement sur les vrais codecs vidéo (H264, VP8, VP9, AV1) pour une image
    ultra-nette même avec 16 384 particules + feedback actifs.
    """
    if not sdp:
        return sdp
    lines = sdp.splitlines()

    # 1) Repère les payload types des vrais codecs vidéo (ignore rtx, red, ulpfec)
    codec_pts = set()
    in_video = False
    for line in lines:
        if line.startswith("m=video "):
            in_video = True
        elif line.startswith("m="):
            in_video = False
        elif in_video and line.startswith("a=rtpmap:"):
            rest = line[9:]
            parts = rest.split(" ", 1)
            if len(parts) == 2:
                pt, codec = parts[0], parts[1].upper()
                if any(codec.startswith(c) for c in ("H264/", "VP8/", "VP9/", "AV1/")):
                    codec_pts.add(pt)

    # 2) Injecte b=AS / b=TIAS + x-google-*-bitrate sur ces codecs uniquement
    out = []
    in_video = False
    fmtp_seen = set()

    def _flush_missing_fmtp():
        for pt in sorted(codec_pts):
            if pt not in fmtp_seen:
                out.append(
                    f"a=fmtp:{pt} x-google-min-bitrate={min_kbps};"
                    f"x-google-start-bitrate={start_kbps};"
                    f"x-google-max-bitrate={max_kbps}"
                )

    for line in lines:
        if line.startswith("m=video "):
            in_video = True
            out.append(line)
            continue
        elif line.startswith("m="):
            if in_video:
                _flush_missing_fmtp()
            in_video = False
            out.append(line)
            continue

        if in_video:
            if line.startswith("b=AS:") or line.startswith("b=TIAS:"):
                continue
            if line.startswith("c=IN "):
                out.append(line)
                out.append(f"b=AS:{max_kbps}")
                out.append(f"b=TIAS:{max_kbps * 1000}")
                continue
            if line.startswith("a=fmtp:"):
                pt = line[7:].split(" ", 1)[0]
                if pt in codec_pts:
                    fmtp_seen.add(pt)
                    if "x-google-max-bitrate" not in line:
                        line = (
                            f"{line};x-google-min-bitrate={min_kbps};"
                            f"x-google-start-bitrate={start_kbps};"
                            f"x-google-max-bitrate={max_kbps}"
                        )
                out.append(line)
                continue

        out.append(line)

    if in_video:
        _flush_missing_fmtp()

    return "\r\n".join(out) + "\r\n"


def on_webrtc_offer(webrtcDAT, connectionId, localSdp):
    try:
        webrtcDAT.setLocalDescription(connectionId, "offer", localSdp)
        boosted = _boost_sdp_bitrate(localSdp)
        ws_client = _conn_to_client.get(connectionId)
        ws_dat = _get_ws()
        if ws_client and ws_dat:
            ws_dat.webSocketSendText(
                ws_client,
                json.dumps({"type": "webrtc_offer", "sdp": boosted}),
            )
            _log(f"WebRTC offer haute qualité (25-45 Mbps) envoyée à {ws_client}")
    except Exception:
        _log("Erreur on_webrtc_offer:\n" + traceback.format_exc())


def on_webrtc_ice(webrtcDAT, connectionId, candidate, lineIndex, sdpMid):
    try:
        ws_client = _conn_to_client.get(connectionId)
        ws_dat = _get_ws()
        if ws_client and ws_dat and candidate:
            ws_dat.webSocketSendText(
                ws_client,
                json.dumps({
                    "type": "webrtc_ice",
                    "candidate": candidate,
                    "sdpMLineIndex": lineIndex,
                    "sdpMid": sdpMid,
                }),
            )
    except Exception:
        pass


def on_webrtc_ice_state(webrtcDAT, connectionId, state):
    _log(f"WebRTC ICE state ({connectionId}): {state}")


def on_webrtc_conn_state(webrtcDAT, connectionId, state):
    _log(f"WebRTC Connection state ({connectionId}): {state}")


# ==============================================================================
# Fallback JPEG Asynchrone (thread OpenCV hors thread principal TD)
# ==============================================================================

def _jpeg_worker_loop():
    while True:
        frame = _jpeg_in_queue.get()
        if frame is None:
            continue
        try:
            # TD numpyArray est RGBA float32 ou uint8 inversé verticalement
            if frame.dtype != np.uint8:
                img = np.clip(frame * 255.0, 0, 255).astype(np.uint8)
            else:
                img = frame
            bgr = cv2.cvtColor(np.flipud(img), cv2.COLOR_RGBA2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 76])
            if ok:
                raw = buf.tobytes()
                if _jpeg_out_queue.full():
                    try:
                        _jpeg_out_queue.get_nowait()
                    except Exception:
                        pass
                _jpeg_out_queue.put_nowait(raw)
        except Exception as e:
            _log(f"Erreur worker JPEG: {e}")


def _ensure_jpeg_worker():
    global _jpeg_worker_started
    if _HAS_CV2 and not _jpeg_worker_started:
        t = threading.Thread(target=_jpeg_worker_loop, daemon=True)
        t.start()
        _jpeg_worker_started = True


def _cleanup_client(ws_client):
    info = _clients.pop(ws_client, None)
    if info and info.get("conn_id"):
        _close_webrtc_conn(info["conn_id"])


def on_frame_end():
    global _last_state_sent, _last_jpeg_grab

    if not _clients:
        return

    ws_dat = _get_ws()
    if ws_dat is None or not ws_dat.par.active.eval():
        return

    now = time.monotonic()

    # Envoi léger de l'état JSON (2 Hz)
    if now - _last_state_sent >= 0.5:
        _last_state_sent = now
        _broadcast_state(ws_dat)

    # Si tous les clients sont en WebRTC natif, ZERO travail CPU sur le thread TD !
    jpeg_clients = [
        cid for cid, info in list(_clients.items()) if info.get("mode") == "jpeg"
    ]
    if not jpeg_clients:
        return

    # Mode fallback JPEG asynchrone uniquement
    _ensure_jpeg_worker()

    # 1) Envoie les frames déjà encodées par le thread OpenCV
    try:
        while not _jpeg_out_queue.empty():
            raw = _jpeg_out_queue.get_nowait()
            for cid in jpeg_clients:
                info = _clients.get(cid)
                if info is None:
                    continue
                if info["in_flight"] >= 2 and (now - info.get("last_send", 0.0)) > 1.0:
                    info["in_flight"] = 0
                if info["in_flight"] < 2:
                    ws_dat.webSocketSendBinary(cid, raw)
                    info["in_flight"] += 1
                    info["last_send"] = now
    except Exception:
        pass

    # 2) Capture GPU non-bloquante (delayed=True -> 0 stall GPU)
    min_interval = 1.0 / max(15.0, float(_stream_fps_target))
    if now - _last_jpeg_grab >= min_interval and not _jpeg_in_queue.full():
        out_top = _get_output_top()
        if out_top is not None and _HAS_CV2:
            try:
                arr = out_top.numpyArray(delayed=True, writable=False)
                if arr is not None:
                    _jpeg_in_queue.put_nowait(arr.copy())
                    _last_jpeg_grab = now
            except Exception:
                pass


# ==============================================================================
# Callbacks du Web Server DAT (/project1/web_bridge/ws)
# ==============================================================================

def on_http_request(webServerDAT, request, response):
    uri = request.get("uri", "/") or "/"
    method = request.get("method", "GET")

    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Content-Type"

    if method == "OPTIONS":
        response["statusCode"] = 204
        response["statusReason"] = "No Content"
        response["data"] = ""
        return response

    if uri == "/api/state":
        response["statusCode"] = 200
        response["statusReason"] = "OK"
        response["Content-Type"] = "application/json; charset=utf-8"
        response["data"] = json.dumps(build_state_payload())
        return response

    rel = uri.lstrip("/")
    if rel == "":
        rel = "index.html"

    try:
        target = (DIST_DIR / rel).resolve()
        if not str(target).startswith(str(DIST_DIR.resolve())):
            response["statusCode"] = 403
            response["statusReason"] = "Forbidden"
            response["data"] = "Forbidden"
            return response

        if target.is_file():
            mime, _ = mimetypes.guess_type(target.name)
            if target.suffix == ".js":
                mime = "application/javascript; charset=utf-8"
            elif target.suffix == ".css":
                mime = "text/css; charset=utf-8"
            elif target.suffix == ".html":
                mime = "text/html; charset=utf-8"
            elif not mime:
                mime = "application/octet-stream"

            response["statusCode"] = 200
            response["statusReason"] = "OK"
            response["Content-Type"] = mime
            response["data"] = target.read_bytes()
            return response
    except Exception as e:
        _log(f"Erreur HTTP {uri}: {e}")

    response["statusCode"] = 404
    response["statusReason"] = "Not Found"
    response["Content-Type"] = "text/plain; charset=utf-8"
    response["data"] = "Not Found"
    return response


def on_ws_open(webServerDAT, client, uri):
    _clients[client] = {
        "mode": "webrtc",
        "conn_id": None,
        "in_flight": 0,
        "last_send": 0.0,
    }
    _log(f"Client WebSocket connecté: {client} ({uri})")
    try:
        webServerDAT.webSocketSendText(client, json.dumps(build_state_payload()))
    except Exception as e:
        _log(f"Erreur envoi état initial: {e}")


def on_ws_close(webServerDAT, client):
    _cleanup_client(client)
    _log(f"Client WebSocket déconnecté: {client}")


def on_ws_text(webServerDAT, client, data):
    global _stream_fps_target

    try:
        msg = json.loads(data)
    except Exception:
        return

    cmd = msg.get("cmd")

    if cmd == "ack":
        info = _clients.get(client)
        if info is not None and info["in_flight"] > 0:
            info["in_flight"] -= 1
        return

    if cmd == "webrtcStart":
        ok = _start_webrtc_for_client(client)
        if not ok:
            info = _clients.get(client)
            if info is not None:
                info["mode"] = "jpeg"
        return

    if cmd == "webrtcAnswer":
        info = _clients.get(client)
        wrtc = _get_webrtc()
        if info and info.get("conn_id") and wrtc is not None:
            try:
                boosted_ans = _boost_sdp_bitrate(msg.get("sdp", ""))
                wrtc.setRemoteDescription(info["conn_id"], "answer", boosted_ans)
                _log(f"WebRTC answer haute qualité appliquée (conn={info['conn_id']})")
            except Exception as e:
                _log(f"Erreur setRemoteDescription(answer): {e}")
        return

    if cmd == "webrtcIce":
        info = _clients.get(client)
        wrtc = _get_webrtc()
        cand = msg.get("candidate", "")
        if info and info.get("conn_id") and wrtc is not None and cand:
            try:
                wrtc.addIceCandidate(
                    info["conn_id"],
                    cand,
                    int(msg.get("sdpMLineIndex", 0)),
                    str(msg.get("sdpMid", "")),
                )
            except Exception as e:
                _log(f"Erreur addIceCandidate: {e}")
        return

    if cmd == "fallbackJpeg":
        info = _clients.get(client)
        if info is not None:
            if info.get("conn_id"):
                _close_webrtc_conn(info["conn_id"])
                info["conn_id"] = None
            info["mode"] = "jpeg"
            _log(f"Client {client} basculé en mode JPEG asynchrone")
        return

    mp = _get_mediapipe()

    if cmd == "setCamera" and mp is not None:
        val = str(msg.get("value", ""))
        if val:
            try:
                par = mp.par.Webcam
                names = list(par.menuNames) if par.menuNames else []
                labels = list(par.menuLabels) if par.menuLabels else []
                if val in names:
                    par.val = val
                elif val in labels:
                    par.val = names[labels.index(val)]
                else:
                    par.val = val
                _log(f"Caméra changée vers: {val}")
            except Exception as e:
                _log(f"Erreur setCamera({val}): {e}")

    elif cmd == "setMirror" and mp is not None:
        try:
            mp.par.Wflip = bool(msg.get("value", False))
            _log(f"Miroir Wflip = {mp.par.Wflip.eval()}")
        except Exception as e:
            _log(f"Erreur setMirror: {e}")

    elif cmd == "setResolution" and mp is not None:
        try:
            w = int(msg.get("width", 1280))
            h = int(msg.get("height", 720))
            mp.par.Resolutionw = w
            mp.par.Resolutionh = h
            _log(f"Résolution MediaPipe = {w}x{h}")
        except Exception as e:
            _log(f"Erreur setResolution: {e}")

    elif cmd == "setStreamFps":
        try:
            _stream_fps_target = max(15.0, min(60.0, float(msg.get("fps", 60))))
            vstream = _get_vstream()
            if vstream is not None:
                vstream.par.fps = int(_stream_fps_target)
            _log(f"Stream FPS cible = {_stream_fps_target}")
        except Exception as e:
            _log(f"Erreur setStreamFps: {e}")

    elif cmd == "resetParticles":
        try:
            p = td.op("/project1/particle1")
            if p is not None:
                p.par.initializepulse.pulse()
                p.par.startpulse.pulse()
            fb = td.op("/project1/feedback")
            if fb is not None:
                fb.par.Reset.pulse()
            _log("Particules & feedback réinitialisés")
        except Exception as e:
            _log(f"Erreur resetParticles: {e}")

    elif cmd == "resetMediaPipe" and mp is not None:
        try:
            mp.par.Reset.pulse()
            _log("MediaPipe réinitialisé")
        except Exception as e:
            _log(f"Erreur resetMediaPipe: {e}")

    elif cmd == "refreshState":
        pass

    _broadcast_state(webServerDAT)


# ==============================================================================
# Initialisation du composant /project1/web_bridge dans TouchDesigner
# ==============================================================================

_WS_CALLBACKS_CODE = f'''import sys
_p = r"{str(BASE_DIR)}"
if _p not in sys.path:
    sys.path.insert(0, _p)
import td_bridge

def onHTTPRequest(webServerDAT, request, response):
    return td_bridge.on_http_request(webServerDAT, request, response)

def onWebSocketOpen(webServerDAT, client, uri):
    td_bridge.on_ws_open(webServerDAT, client, uri)

def onWebSocketClose(webServerDAT, client):
    td_bridge.on_ws_close(webServerDAT, client)

def onWebSocketReceiveText(webServerDAT, client, data):
    td_bridge.on_ws_text(webServerDAT, client, data)

def onWebSocketReceiveBinary(webServerDAT, client, data):
    return

def onWebSocketReceivePing(webServerDAT, client, data):
    webServerDAT.webSocketSendPong(client, data=data)

def onWebSocketReceivePong(webServerDAT, client, data):
    return

def onServerStart(webServerDAT):
    td_bridge._log("Web Server DAT démarré sur le port " + str(webServerDAT.par.port.eval()))

def onServerStop(webServerDAT):
    td_bridge._log("Web Server DAT arrêté")
'''

_WEBRTC_CALLBACKS_CODE = f'''import sys
_p = r"{str(BASE_DIR)}"
if _p not in sys.path:
    sys.path.insert(0, _p)
import td_bridge

def onOffer(webrtcDAT, connectionId, localSdp):
    td_bridge.on_webrtc_offer(webrtcDAT, connectionId, localSdp)

def onAnswer(webrtcDAT, connectionId, localSdp):
    return

def onIceCandidate(webrtcDAT, connectionId, candidate, lineIndex, sdpMid):
    td_bridge.on_webrtc_ice(webrtcDAT, connectionId, candidate, lineIndex, sdpMid)

def onIceConnectionStateChange(webrtcDAT, connectionId, state):
    td_bridge.on_webrtc_ice_state(webrtcDAT, connectionId, state)

def onConnectionStateChange(webrtcDAT, connectionId, state):
    td_bridge.on_webrtc_conn_state(webrtcDAT, connectionId, state)
'''

_FRAME_EXEC_CODE = f'''import sys
_p = r"{str(BASE_DIR)}"
if _p not in sys.path:
    sys.path.insert(0, _p)
import td_bridge

def onFrameEnd(frame):
    td_bridge.on_frame_end()
'''


def boot():
    try:
        try:
            LOG_PATH.write_text("", encoding="utf-8")
        except Exception:
            pass

        _log("Démarrage du pont TouchDesigner WebRTC 60 FPS...")

        bridge = td.op("/project1/web_bridge")
        if bridge is None:
            bridge = td.op("/project1").create(baseCOMP, "web_bridge")
            bridge.nodeX = 3650
            bridge.nodeY = -200

        # 1) Select TOP sur /project1/comp1 -> Video Stream Out TOP (WebRTC GPU)
        sel = bridge.op("select_comp1")
        if sel is None:
            sel = bridge.create(selectTOP, "select_comp1")
            sel.nodeX = -250
            sel.nodeY = 120
        sel.par.top = "/project1/comp1"

        wrtc_cb = bridge.op("webrtc_callbacks")
        if wrtc_cb is None:
            wrtc_cb = bridge.create(textDAT, "webrtc_callbacks")
            wrtc_cb.nodeX = 0
            wrtc_cb.nodeY = -120
        wrtc_cb.text = _WEBRTC_CALLBACKS_CODE

        wrtc = bridge.op("webrtc1")
        if wrtc is None:
            wrtc = bridge.create(webrtcDAT, "webrtc1")
            wrtc.nodeX = 0
            wrtc.nodeY = 0
        wrtc.par.active = False
        wrtc.par.callbacks = "webrtc_callbacks"
        wrtc.par.stun = ""
        wrtc.par.active = True

        vstream = bridge.op("vstream")
        if vstream is None:
            vstream = bridge.create(videostreamoutTOP, "vstream")
            vstream.nodeX = 0
            vstream.nodeY = 120
        if len(vstream.inputConnectors[0].connections) == 0:
            sel.outputConnectors[0].connect(vstream.inputConnectors[0])
        vstream.par.active = False
        vstream.par.mode = "webrtc"
        vstream.par.fps = int(_stream_fps_target)
        vstream.par.webrtc = "webrtc1"
        vstream.par.webrtcvideotrack = "TDVideo"

        # Logue les paramètres de Video Stream Out TOP et pousse le bitrate au max si dispo
        try:
            pnames = [p.name for p in vstream.pars()]
            _log("vstream pars: " + ", ".join(pnames))
            for pname in ("avgbitrate", "peakbitrate", "bitrate", "vbitrate", "maxbitrate"):
                if hasattr(vstream.par, pname):
                    p = getattr(vstream.par, pname)
                    # En kbps (25000..45000) ou Mbps selon la plage du paramètre
                    if getattr(p, "normMax", 100000) > 1000:
                        p.val = 30000
                    else:
                        p.val = 30
                    _log(f"vstream.par.{pname} = {p.eval()}")
        except Exception as e:
            _log(f"Info vstream pars: {e}")

        # 2) Web Server DAT (HTTP + WebSocket signalisation & contrôle)
        cb = bridge.op("ws_callbacks")
        if cb is None:
            cb = bridge.create(textDAT, "ws_callbacks")
            cb.nodeX = 280
            cb.nodeY = -120
        cb.text = _WS_CALLBACKS_CODE

        ws = bridge.op("ws")
        if ws is None:
            ws = bridge.create(webserverDAT, "ws")
            ws.nodeX = 280
            ws.nodeY = 0
        ws.par.active = False
        ws.par.port = BRIDGE_PORT
        ws.par.callbacks = "ws_callbacks"
        ws.par.active = True
        if hasattr(ws.par, "restart"):
            ws.par.restart.pulse()

        fexec = bridge.op("frame_exec")
        if fexec is None:
            fexec = bridge.create(executeDAT, "frame_exec")
            fexec.nodeX = 280
            fexec.nodeY = 120
        fexec.text = _FRAME_EXEC_CODE
        fexec.par.frameend = True

        try:
            td.project.realTime = True
            td.project.cookRate = 60
        except Exception:
            pass

        _log(f"Pont WebRTC prêt sur http://127.0.0.1:{BRIDGE_PORT}")
    except Exception:
        _log("ERREUR FATALE boot():\n" + traceback.format_exc())
