"use client";

import Link from "next/link";
import useSWR from "swr";
import { fetcher, type CallList } from "@/lib/api";
import { formatDate, formatDuration } from "@/lib/format";
import { OutcomeBadge } from "@/components/Badges";

/**
 * The review queue for calls the analysis couldn't place.
 *
 * `other` is a designed outcome, not a failure: the taxonomy is deliberately closed so
 * the model can never invent a key and fragment the charts, which means everything that
 * doesn't fit has to land somewhere. This page is that somewhere. It exists so the
 * bucket list can grow from evidence — read the notes, spot a theme occurring often
 * enough to be worth its own slice, and add it in Settings.
 *
 * The note is the whole point, so it gets the room rather than being a hover title as
 * it is elsewhere. Reads the ordinary calls endpoint; nothing bespoke on the server.
 */

const PAGE_SIZE = 100;

export default function OtherPage() {
  const { data } = useSWR<CallList>(
    `/api/v1/calls?bucket=other&limit=${PAGE_SIZE}`,
    fetcher,
    { refreshInterval: 30000, keepPreviousData: true },
  );

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Other</h1>
        <p className="text-sm text-zinc-500">
          {data ? `${data.total} calls didn't fit any bucket` : "Loading…"}
          {data && data.total > 0 && (
            <>
              {" "}— read the notes, and when the same thing keeps appearing, add a bucket
              for it in{" "}
              <Link href="/settings" className="text-indigo-400 hover:underline">
                Settings
              </Link>
              .
            </>
          )}
        </p>
      </div>

      {data && data.total === 0 && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-10 text-center">
          <p className="text-sm text-zinc-400">Nothing here.</p>
          <p className="mt-1 text-sm text-zinc-500">
            Every analysed call landed in a named bucket. Worth checking back after a
            change to the agent or the bucket descriptions.
          </p>
        </div>
      )}

      <div className="space-y-2">
        {data?.items.map((call) => (
          <div
            key={call.id}
            className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4"
          >
            <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-500">
              <Link
                href={`/calls/${call.id}`}
                className="font-medium text-indigo-400 hover:underline"
              >
                {formatDate(call.started_at)}
              </Link>
              <span>{call.agent_id}</span>
              <span>{formatDuration(call.duration_seconds)}</span>
              <OutcomeBadge outcome={call.outcome} />
              {/* The agent's own id for this call, so it can be pulled up in whatever
                  system the customer actually works in. */}
              {call.external_id && (
                <span className="font-mono text-zinc-600">{call.external_id}</span>
              )}
            </div>
            <p className="mt-2 text-sm leading-relaxed text-zinc-200">
              {call.issue_note ?? (
                <span className="text-zinc-600">
                  No note — the analysis placed this in `other` without explaining why.
                </span>
              )}
            </p>
            {call.summary && (
              <p className="mt-1.5 text-sm leading-relaxed text-zinc-500">
                {call.summary}
              </p>
            )}
          </div>
        ))}
      </div>

      {data && data.total > PAGE_SIZE && (
        <p className="text-sm text-zinc-500">
          Showing the {PAGE_SIZE} most recent of {data.total}. If this list is long, that
          is itself the finding — the bucket descriptions probably need work.
        </p>
      )}
    </div>
  );
}
