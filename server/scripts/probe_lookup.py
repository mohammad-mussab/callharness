"""Find out what a lookup API actually accepts, before trusting a probe config.

WHY THIS EXISTS

A gap verification probe (app/gap_verification.py) re-asks a question that came back
empty during a call, and reads "nothing came back" as proof the record is absent. That
inference is only safe if the request reached the right handler. If the URL and the tool
name disagree, these backends answer 200 OK with a polite Italian sentence saying the
tool is not supported — which every empty-marker check in this repo reads as "the record
is missing", and which then goes to the customer as a record they should add.

That is not hypothetical here. The Lazio agent's production .env posts

    knowledge_base_lazio  ->  {base}/query_new
    call_graph_lazio      ->  {base}/call_graph

while the voilavoiceagent source tree routes those two tool names to /lazio/rag_lazio
and /lazio/call_graph_lazio, and matches only knowledge_base_new / call_graph on the two
paths the agent actually uses. The agent also talks to a different Azure hostname than
the one the repo's GitHub Action deploys to, so the running build is probably not the
source we can read. Guessing is not an option; this script asks.

COST

Discovery (--routes) is free: two GETs, no LLM involved.

Probing (--probe) is NOT free and does not bill us. Every call to a RAG endpoint spends
the API owner's OpenAI credits (an STT-cleanup call, an embedding, and an answer-
generation call); every call to a graph endpoint is two or three gpt-4.1 calls plus a
Neo4j round trip. There is no rate limiting, caching or circuit breaker on that side and
the same instance serves live phone calls, so this runs one request at a time and makes
you pass --yes.

USAGE

    python -m scripts.probe_lookup --base https://HOST --routes
    python -m scripts.probe_lookup --base https://HOST --probe --query "orari della sede di Boccea 628" --yes

Pick a --query you know had a real answer during a call. A miss looks the same whether
the record is absent or the tool name was wrong, so only a query that *should* answer
tells the two apart.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx

# Candidate (path, tool name) pairs, in the order worth trying. The first two are what
# the Lazio agent sends today; the second two are where the source tree says those tool
# names live. Exactly one column should answer.
CANDIDATES: list[tuple[str, str, str]] = [
    ("rag", "/query_new", "knowledge_base_lazio"),
    ("rag", "/lazio/rag_lazio", "knowledge_base_lazio"),
    ("graph", "/call_graph", "call_graph_lazio"),
    ("graph", "/lazio/call_graph_lazio", "call_graph_lazio"),
]

# The argument key differs per source: the RAG endpoints read "query", the graph
# endpoints read "request" (falling back to "q"). Sending the wrong key produces
# "Errore: manca il parametro ..." rather than a miss, which is at least distinguishable.
ARG_KEYS = {"rag": "query", "graph": "request"}


def envelope(tool_name: str, arg_key: str, query: str) -> dict[str, Any]:
    """The VAPI tool-call envelope every one of these endpoints expects.

    `arguments` is a JSON *string* nested inside JSON. json.dumps does the escaping, so
    an Italian query with quotes or accents cannot break the envelope.
    """
    return {
        "message": {
            "toolCallList": [
                {
                    "id": "callharness-probe",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps({arg_key: query}, ensure_ascii=False),
                    },
                }
            ]
        }
    }


async def show_routes(base: str) -> None:
    """List what the deployed build actually serves. Free — two GETs, no LLM."""
    async with httpx.AsyncClient(timeout=30) as client:
        for path in ("/openapi.json", "/"):
            try:
                resp = await client.get(base + path)
            except Exception as exc:  # noqa: BLE001
                print(f"GET {path}  -> failed: {exc}")
                continue
            print(f"GET {path}  -> {resp.status_code}")
            if path == "/openapi.json" and resp.status_code == 200:
                try:
                    paths = sorted((resp.json().get("paths") or {}).keys())
                except ValueError:
                    print("  (body was not JSON)")
                    continue
                print(f"  {len(paths)} routes:")
                for p in paths:
                    print(f"    {p}")
            else:
                print(f"  {resp.text[:600]}")
            print()


async def probe_one(
    client: httpx.AsyncClient, base: str, kind: str, path: str, tool: str, query: str
) -> None:
    body = envelope(tool, ARG_KEYS[kind], query)
    started = time.monotonic()
    try:
        resp = await client.post(base + path, json=body)
    except Exception as exc:  # noqa: BLE001
        print(f"POST {path}  [{tool}]  -> failed after "
              f"{(time.monotonic() - started) * 1000:.0f}ms: {exc}\n")
        return
    ms = (time.monotonic() - started) * 1000
    print(f"POST {path}  [{tool}]  -> {resp.status_code} in {ms:.0f}ms")

    # These endpoints answer {"results": [{"toolCallId": ..., "result": ...}]}. Print the
    # result verbatim: distinguishing "tool not supported" from a real miss from a real
    # answer is the entire purpose of this run, and all three arrive as 200 OK.
    try:
        data = resp.json()
    except ValueError:
        print(f"  body (not JSON): {resp.text[:800]}\n")
        return
    results = data.get("results") if isinstance(data, dict) else None
    if isinstance(results, list) and results:
        result = results[0].get("result")
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        print(f"  result: {text[:800]}")
    else:
        print(f"  body: {json.dumps(data, ensure_ascii=False)[:800]}")
    print()


async def probe_all(base: str, query: str, only: str | None) -> None:
    # One at a time, deliberately. Concurrency here would multiply load on a service that
    # is also answering live calls.
    async with httpx.AsyncClient(timeout=120) as client:
        for kind, path, tool in CANDIDATES:
            if only and kind != only:
                continue
            await probe_one(client, base, kind, path, tool, query)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Service base URL, no trailing slash")
    parser.add_argument("--routes", action="store_true", help="List routes (free)")
    parser.add_argument("--probe", action="store_true", help="POST the candidate pairs (costs the API owner money)")
    parser.add_argument("--query", help="Question to send. Use one you know had a real answer.")
    parser.add_argument("--kind", choices=("rag", "graph"), help="Probe only one source")
    parser.add_argument("--yes", action="store_true", help="Required for --probe")
    args = parser.parse_args()

    base = args.base.rstrip("/")

    if not args.routes and not args.probe:
        parser.error("nothing to do: pass --routes and/or --probe")

    if args.routes:
        asyncio.run(show_routes(base))

    if args.probe:
        if not args.query:
            parser.error("--probe needs --query")
        if not args.yes:
            n = len([c for c in CANDIDATES if not args.kind or c[0] == args.kind])
            print(
                f"Refusing to send {n} requests to {base} without --yes.\n"
                "Each one spends the API owner's OpenAI credits and lands on a service "
                "that is also handling live calls."
            )
            return 1
        asyncio.run(probe_all(base, args.query, args.kind))

    return 0


if __name__ == "__main__":
    sys.exit(main())
