"""HTTP-transport concurrency: sync tools must not block the async event loop.

FastMCP invokes a *synchronous* tool function INLINE on the event loop
(``mcp/server/fastmcp/utilities/func_metadata.py``: ``return fn(**args)``).
Our read/search tools scan the corpus and can run for hundreds of ms, so under
the shared ``streamable-http`` server (``AI_R_MCP_TRANSPORT=http``) one in-flight
call would freeze the loop and starve every other connection until uvicorn's
keep-alive dropped it ("not connected" under N parallel readers).

These tests pin the spec: ``_StrictArgsFastMCP.call_tool`` offloads a sync tool
to a worker thread so (1) it runs OFF the event-loop thread and (2) N calls run
concurrently (> 1 in flight at once), never serialized. Fully hermetic — a
purpose-built in-test tool, no host session data.
"""

from __future__ import annotations

import threading

from ai_r.mcp_server import _StrictArgsFastMCP


async def test_sync_tool_runs_off_the_event_loop_thread() -> None:
    """A sync tool must execute on a worker thread, not the event-loop thread.

    If FastMCP ran it inline (the bug), the tool body would observe the same
    thread id as the awaiting test coroutine.
    """
    mcp = _StrictArgsFastMCP(name="test-offload")
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
    at the same time (concurrency > 1, the spec). If the calls serialized on the
    single event-loop thread, the first ``barrier.wait()`` would block the loop,
    the rest would never start, and the barrier would time out (BrokenBarrier).
    """
    import asyncio

    n = 3
    barrier = threading.Barrier(n, timeout=10)
    mcp = _StrictArgsFastMCP(name="test-concurrency")

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
