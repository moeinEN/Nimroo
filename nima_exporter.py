#!/usr/bin/env python3
"""Cross-platform Nima replay URL-to-MP4 exporter.

Usage:
    nima-export "https://example.edu/room/ROOM_CODE"

The exporter does not open or record a browser. It authenticates with an existing
saved browser session (or a Netscape cookie file), fetches Nima's SockJS/DDP replay
events and original audio, reconstructs the whiteboard timeline, and writes MP4.
"""
from __future__ import annotations

VERSION = "2.1.0"

import argparse
import http.cookiejar
import json
import math
import shutil
import subprocess
import os
import platform
import random
import re
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

try:
    import browser_cookie3
except ImportError:
    print("Missing browser-cookie3.")
    print("Install: python -m pip install browser-cookie3 websocket-client requests pillow imageio-ffmpeg")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Missing requests.")
    print("Install: python -m pip install browser-cookie3 websocket-client requests pillow imageio-ffmpeg")
    sys.exit(1)

try:
    import websocket
except ImportError:
    print("Missing websocket-client.")
    print("Install: python -m pip install browser-cookie3 websocket-client requests pillow imageio-ffmpeg")
    sys.exit(1)


ID_RE = re.compile(r"^[A-Za-z0-9_-]{15,24}$")
MEDIA_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+(?:recordedmedias|recordedmedia|/uploads/|\.webm|\.mp4|\.m4a|\.mp3|\.wav|\.ogg)[^\s\"'<>\\]*",
    re.IGNORECASE,
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145 Safari/537.36"
)
FFMPEG_BIN = "ffmpeg"


def die(message: str, code: int = 1) -> None:
    print(f"\nERROR: {message}")
    sys.exit(code)


def random_sockjs_server() -> str:
    return f"{random.randint(0, 999):03d}"


