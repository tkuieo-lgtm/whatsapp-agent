# WhatsApp AI Agent

סוכן AI אישי שפועל דרך WhatsApp — מחובר ל-Gmail וליומן Google Calendar.

## תכונות

- **WhatsApp** — מאזין להודעות מהמספר הראשי בלבד (אבטחה מלאה)
- **Gmail** — קריאת מיילים עם סינון חכם, שליחה, תיוק, סימון כנקרא
- **Google Calendar** — צפייה, יצירה ומחיקת אירועים
- **מערכת אישורים** — כל פעולה כותבת דורשת אישור (כן/לא)
- **חוקי מייל** — אוטומציה לפי תנאים (כל 15 דקות)
- **סיכום בוקר** — כל יום ב-08:00 עם יומן ומיילים דחופים
- **סיכום שבועי** — כל שישי ב-17:00
- **תזכורות** — מיילים שממתינים לתשובה מעל 6 שעות
- **מצב אוטומטי** — ניתן לכבות אישורים לכל סוג פעולה
- **ממשק ניהול** — React SPA לניהול חוקים ולוג פעולות

## מבנה הפרויקט

```
/whatsapp-agent
  /backend          ← FastAPI (Python 3.11)
  /whatsapp         ← whatsapp-web.js bridge (Node.js)
  /frontend         ← React + Vite + Tailwind
  /docs             ← מדריך הגדרה מפורט
  docker-compose.yml
```

## התחלה מהירה

ראה [docs/SETUP.md](docs/SETUP.md) למדריך מלא.

```bash
# 1. העתק קובץ .env
cp .env.example .env   # ועדכן את כל הערכים

# 2. הרץ עם Docker Compose
docker-compose up --build

# 3. סרוק את ה-QR בלוגים של שירות whatsapp
docker-compose logs whatsapp

# 4. חבר Google (פעם אחת)
open http://localhost:8000/auth/google

# 5. ממשק ניהול
open http://localhost:5173
```

## דוגמאות שיחה

```
אתה: מה יש לי ביומן היום?
סוכן: 📅 אירועים להיום:
      • 10:00 — פגישה עם דני
      • 14:00 — שיחת זום עם הצוות

אתה: שלח מייל לdani@example.com נושא "סיכום פגישה" תוכן "כפי שדיברנו..."
סוכן: 📧 שליחת מייל
      ל: dani@example.com
      נושא: סיכום פגישה
      ...
      להמשיך? ענה כן או לא

אתה: כן
סוכן: ✅ המייל נשלח בהצלחה

אתה: כל מייל מ-aliexpress יעבור לתיקיית קניות
סוכן: 📋 חוק מייל חדש
      תנאים: {"from_contains": "aliexpress"}
      פעולות: {"move_to_folder": "קניות"}
      להמשיך? ענה כן או לא
```

## טכנולוגיות

| שכבה | טכנולוגיה |
|------|-----------|
| AI | Claude Sonnet (Anthropic API) |
| Backend | FastAPI + APScheduler |
| WhatsApp | whatsapp-web.js |
| Database | Supabase (PostgreSQL) |
| Google | Gmail API + Calendar API |
| Frontend | React + Vite + Tailwind |
| Deployment | Railway / Docker |

## אבטחה

- רק `OWNER_PHONE` יכול לתקשר עם הסוכן — כל שאר ההודעות נדחות בשקט
- כל פעולת כתיבה דורשת אישור מפורש (אלא אם auto-mode מופעל)
- כל פעולה נרשמת ב-`action_log`
- מפתחות רק ב-`.env` — לא בקוד
- `pending_actions` מתנקים אוטומטית אחרי 30 דקות
- Rate limiting: מגבלת 20 קריאות לשעה ל-Claude API
