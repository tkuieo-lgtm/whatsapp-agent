# הגדרת WhatsApp AI Agent — מדריך שלב אחר שלב

## שלב 1 — Google Cloud Project

1. פתח https://console.cloud.google.com ← צור פרויקט חדש
2. **APIs & Services → Enable APIs:**
   - Gmail API
   - Google Calendar API
3. **APIs & Services → OAuth consent screen:**
   - User type: External
   - מלא שם אפליקציה, מייל
   - Scopes: הוסף את כולם:
     - `gmail.readonly`, `gmail.send`, `gmail.modify`
     - `calendar.readonly`, `calendar.events`
   - Test users: הוסף את כתובת ה-Gmail שלך
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:8000/auth/google/callback`
   - שמור את **Client ID** ו-**Client Secret**

---

## שלב 2 — Supabase

1. פתח https://supabase.com ← צור פרויקט חדש
2. **SQL Editor** → הרץ את ה-SQL הבא:

```sql
-- חוקי מייל
create table email_rules (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  conditions jsonb not null default '{}',
  actions jsonb not null default '{}',
  is_active boolean default true,
  created_at timestamptz default now()
);

-- פעולות הממתינות לאישור
create table pending_actions (
  id uuid primary key default gen_random_uuid(),
  type text not null,
  payload jsonb not null,
  status text default 'pending',
  created_at timestamptz default now(),
  expires_at timestamptz default now() + interval '30 minutes'
);

-- היסטוריית שיחות
create table conversation_history (
  id uuid primary key default gen_random_uuid(),
  role text not null,
  content text not null,
  created_at timestamptz default now()
);

-- לוג פעולות
create table action_log (
  id uuid primary key default gen_random_uuid(),
  action_type text not null,
  details jsonb,
  status text,
  created_at timestamptz default now()
);

-- הגדרות
create table settings (
  key text primary key,
  value jsonb,
  updated_at timestamptz default now()
);

-- ניקוי אוטומטי של pending_actions פגי תוקף (optional cron)
-- Run every hour in Supabase:
-- delete from pending_actions where expires_at < now();
```

3. **Project Settings → API:**
   - העתק **Project URL** ו-**anon public key**

---

## שלב 3 — מילוי .env

```bash
cp .env.example .env
```

ערוך את `.env` עם:
- `OWNER_PHONE` — המספר שלך (972XXXXXXXXX, ללא +)
- `ANTHROPIC_API_KEY` — מ-https://console.anthropic.com
- `SUPABASE_URL` + `SUPABASE_KEY`
- `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`

---

## שלב 4 — התקנת תלויות

### Backend (Python)
```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### WhatsApp Bridge (Node.js)
```bash
cd whatsapp
npm install
```

### Frontend (React)
```bash
cd frontend
npm install
```

---

## שלב 5 — הרצה ראשונה

### טרמינל 1 — WhatsApp Bridge
```bash
cd whatsapp
cp .env.example .env   # ערוך עם OWNER_PHONE ו-BACKEND_URL
node index.js
```
יופיע QR code בטרמינל. סרוק אותו עם **WhatsApp Business** (ולא WhatsApp הראשי שלך).  
לאחר הסריקה, הסשן נשמר ב-`.wwebjs_auth/` — לא צריך לסרוק שוב.

