import { useState } from "react";

const EMPTY = {
  name: "",
  from_contains: "",
  subject_contains: "",
  to_contains: "",
  move_to_folder: "",
  mark_as_read: false,
  is_active: true,
};

export default function RuleModal({ initial = null, onSave, onClose }) {
  const [form, setForm] = useState(initial ? flattenRule(initial) : EMPTY);
  const [saving, setSaving] = useState(false);

  function set(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    try {
      const conditions = {};
      if (form.from_contains) conditions.from_contains = form.from_contains;
      if (form.subject_contains) conditions.subject_contains = form.subject_contains;
      if (form.to_contains) conditions.to_contains = form.to_contains;

      const actions = {};
      if (form.move_to_folder) actions.move_to_folder = form.move_to_folder;
      actions.mark_as_read = form.mark_as_read;

      await onSave({ name: form.name, conditions, actions, is_active: form.is_active });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg p-6">
        <h2 className="text-lg font-bold mb-4">
          {initial ? "עריכת חוק" : "חוק חדש"}
        </h2>

        <form onSubmit={submit} className="space-y-4">
          <Field label="שם החוק" required>
            <input
              className="input"
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              required
            />
          </Field>

          <p className="text-sm font-semibold text-gray-500 mt-2">תנאים</p>

          <Field label="From מכיל">
            <input className="input" value={form.from_contains} onChange={(e) => set("from_contains", e.target.value)} />
          </Field>
          <Field label="Subject מכיל">
            <input className="input" value={form.subject_contains} onChange={(e) => set("subject_contains", e.target.value)} />
          </Field>
          <Field label="To מכיל">
            <input className="input" value={form.to_contains} onChange={(e) => set("to_contains", e.target.value)} />
          </Field>

          <p className="text-sm font-semibold text-gray-500 mt-2">פעולות</p>

          <Field label="העבר לתיקייה">
            <input className="input" value={form.move_to_folder} onChange={(e) => set("move_to_folder", e.target.value)} />
          </Field>

          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={form.mark_as_read} onChange={(e) => set("mark_as_read", e.target.checked)} />
            <span className="text-sm">סמן כנקרא</span>
          </label>

          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={form.is_active} onChange={(e) => set("is_active", e.target.checked)} />
            <span className="text-sm">חוק פעיל</span>
          </label>

          <div className="flex gap-3 pt-2">
            <button type="submit" disabled={saving} className="btn-primary flex-1">
              {saving ? "שומר…" : "שמור"}
            </button>
            <button type="button" onClick={onClose} className="btn-secondary flex-1">
              ביטול
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({ label, required, children }) {
  return (
    <div>
      <label className="block text-sm text-gray-600 mb-1">
        {label}
        {required && <span className="text-red-500 mr-1">*</span>}
      </label>
      {children}
    </div>
  );
}

function flattenRule(rule) {
  return {
    name: rule.name,
    from_contains: rule.conditions?.from_contains ?? "",
    subject_contains: rule.conditions?.subject_contains ?? "",
    to_contains: rule.conditions?.to_contains ?? "",
    move_to_folder: rule.actions?.move_to_folder ?? "",
    mark_as_read: Boolean(rule.actions?.mark_as_read),
    is_active: rule.is_active,
  };
}
