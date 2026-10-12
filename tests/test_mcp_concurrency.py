"""HTTP-transport concurrency: sync tools must not block the async event loop.

mcp 1.x invoked a *synchronous* tool function INLINE on the event loop
(``func_metadata.py: ``return fn(**args)``), so a slow corpus scan froze every
other connection under the shared ``streamable-http`` server.  mcp 2.x fixed
that in the SDK: ``FuncMetadata.call_fn`` offloads a sync tool to a worker
thread (``anyio.to_thread.run_sync``) itself.

These tests pin the spec against our server (``_StrictArgsMCPServer``, whose
``call_tool`` delegates the dispatch to the SDK path): a sync tool
(1) runs OFF the event-loop thread and (2) N calls run concurrently
(> 1 in flight at once), never serialized.  Fully hermetic — a
purpose-built in-test tool, no host session data.
"""

from __future__ import annotations

import threading

from ai_r.mcp_server import _StrictArgsMCPServer


async def test_sync_tool_runs_off_the_event_loop_thread() -> None:
    """A sync tool must execute on a worker thread, not the event-loop thread.

    If the SDK ran it inline (the mcp 1.x bug), the tool body would observe
    the same thread id as the awaiting test coroutine.
    """
    mcp = _StrictArgsMCPServer(name="test-offload")
    seen: dict[str, int] = {}

    @mcp.tool()
    def whoami() -> dict:
        seen["thread"] = threading.get_ident()
        return {"ok": True}

    loop_thread = threading.get_ident()
    await mcp.call_tool("whoami", {})

    assert seen["thread"] != loop_thread


async def test_sync_tools_run_concurrently_not_serialized() -> None:
    """N sync tool calls must be in flight at once, not serialized on the loop.

    A ``threading.Barrier`` of N parties only releases if all N tool bodies run
    at the same time (concurrency > 1, the spec). If the calls serialized on
    the single event-loop thread, the first ``barrier.wait()`` would block the
    loop, the rest would never start, and the barrier would time out
    (BrokenBarrier).
    """
    import asyncio

    n = 3
    barrier = threading.Barrier(n, timeout=10)
    mcp = _StrictArgsMCPServer(name="test-concurrency")

    @mcp.tool()
    def rendezvous(i: int) -> dict:
        # Blocks until all N calls arrive — only possible if they run in
        # parallel worker threads rather than one-at-a-time on the loop.
        idx = barrier.wait()
        return {"i": i, "arrival": idx}

    results = await asyncio.gather(
        *(mcp.call_tool("rendezvous", {"i": i}) for i in range(n))
    )

    assert len(results) == n
