"use client";

import { AgentConfigProvider } from "@/lib/AgentConfigContext";
import Sidebar from "@/components/Sidebar";

/**
 * Wraps every page with the live agent/company name context, so any
 * component (Sidebar, TranscriptModal, Documents page, etc.) can call
 * useAgentConfig() instead of hardcoding "Velu" / "Technozis". The values
 * come from the Supabase `settings` table via GET /api/settings — the same
 * data the Settings page lets you edit live, no rebuild/redeploy needed.
 */
export default function AppShell({ children }) {
  return (
    <AgentConfigProvider>
      <div className="flex min-h-screen">
        <Sidebar />
        <main className="flex-1 overflow-auto">{children}</main>
      </div>
    </AgentConfigProvider>
  );
}
