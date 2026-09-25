"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  PhoneCall,
  Users,
  Calendar,
  AlertTriangle,
  Settings,
  Mic,
  FileText,
  BarChart3,
  UserCheck,
} from "lucide-react";
import { useAgentConfig } from "@/lib/AgentConfigContext";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/calls", label: "Calls", icon: PhoneCall },
  { href: "/leads", label: "Leads", icon: Users },
  { href: "/meetings", label: "Meetings", icon: Calendar },
  { href: "/escalations", label: "Escalations", icon: AlertTriangle },
  { href: "/documents", label: "Knowledge Base", icon: FileText },
  { href: "/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/interview", label: "Mock Interview", icon: UserCheck },
  { href: "/settings", label: "Settings", icon: Settings },
];

export default function Sidebar() {
  const pathname = usePathname();
  // Live from the Supabase `settings` table (editable on the Settings page,
  // no rebuild needed) — never hardcoded here.
  const { agentName, companyName } = useAgentConfig();

  return (
    <aside className="flex flex-col w-60 min-h-screen bg-[#1a2744] text-white">
      {/* Logo */}
      <div className="flex items-center gap-2 px-6 py-5 border-b border-white/10">
        <Mic className="w-6 h-6 text-blue-400" />
        <div>
          <div className="font-bold text-sm leading-tight">{companyName}</div>
          <div className="text-xs text-white/50 leading-tight">Voice Agent — {agentName}</div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
          const active = pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                active
                  ? "bg-blue-600 text-white"
                  : "text-white/60 hover:bg-white/10 hover:text-white"
              }`}
            >
              <Icon className="w-4 h-4 flex-shrink-0" />
              {label}
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-6 py-4 border-t border-white/10">
        <div className="text-xs text-white/30">
          v{process.env.NEXT_PUBLIC_APP_VERSION || "3.0.0"} · {companyName} © {new Date().getFullYear()}
        </div>
      </div>
    </aside>
  );
}
