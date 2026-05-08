import { useEffect, useState } from "react";
import { api } from "../api";

const AUTO_MODES = [
  { key: "auto_mode_send_email", label: "שליחת מייל" },
  { key: "auto_mode_create_calendar_event", label: "יצירת אירוע" },
  { key: "auto_mode_delete_calendar_event", label: "מחיקת אירוע" },
  { key: "auto_mode_create_email_rule", label: "יצירת חוק מייל" },
];

function Card({ title, children, className = "" }) {
  return (
    <div className={`bg-white rounded-xl shadow p-5 ${className}`}>
      <h2 className="font-semibold text-gray-600 mb-3">{title}</h2>
      {children}
    </div>
  );
}

export default function Dashboard() {
  const [health, setHealth] = useState(null);
  const [auth, setAuth] = useState(null);
  const [stats, setStats] = useState(null);
  const [settings, setSettings] = useState({});
  const [logs, setLogs] = useState([]);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ status: "offline" }));
    api.authStatus().then(setAuth).catch(() => null);
    api.stats().then(setStats).catch(() => null);
    api.getSettings().then(setSettings).catch(() => null);
    api.logs(10).then(setLogs).catch(() => null);
  }, []);

  async function toggleAutoMode(key, current) {
    const next = !current;
    await api.setSetting(key, next);
    setSettings((s) => ({ ...s, [key]: next }));
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">לוח בקרה</h1>

      {/* Status row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card title="סטטוס שרת">
          <span
            className={`text-2xl font-bold ${
              health?.status === "ok" ? "text-green-600" : "text-red-500"
            }`}
          >
            {health?.status === "ok" ? "✅ פעיל" : "❌ לא מחובר"}
          </span>
        </Card>

        <Card title="Google">
          <span
            className={`text-2xl font-bold ${
              auth?.google_authenticated ? "text-green-600" : "text-orange-500"
            }`}
          >
            {auth?.google_authenticated ? "✅ מחובר" : "⚠️ לא מחובר"}
          </span>
          {!auth?.google_authenticated && (
            <a
              href="/auth/google"
              className="mt-2 block text-sm text-blue-600 underline"
            >
              חבר חשבון Google
            </a>
          )}
        </Card>

        <Card title="פעולות היום">
          <div className="text-3xl font-bold text-green-600">
            {stats?.total_actions_today ?? "—"}
          </div>
          <div className="text-sm text-gray-500">
            {stats?.emails_auto_handled_today ?? 0} מיילים טופלו אוטומטית
          </div>
        </Card>
      </div>

      {/* Auto-mode toggles */}
      <Card title="מצב אוטומטי (ללא בקשת אישור)">
        <div className="divide-y">
          {AUTO_MODES.map(({ key, label }) => {
            const on = Boolean(settings[key]);
            return (
              <div key={key} className="flex items-center justify-between py-3">
                <span>{label}</span>
                <button
                  onClick={() => toggleAutoMode(key, on)}
                  className={`w-12 h-6 rounded-full transition-colors ${
                    on ? "bg-green-500" : "bg-gray-300"
                  } relative`}
                >
                  <span
                    className={`absolute top-1 w-4 h-4 bg-white rounded-full shadow transition-all ${
                      on ? "right-1" : "left-1"
                    }`}
                  />
                </button>
              </div>
            );
          })}
        </div>
      </Card>

      {/* Recent actions */}
      <Card title="פעולות אחרונות">
        {logs.length === 0 ? (
          <p className="text-gray-400 text-sm">אין פעולות עדיין</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-right text-gray-500 border-b">
                <th className="pb-2 font-medium">סוג</th>
                <th className="pb-2 font-medium">סטטוס</th>
                <th className="pb-2 font-medium">זמן</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id} className="border-b last:border-0">
                  <td className="py-2">{log.action_type}</td>
                  <td className="py-2">
                    <span
                      className={`px-2 py-0.5 rounded text-xs ${
                        log.status === "success" || log.status === "approved"
                          ? "bg-green-100 text-green-700"
                          : log.status === "rejected"
                          ? "bg-red-100 text-red-700"
                          : "bg-gray-100 text-gray-600"
                      }`}
                    >
                      {log.status}
                    </span>
                  </td>
                  <td className="py-2 text-gray-400">
                    {new Date(log.created_at).toLocaleTimeString("he-IL")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
