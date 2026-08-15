"use client";

import { useMemo, useState } from "react";

/**
 * Renders one call's raw agent log.
 *
 * The agent writes loguru's default-ish format:
 *   2026-08-14 05:40:36.708 | INFO     | services.call_logger:start_call_logging:141 - message
 *
 * Parsing happens here rather than server-side for two reasons: the endpoint streams
 * the blob straight through with no work, and a 200KB log inflates well past that as
 * JSON. The regex is deliberately forgiving — anything it can't parse is kept as a
 * continuation of the preceding record, which is what tracebacks and the pretty-printed
 * JSON payloads the agent logs actually are. Nothing is ever dropped.
 *
 * DEBUG is hidden by default because roughly half of every log is pipecat's internal
 * frame-linking chatter, and the lines someone opens this panel to find are not in it.
 */

const LINE_RE =
  /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| (\w+)\s*\| ([^\s]+?):([^\s:]+):(\d+) - ([\s\S]*)$/;

// Rows rendered before the "show all" escape. A long log is ~2,000 records and each is
// a flex row, so rendering the lot on open costs a visible stall for no benefit.
const INITIAL_ROWS = 500;

const LEVEL_ORDER = ["ERROR", "CRITICAL", "WARNING", "SUCCESS", "INFO", "DEBUG", "TRACE"];

const LEVEL_STYLES: Record<string, string> = {
  CRITICAL: "text-red-300",
  ERROR: "text-red-400",
  WARNING: "text-amber-400",
  SUCCESS: "text-emerald-400",
  INFO: "text-zinc-300",
  DEBUG: "text-zinc-500",
  TRACE: "text-zinc-600",
};

type Entry = {
  time: string;
  level: string;
  source: string;
  message: string;
};

function parseLog(text: string): Entry[] {
  const entries: Entry[] = [];
  for (const line of text.split("\n")) {
    const match = LINE_RE.exec(line);
    if (match) {
      const [, time, level, mod, fn, lineNo, message] = match;
      entries.push({
        time: time.slice(11), // date is the same all through; the clock time is the useful part
        level: level.toUpperCase(),
        source: `${mod}:${fn}:${lineNo}`,
        message,
      });
    } else if (entries.length > 0) {
      // Traceback / wrapped JSON — belongs to the record above it.
      entries[entries.length - 1].message += `\n${line}`;
    } else if (line.trim()) {
      entries.push({ time: "", level: "INFO", source: "", message: line });
    }
  }
  return entries;
}

// A handful of lines per log are enormous — the verbatim telephony start event, full
// LLM request bodies. Wrapped, one of those is a screenful on its own and buries the
// lines around it, so they open on demand.
const CLAMP_CHARS = 600;

function LogMessage({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (text.length <= CLAMP_CHARS) return <>{text}</>;
  return (
    <>
      {open ? text : text.slice(0, CLAMP_CHARS)}
      <button
        onClick={() => setOpen((v) => !v)}
        className="ml-1 rounded bg-zinc-800 px-1.5 text-zinc-400 hover:text-zinc-200"
      >
        {open ? "less" : `… +${(text.length - CLAMP_CHARS).toLocaleString()} chars`}
      </button>
    </>
  );
}

export default function LogViewer({ text, downloadUrl }: { text: string; downloadUrl: string }) {
  const entries = useMemo(() => parseLog(text), [text]);

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const e of entries) out[e.level] = (out[e.level] ?? 0) + 1;
    return out;
  }, [entries]);

  // Most severe first; anything loguru-custom that we don't know about sorts to the end.
  const rank = (level: string) => {
    const i = LEVEL_ORDER.indexOf(level);
    return i === -1 ? LEVEL_ORDER.length : i;
  };
  const levels = useMemo(
    () => Object.keys(counts).sort((a, b) => rank(a) - rank(b)),
    [counts]
  );

  const [hidden, setHidden] = useState<Set<string>>(new Set(["DEBUG", "TRACE"]));
  const [query, setQuery] = useState("");
  const [showAll, setShowAll] = useState(false);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return entries.filter(
      (e) =>
        !hidden.has(e.level) &&
        (!needle ||
          e.message.toLowerCase().includes(needle) ||
          e.source.toLowerCase().includes(needle))
    );
  }, [entries, hidden, query]);

  const visible = showAll ? filtered : filtered.slice(0, INITIAL_ROWS);

  function toggle(level: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(level)) next.delete(level);
      else next.add(level);
      return next;
    });
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {levels.map((level) => (
          <button
            key={level}
            onClick={() => toggle(level)}
            className={`rounded-full border px-2.5 py-1 text-xs transition ${
              hidden.has(level)
                ? "border-zinc-800 text-zinc-600"
                : `border-zinc-700 bg-zinc-800/60 ${LEVEL_STYLES[level] ?? "text-zinc-300"}`
            }`}
            title={hidden.has(level) ? `Show ${level} lines` : `Hide ${level} lines`}
          >
            {level} {counts[level]}
          </button>
        ))}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter lines…"
          className="ml-auto w-56 rounded-md border border-zinc-800 bg-zinc-950 px-2.5 py-1 text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-zinc-600 focus:outline-none"
        />
        <a
          href={downloadUrl}
          className="rounded-md border border-zinc-800 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
        >
          Download .log
        </a>
      </div>

      {/* No min-w-max here, and the message column wraps. Production logs inline the
          verbatim telephony start event as a single ~2KB unbroken line; letting the
          row size to its widest content blows the container out to tens of thousands
          of pixels and freezes the renderer on open. Wrap instead of scroll. */}
      <div className="max-h-[32rem] overflow-y-auto overflow-x-hidden rounded-lg border border-zinc-800 bg-zinc-950">
        <div className="font-mono text-xs leading-relaxed">
          {visible.map((entry, i) => (
            <div key={i} className="flex gap-3 px-3 py-0.5 hover:bg-zinc-900/60">
              <span className="shrink-0 text-zinc-600">{entry.time}</span>
              <span className={`w-16 shrink-0 ${LEVEL_STYLES[entry.level] ?? "text-zinc-400"}`}>
                {entry.level}
              </span>
              <span
                className="hidden w-56 shrink-0 truncate text-zinc-600 xl:block"
                title={entry.source}
              >
                {entry.source}
              </span>
              <span
                className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${
                  LEVEL_STYLES[entry.level] ?? "text-zinc-300"
                }`}
              >
                <LogMessage text={entry.message} />
              </span>
            </div>
          ))}
          {visible.length === 0 && (
            <div className="px-3 py-6 text-center text-zinc-600">
              No lines match this filter.
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-zinc-600">
        <span>
          {filtered.length.toLocaleString()} of {entries.length.toLocaleString()} lines
          {!showAll && filtered.length > visible.length
            ? ` — showing the first ${INITIAL_ROWS.toLocaleString()}`
            : ""}
        </span>
        {!showAll && filtered.length > visible.length && (
          <button onClick={() => setShowAll(true)} className="text-zinc-400 hover:text-zinc-200">
            Show all
          </button>
        )}
      </div>
    </div>
  );
}