def random_sockjs_session(length: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def random_ddp_id(length: int = 17) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def extract_meeting_id(url: str) -> str:
    path_parts = [x for x in urlparse(url).path.split("/") if x]
    for marker in ("room", "join"):
        if marker in path_parts:
            idx = path_parts.index(marker)
            if idx + 1 < len(path_parts):
                return path_parts[idx + 1]
    if path_parts:
        return path_parts[-1]
    die("Could not extract room/meeting ID from URL.")
    raise AssertionError


def browser_candidates(requested: str) -> list[str]:
    if requested != "auto":
        return [requested]
    if sys.platform == "darwin":
        return ["firefox", "chrome", "chromium", "edge", "brave", "vivaldi", "safari"]
    if os.name == "nt":
        return ["firefox", "chrome", "edge", "brave", "chromium", "vivaldi", "opera"]
    return ["firefox", "chrome", "chromium", "edge", "brave", "vivaldi", "librewolf", "opera"]


def load_browser_cookies(browser: str, domain: str) -> http.cookiejar.CookieJar:
    errors: list[str] = []
    for candidate in browser_candidates(browser):
        loader = getattr(browser_cookie3, candidate, None)
        if loader is None:
            errors.append(f"{candidate}: unsupported by installed browser-cookie3")
            continue
        print(f"Trying saved cookies from {candidate} for {domain}...")
        try:
            jar = loader(domain_name=domain)
            matching = [c for c in jar if domain.endswith(c.domain.lstrip('.'))]
            if matching:
                print(f"Using saved session from {candidate}.")
                return jar
            errors.append(f"{candidate}: no matching cookies")
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")

    die(
        "Could not find a valid saved browser session for this Nima host.\n"
        "Log into Nima once, close the browser, and retry; or provide --cookies-file.\n"
        + "\n".join(f"  - {item}" for item in errors)
    )
    raise AssertionError


def load_netscape_cookie_file(path: Path) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        die(f"Could not load Netscape cookie file {path}: {exc}")
    return jar

def find_resume_token(cookies: http.cookiejar.CookieJar, domain: str) -> str:
    preferred_names = [
        "meteor_logintoken",
        "Meteor.loginToken",
        "meteor_login_token",
    ]

    cookie_list = list(cookies)

    for name in preferred_names:
        for cookie in cookie_list:
            if cookie.name == name and domain.endswith(cookie.domain.lstrip(".")):
                token = unquote(cookie.value).strip('"')
                if token:
                    return token

    # Conservative fallback: a Meteor resume token is generally a long URL-safe value.
    for cookie in cookie_list:
        if domain.endswith(cookie.domain.lstrip(".")):
            value = unquote(cookie.value).strip('"')
            if "token" in cookie.name.lower() and len(value) >= 30:
                return value

    die(
        "No saved Nima/Meteor resume token was found.\n"
        f"Log into https://{domain} once using the selected browser, close the browser, then rerun."
    )
    raise AssertionError


def cookie_header(cookies: http.cookiejar.CookieJar, domain: str) -> str:
    parts = []
    for cookie in cookies:
        if domain.endswith(cookie.domain.lstrip(".")):
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


def requests_session(cookies: http.cookiejar.CookieJar, user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    for cookie in cookies:
        session.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return session


def sockjs_pack(message: dict[str, Any]) -> str:
    inner = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return json.dumps([inner], ensure_ascii=False, separators=(",", ":"))


def sockjs_unpack(payload: str) -> list[dict[str, Any]]:
    if not payload or payload in {"o", "h"} or payload.startswith("c["):
        return []

    try:
        if payload.startswith("a["):
            outer = json.loads(payload[1:])
        elif payload.startswith("["):
            outer = json.loads(payload)
        else:
            outer = [payload]
    except Exception:
        return []

    output: list[dict[str, Any]] = []
    for item in outer:
        try:
            value = json.loads(item) if isinstance(item, str) else item
        except Exception:
            continue
        if isinstance(value, dict):
            output.append(value)
    return output


def recursively_find_media_urls(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        for url in MEDIA_URL_RE.findall(value.replace("\\/", "/")):
            found.add(url)
    elif isinstance(value, dict):
        for v in value.values():
            recursively_find_media_urls(v, found)
    elif isinstance(value, list):
        for v in value:
            recursively_find_media_urls(v, found)


def recursively_collect_prioritized_ids(
    value: Any,
    output: set[str],
    parent_key: str = "",
) -> None:
    """
    Collect only IDs from fields likely relevant to media/room/stream storage.
    This avoids broad unauthenticated enumeration.
    """
    useful_key_terms = (
        "media", "record", "upload", "file", "root", "room",
        "stream", "publisher", "presenter", "whiteboard", "tab",
    )

    if isinstance(value, dict):
        for key, val in value.items():
            recursively_collect_prioritized_ids(val, output, str(key))
    elif isinstance(value, list):
        for item in value:
            recursively_collect_prioritized_ids(item, output, parent_key)
    elif isinstance(value, str):
        if any(term in parent_key.lower() for term in useful_key_terms):
            if ID_RE.fullmatch(value):
                output.add(value)


class HeadlessNimaClient:
    def __init__(
        self,
        base_url: str,
        meeting_id: str,
        resume_token: str,
        cookies: str,
        timeout: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.meeting_id = meeting_id
        self.resume_token = resume_token
        self.cookies = cookies
        self.timeout = timeout

        parsed = urlparse(self.base_url)
        self.host = parsed.netloc
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self.ws_url = (
            f"{ws_scheme}://{self.host}/room/api/sockjs/"
            f"{random_sockjs_server()}/{random_sockjs_session()}/websocket"
        )

        self.ws = None
        self.all_messages: list[dict[str, Any]] = []
        self.playback_result: Optional[dict[str, Any]] = None
        self.recorded_events: list[dict[str, Any]] = []
        self.media_urls: set[str] = set()

    def send(self, message: dict[str, Any]) -> None:
        assert self.ws is not None
        self.ws.send(sockjs_pack(message))

    def receive_messages(self, wait_seconds: float = 30.0) -> Iterable[dict[str, Any]]:
        assert self.ws is not None
        deadline = time.time() + wait_seconds

        while time.time() < deadline:
            try:
                payload = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue

            if payload is None:
                break

            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")

            for msg in sockjs_unpack(payload):
                self.all_messages.append(msg)
                recursively_find_media_urls(msg, self.media_urls)
                yield msg

    def wait_for(self, predicate, description: str, seconds: float = 30.0) -> dict[str, Any]:
        for msg in self.receive_messages(seconds):
            if predicate(msg):
                return msg
            if msg.get("msg") == "error":
                die(f"Server returned an error while waiting for {description}: {msg}")
        die(f"Timed out waiting for {description}.")
        raise AssertionError

    def connect(self) -> None:
        # Avoid accidental proxying of the university WebSocket.
        current_no_proxy = os.environ.get("NO_PROXY", "")
        no_proxy_items = [x for x in current_no_proxy.split(",") if x]
        if self.host not in no_proxy_items:
            no_proxy_items.append(self.host)
        os.environ["NO_PROXY"] = ",".join(no_proxy_items)
        os.environ["no_proxy"] = os.environ["NO_PROXY"]

        print(f"Connecting directly to:\n  {self.ws_url}")

        try:
            self.ws = websocket.create_connection(
                self.ws_url,
                origin=self.origin,
                cookie=self.cookies,
                timeout=5,
                header=[
                    f"User-Agent: {DEFAULT_USER_AGENT}"
                ],
            )
        except Exception as exc:
            die(f"WebSocket connection failed: {exc}")

        # SockJS first sends "o".
        opened = False
        for _ in range(10):
            try:
                first = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if first == "o":
                opened = True
                break

        if not opened:
            die("SockJS connection opened, but the expected SockJS 'o' frame was not received.")

        self.send({"msg": "connect", "version": "1", "support": ["1", "pre2", "pre1"]})
        self.wait_for(lambda m: m.get("msg") == "connected", "DDP connected message", 20)

        print("Authenticating with saved Meteor resume token...")
        self.send({
            "msg": "method",
            "method": "login",
            "params": [{"resume": self.resume_token}],
            "id": "1",
        })

        login_result = self.wait_for(
            lambda m: m.get("msg") == "result" and m.get("id") == "1",
            "login result",
            20,
        )

        if "error" in login_result:
            die(
                f"Saved token was rejected: {login_result['error']}\n"
                "Log into Nima normally once to refresh the token, then rerun."
            )

        print("Authenticated.")

    def join_and_fetch(self) -> dict[str, Any]:
        assert self.ws is not None

        # This 32-hex join token is generated client-side by Nima.
        self.send({
            "token": secrets.token_hex(16),
            "meetingId": self.meeting_id,
            "msg": "streamy$join",
        })

        playback = self.wait_for(
            lambda m: m.get("msg") == "streamy$playbackResult",
            "streamy playback result",
            30,
        )
        self.playback_result = playback

        room = playback.get("room") or {}
        internal_room_id = room.get("_id")
        if not internal_room_id:
            die(f"Playback result did not contain an internal room ID: {playback}")

        print(
            "Playback authorized:\n"
            f"  title: {room.get('title')}\n"
            f"  internal room ID: {internal_room_id}\n"
            f"  duration: {room.get('elapsedTime')} seconds"
        )

        sub_id = random_ddp_id()
        self.send({
            "msg": "sub",
            "id": sub_id,
            "name": "recorded-events",
            "params": [internal_room_id],
        })
        self.send({"msg": "streamy$ready"})

        print("Downloading full recorded-event timeline through SockJS/DDP...")

        started = time.time()
        last_print = 0.0
        ready = False

        while time.time() - started < self.timeout:
            try:
                payload = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                # Keep the DDP connection alive during long transfers.
                heartbeat_id = f"hb{int(time.time())}"
                self.send({
                    "msg": "method",
                    "method": "heartbeat",
                    "params": [],
                    "id": heartbeat_id,
                })
                continue

            if payload is None:
                break
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")

            for msg in sockjs_unpack(payload):
                self.all_messages.append(msg)
                recursively_find_media_urls(msg, self.media_urls)

                if msg.get("msg") == "added" and msg.get("collection") == "recorded-event":
                    self.recorded_events.append(msg)

                if msg.get("msg") == "ready" and sub_id in (msg.get("subs") or []):
                    ready = True

                if msg.get("msg") == "nosub" and msg.get("id") == sub_id:
                    die(f"recorded-events subscription failed: {msg}")

            now = time.time()
            if now - last_print >= 3:
                max_ts = max(
                    [
                        float((x.get("fields") or {}).get("timestamp") or 0)
                        for x in self.recorded_events
                    ]
                    or [0]
                )
                print(
                    f"  received events={len(self.recorded_events)} "
                    f"max_timestamp={max_ts:.3f}s ready={ready}"
                )
                last_print = now

            if ready:
                # Wait briefly for any frames already in transit.
                quiet_deadline = time.time() + 2.0
                while time.time() < quiet_deadline:
                    try:
                        self.ws.settimeout(0.5)
                        extra = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        break
                    if not extra:
                        break
                    if isinstance(extra, bytes):
                        extra = extra.decode("utf-8", errors="replace")
                    for msg in sockjs_unpack(extra):
                        self.all_messages.append(msg)
                        recursively_find_media_urls(msg, self.media_urls)
                        if msg.get("msg") == "added" and msg.get("collection") == "recorded-event":
                            self.recorded_events.append(msg)
                break

        if not ready:
            die(
                "The recorded-events subscription did not finish before timeout.\n"
                "Increase --timeout if the class has a very large event log."
            )

        return playback

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass


def event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    collections: dict[str, int] = {}
    max_timestamp = 0.0
    stream_ids: set[str] = set()

    for msg in events:
        fields = msg.get("fields") or {}
        collection = str(fields.get("collection") or "unknown")
        collections[collection] = collections.get(collection, 0) + 1

        try:
            max_timestamp = max(max_timestamp, float(fields.get("timestamp") or 0))
        except Exception:
            pass

        if collection == "streams":
            values = fields.get("values")
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict) and item.get("_id"):
                        stream_ids.add(str(item["_id"]))
            elif isinstance(values, dict):
                stream_id = values.get("_id") or values.get("id")
                if stream_id:
                    stream_ids.add(str(stream_id))

    return {
        "event_count": len(events),
        "collections": collections,
        "max_timestamp": max_timestamp,
        "stream_ids": sorted(stream_ids),
    }


def safe_filename_from_url(url: str, index: int) -> str:
    path_name = Path(urlparse(url).path).name
    if path_name and "." in path_name:
        return f"{index:03d}_{path_name}"
    return f"{index:03d}_media.bin"


def download_direct_media_urls(
    session: requests.Session,
    urls: Iterable[str],
    media_dir: Path,
    referer: str,
) -> list[Path]:
    media_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for index, url in enumerate(sorted(set(urls))):
        if url.startswith("blob:"):
            continue

        destination = media_dir / safe_filename_from_url(url, index)
        print(f"Downloading media URL:\n  {url}")

        try:
            with session.get(
                url,
                headers={"Referer": referer, "Range": "bytes=0-"},
                stream=True,
                timeout=60,
            ) as response:
                if response.status_code not in (200, 206):
                    print(f"  skipped: HTTP {response.status_code}")
                    continue

                with destination.open("wb") as fh:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            fh.write(chunk)

            downloaded.append(destination)
            print(f"  saved: {destination}")
        except Exception as exc:
            print(f"  failed: {exc}")

    return downloaded


def probe_authorized_recorded_media_paths(
    session: requests.Session,
    base_url: str,
    referer: str,
    playback_result: dict[str, Any],
    events: list[dict[str, Any]],
    stream_ids: list[str],
    media_dir: Path,
    max_candidates: int,
) -> list[Path]:
    """
    Test only IDs contained in this authorized room's own replay data.
    This is not broad directory enumeration.
    """
    candidate_ids: set[str] = set()
    recursively_collect_prioritized_ids(playback_result, candidate_ids)
    for event in events:
        recursively_collect_prioritized_ids(event, candidate_ids)

    # Stream IDs are filenames, not candidate folders.
    candidate_ids.difference_update(stream_ids)

    candidates = sorted(candidate_ids)[:max_candidates]
    found: list[Path] = []
    media_dir.mkdir(parents=True, exist_ok=True)

    if not candidates or not stream_ids:
        return found

    print(
        f"Testing {len(candidates)} authorized replay IDs against "
        f"{len(stream_ids)} stream ID(s) for recorded media paths..."
    )

    for folder_id in candidates:
        for stream_id in stream_ids:
            url = (
                f"{base_url.rstrip('/')}/api//uploads/recordedmedias/"
                f"{folder_id}/{stream_id}.webm"
            )

            try:
                response = session.get(
                    url,
                    headers={"Referer": referer, "Range": "bytes=0-0"},
                    timeout=15,
                    stream=True,
                )
            except Exception:
                continue

            if response.status_code not in (200, 206):
                response.close()
                continue

            content_type = response.headers.get("content-type", "")
            response.close()

            print(f"Found recorded media: {url} ({content_type})")
            destination = media_dir / f"{folder_id}_{stream_id}.webm"

            try:
                with session.get(
                    url,
                    headers={"Referer": referer, "Range": "bytes=0-"},
                    timeout=60,
                    stream=True,
                ) as full:
                    if full.status_code not in (200, 206):
                        continue
                    with destination.open("wb") as fh:
                        for chunk in full.iter_content(1024 * 1024):
                            if chunk:
                                fh.write(chunk)
                found.append(destination)
            except Exception as exc:
                print(f"Failed downloading discovered media: {exc}")

    return found



try:
    from PIL import Image, ImageDraw
except ImportError:
    print("Missing pillow.")
    print("Install: python -m pip install browser-cookie3 websocket-client requests pillow imageio-ffmpeg")
    sys.exit(1)

LOGICAL_WIDTH = 960.0
LOGICAL_HEIGHT = 720.0


def ffmpeg_install_hint() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "winget install Gyan.FFmpeg"
    if system == "darwin":
        return "brew install ffmpeg"
    return "Install FFmpeg using your distribution package manager, for example: sudo apt install ffmpeg"


def resolve_ffmpeg() -> str:
    candidates: list[Path] = []
    executable_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    env_dir = os.environ.get("NIMA_FFMPEG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / executable_name)

    # Supports PyInstaller one-file/one-folder bundles and portable release folders.
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / executable_name)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / executable_name)
    candidates.append(Path(__file__).resolve().parent / executable_name)

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    # imageio-ffmpeg ships platform-specific FFmpeg binaries and is convenient
    # for pip/PyInstaller distributions.
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except Exception:
        pass

    die(
        "FFmpeg was not found. Install the optional imageio-ffmpeg package or system FFmpeg.\n"
        f"Suggested command: {ffmpeg_install_hint()}"
    )
    raise AssertionError


def configure_tools() -> None:
    global FFMPEG_BIN
    FFMPEG_BIN = resolve_ffmpeg()
    print(f"Using FFmpeg: {FFMPEG_BIN}")


def run_process(command: list[str], capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    print("\n>>> " + " ".join(f'"{x}"' if " " in str(x) else str(x) for x in command))
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )

def sanitize_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', '_', value).strip().strip('.')
    value = re.sub(r'\\s+', ' ', value)
    return value[:120] or 'nima_lecture'


def ffprobe_duration(path: Path) -> float:
    """Read media duration using FFmpeg itself, avoiding a separate ffprobe dependency."""
    result = run_process(
        [FFMPEG_BIN, '-hide_banner', '-i', str(path)],
        capture=True,
        check=False,
    )
    match = re.search(r'Duration:\s*(\d+):(\d+):([0-9.]+)', result.stdout or '')
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

def find_recorded_media_pairs(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    stream_ids: set[str] = set()
    recorded_by_stream: dict[str, str] = {}

    for message in events:
        fields = message.get('fields') or {}
        if fields.get('collection') != 'streams':
            continue
        values = fields.get('values')
        if fields.get('action') == 'create' and isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and item.get('_id'):
                    stream_ids.add(str(item['_id']))
        elif fields.get('action') == 'update' and isinstance(values, dict):
            stream_id = values.get('id')
            changes = (values.get('changes') or {}).get('!set') or {}
            recorded_id = changes.get('recordedId')
            if stream_id and recorded_id:
                recorded_by_stream[str(stream_id)] = str(recorded_id)

    return [
        (recorded_by_stream[stream_id], stream_id)
        for stream_id in sorted(stream_ids)
        if stream_id in recorded_by_stream
    ]


def download_recorded_audio(
    session: requests.Session,
    base_url: str,
    referer: str,
    events: list[dict[str, Any]],
    work_dir: Path,
) -> Path:
    pairs = find_recorded_media_pairs(events)
    if not pairs:
        die('The replay finished downloading, but no recordedId/streamId media pair was found.')

    candidates: list[tuple[float, Path]] = []
    media_dir = work_dir / 'media'
    media_dir.mkdir(parents=True, exist_ok=True)

    for recorded_id, stream_id in pairs:
        url = f"{base_url.rstrip('/')}/api//uploads/recordedmedias/{recorded_id}/{stream_id}.webm"
        destination = media_dir / f'{recorded_id}_{stream_id}.webm'
        print(f'Downloading recorded media:\n  {url}')
        with session.get(
            url,
            headers={'Referer': referer, 'Range': 'bytes=0-'},
            stream=True,
            timeout=120,
        ) as response:
            if response.status_code not in (200, 206):
                print(f'  skipped: HTTP {response.status_code}')
                continue
            with destination.open('wb') as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        duration = ffprobe_duration(destination)
        print(f'  saved: {destination} ({duration:.3f}s)')
        if duration > 1:
            candidates.append((duration, destination))

    if not candidates:
        die('No usable recorded audio file was downloaded.')

    candidates.sort(reverse=True, key=lambda item: item[0])
    return candidates[0][1]


def color_rgba(value: Any, opacity: float = 1.0) -> Optional[tuple[int, int, int, int]]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {'null', 'transparent', 'none'}:
        return None

    names = {
        'black': (0, 0, 0, 255),
        'white': (255, 255, 255, 255),
        'blue': (0, 0, 255, 255),
        'yellow': (255, 255, 0, 255),
        'red': (255, 0, 0, 255),
    }
    if text.lower() in names:
        r, g, b, a = names[text.lower()]
        return r, g, b, round(a * opacity)

    if text.startswith('#'):
        raw = text[1:]
        try:
            if len(raw) == 6:
                r, g, b = (int(raw[i:i+2], 16) for i in (0, 2, 4))
                return r, g, b, round(255 * opacity)
            if len(raw) == 8:
                r, g, b, a = (int(raw[i:i+2], 16) for i in (0, 2, 4, 6))
                return r, g, b, round(a * opacity)
        except ValueError:
            pass

    match = re.match(r'rgba?\\(([^)]+)\\)', text, re.I)
    if match:
        parts = [part.strip() for part in match.group(1).split(',')]
        try:
            r, g, b = (int(float(parts[i])) for i in range(3))
            alpha = float(parts[3]) if len(parts) > 3 else 1.0
            return r, g, b, round(255 * alpha * opacity)
        except Exception:
            pass

    return 0, 0, 0, round(255 * opacity)


def path_coordinates(path: list[list[Any]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in path:
        values = segment[1:]
        for index in range(0, len(values) - 1, 2):
            x, y = values[index], values[index + 1]
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                points.append((float(x), float(y)))
    return points


def fabric_transform(obj: dict[str, Any], output_scale: float, margin_x: float, margin_y: float):
    path = obj.get('path') or []
    coords = path_coordinates(path)
    if not coords:
        return lambda x, y: (x * output_scale + margin_x, y * output_scale + margin_y)

    min_x = min(x for x, _ in coords)
    min_y = min(y for _, y in coords)
    max_x = max(x for x, _ in coords)
    max_y = max(y for _, y in coords)

    width = float(obj.get('width') if obj.get('width') is not None else max_x - min_x)
    height = float(obj.get('height') if obj.get('height') is not None else max_y - min_y)

    path_offset = obj.get('pathOffset') or {}
    offset_x = float(path_offset.get('x', min_x + width / 2.0))
    offset_y = float(path_offset.get('y', min_y + height / 2.0))

    scale_x = float(obj.get('scaleX') if obj.get('scaleX') is not None else 1.0)
    scale_y = float(obj.get('scaleY') if obj.get('scaleY') is not None else 1.0)
    if obj.get('flipX'):
        scale_x *= -1
    if obj.get('flipY'):
        scale_y *= -1

    left = float(obj.get('left') or 0)
    top = float(obj.get('top') or 0)
    origin_x = {'left': 0.0, 'center': 0.5, 'right': 1.0}.get(obj.get('originX'), 0.5)
    origin_y = {'top': 0.0, 'center': 0.5, 'bottom': 1.0}.get(obj.get('originY'), 0.5)

    center_x = left + (0.5 - origin_x) * width * abs(scale_x)
    center_y = top + (0.5 - origin_y) * height * abs(scale_y)

    angle = math.radians(float(obj.get('angle') or 0))
    cosine = math.cos(angle)
    sine = math.sin(angle)

    def transform(x: float, y: float) -> tuple[float, float]:
        local_x = (x - offset_x) * scale_x
        local_y = (y - offset_y) * scale_y
        rotated_x = center_x + local_x * cosine - local_y * sine
        rotated_y = center_y + local_x * sine + local_y * cosine
        return rotated_x * output_scale + margin_x, rotated_y * output_scale + margin_y

    return transform


def quadratic_points(p0, p1, p2, count: int = 12):
    for index in range(1, count + 1):
        t = index / count
        yield (
            (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0],
            (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1],
        )


def cubic_points(p0, p1, p2, p3, count: int = 16):
    for index in range(1, count + 1):
        t = index / count
        yield (
            (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0],
            (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1],
        )


def draw_fabric_path(
    image: Image.Image,
    obj: dict[str, Any],
    output_scale: float,
    margin_x: float,
    margin_y: float,
) -> None:
    if obj.get('type') != 'path' or obj.get('visible') is False:
        return

    transform = fabric_transform(obj, output_scale, margin_x, margin_y)
    subpaths: list[tuple[list[tuple[float, float]], bool]] = []
    current: Optional[tuple[float, float]] = None
    start: Optional[tuple[float, float]] = None
    points: list[tuple[float, float]] = []
    closed = False

    for segment in obj.get('path') or []:
        if not segment:
            continue
        command = str(segment[0]).upper()
        values = segment[1:]

        try:
            if command == 'M':
                if points:
                    subpaths.append((points, closed))
                current = (float(values[0]), float(values[1]))
                start = current
                points = [transform(*current)]
                closed = False
            elif command == 'L' and current is not None:
                current = (float(values[0]), float(values[1]))
                points.append(transform(*current))
            elif command == 'Q' and current is not None:
                control = (float(values[0]), float(values[1]))
                endpoint = (float(values[2]), float(values[3]))
                points.extend(transform(*point) for point in quadratic_points(current, control, endpoint))
                current = endpoint
            elif command == 'C' and current is not None:
                control1 = (float(values[0]), float(values[1]))
                control2 = (float(values[2]), float(values[3]))
                endpoint = (float(values[4]), float(values[5]))
                points.extend(transform(*point) for point in cubic_points(current, control1, control2, endpoint))
                current = endpoint
            elif command == 'Z' and current is not None and start is not None:
                points.append(transform(*start))
                current = start
                closed = True
        except Exception:
            continue

    if points:
        subpaths.append((points, closed))

    opacity = float(obj.get('opacity') if obj.get('opacity') is not None else 1.0)
    fill = color_rgba(obj.get('fill'), opacity)
    stroke = color_rgba(obj.get('stroke'), opacity)
    stroke_width = max(
        1,
        round(
            float(obj.get('strokeWidth') or 1)
            * max(abs(float(obj.get('scaleX') or 1)), abs(float(obj.get('scaleY') or 1)))
            * output_scale
        ),
    )

    draw = ImageDraw.Draw(image, 'RGBA')
    for subpath, is_closed in subpaths:
        if fill and is_closed and len(subpath) >= 3:
            draw.polygon(subpath, fill=fill)
        if stroke and len(subpath) >= 2:
            draw.line(subpath, fill=stroke, width=stroke_width, joint='curve')


def is_pointer_shape(wrapper: dict[str, Any], obj: dict[str, Any]) -> bool:
    data = obj.get('data')
    return bool(wrapper.get('pointer') or (isinstance(data, dict) and data.get('pointer')))


def render_replay(
    events: list[dict[str, Any]],
    duration: float,
    work_dir: Path,
    width: int,
    height: int,
    antialias: int,
    draw_pointer: bool,
) -> Path:
    frames_dir = work_dir / 'frames'
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    high_width = width * antialias
    high_height = height * antialias
    fit_scale = min(width / LOGICAL_WIDTH, height / LOGICAL_HEIGHT)
    high_scale = fit_scale * antialias
    margin_x = ((width - LOGICAL_WIDTH * fit_scale) / 2.0) * antialias
    margin_y = ((height - LOGICAL_HEIGHT * fit_scale) / 2.0) * antialias

    pages_by_index: dict[int, str] = {}
    shapes: dict[str, dict[str, Any]] = {}
    page_cache: dict[str, Image.Image] = {}
    dirty_pages: set[str] = set()
    current_page_index = 0

    snapshots: list[tuple[float, Path]] = []

    def blank_page() -> Image.Image:
        return Image.new('RGB', (high_width, high_height), 'white')

    def rebuild_page(page_id: Optional[str]) -> Image.Image:
        if not page_id:
            return blank_page()
        if page_id not in page_cache or page_id in dirty_pages:
            image = blank_page()
            page_shapes = [
                item for item in shapes.values()
                if item.get('page_id') == page_id
                and not item.get('removed')
                and not item.get('pointer')
            ]
            page_shapes.sort(key=lambda item: item.get('index', 0))
            for item in page_shapes:
                draw_fabric_path(image, item['object'], high_scale, margin_x, margin_y)
            page_cache[page_id] = image
            dirty_pages.discard(page_id)
        return page_cache[page_id]

    def save_snapshot(timestamp: float) -> None:
        page_id = pages_by_index.get(current_page_index)
        image = rebuild_page(page_id).copy()
        if draw_pointer and page_id:
            pointers = [
                item for item in shapes.values()
                if item.get('page_id') == page_id
                and not item.get('removed')
                and item.get('pointer')
            ]
            pointers.sort(key=lambda item: item.get('index', 0))
            for item in pointers:
                draw_fabric_path(image, item['object'], high_scale, margin_x, margin_y)

        if antialias > 1:
            image = image.resize((width, height), Image.Resampling.LANCZOS)

        path = frames_dir / f'frame_{len(snapshots):05d}.png'
        image.save(path, compress_level=1)
        snapshots.append((max(0.0, timestamp), path))
        if len(snapshots) % 250 == 0:
            print(f'  rendered {len(snapshots)} visual states...')

    visual_events: list[tuple[float, int, dict[str, Any]]] = []
    for order, message in enumerate(events):
        fields = message.get('fields') or {}
        collection = fields.get('collection')
        if collection in {'whiteboard-pages', 'presenter_tabs', 'whiteboard-shapes'}:
            try:
                timestamp = float(fields.get('timestamp') or 0)
            except Exception:
                timestamp = 0.0
            visual_events.append((timestamp, order, fields))

    visual_events.sort(key=lambda item: (item[0], item[1]))
    index = 0

    while index < len(visual_events):
        timestamp = visual_events[index][0]
        changed = False

        while index < len(visual_events) and visual_events[index][0] == timestamp:
            _, _, fields = visual_events[index]
            collection = fields.get('collection')
            action = fields.get('action')
            values = fields.get('values')

            if collection == 'whiteboard-pages' and action == 'create' and isinstance(values, list):
                for item in values:
                    if isinstance(item, dict) and item.get('_id') is not None:
                        pages_by_index[int(item.get('index') or 0)] = str(item['_id'])
                        if int(item.get('index') or 0) == current_page_index:
                            changed = True

            elif collection == 'presenter_tabs':
                if action == 'create' and isinstance(values, list):
                    for item in values:
                        if isinstance(item, dict) and item.get('currentPage') is not None:
                            new_page = int(item['currentPage'])
                            if new_page != current_page_index:
                                current_page_index = new_page
                                changed = True
                elif action == 'update' and isinstance(values, dict):
                    changes = (values.get('changes') or {}).get('!set') or {}
                    if changes.get('currentPage') is not None:
                        new_page = int(changes['currentPage'])
                        if new_page != current_page_index:
                            current_page_index = new_page
                            changed = True

            elif collection == 'whiteboard-shapes':
                if action == 'create' and isinstance(values, list):
                    for wrapper in values:
                        if not isinstance(wrapper, dict) or not wrapper.get('_id') or not wrapper.get('objectString'):
                            continue
                        try:
                            obj = json.loads(wrapper['objectString'])
                        except Exception:
                            continue
                        shape_id = str(wrapper['_id'])
                        page_id = str(wrapper.get('whiteboardPageId') or '')
                        pointer = is_pointer_shape(wrapper, obj)
                        shapes[shape_id] = {
                            'page_id': page_id,
                            'index': int(wrapper.get('index') or 0),
                            'object': obj,
                            'pointer': pointer,
                            'removed': False,
                        }
                        if not pointer:
                            # Normal pen strokes are immutable in Nima recordings, so draw them
                            # incrementally. This avoids rebuilding an entire page after every stroke.
                            if page_id not in dirty_pages:
                                if page_id not in page_cache:
                                    page_cache[page_id] = blank_page()
                                draw_fabric_path(page_cache[page_id], obj, high_scale, margin_x, margin_y)
                            else:
                                dirty_pages.add(page_id)
                        if pages_by_index.get(current_page_index) == page_id:
                            changed = True

                elif action == 'update' and isinstance(values, dict):
                    shape_id = str(values.get('id') or '')
                    item = shapes.get(shape_id)
                    if item:
                        changes = (values.get('changes') or {}).get('!set') or {}
                        old_pointer = bool(item.get('pointer'))
                        if changes.get('objectString'):
                            try:
                                item['object'] = json.loads(changes['objectString'])
                            except Exception:
                                pass
                        if changes.get('index') is not None:
                            item['index'] = int(changes['index'])
                        if changes.get('removed') is not None:
                            item['removed'] = bool(changes['removed'])
                        item['pointer'] = is_pointer_shape({}, item['object']) or old_pointer
                        if not item['pointer']:
                            dirty_pages.add(item['page_id'])
                        if pages_by_index.get(current_page_index) == item['page_id']:
                            changed = True

            index += 1

        if changed or not snapshots:
            save_snapshot(timestamp)

    if not snapshots:
        save_snapshot(0.0)

    # Remove duplicate timestamps, keeping the last state at each timestamp.
    deduplicated: list[tuple[float, Path]] = []
    for timestamp, path in snapshots:
        if deduplicated and abs(deduplicated[-1][0] - timestamp) < 1e-9:
            old_path = deduplicated[-1][1]
            try:
                old_path.unlink()
            except OSError:
                pass
            deduplicated[-1] = (timestamp, path)
        else:
            deduplicated.append((timestamp, path))

    concat_path = work_dir / 'frames.txt'
    with concat_path.open('w', encoding='utf-8') as handle:
        for position, (timestamp, path) in enumerate(deduplicated):
            next_timestamp = deduplicated[position + 1][0] if position + 1 < len(deduplicated) else duration
            frame_duration = max(0.04, next_timestamp - timestamp)
            safe_path = ffconcat_path(path)
            handle.write(f"file '{safe_path}'\n")
            handle.write(f'duration {frame_duration:.6f}\n')
        safe_last = ffconcat_path(deduplicated[-1][1])
        handle.write(f"file '{safe_last}'\n")

    print(f'Rendered {len(deduplicated)} visual states.')
    return concat_path


def ffconcat_path(path: Path) -> str:
    """Return a path safely quoted for FFmpeg's concat demuxer."""
    value = str(path.resolve()).replace('\\', '/')
    return value.replace("'", "'\\''")


def repair_concat_file(concat_path: Path) -> None:
    """Repair concat files created by v2.0, which wrote literal \n text."""
    text = concat_path.read_text(encoding='utf-8')
    if r'\n' in text:
        concat_path.write_text(text.replace(r'\n', '\n'), encoding='utf-8', newline='\n')
        print(f'Repaired concat-list newline formatting: {concat_path}')


def encode_visual(concat_path: Path, destination: Path, fps: int) -> None:
    repair_concat_file(concat_path)
    run_process([
        FFMPEG_BIN, '-y',
        '-f', 'concat', '-safe', '0',
        '-i', str(concat_path),
        '-fps_mode', 'vfr',
        '-pix_fmt', 'yuv420p',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '21',
        str(destination),
    ])


def mux_final(visual: Path, audio: Path, destination: Path) -> None:
    run_process([
        FFMPEG_BIN, '-y',
        '-i', str(visual),
        '-i', str(audio),
        '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest',
        '-movflags', '+faststart',
        str(destination),
    ])


def fetch_replay(args: argparse.Namespace) -> tuple[str, float, list[dict[str, Any]], requests.Session, str]:
    parsed = urlparse(args.url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        die('Invalid room URL.')

    base_url = f'{parsed.scheme}://{parsed.netloc}'
    meeting_id = extract_meeting_id(args.url)
    cookies = load_netscape_cookie_file(Path(args.cookies_file)) if args.cookies_file else load_browser_cookies(args.browser, parsed.netloc)
    resume_token = find_resume_token(cookies, parsed.netloc)
    print('Found saved Meteor resume token. Token value will not be printed.')

    cookie_string = cookie_header(cookies, parsed.netloc)
    client = HeadlessNimaClient(
        base_url=base_url,
        meeting_id=meeting_id,
        resume_token=resume_token,
        cookies=cookie_string,
        timeout=args.timeout,
    )

    try:
        client.connect()
        playback = client.join_and_fetch()
    finally:
        client.close()

    room = playback.get('room') or {}
    title = str(room.get('title') or f'Nima {meeting_id}')
    duration = float(room.get('elapsedTime') or 0)

    user_agent = DEFAULT_USER_AGENT
    session = requests_session(cookies, user_agent)
    session.headers.update({'Origin': base_url, 'Referer': args.url})

    return title, duration, client.recorded_events, session, base_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='nima-export',
        description='Export a Nima replay directly from its URL without opening or recording a browser.',
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')
    parser.add_argument('url', help='Nima room/replay URL')
    parser.add_argument(
        '--browser',
        choices=['auto', 'firefox', 'chrome', 'chromium', 'edge', 'brave', 'vivaldi', 'librewolf', 'opera', 'safari'],
        default='auto',
        help='Browser containing a valid saved Nima login session; default tries installed browsers automatically',
    )
    parser.add_argument('--cookies-file', help='Netscape-format cookies.txt file; useful on servers or unsupported browsers')
    parser.add_argument('-o', '--output', default=None, help='Final MP4 path; default uses the class title')
    parser.add_argument('--work-dir', default=None, help='Working directory; default is .nima-export-work/<room-id>')
    parser.add_argument('--timeout', type=int, default=300, help='Maximum seconds to fetch replay events')
    parser.add_argument('--width', type=int, default=960, help='Output video width')
    parser.add_argument('--height', type=int, default=720, help='Output video height')
    parser.add_argument('--antialias', type=int, choices=[1, 2, 3], default=2, help='Rendering antialias multiplier')
    parser.add_argument('--fps', type=int, default=25, help='Output video frame rate')
    parser.add_argument('--no-pointer', action='store_true', help="Hide the presenter's pointer")
    parser.add_argument('--keep-work', action='store_true', help='Keep generated PNG frames and temporary files')
    parser.add_argument('--resume-work', action='store_true', help='Resume encoding from an existing work directory')
    parser.add_argument('--force', action='store_true', help='Delete an existing work directory before starting a fresh export')
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    configure_tools()

    room_id = sanitize_filename(extract_meeting_id(args.url))
    work_dir = Path(args.work_dir) if args.work_dir else Path('.nima-export-work') / room_id

    if args.resume_work:
        concat_path = work_dir / 'frames.txt'
        if not concat_path.exists():
            die(f'Cannot resume: missing {concat_path}')
        media_candidates = sorted((work_dir / 'media').glob('*'))
        if not media_candidates:
            die(f'Cannot resume: no media files found in {work_dir / "media"}')
        audio_path = max(media_candidates, key=lambda p: ffprobe_duration(p))
        visual_path = work_dir / 'visual.mp4'
        encode_visual(concat_path, visual_path, args.fps)
        output_path = Path(args.output) if args.output else Path('nima-export.mp4')
        mux_final(visual_path, audio_path, output_path)
        print('\nDONE')
        print(f'Output: {output_path.resolve()}')
        return

    if work_dir.exists():
        if args.force:
            shutil.rmtree(work_dir)
        else:
            die(f'Work directory already exists: {work_dir}\nUse --resume-work to continue it, or --force to replace it.')
    work_dir.mkdir(parents=True, exist_ok=True)

    title, room_duration, events, session, base_url = fetch_replay(args)
    summary = event_summary(events)
    print('\nReplay summary')
    print('--------------')
    print(f'Title:          {title}')
    print(f'Events:         {summary["event_count"]}')
    print(f'Collections:    {summary["collections"]}')
    print(f'Room duration:  {room_duration:.3f}s')

    audio_path = download_recorded_audio(session, base_url, args.url, events, work_dir)
    audio_duration = ffprobe_duration(audio_path)
    duration = max(room_duration, audio_duration, float(summary.get('max_timestamp') or 0))

    concat_path = render_replay(
        events=events,
        duration=duration,
        work_dir=work_dir,
        width=args.width,
        height=args.height,
        antialias=args.antialias,
        draw_pointer=not args.no_pointer,
    )

    visual_path = work_dir / 'visual.mp4'
    encode_visual(concat_path, visual_path, args.fps)

    output_path = Path(args.output) if args.output else Path(f'{sanitize_filename(title)}.mp4')
    mux_final(visual_path, audio_path, output_path)

    print('\nDONE')
    print(f'Output: {output_path.resolve()}')

    if not args.keep_work:
        try:
            shutil.rmtree(work_dir)
        except OSError as exc:
            print(f'WARNING: could not remove work directory: {exc}')


if __name__ == '__main__':
    main()
