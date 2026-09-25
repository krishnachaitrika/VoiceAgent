"use client";

import { useEffect, useState, useCallback } from "react";
import { Smile, Meh, Frown, Flag, Activity, Database } from "lucide-react";
import StatCard from "@/components/StatCard";
import { fetchSentimentSummary, fetchSentimentReports, fetchRecentEvents, fetchSystemHealth } from "@/lib/api";

const SENTIMENT_ICON = { positive: Smile, neutral: Meh, negative: Frown };
const SENTIMENT_COLOR = {
  positive: "text-green-600 bg-green-50",
  neutral: "text-gray-500 bg-gray-100",
  negative: "text-red-600 bg-red-50",
};

// Matches dashboard/page.jsx's REFRESH_MS — keeps the "live" feed/reports
// actually refreshing instead of being a one-shot fetch.
const REFRESH_MS = 30_000;

export default function AnalyticsPage() {
  const [summary, setSummary] = useState(null);
  const [reports, setReports] = useState([]);
  const [events, setEvents] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const [s, r, e, h] = await Promise.all([
        fetchSentimentSummary().catch(() => null),
        fetchSentimentReports(1, 15).catch(() => ({ reports: [] })),
        fetchRecentEvents(20).catch(() => ({ events: [] })),
        fetchSystemHealth().catch(() => null),
      ]);
      setSummary(s);
      setReports(r.reports || []);
      setEvents(e.events || []);
      setHealth(h);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, REFRESH_MS);
    return () => clearInterval(interval);
  }, [load]);

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Analytics</h1>
          <p className="text-sm text-gray-400 mt-0.5">Post-call sentiment reports and live system activity</p>
        </div>
        <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium ${health?.redis_connected ? "bg-green-50 text-green-700" : "bg-gray-100 text-gray-500"}`}>
          <Database className="w-3.5 h-3.5" />
          Redis {health?.redis_connected ? "connected" : "unavailable"}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Calls analyzed" value={summary?.total_analyzed ?? "—"} icon={Activity} color="blue" />
        <StatCard label="Positive" value={summary?.positive_count ?? "—"} icon={Smile} color="green" />
        <StatCard label="Negative" value={summary?.negative_count ?? "—"} icon={Frown} color="red" />
        <StatCard label="Flagged for review" value={summary?.flagged_count ?? "—"} icon={Flag} color="orange" />
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-400 text-sm animate-pulse">Loading analytics...</div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          {/* Sentiment reports */}
          <div className="lg:col-span-2 bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-100 font-medium text-sm text-gray-700">
              Recent sentiment reports
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50">
                  <th className="px-4 py-2.5 text-left font-medium text-gray-500">Call</th>
                  <th className="px-4 py-2.5 text-left font-medium text-gray-500">Sentiment</th>
                  <th className="px-4 py-2.5 text-left font-medium text-gray-500">Outcome</th>
                  <th className="px-4 py-2.5 text-left font-medium text-gray-500">Summary</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {reports.length === 0 && (
                  <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-400">No sentiment reports yet</td></tr>
                )}
                {reports.map((r) => {
                  const Icon = SENTIMENT_ICON[r.sentiment] || Meh;
                  return (
                    <tr key={r.id} className={r.flagged_for_review ? "bg-orange-50/40" : "hover:bg-blue-50/20"}>
                      <td className="px-4 py-2.5 font-mono text-xs text-gray-500">{r.call_id.slice(0, 8)}</td>
                      <td className="px-4 py-2.5">
                        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${SENTIMENT_COLOR[r.sentiment] || SENTIMENT_COLOR.neutral}`}>
                          <Icon className="w-3 h-3" />
                          {r.sentiment}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-gray-600 capitalize">{r.outcome?.replace("_", " ")}</td>
                      <td className="px-4 py-2.5 text-gray-500 text-xs max-w-xs truncate">{r.intent_summary}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Live event feed */}
          <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-100 font-medium text-sm text-gray-700 flex items-center gap-2">
              <Activity className="w-4 h-4 text-blue-500" />
              Live activity (Redis Streams)
            </div>
            <div className="divide-y divide-gray-50 max-h-[480px] overflow-y-auto">
              {events.length === 0 && (
                <div className="px-4 py-8 text-center text-gray-400 text-sm">No events yet</div>
              )}
              {events.map((e) => (
                <div key={e.id} className="px-4 py-2.5 text-xs">
                  <div className="font-medium text-gray-700">{e.type?.replace("_", " ")}</div>
                  <div className="text-gray-400 font-mono">{e.call_id?.slice(0, 8)}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
