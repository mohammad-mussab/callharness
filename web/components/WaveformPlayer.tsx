"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { formatClock } from "@/lib/format";

/**
 * Stereo waveform player for a call recording.
 *
 * WHY A WAVEFORM AND NOT JUST <audio controls>
 * The SDK records with num_channels=2 — caller on the left channel, assistant on
 * the right (see attach_audio's placement rules). Drawing the two channels as
 * separate lanes turns the recording into a picture of the conversation: who
 * spoke when, how long the silences were, and where the two overlap, which is
 * exactly what an interruption looks like. A single mixed bar shows none of that.
 *
 * Colours are deliberately the ones the transcript below already uses — indigo
 * for the assistant, zinc for the caller — so the two views read as one thing
 * and no legend has to be learned.
 *
 * Falls back to the native player if Web Audio can't decode the file, so a
 * recording is never unplayable just because it couldn't be drawn.
 */

const LANE_HEIGHT = 44;
const LANE_GAP = 6;
const CALLER = { played: "#d4d4d8", pending: "#3f3f46" }; // zinc-300 / zinc-700
const ASSISTANT = { played: "#818cf8", pending: "#3730a3" }; // indigo-400 / indigo-800

type Peaks = { caller: number[]; assistant: number[]; stereo: boolean };

/** Bucket samples into one peak per pixel column. */
function peaksFor(data: Float32Array, buckets: number): number[] {
  const size = Math.floor(data.length / buckets) || 1;
  const out = new Array<number>(buckets);
  for (let i = 0; i < buckets; i++) {
    const start = i * size;
    let max = 0;
    // Step through the bucket rather than reading every sample: at ~50 samples
    // per pixel the peak is identical and the decode stays instant on long calls.
    for (let j = start; j < start + size && j < data.length; j += 4) {
      const v = Math.abs(data[j]);
      if (v > max) max = v;
    }
    out[i] = max;
  }
  return out;
}

export default function WaveformPlayer({
  src,
  audioRef,
  onTimeUpdate,
}: {
  src: string;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  onTimeUpdate: (t: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [failed, setFailed] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [width, setWidth] = useState(0);

  // --- decode once -----------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(src);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const buf = await res.arrayBuffer();
        const Ctx =
          window.AudioContext ||
          (window as unknown as { webkitAudioContext: typeof AudioContext })
            .webkitAudioContext;
        const ctx = new Ctx();
        const decoded = await ctx.decodeAudioData(buf);
        await ctx.close();
        if (cancelled) return;
        const buckets = 900; // resampled to the canvas width at draw time
        const stereo = decoded.numberOfChannels > 1;
        setPeaks({
          caller: peaksFor(decoded.getChannelData(0), buckets),
          assistant: peaksFor(
            decoded.getChannelData(stereo ? 1 : 0),
            buckets
          ),
          stereo,
        });
        setDuration(decoded.duration);
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [src]);

  // --- track width -----------------------------------------------------------
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  // --- draw ------------------------------------------------------------------
  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !peaks || !width) return;
    const dpr = window.devicePixelRatio || 1;
    const lanes = peaks.stereo ? 2 : 1;
    const height = lanes * LANE_HEIGHT + (lanes - 1) * LANE_GAP;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const total = duration || audioRef.current?.duration || 0;
    const progressX = total > 0 ? (time / total) * width : 0;
    const barW = 2;
    const step = 3; // bar + 1px gap
    const columns = Math.floor(width / step);

    const series: Array<{ data: number[]; color: typeof CALLER }> = peaks.stereo
      ? [
          { data: peaks.caller, color: CALLER },
          { data: peaks.assistant, color: ASSISTANT },
        ]
      : [{ data: peaks.caller, color: ASSISTANT }];

    series.forEach((s, lane) => {
      const mid = lane * (LANE_HEIGHT + LANE_GAP) + LANE_HEIGHT / 2;
      for (let i = 0; i < columns; i++) {
        const x = i * step;
        const peak = s.data[Math.floor((i / columns) * s.data.length)] ?? 0;
        // sqrt lifts quiet speech into view without clipping the loud parts
        const h = Math.max(1.5, Math.sqrt(peak) * (LANE_HEIGHT / 2 - 2));
        ctx.fillStyle = x <= progressX ? s.color.played : s.color.pending;
        ctx.fillRect(x, mid - h, barW, h * 2);
      }
    });

    if (total > 0) {
      ctx.fillStyle = "#fafafa";
      ctx.fillRect(progressX, 0, 1, height);
    }
  }, [peaks, width, time, duration, audioRef]);

  useEffect(() => {
    draw();
  }, [draw]);

  // --- smooth playhead while playing ----------------------------------------
  useEffect(() => {
    if (!playing) {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      return;
    }
    const tick = () => {
      const el = audioRef.current;
      if (el) {
        setTime(el.currentTime);
        onTimeUpdate(el.currentTime);
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [playing, audioRef, onTimeUpdate]);

  function seekTo(clientX: number) {
    const canvas = canvasRef.current;
    const el = audioRef.current;
    if (!canvas || !el) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    const total = duration || el.duration || 0;
    if (!total) return;
    el.currentTime = ratio * total;
    setTime(el.currentTime);
    onTimeUpdate(el.currentTime);
  }

  function toggle() {
    const el = audioRef.current;
    if (!el) return;
    el.paused ? el.play() : el.pause();
  }

  const total = duration || audioRef.current?.duration || 0;

  return (
    <div ref={wrapRef}>
      <audio
        ref={audioRef}
        preload="metadata"
        src={src}
        controls={failed}
        className={failed ? "w-full" : "hidden"}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onLoadedMetadata={(e) => {
          if (!duration) setDuration(e.currentTarget.duration);
        }}
        onTimeUpdate={(e) => {
          // Keeps the playhead honest when paused or seeked from the transcript,
          // where the rAF loop above isn't running.
          if (!playing) {
            setTime(e.currentTarget.currentTime);
            onTimeUpdate(e.currentTarget.currentTime);
          }
        }}
      />

      {failed ? null : !peaks ? (
        <div className="flex h-[94px] items-center justify-center rounded-lg border border-zinc-800 bg-zinc-950/40 text-xs text-zinc-600">
          Loading waveform…
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <button
            onClick={toggle}
            aria-label={playing ? "Pause" : "Play"}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-zinc-700 bg-zinc-800 text-zinc-200 transition hover:border-indigo-500 hover:text-indigo-300"
          >
            {playing ? (
              <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
                <rect x="2" y="1" width="3.5" height="12" rx="1" />
                <rect x="8.5" y="1" width="3.5" height="12" rx="1" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
                <path d="M3 1.5v11l9.5-5.5L3 1.5z" />
              </svg>
            )}
          </button>

          <div className="min-w-0 flex-1">
            <canvas
              ref={canvasRef}
              onClick={(e) => seekTo(e.clientX)}
              className="w-full cursor-pointer"
            />
            {peaks.stereo && (
              <div className="mt-1 flex items-center gap-4 text-[11px] text-zinc-500">
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-sm bg-zinc-300" />
                  Caller
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-sm bg-indigo-400" />
                  Assistant
                </span>
              </div>
            )}
          </div>

          <div className="shrink-0 font-mono text-xs tabular-nums text-zinc-400">
            {formatClock(time)} / {total ? formatClock(total) : "–:–"}
          </div>
        </div>
      )}
    </div>
  );
}
