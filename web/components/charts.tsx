"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimeseriesPoint } from "@/lib/api";

const tooltipStyle = {
  backgroundColor: "#18181b",
  border: "1px solid #3f3f46",
  borderRadius: 8,
  fontSize: 12,
  color: "#e4e4e7",
};

function shortDate(d: string) {
  return d.slice(5); // MM-DD
}

export function VolumeChart({ data }: { data: TimeseriesPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -24 }}>
        <defs>
          <linearGradient id="volFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#6366f1" stopOpacity={0.5} />
            <stop offset="100%" stopColor="#6366f1" stopOpacity={0.05} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#27272a" vertical={false} />
        <XAxis dataKey="date" tickFormatter={shortDate} stroke="#52525b" fontSize={11} />
        <YAxis allowDecimals={false} stroke="#52525b" fontSize={11} />
        <Tooltip contentStyle={tooltipStyle} />
        <Area type="monotone" dataKey="calls" stroke="#818cf8" fill="url(#volFill)" strokeWidth={2} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function SuccessChart({ data }: { data: TimeseriesPoint[] }) {
  const rows = data.map((d) => ({
    ...d,
    success_pct: d.success_rate == null ? null : Math.round(d.success_rate * 100),
  }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -24 }}>
        <CartesianGrid stroke="#27272a" vertical={false} />
        <XAxis dataKey="date" tickFormatter={shortDate} stroke="#52525b" fontSize={11} />
        <YAxis domain={[0, 100]} stroke="#52525b" fontSize={11} unit="%" />
        <Tooltip contentStyle={tooltipStyle} />
        <Line
          type="monotone"
          dataKey="success_pct"
          name="success rate"
          stroke="#34d399"
          strokeWidth={2}
          dot={false}
          connectNulls
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function LatencyTrend({
  data,
}: {
  data: { date: string; p50: number | null; p95: number | null }[];
}) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
        <CartesianGrid stroke="#27272a" vertical={false} />
        <XAxis dataKey="date" tickFormatter={shortDate} stroke="#52525b" fontSize={11} />
        <YAxis stroke="#52525b" fontSize={11} unit="ms" />
        <Tooltip contentStyle={tooltipStyle} />
        <Line type="monotone" dataKey="p50" stroke="#818cf8" strokeWidth={2} dot={false} connectNulls />
        <Line type="monotone" dataKey="p95" stroke="#f59e0b" strokeWidth={2} dot={false} connectNulls />
      </LineChart>
    </ResponsiveContainer>
  );
}

const SENTIMENT_COLORS: Record<string, string> = {
  positive: "#34d399",
  neutral: "#a1a1aa",
  negative: "#f87171",
};

export function SentimentDonut({ distribution }: { distribution: Record<string, number> }) {
  const data = Object.entries(distribution)
    .map(([name, value]) => ({ name, value }))
    .filter((d) => d.value > 0);
  if (data.length === 0)
    return <div className="flex h-[220px] items-center justify-center text-sm text-zinc-500">No sentiment data</div>;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={55} outerRadius={85} paddingAngle={3} stroke="none">
          {data.map((entry) => (
            <Cell key={entry.name} fill={SENTIMENT_COLORS[entry.name] ?? "#a1a1aa"} />
          ))}
        </Pie>
        <Tooltip contentStyle={tooltipStyle} />
      </PieChart>
    </ResponsiveContainer>
  );
}

const OUTCOME_COLORS: Record<string, string> = {
  completed: "#34d399",
  transferred: "#a78bfa",
  non_completed: "#f87171",
};

const OUTCOME_LABELS: Record<string, string> = {
  completed: "Completed",
  transferred: "Transferred",
  non_completed: "Non-completed",
};

export function OutcomeDonut({ distribution }: { distribution: Record<string, number> }) {
  const data = Object.entries(distribution)
    .map(([name, value]) => ({ name: OUTCOME_LABELS[name] ?? name, key: name, value }))
    .filter((d) => d.value > 0);
  if (data.length === 0)
    return <div className="flex h-[220px] items-center justify-center text-sm text-zinc-500">No outcome data</div>;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={55} outerRadius={85} paddingAngle={3} stroke="none">
          {data.map((entry) => (
            <Cell key={entry.key} fill={OUTCOME_COLORS[entry.key] ?? "#a1a1aa"} />
          ))}
        </Pie>
        <Tooltip contentStyle={tooltipStyle} />
      </PieChart>
    </ResponsiveContainer>
  );
}

export function ReasonBars({ data }: { data: { reason: string; count: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} layout="vertical" margin={{ top: 8, right: 16, bottom: 0, left: 24 }}>
        <CartesianGrid stroke="#27272a" horizontal={false} />
        <XAxis type="number" allowDecimals={false} stroke="#52525b" fontSize={11} />
        <YAxis type="category" dataKey="reason" stroke="#52525b" fontSize={11} width={90} />
        <Tooltip contentStyle={tooltipStyle} />
        <Bar dataKey="count" fill="#818cf8" radius={[0, 4, 4, 0]} barSize={18} />
      </BarChart>
    </ResponsiveContainer>
  );
}
