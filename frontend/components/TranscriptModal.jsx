"use client";

import { useEffect, useState } from "react";
import { X, Loader2 } from "lucide-react";
import { fetchTranscript } from "@/lib/api";
import { useAgentConfig } from "@/lib/AgentConfigContext";

export default function TranscriptModal({ call, onClose }) {
  const [transcript, setTranscript] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const { agentName } = useAgentConfig();

  useEffect(() => {
    if (!call?.id) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchTranscript(call.id)
      .then((data) => { if (!cancelled) setTranscript(data); })
      .catch(() => { if (!cancelled) setError("Failed to load transcript"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [call?.id]);

  if (!call) return null;

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div>
            <h2 className="font-semibold text-gray-900">Call Transcript</h2>
            <p className="text-xs text-gray-400 mt-0.5">{call.phone_number} · {call.created_at ? new Date(call.created_at).toLocaleString() : ""}</p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors">
            <X className="w-5 h-5 text-gray-500" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading && (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
            </div>
          )}
          {error && (
            <div className="text-center py-12 text-gray-400">{error}</div>
          )}
          {transcript && !loading && (
            <div className="space-y-3">
              {(transcript.turns || []).map((turn, i) => (
                <div
                  key={i}
                  className={`flex ${turn.role === "assistant" ? "justify-start" : "justify-end"}`}
                >
                  <div
                    className={`max-w-[80%] px-4 py-2.5 rounded-2xl text-sm ${
                      turn.role === "assistant"
                        ? "bg-gray-100 text-gray-800 rounded-tl-sm"
                        : "bg-blue-600 text-white rounded-tr-sm"
                    }`}
                  >
                    <div className="text-xs font-medium mb-1 opacity-60 capitalize">{turn.role === "assistant" ? agentName : "Caller"}</div>
                    {turn.content}
                  </div>
                </div>
              ))}
              {(!transcript.turns || transcript.turns.length === 0) && transcript.full_text && (
                <pre className="whitespace-pre-wrap text-sm text-gray-700 font-mono bg-gray-50 p-4 rounded-lg">
                  {transcript.full_text}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}