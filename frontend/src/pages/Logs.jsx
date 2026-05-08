import { useEffect, useState } from "react";
import { api } from "../api";

const ACTION_TYPES = [
  "",
  "claude_call",
  "email_rule_applied",
  "send_email",
  "create_calendar_event",
  "delete_calendar_event",
];

const STATUS_COLORS = {
  success: "bg-green-100 text-green-700",
  approved: "bg-green-100 text-green-700",
  rejected: "bg-red-100 text-red-700",
  started: "bg-blue-100 text-blue-700",
  expired: "bg-yellow-100 text-yellow-700",
};

export default function Logs() {
  const [logs, setLogs] = useState([]);
  const [type, setType] = useState("");
  const [limit, setLimit] = useState(50);

  useEffect(() => {
    api.logs(limit, type).then(setLogs).catch(console.error);
  }, [type, limit]);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">לוג פעולות</h1>

      <div className="flex gap-3 items-center">
        <select
          className="input w-48"
          value={type}
          onChange={(e) => setType(e.target.value)}
        >
          <option value="">כל הסוגים</option>
          {ACTION_TYPES.filter(Boolean).map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <select
          className="input w-32"
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
        >
          {[25, 50, 100, 200].map((n) => (
            <option key={n} value={n}>{n} שורות</option>
          ))}
        </select>
      </div>

      <div className="bg-white rounded-xl shadow overflow-hidden">
        {logs.length === 0 ? (
          <p className="text-gray-400 text-center py-12">אין רשומות</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr className="text-right text-gray-500">
                <th className="px-4 py-3 font-medium">זמן</th>
                <th className="px-4 py-3 font-medium">סוג</th>
                <th className="px-4 py-3 font-medium">סטטוס</th>
                <th className="px-4 py-3 font-medium">פרטים</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {logs.map((log) => (
                <tr key={log.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-400 whitespace-nowrap">
                    {new Date(log.created_at).toLocaleString("he-IL")}
                  </td>
                  <td className="px-4 py-3">{log.action_type}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-semibold ${
                        STATUS_COLORS[log.status] ?? "bg-gray-100 text-gray-600"
                      }`}
                    >
                      {log.status ?? "—"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-500 max-w-xs truncate">
                    {log.details
                      ? JSON.stringify(log.details).slice(0, 80)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
