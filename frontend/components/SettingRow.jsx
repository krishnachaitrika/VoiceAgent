"use client";

import { useState } from "react";
import { Check, Loader2 } from "lucide-react";
import { updateSetting } from "@/lib/api";

export default function SettingRow({ label, settingKey, initialValue = "", type = "text", rows = 1, options = null, onSaved = null }) {
  const [value, setValue] = useState(initialValue);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  const inputId = `setting-${settingKey}`;

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await updateSetting(settingKey, value);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      onSaved?.(settingKey, value);
    } catch {
      setError("Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 py-4 border-b border-gray-100 last:border-0">
      <label htmlFor={inputId} className="text-sm font-medium text-gray-700">{label}</label>

      {options ? (
        <select
          id={inputId}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-full max-w-sm border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      ) : rows > 1 ? (
        <textarea
          id={inputId}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={rows}
          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
        />
      ) : (
        <input
          id={inputId}
          type={type}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-full max-w-sm border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-1.5 px-4 py-1.5 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
          Save
        </button>
        {saved && (
          <span className="flex items-center gap-1 text-xs text-green-600">
            <Check className="w-3.5 h-3.5" /> Saved
          </span>
        )}
        {error && <span className="text-xs text-red-500">{error}</span>}
      </div>
    </div>
  );
}