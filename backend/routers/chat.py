import base64
import hashlib
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from config import settings

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)

# Server-side dedup: {hash: (response, timestamp)}
_recent: dict = {}

# ---------------------------------------------------------------------------
# HTML page — WhatsApp-style chat with microphone support
# ---------------------------------------------------------------------------

_CHAT_HTML = r"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{bot_name}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#e5ddd5;height:100dvh;display:flex;flex-direction:column}}
    #header{{background:#075e54;color:#fff;padding:14px 16px;font-size:17px;font-weight:600;display:flex;align-items:center;gap:10px}}
    #messages{{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:6px}}
    .wrap{{display:flex;flex-direction:column}}
    .bubble{{max-width:78%;padding:8px 12px;border-radius:12px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;font-size:14.5px}}
    .user .bubble{{background:#dcf8c6;align-self:flex-end;border-radius:12px 0 12px 12px}}
    .bot .bubble{{background:#fff;align-self:flex-start;border-radius:0 12px 12px 12px;box-shadow:0 1px 2px rgba(0,0,0,.12)}}
    .user{{align-items:flex-end}}
    .bot{{align-items:flex-start}}
    .ts{{font-size:11px;color:#999;margin-top:2px;padding:0 4px}}
    .typing{{color:#555;font-size:13px;padding:7px 12px;background:#fff;border-radius:12px;align-self:flex-start;box-shadow:0 1px 2px rgba(0,0,0,.08)}}
    #bar{{display:flex;gap:8px;padding:10px 12px;background:#f0f0f0;border-top:1px solid #ddd;align-items:center}}
    #inp{{flex:1;padding:10px 14px;border:none;border-radius:22px;font-size:15px;outline:none;font-family:inherit;background:#fff}}
    .icon-btn{{background:#25d366;color:#fff;border:none;border-radius:50%;width:44px;height:44px;cursor:pointer;font-size:19px;flex-shrink:0;transition:background .2s}}
    .icon-btn:disabled{{background:#aaa;cursor:default}}
    #mic-btn.recording{{background:#e53e3e;animation:pulse 1s infinite}}
    @keyframes pulse{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.1)}}}}
    #overlay{{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:10}}
    #login-box{{background:#fff;border-radius:14px;padding:28px 24px;width:300px;text-align:center}}
    #login-box h2{{margin-bottom:16px;color:#075e54}}
    #login-box input{{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:15px;margin-bottom:12px;text-align:center}}
    #login-box button{{width:100%;padding:11px;background:#25d366;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer;font-weight:600}}
    #err{{color:red;font-size:13px;margin-top:8px;min-height:18px}}
  </style>
</head>
<body>

<div id="overlay">
  <div id="login-box">
    <h2>🤖 {bot_name}</h2>
    <input type="password" id="pwd" placeholder="סיסמה" autocomplete="current-password"
           onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()">כניסה</button>
    <div id="err"></div>
  </div>
</div>

<div id="header"><span>🤖</span>{bot_name}</div>
<div id="messages"></div>
<div id="bar">
  <button id="mic-btn" class="icon-btn" onclick="toggleMic()" title="הקלטה קולית">🎤</button>
  <input id="inp" type="text" placeholder="כתוב הודעה…"
         onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendText()}}">
  <button id="send-btn" class="icon-btn" onclick="sendText()">➤</button>
</div>

<script>
const BOT = '{bot_name}';
let pwd = sessionStorage.getItem('cp') || '';
if (pwd) document.getElementById('overlay').style.display = 'none';

// ---------------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------------
async function login() {{
  pwd = document.getElementById('pwd').value;
  const r = await fetch('/chat/send', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{message:'שלום', password:pwd}})
  }});
  if (r.ok) {{
    sessionStorage.setItem('cp', pwd);
    document.getElementById('overlay').style.display = 'none';
    const d = await r.json();
    addBubble('bot', d.response, d.audio);
    document.getElementById('inp').focus();
  }} else {{
    document.getElementById('err').textContent = 'סיסמה שגויה';
    pwd = '';
  }}
}}

// ---------------------------------------------------------------------------
// Message bubbles
// ---------------------------------------------------------------------------
function addBubble(role, text, audioB64) {{
  const wrap = document.createElement('div');
  wrap.className = `wrap ${{role}}`;
  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  wrap.appendChild(b);

  // 🔊 button on bot messages — click to play, not auto-play
  if (role === 'bot') {{
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:2px';
    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = new Date().toLocaleTimeString('he-IL', {{hour:'2-digit',minute:'2-digit'}});
    const speakBtn = document.createElement('button');
    speakBtn.textContent = '🔊';
    speakBtn.title = 'השמע';
    speakBtn.style.cssText = 'background:none;border:none;cursor:pointer;font-size:14px;padding:0 2px;opacity:.7';
    if (audioB64) {{
      speakBtn.dataset.audio = audioB64;
      speakBtn.onclick = () => playAudio(speakBtn.dataset.audio);
    }} else {{
      speakBtn.onclick = () => requestAudio(text, speakBtn);
    }}
    row.appendChild(ts);
    row.appendChild(speakBtn);
    wrap.appendChild(row);
  }} else {{
    const ts = document.createElement('div');
    ts.className = 'ts';
    ts.textContent = new Date().toLocaleTimeString('he-IL', {{hour:'2-digit',minute:'2-digit'}});
    wrap.appendChild(ts);
  }}

  document.getElementById('messages').appendChild(wrap);
  b.scrollIntoView({{block:'end'}});
  return b;
}}

function playAudio(b64) {{
  try {{
    const audio = new Audio(`data:audio/ogg;base64,${{b64}}`);
    audio.play().catch(() => {{
      new Audio(`data:audio/mpeg;base64,${{b64}}`).play().catch(e => console.warn('Audio:', e));
    }});
  }} catch(e) {{ console.warn('Audio init:', e); }}
}}

async function requestAudio(text, btn) {{
  btn.disabled = true;
  btn.textContent = '⏳';
  try {{
    const r = await fetch('/chat/speak', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{text, password: pwd}})
    }});
    const d = await r.json();
    if (d.audio) {{
      btn.dataset.audio = d.audio;
      btn.textContent = '🔊';
      btn.disabled = false;
      playAudio(d.audio);
    }}
  }} catch(e) {{
    btn.textContent = '❌';
  }}
}}

