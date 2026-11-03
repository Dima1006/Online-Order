"""In-process publish/subscribe for WebSocket pushes. Works with ONE server process."""
import asyncio
from collections import defaultdict

from starlette.websockets import WebSocketDisconnect

from . import config as cfg

MAX_PER_CHANNEL = 20
MAX_MESSAGE_CHARS = 16  # clients only ever send "ping"


class Hub:
    def __init__(self):
        self.channels: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.per_ip: dict[str, int] = defaultdict(int)

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


async def serve(ws, channels: list[str], ip: str = "") -> None:
    """Accept the socket and forward hub messages until the client goes away."""
    if ip and hub.per_ip[ip] >= cfg.WS_MAX_PER_IP:
        await ws.close(code=1013)  # too many connections from one address
        return
    q = hub.subscribe(channels)
    if q is None:
        await ws.close(code=1013)  # try again later
        return
    if ip:
        hub.per_ip[ip] += 1
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
                text = msg.get("text")
                if msg.get("bytes") or (text is not None and len(text) > MAX_MESSAGE_CHARS):
                    await ws.close(code=1009)  # message too big: we never expect more than "ping"
                    break
                if text == "ping":  # client heartbeat keeps proxies from closing idle sockets
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
        if ip:
            hub.per_ip[ip] -= 1
            if hub.per_ip[ip] <= 0:
                del hub.per_ip[ip]