### טרמינל 2 — Backend
```bash
cd backend
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

### טרמינל 3 — Frontend
```bash
cd frontend
npm run dev
```

---

## שלב 6 — חיבור Google

פתח בדפדפן: http://localhost:8000/auth/google  
התחבר עם חשבון ה-Gmail שלך ואשר את ההרשאות.  
לאחר הצלחה תראה ✅ ותוכל לסגור את הדף.

---

## שלב 7 — בדיקה

שלח הודעת WhatsApp מהמספר הראשי שלך אל מספר ה-WhatsApp Business:
```
שלום! מה יש לי ביומן היום?
```
הסוכן אמור להגיב תוך כמה שניות.

---

## שלב 8 — פריסה ל-Railway

כל שירות הוא **Railway Service** נפרד שמצביע על תיקיית משנה אחרת באותו repo.

### הגדרת הפרויקט

1. פתח https://railway.app → **New Project → Deploy from GitHub repo**
2. בחר את ה-repo שלך
3. Railway ייצור שירות ראשון אוטומטית — מחק אותו, נגדיר ידנית

---

### שירות 1 — Backend (FastAPI)

**New Service → GitHub repo → Root Directory: `backend`**

Railway מזהה Python אוטומטית דרך `requirements.txt` ו-`runtime.txt`.

**Environment Variables** (Settings → Variables):
```
OWNER_PHONE=972546670073
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=<Railway PostgreSQL internal URL>
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://YOUR-BACKEND.up.railway.app/auth/google/callback
WHATSAPP_SERVICE_URL=https://YOUR-WHATSAPP.up.railway.app
TIMEZONE=Asia/Jerusalem
CLAUDE_RATE_LIMIT_PER_HOUR=20
```

> Railway מספק `DATABASE_URL` אוטומטית אם מוסיפים PostgreSQL לפרויקט.  
> ב-Settings → Networking → **Generate Domain** לקבלת ה-URL הציבורי.

---

### שירות 2 — WhatsApp Bridge (Node.js)

**New Service → GitHub repo → Root Directory: `whatsapp`**

Railway ישתמש ב-`Dockerfile` שכולל Chromium.  
הסשן נשמר אוטומטית ב-PostgreSQL — לא צריך Volume ולא QR חוזר אחרי restart.

**Environment Variables**:
```
OWNER_PHONE=972546670073
BOT_NAME=מקס
BACKEND_URL=https://YOUR-BACKEND.up.railway.app
DATABASE_URL=${{Postgres.DATABASE_URL}}
PORT=3000
```

> `${{Postgres.DATABASE_URL}}` — Railway ממלא זאת אוטומטית מה-PostgreSQL service.

**סריקת QR ראשונה (פעם אחת בלבד):**
1. לאחר deploy, פתח: `https://YOUR-WHATSAPP.up.railway.app/qr`
2. הדף יציג QR code — סרוק עם WhatsApp Business
3. הסשן נשמר ב-PostgreSQL בטבלת `whatsapp_sessions`
4. כל restart עתידי — הסשן משוחזר אוטומטית, ללא QR

**מה קורה מאחורי הקלעים:**
- `authenticated` event → שמירה ל-DB תוך 3 שניות
- `ready` event → שמירה נוספת תוך 5 שניות
- גיבוי אוטומטי כל 5 דקות

---

### שירות 3 — Frontend (React)

**New Service → GitHub repo → Root Directory: `frontend`**

**Environment Variables**:
```
VITE_API_URL=https://YOUR-BACKEND.up.railway.app
```

> `VITE_API_URL` חייב להיות מוגדר **לפני** ה-build — Vite מטמיע אותו בזמן בנייה.

---

### שירות 4 — PostgreSQL

**New Service → Database → Add PostgreSQL**

Railway יוסיף אוטומטית את `DATABASE_URL` לכל שירות בפרויקט.

---

### עדכון Google OAuth לאחר Deploy

ב-Google Cloud Console → Credentials → OAuth Client → **Authorized redirect URIs**:
```
https://YOUR-BACKEND.up.railway.app/auth/google/callback
```

לאחר מכן בקר ב: `https://YOUR-BACKEND.up.railway.app/auth/google`

---

## שגיאות נפוצות

| שגיאה | פתרון |
|-------|-------|
| `Google credentials not configured` | בקר ב-`/auth/google` ואשר את ה-OAuth |
| WhatsApp לא מתחבר | בקר ב-`/qr` על ה-WhatsApp service וסרוק את הקוד |
| QR מוצג כ-"Waiting..." | המתן 30 שניות ל-Chromium לעלות, רענן את הדף |
| הסוכן לא מגיב | ודא ש-`OWNER_PHONE` נכון (ספרות בלבד, ללא +) |
| `Database error` | ודא ש-`DATABASE_URL` מצביע ל-Railway PostgreSQL |
| Frontend לא מחובר ל-backend | ודא ש-`VITE_API_URL` מוגדר ונעשה re-deploy |
| `Rate limit exceeded` | הגדל את `CLAUDE_RATE_LIMIT_PER_HOUR` |
