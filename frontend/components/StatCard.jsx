import { TrendingUp, TrendingDown } from "lucide-react";

export default function StatCard({ label, value, trend, trendLabel, icon: Icon, color = "blue" }) {
  const colorMap = {
    blue: "bg-blue-50 text-blue-600",
    green: "bg-green-50 text-green-600",
    orange: "bg-orange-50 text-orange-600",
    red: "bg-red-50 text-red-600",
  };

  return (
    <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-gray-500 font-medium">{label}</p>
          <p className="mt-1 text-2xl font-bold text-gray-900">{value ?? "—"}</p>
          {trendLabel && (
            <div className="flex items-center gap-1 mt-1">
              {trend === "up" ? (
                <TrendingUp className="w-3 h-3 text-green-500" />
              ) : trend === "down" ? (
                <TrendingDown className="w-3 h-3 text-red-500" />
              ) : null}
              <span className="text-xs text-gray-400">{trendLabel}</span>
            </div>
          )}
        </div>
        {Icon && (
          <div className={`p-2.5 rounded-lg ${colorMap[color] || colorMap.blue}`}>
            <Icon className="w-5 h-5" />
          </div>
        )}
      </div>
    </div>
  );
}