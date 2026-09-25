"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { fetchSettings } from "@/lib/api";

// Build-time fallback only — used for the very first render before the
// live Settings fetch resolves, and as a last-resort fallback if the
// backend is unreachable. The actual source of truth is the Supabase
// `settings` table (editable live from /settings — no rebuild needed).
const FALLBACK_AGENT_NAME = process.env.NEXT_PUBLIC_AGENT_NAME || "Velu";
const FALLBACK_COMPANY_NAME = process.env.NEXT_PUBLIC_COMPANY_NAME || "Technozis";

const AgentConfigContext = createContext({
  agentName: FALLBACK_AGENT_NAME,
  companyName: FALLBACK_COMPANY_NAME,
  loading: true,
  refresh: () => {},
});

export function AgentConfigProvider({ children }) {
  const [config, setConfig] = useState({
    agentName: FALLBACK_AGENT_NAME,
    companyName: FALLBACK_COMPANY_NAME,
    loading: true,
  });

  useEffect(() => {
    let cancelled = false;
    fetchSettings()
      .then((data) => {
        if (cancelled) return;
        const map = {};
        (data.settings || []).forEach((s) => { map[s.key] = s.value; });
        setConfig({
          agentName: map.agent_name || FALLBACK_AGENT_NAME,
          companyName: map.company_name || FALLBACK_COMPANY_NAME,
          loading: false,
        });
      })
      .catch(() => {
        // Backend unreachable — quietly keep the env-based fallback rather
        // than breaking the dashboard UI.
        if (!cancelled) setConfig((c) => ({ ...c, loading: false }));
      });
    return () => { cancelled = true; };
  }, []);

  // Re-fetches settings on demand so consumers (Sidebar, Documents,
  // TranscriptModal) pick up agent_name/company_name edits made on the
  // Settings page immediately, without a full page reload.
  async function refresh() {
    try {
      const data = await fetchSettings();
      const map = {};
      (data.settings || []).forEach((s) => { map[s.key] = s.value; });
      setConfig((c) => ({
        ...c,
        agentName: map.agent_name || FALLBACK_AGENT_NAME,
        companyName: map.company_name || FALLBACK_COMPANY_NAME,
      }));
    } catch {
      // Keep whatever config is currently loaded.
    }
  }

  return (
    <AgentConfigContext.Provider value={{ ...config, refresh }}>
      {children}
    </AgentConfigContext.Provider>
  );
}

export function useAgentConfig() {
  return useContext(AgentConfigContext);
}