// ---------------------------------------------------------------------------
// Text send
// ---------------------------------------------------------------------------
async function sendText() {{
  const inp = document.getElementById('inp');
  const btn = document.getElementById('send-btn');
  const text = inp.value.trim();
  if (!text || btn.disabled) return;
  inp.value = '';
  addBubble('user', text);
  await doSend({{message: text, password: pwd}});
}}

// ---------------------------------------------------------------------------
// Voice recording (MediaRecorder)
// ---------------------------------------------------------------------------
let mediaRecorder = null;
let audioChunks = [];
let recStream = null;
let isRecording = false;

async function toggleMic() {{
  if (isRecording) {{
    mediaRecorder.stop();
    return;
  }}
  try {{
    recStream = await navigator.mediaDevices.getUserMedia({{audio: true}});
  }} catch(e) {{
    addBubble('bot', 'לא ניתן לגשת למיקרופון. ודא שנתת הרשאה.');
    return;
  }}

  const mimeType = ['audio/webm;codecs=opus','audio/webm','audio/ogg'].find(t => MediaRecorder.isTypeSupported(t)) || '';
  mediaRecorder = new MediaRecorder(recStream, mimeType ? {{mimeType}} : {{}});
  audioChunks = [];

  mediaRecorder.ondataavailable = e => {{ if (e.data.size > 0) audioChunks.push(e.data); }};
  mediaRecorder.onstop = async () => {{
    recStream.getTracks().forEach(t => t.stop());
    isRecording = false;
    const micBtn = document.getElementById('mic-btn');
    micBtn.textContent = '🎤';
    micBtn.classList.remove('recording');

    const blob = new Blob(audioChunks, {{type: mediaRecorder.mimeType || 'audio/webm'}});
    const b64 = await blobToBase64(blob);
    addBubble('user', '🎤 [הודעה קולית]');
    await doSend({{message: '', password: pwd, audio_data: b64, audio_mime: blob.type}});
  }};

  mediaRecorder.start();
  isRecording = true;
  const micBtn = document.getElementById('mic-btn');
  micBtn.textContent = '⏹';
  micBtn.classList.add('recording');
}}

