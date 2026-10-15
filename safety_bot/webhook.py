import hashlib
import hmac
import time

from aiohttp import web


def valid_signature(secret, timestamp, signature, body, now=None):
    try:
        if abs((time.time() if now is None else now) - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    expected = "v1=" + hmac.new(secret, b"v1:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def start_webhook(secret, port, wake):
    async def receive(request):
        if not secret:
            raise web.HTTPNotFound()
        body = await request.read()
        if not valid_signature(secret, request.headers.get("X-Samsara-Timestamp", ""), request.headers.get("X-Samsara-Signature", ""), body):
            raise web.HTTPUnauthorized()
        # Treat alerts as hints. Stream is the source of truth, IDs in alert payloads
        # are not necessarily Safety Event IDs. Persistent cursor recovers missed hints.
        wake.set()
        return web.Response(status=202)

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/samsara/webhook", receive)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
