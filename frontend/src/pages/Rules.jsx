import { useEffect, useState } from "react";
import { api } from "../api";
import RuleModal from "../components/RuleModal";

export default function Rules() {
  const [rules, setRules] = useState([]);
  const [modal, setModal] = useState(null); // null | "new" | rule object

  useEffect(() => {
    api.listRules().then(setRules).catch(console.error);
  }, []);

  async function handleSave(data) {
    if (modal === "new") {
      const created = await api.createRule(data);
      setRules((r) => [created, ...r]);
    } else {
      const updated = await api.updateRule(modal.id, data);
      setRules((r) => r.map((x) => (x.id === updated.id ? updated : x)));
    }
  }

  async function toggleActive(rule) {
    const updated = await api.updateRule(rule.id, { is_active: !rule.is_active });
    setRules((r) => r.map((x) => (x.id === updated.id ? updated : x)));
  }

  async function deleteRule(id) {
    if (!confirm("למחוק את החוק?")) return;
    await api.deleteRule(id);
    setRules((r) => r.filter((x) => x.id !== id));
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">חוקי מייל</h1>
        <button className="btn-primary" onClick={() => setModal("new")}>
          + הוסף חוק
        </button>
      </div>

      {rules.length === 0 ? (
        <p className="text-gray-400 text-center py-12">אין חוקים עדיין. צור חוק ראשון!</p>
      ) : (
        <div className="bg-white rounded-xl shadow overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr className="text-right text-gray-500">
                <th className="px-4 py-3 font-medium">שם</th>
                <th className="px-4 py-3 font-medium">תנאים</th>
                <th className="px-4 py-3 font-medium">פעולות</th>
                <th className="px-4 py-3 font-medium">סטטוס</th>
                <th className="px-4 py-3 font-medium"></th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rules.map((rule) => (
                <tr key={rule.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{rule.name}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {Object.entries(rule.conditions || {})
                      .map(([k, v]) => `${k}: "${v}"`)
                      .join(", ") || "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {Object.entries(rule.actions || {})
                      .filter(([, v]) => v !== false && v != null)
                      .map(([k, v]) => `${k}: ${v}`)
                      .join(", ") || "—"}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => toggleActive(rule)}
                      className={`px-3 py-1 rounded-full text-xs font-semibold ${
                        rule.is_active
                          ? "bg-green-100 text-green-700"
                          : "bg-gray-100 text-gray-500"
                      }`}
                    >
                      {rule.is_active ? "פעיל" : "כבוי"}
                    </button>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex gap-2 justify-end">
                      <button
                        className="text-blue-500 hover:underline text-xs"
                        onClick={() => setModal(rule)}
                      >
                        ערוך
                      </button>
                      <button
                        className="text-red-500 hover:underline text-xs"
                        onClick={() => deleteRule(rule.id)}
                      >
                        מחק
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {modal && (
        <RuleModal
          initial={modal === "new" ? null : modal}
          onSave={handleSave}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  );
}
