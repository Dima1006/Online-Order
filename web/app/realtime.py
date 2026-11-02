"""In-process publish/subscribe for WebSocket pushes. Works with ONE server process."""
import asyncio
from collections import defaultdict

from starlette.websockets import WebSocketDisconnect

MAX_PER_CHANNEL = 20


class Hub:
    def __init__(self):
        self.channels: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, channels: list[str]) -> asyncio.Queue | None:
        if any(len(self.channels[c]) >= MAX_PER_CHANNEL for c in channels):
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        q.channels = list(channels)  # type: ignore[attr-defined]
        for c in channels:
            self.channels[c].add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for c in q.channels:  # type: ignore[attr-defined]
            self.channels[c].discard(q)
            if not self.channels[c]:
                del self.channels[c]

    def publish(self, channel: str, message: dict) -> None:
        for q in list(self.channels.get(channel, ())):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass  # a stuck client must not block everyone else; it will resync on reconnect


hub = Hub()


async def serve(ws, channels: list[str]) -> None:
    """Accept the socket and forward hub messages until the client goes away."""
    q = hub.subscribe(channels)
    if q is None:
        await ws.close(code=1013)  # try again later
        return
    await ws.accept()
    getter = asyncio.ensure_future(q.get())
    receiver = asyncio.ensure_future(ws.receive())
    try:
        while True:
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                msg = receiver.result()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") == "ping":  # client heartbeat keeps proxies from closing idle sockets
                    await ws.send_text("pong")
                receiver = asyncio.ensure_future(ws.receive())
            if getter in done:
                await ws.send_json(getter.result())
                getter = asyncio.ensure_future(q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        getter.cancel()
        receiver.cancel()
        hub.unsubscribe(q)