function blobToBase64(blob) {{
  return new Promise(resolve => {{
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result.split(',')[1]);
    reader.readAsDataURL(blob);
  }});
}}

// ---------------------------------------------------------------------------
// Core send — shared by text and voice paths
// ---------------------------------------------------------------------------
async function doSend(payload) {{
  const sendBtn = document.getElementById('send-btn');
  const micBtn  = document.getElementById('mic-btn');
  sendBtn.disabled = true;
  micBtn.disabled  = true;

  const typing = document.createElement('div');
  typing.className = 'typing';
  typing.textContent = 'מקליד…';
  document.getElementById('messages').appendChild(typing);
  typing.scrollIntoView({{block:'end'}});

  try {{
    const r = await fetch('/chat/send', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    typing.remove();
    const d = await r.json();
    const text = d.transcription ? `🎤 ${{d.transcription}}\n\n${{d.response}}` : (d.response || d.error || 'שגיאה');
    addBubble('bot', text, d.audio);
  }} catch(e) {{
    typing.remove();
    addBubble('bot', 'שגיאת תקשורת');
  }} finally {{
    sendBtn.disabled = false;
    micBtn.disabled  = false;
    document.getElementById('inp').focus();
  }}
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    message: str = ""
    password: str
    audio_data: Optional[str] = None   # base64 encoded audio from browser
    audio_mime: Optional[str] = None   # e.g. audio/webm;codecs=opus


@router.get("", response_class=HTMLResponse)
async def chat_page():
    return _CHAT_HTML.format(bot_name=settings.bot_name)


@router.post("/send")
async def chat_send(body: ChatMessage):
    if body.password != settings.web_chat_password:
        raise HTTPException(status_code=401, detail="Unauthorized")

    text = body.message.strip()
    was_voice = False
    transcription = None

    # --- Voice input ---
    if body.audio_data:
        try:
            from services.voice_service import transcribe_voice
            transcription = await transcribe_voice(
                body.audio_data, body.audio_mime or "audio/webm"
            )
            if not transcription:
                return {"response": "לא הצלחתי להבין, נסה שוב 🎤"}
            text = transcription
            was_voice = True
            logger.info(f"[CHAT] Voice transcribed: {text[:60]}")
        except Exception as e:
            logger.error(f"[CHAT] Transcription error: {e}")
            return {"response": f"שגיאה בתמלול: {type(e).__name__}"}

    if not text:
        return {"response": "שלח הודעה טקסט או הקלטה קולית."}

    # --- Server-side dedup (prevents double-send from rapid retries) ---
    key = hashlib.md5(f"{body.password}:{text}".encode()).hexdigest()
    now = time.time()
    _recent.update({k: v for k, v in _recent.items() if now - v[1] < 10})  # expire old
    if key in _recent:
        logger.info(f"[CHAT] Dedup hit for: {text[:40]}")
        return _recent[key][0]

    # --- Approval flow (shared with WhatsApp) ---
    from services.claude_service import check_and_handle_approval, process_message
    approval = await check_and_handle_approval(text, channel="web")
    if approval is not None:
        response_text, _ = approval
        result: dict = {"response": response_text}
        _recent[key] = (result, now)
        return result

    # --- Process ---
    response_text, _ = await process_message(text, channel="web")

    # --- TTS only when user sent voice (mirrors voice with voice) ---
    response_audio = None
    if was_voice:
        try:
            from services.tts_service import text_to_speech
            audio_bytes = await text_to_speech(response_text)
            response_audio = base64.b64encode(audio_bytes).decode()
        except Exception as e:
            logger.warning(f"[CHAT] TTS failed: {e}")

    result: dict = {"response": response_text}
    if transcription:
        result["transcription"] = transcription
    if response_audio:
        result["audio"] = response_audio   # pre-loaded audio for the 🔊 button
    _recent[key] = (result, now)
    return result


class SpeakRequest(BaseModel):
    text: str
    password: str


@router.post("/speak")
async def chat_speak(body: SpeakRequest):
    """Generate TTS for a specific message on demand (when user clicks 🔊)."""
    if body.password != settings.web_chat_password:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        from services.tts_service import text_to_speech
        audio_bytes = await text_to_speech(body.text)
        return {"audio": base64.b64encode(audio_bytes).decode()}
    except Exception as e:
        logger.error(f"[CHAT] Speak error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
