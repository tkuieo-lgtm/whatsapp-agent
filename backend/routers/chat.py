from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from config import settings

router = APIRouter(prefix="/chat", tags=["chat"])

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
    #header span{{font-size:22px}}
    #messages{{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:6px}}
    .bubble{{max-width:78%;padding:8px 12px;border-radius:12px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;font-size:14.5px}}
    .user{{background:#dcf8c6;align-self:flex-end;border-radius:12px 0 12px 12px}}
    .bot{{background:#fff;align-self:flex-start;border-radius:0 12px 12px 12px;box-shadow:0 1px 2px rgba(0,0,0,.12)}}
    .ts{{font-size:11px;color:#999;margin-top:3px;text-align:left}}
    .typing{{color:#555;font-size:13px;padding:6px 10px;background:#fff;border-radius:12px;align-self:flex-start;box-shadow:0 1px 2px rgba(0,0,0,.08)}}
    #bar{{display:flex;gap:8px;padding:10px 12px;background:#f0f0f0;border-top:1px solid #ddd}}
    #inp{{flex:1;padding:10px 14px;border:none;border-radius:22px;font-size:15px;outline:none;font-family:inherit;background:#fff}}
    #btn{{background:#25d366;color:#fff;border:none;border-radius:50%;width:44px;height:44px;cursor:pointer;font-size:19px;flex-shrink:0}}
    #btn:disabled{{background:#aaa}}
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
  <button id="btn" onclick="send()">➤</button>
  <input id="inp" type="text" placeholder="כתוב הודעה..."
         onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();send()}}">
</div>

<script>
const BOT = '{bot_name}';
let pwd = sessionStorage.getItem('cp') || '';
if (pwd) document.getElementById('overlay').style.display = 'none';

async function login() {{
  pwd = document.getElementById('pwd').value;
  const r = await fetch('/chat/send', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{message:'שלום', password:pwd}})
  }});
  if (r.ok) {{
    sessionStorage.setItem('cp', pwd);
    document.getElementById('overlay').style.display = 'none';
    addBubble('bot', `שלום! אני ${{BOT}}. 👋`);
    document.getElementById('inp').focus();
  }} else {{
    document.getElementById('err').textContent = 'סיסמה שגויה';
  }}
}}

function addBubble(role, text) {{
  const wrap = document.createElement('div');
  const b = document.createElement('div');
  b.className = `bubble ${{role}}`;
  b.textContent = text;
  const ts = document.createElement('div');
  ts.className = 'ts';
  ts.textContent = new Date().toLocaleTimeString('he-IL', {{hour:'2-digit',minute:'2-digit'}});
  wrap.appendChild(b);
  wrap.appendChild(ts);
  document.getElementById('messages').appendChild(wrap);
  b.scrollIntoView({{block:'end'}});
  return b;
}}

async function send() {{
  const inp = document.getElementById('inp');
  const btn = document.getElementById('btn');
  const text = inp.value.trim();
  if (!text || btn.disabled) return;
  inp.value = '';
  addBubble('user', text);
  btn.disabled = true;
  const typing = document.createElement('div');
  typing.className = 'typing';
  typing.textContent = 'מקליד…';
  document.getElementById('messages').appendChild(typing);
  typing.scrollIntoView({{block:'end'}});
  try {{
    const r = await fetch('/chat/send', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{message:text, password:pwd}})
    }});
    typing.remove();
    const d = await r.json();
    addBubble('bot', d.response || d.error || 'שגיאה');
  }} catch(e) {{
    typing.remove();
    addBubble('bot', 'שגיאת תקשורת');
  }} finally {{
    btn.disabled = false;
    inp.focus();
  }}
}}
</script>
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
async def chat_page():
    return _CHAT_HTML.format(bot_name=settings.bot_name)


class ChatMessage(BaseModel):
    message: str
    password: str


@router.post("/send")
async def chat_send(body: ChatMessage):
    if body.password != settings.web_chat_password:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from services.claude_service import process_message
    response_text, _ = await process_message(body.message, channel="web")
    return {"response": response_text}
