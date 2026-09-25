const STATUS_CONFIG = {
  completed: { label: "Completed", className: "bg-green-50 text-green-700 border-green-200" },
  escalated: { label: "Escalated", className: "bg-orange-50 text-orange-700 border-orange-200" },
  error: { label: "Error", className: "bg-red-50 text-red-700 border-red-200" },
  resolved: { label: "Resolved", className: "bg-blue-50 text-blue-700 border-blue-200" },
  scheduled: { label: "Scheduled", className: "bg-purple-50 text-purple-700 border-purple-200" },
};

export default function StatusBadge({ status }) {
  const config = STATUS_CONFIG[status] || {
    label: status || "Unknown",
    className: "bg-gray-50 text-gray-600 border-gray-200",
  };

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${config.className}`}>
      {config.label}
    </span>
  );
}