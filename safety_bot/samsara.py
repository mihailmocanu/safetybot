import asyncio
import time
from urllib.parse import urlsplit

import aiohttp


class ApiError(Exception):
    def __init__(self, status, delay=60):
        self.status = status
        self.delay = delay
        super().__init__(f"Samsara HTTP {status}")


class MediaError(Exception):
    pass


class Samsara:
    def __init__(self, session, token, base_url):
        self.session, self.token, self.base_url = session, token, base_url
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    async def get(self, path, params):
        async with self.lock:
            await asyncio.sleep(max(0, .25 - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            async with self.session.get(
                self.base_url + path, params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=60), allow_redirects=False,
            ) as response:
                if response.status != 200:
                    try:
                        delay = max(1, float(response.headers.get("Retry-After", "60")))
                    except ValueError:
                        delay = 60
                    raise ApiError(response.status, delay)
                return await response.json()

    @staticmethod
    def expanded():
        return {"includeAsset": "true", "includeDriver": "true", "includeVgOnlyEvents": "true"}

    async def stream(self, start, cursor):
        params = {**self.expanded(), "startTime": start, "queryByTimeField": "updatedAtTime"}
        if cursor:
            params["after"] = cursor
        return await self.get("/safety-events/stream", params)

    async def event(self, event_id):
        result = await self.get("/safety-events", {**self.expanded(), "safetyEventIds": event_id})
        return next((x for x in result["data"] if x["id"] == event_id), None)

    async def pages(self, path, params):
        items, after = [], None
        while True:
            query = dict(params)
            if after:
                query["after"] = after
            page = await self.get(path, query)
            items.extend(page["data"])
            pagination = page["pagination"]
            if not pagination["hasNextPage"]:
                return items
            cursor = pagination.get("endCursor")
            if not cursor or cursor == after:
                raise ValueError("Pagination did not advance")
            after = cursor

    async def speed_readings(self):
        return await self.pages("/readings/latest", {
            "readingIds": "samsaraSpeed,samsaraSpeedLimit,location",
            "entityType": "asset", "assetTypes": "vehicle",
        })

    async def vehicles(self):
        return await self.pages("/assets", {"type": "vehicle"})

    async def safety_history(self, start, end, cursor=None):
        params = {**self.expanded(), "startTime": start, "endTime": end,
                  "queryByTimeField": "createdAtTime"}
        if cursor:
            params["after"] = cursor
        return await self.get("/safety-events/stream", params)

    async def download(self, url, path):
        # Signed URLs from official event media. Do not forward the API Bearer token.
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or not (
            host.endswith(".amazonaws.com") or host.endswith(".samsara.com")
            or host.endswith(".samsara.net")
        ):
            raise MediaError("Media host not in official allowlist; review API response")
        async with self.session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=180)) as response:
            if response.status != 200:
                raise MediaError(f"Media HTTP {response.status}; URL will be refreshed")
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            if content_type not in ("video/mp4", "application/octet-stream", "binary/octet-stream"):
                raise MediaError("Media is not an MP4 clip")
            size = 0
            with path.open("wb") as output:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    size += len(chunk)
                    if size > 250 * 1024 * 1024:
                        raise MediaError("Media exceeds temporary download limit (250 MiB)")
                    output.write(chunk)
            if size < 12:
                raise MediaError("Empty or invalid media")
            with path.open("rb") as video:
                if video.read(12)[4:8] != b"ftyp":
                    raise MediaError("Media is not an MP4 container")


async def telegram_size(path):
    # Telegram cloud Bot API upload limit is 50 MB. Leave margin below the limit.
    if path.stat().st_size <= 49_000_000:
        return path
    output = path.with_name("compressed.mp4")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-i", str(path), "-vf", "scale=960:-2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "30", "-c:a", "aac",
        "-b:a", "64k", "-movflags", "+faststart", str(output),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), 180)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode or not output.exists() or output.stat().st_size > 49_000_000:
        raise MediaError("Clip cannot fit Telegram upload limit; will retry later")
    return output
