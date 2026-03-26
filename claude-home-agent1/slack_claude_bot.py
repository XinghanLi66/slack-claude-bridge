#!/usr/bin/env python3
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


WORKSPACE_ROOT = Path(__file__).resolve().parent
RUNNER_PATH = WORKSPACE_ROOT / "run_claude.sh"
STATE_DIR = WORKSPACE_ROOT / ".slack-bot"
STATE_FILE = STATE_DIR / "sessions.json"
DOWNLOADS_DIR = STATE_DIR / "downloads"
DEFAULT_TIMEOUT_SECONDS = 1800
SLACK_MESSAGE_LIMIT = 3500
STREAM_UPDATE_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
MENTION_RE = re.compile(r"<@[^>]+>")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


class SessionStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.lock = threading.Lock()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            self.state_file.write_text("{}", encoding="utf-8")

    def _read(self) -> Dict[str, str]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _write(self, data: Dict[str, str]) -> None:
        self.state_file.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )

    def get(self, key: str) -> Optional[str]:
        with self.lock:
            return self._read().get(key)

    def set(self, key: str, session_id: str) -> None:
        with self.lock:
            data = self._read()
            data[key] = session_id
            self._write(data)

    def reset(self, key: str) -> None:
        with self.lock:
            data = self._read()
            data.pop(key, None)
            self._write(data)


session_store = SessionStore(STATE_FILE)
thread_locks: Dict[str, threading.Lock] = {}
thread_locks_guard = threading.Lock()


def get_thread_lock(key: str) -> threading.Lock:
    with thread_locks_guard:
        if key not in thread_locks:
            thread_locks[key] = threading.Lock()
        return thread_locks[key]


def session_key(channel: str, thread_ts: Optional[str]) -> str:
    if thread_ts:
        return f"{channel}:{thread_ts}"
    return channel


def strip_bot_mentions(text: str) -> str:
    text = MENTION_RE.sub("", text or "")
    return text.strip()


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "")
    return cleaned.strip("._") or "attachment"


def split_message(text: str, limit: int = SLACK_MESSAGE_LIMIT) -> List[str]:
    text = text.strip()
    if not text:
        return ["(empty response)"]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def build_help_text() -> str:
    return (
        "Send me a message in DM or mention me in a channel thread.\n"
        "Commands:\n"
        "- `help`: show this message\n"
        "- `reset`: start a fresh Claude session for this thread\n"
        "- upload files with or without text: I will save them locally and pass them to Claude\n"
        "- anything else: forward it to Claude Code in this workspace"
    )


def extract_text_from_payload(payload: dict) -> str:
    chunks: List[str] = []
    for item in payload.get("message", {}).get("content", []):
        if item.get("type") == "text" and item.get("text"):
            chunks.append(item["text"])
    return "".join(chunks)


def merge_text(existing: str, incoming: str) -> str:
    if not incoming:
        return existing
    if not existing:
        return incoming
    if incoming.startswith(existing):
        return incoming
    if existing.endswith(incoming):
        return existing
    return existing + incoming


def get_max_file_bytes() -> int:
    raw_value = os.environ.get("SLACK_MAX_FILE_BYTES", "").strip()
    if not raw_value:
        return DEFAULT_MAX_FILE_BYTES
    try:
        return max(1, int(raw_value))
    except ValueError:
        return DEFAULT_MAX_FILE_BYTES


def fetch_file_info(client, file_payload: dict, logger) -> Optional[dict]:
    if file_payload.get("url_private_download") or file_payload.get("url_private"):
        return file_payload

    file_id = file_payload.get("id")
    if not file_id:
        return None

    try:
        response = client.files_info(file=file_id)
    except Exception as exc:
        logger.warning("files_info(%s) failed: %s", file_id, exc)
        return None

    return response.get("file")


def download_slack_file(file_info: dict, bot_token: str, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)

    download_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not download_url:
        raise RuntimeError("Slack file payload did not include a downloadable URL.")

    file_size = int(file_info.get("size") or 0)
    max_file_bytes = get_max_file_bytes()
    if file_size and file_size > max_file_bytes:
        raise RuntimeError(
            f"File is too large ({file_size} bytes). Limit is {max_file_bytes} bytes."
        )

    original_name = (
        file_info.get("name")
        or file_info.get("title")
        or file_info.get("id")
        or "attachment"
    )
    safe_name = sanitize_filename(original_name)
    file_path = destination_dir / f"{file_info.get('id', uuid.uuid4().hex)}_{safe_name}"

    request = urllib.request.Request(
        download_url,
        headers={"Authorization": f"Bearer {bot_token}"},
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(max_file_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Slack download failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Slack download failed: {exc.reason}") from exc

    if len(data) > max_file_bytes:
        raise RuntimeError(f"File is too large. Limit is {max_file_bytes} bytes.")

    file_path.write_bytes(data)
    return file_path


def prepare_prompt_from_event(client, event: dict, logger) -> Tuple[Optional[str], List[str]]:
    prompt_text = strip_bot_mentions(event.get("text", ""))
    files = event.get("files") or []
    if not files:
        return (prompt_text or None), []

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("Missing SLACK_BOT_TOKEN for Slack file downloads.")

    channel_id = str(event.get("channel") or "unknown-channel")
    thread_ref = str(event.get("thread_ts") or event.get("ts") or uuid.uuid4().hex).replace(".", "_")
    destination_dir = DOWNLOADS_DIR / sanitize_filename(channel_id) / sanitize_filename(thread_ref)

    saved_paths: List[str] = []
    details: List[str] = []
    for file_payload in files:
        file_info = fetch_file_info(client, file_payload, logger)
        if not file_info:
            raise RuntimeError("Could not fetch Slack file metadata.")

        saved_path = download_slack_file(file_info, bot_token, destination_dir)
        saved_paths.append(str(saved_path))

        detail = f"- {file_info.get('name') or file_info.get('title') or saved_path.name}: {saved_path}"
        if file_info.get("mimetype"):
            detail += f" ({file_info['mimetype']})"
        details.append(detail)

    prompt_parts: List[str] = []
    if prompt_text:
        prompt_parts.append(prompt_text)
    else:
        prompt_parts.append("The user shared file(s) in Slack. Please inspect them and help.")
    prompt_parts.append("Slack file downloads saved in the workspace:")
    prompt_parts.extend(details)

    return "\n\n".join([prompt_parts[0], "\n".join(prompt_parts[1:])]), saved_paths


def run_claude_stream(
    key: str,
    prompt: str,
    current_session_id: Optional[str],
    on_update: Callable[[str], None],
) -> str:
    timeout_seconds = int(
        os.environ.get("CLAUDE_SLACK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    is_new_session = current_session_id is None
    session_id = current_session_id or str(uuid.uuid4())

    command = [
        "bash",
        str(RUNNER_PATH),
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
    ]
    if is_new_session:
        command.extend(["--session-id", session_id])
    else:
        command.extend(["--resume", session_id])
    command.append(prompt)

    process = subprocess.Popen(
        command,
        cwd=str(WORKSPACE_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )
    accumulated_text = ""
    non_json_lines: List[str] = []
    terminal_error_text = ""
    saw_partial_text = False
    last_update_text = ""
    last_update_time = 0.0
    timed_out = False

    def kill_for_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        process.kill()

    timeout_timer = threading.Timer(timeout_seconds, kill_for_timeout)
    timeout_timer.start()

    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                non_json_lines.append(line)
                continue

            payload_type = payload.get("type")
            if payload_type == "system" and payload.get("subtype") == "init":
                session_store.set(key, payload.get("session_id", session_id))
                continue

            if payload_type == "stream_event":
                event = payload.get("event", {})
                delta = event.get("delta", {})
                if (
                    event.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"
                    and delta.get("text")
                ):
                    saw_partial_text = True
                    accumulated_text += delta["text"]
                else:
                    continue
            elif payload_type == "assistant":
                assistant_text = extract_text_from_payload(payload)
                if assistant_text:
                    if saw_partial_text:
                        accumulated_text = merge_text(accumulated_text, assistant_text)
                    else:
                        accumulated_text = assistant_text
                if payload.get("error") and assistant_text:
                    terminal_error_text = assistant_text
            elif payload_type == "result":
                result_text = payload.get("result")
                if isinstance(result_text, str) and result_text.strip():
                    if payload.get("is_error"):
                        terminal_error_text = result_text
                    if saw_partial_text:
                        accumulated_text = merge_text(accumulated_text, result_text)
                    else:
                        accumulated_text = result_text
                continue
            else:
                continue

            now = time.monotonic()
            if (
                accumulated_text
                and accumulated_text != last_update_text
                and now - last_update_time >= STREAM_UPDATE_INTERVAL_SECONDS
            ):
                on_update(accumulated_text)
                last_update_text = accumulated_text
                last_update_time = now

        return_code = process.wait()
    finally:
        timeout_timer.cancel()

    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    if return_code != 0:
        detail = (
            terminal_error_text.strip()
            or accumulated_text.strip()
            or "\n".join(non_json_lines).strip()
            or f"Claude exited with code {return_code}."
        )
        raise RuntimeError(detail)

    session_store.set(key, session_id)
    return accumulated_text.strip() or "(Claude returned no text.)"


def render_streaming_text(text: str) -> str:
    text = text.strip()
    if not text:
        return "Thinking..."
    suffix = "\n\n[streaming...]"
    available = SLACK_MESSAGE_LIMIT - len(suffix)
    if len(text) <= available:
        return text + suffix
    return text[:available].rstrip() + suffix


def post_response(client, channel: str, thread_ts: Optional[str], placeholder_ts: str, text: str) -> None:
    chunks = split_message(text)
    client.chat_update(
        channel=channel,
        ts=placeholder_ts,
        text=chunks[0],
    )
    for chunk in chunks[1:]:
        kwargs = {"channel": channel, "text": chunk}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        client.chat_postMessage(**kwargs)


def _post(client, channel: str, thread_ts: Optional[str], text: str):
    kwargs = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    return client.chat_postMessage(**kwargs)


def prepare_prompt_or_report_error(
    client,
    event: dict,
    logger,
    response_thread_ts: Optional[str],
) -> Optional[str]:
    try:
        prompt_text, _ = prepare_prompt_from_event(client, event, logger)
        return prompt_text
    except Exception as exc:
        logger.exception("Failed to prepare Slack file prompt: %s", exc)
        _post(
            client,
            str(event.get("channel") or ""),
            response_thread_ts,
            (
                "I saw the file, but I couldn't download it from Slack. "
                f"Error: {exc}"
            ),
        )
        return None


def process_prompt(client, channel: str, thread_ts: Optional[str], prompt: str) -> None:
    key = session_key(channel, thread_ts)
    prompt_lower = prompt.strip().lower()

    if prompt_lower in {"help", "/help"}:
        _post(client, channel, thread_ts, build_help_text())
        return

    if prompt_lower in {"reset", "/reset", "new", "/new"}:
        session_store.reset(key)
        _post(client, channel, thread_ts,
              "Session cleared. Your next message will start a new Claude conversation.")
        return

    placeholder = _post(client, channel, thread_ts, "Thinking...")
    placeholder_ts = placeholder["ts"]

    lock = get_thread_lock(key)
    with lock:
        current_session_id = session_store.get(key)
        try:
            def on_stream_update(text: str) -> None:
                client.chat_update(
                    channel=channel,
                    ts=placeholder_ts,
                    text=render_streaming_text(text),
                )

            reply = run_claude_stream(key, prompt, current_session_id, on_stream_update)
            post_response(client, channel, thread_ts, placeholder_ts, reply)
        except subprocess.TimeoutExpired:
            client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=(
                    "Claude timed out before finishing. "
                    "Increase `CLAUDE_SLACK_TIMEOUT_SECONDS` if you expect longer tasks."
                ),
            )
        except Exception as exc:
            client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=f"Claude failed: {exc}",
            )


def ensure_in_channel(client, channel: str, logger) -> None:
    """Auto-join a channel so the bot can post messages there."""
    try:
        client.conversations_join(channel=channel)
    except Exception as exc:
        logger.warning("conversations_join(%s) failed (may already be a member or private): %s", channel, exc)


def spawn_prompt_worker(client, channel: str, thread_ts: Optional[str], prompt: str) -> None:
    worker = threading.Thread(
        target=process_prompt,
        args=(client, channel, thread_ts, prompt),
        daemon=True,
    )
    worker.start()


bot_user_id: Optional[str] = None


def main() -> None:
    global bot_user_id
    load_dotenv(WORKSPACE_ROOT / ".env")

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise SystemExit(
            "Missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN. Put them in the environment or .env."
        )

    app = App(token=bot_token)

    auth_result = app.client.auth_test()
    bot_user_id = auth_result.get("user_id")
    logging.info("Bot user ID: %s", bot_user_id)

    @app.use
    def log_incoming_events(logger, body, next):
        event = (body or {}).get("event", {})
        logger.info(
            "incoming payload: type=%s event_type=%s channel=%s channel_type=%s text=%s",
            (body or {}).get("type"),
            event.get("type"),
            event.get("channel"),
            event.get("channel_type"),
            event.get("text"),
        )
        next()

    @app.event("app_mention")
    def handle_app_mention(event, client, logger):
        logger.info("app_mention event received: channel=%s text=%s", event.get("channel"), event.get("text"))
        if event.get("bot_id") or event.get("subtype"):
            return
        ensure_in_channel(client, event["channel"], logger)
        prompt = strip_bot_mentions(event.get("text", ""))
        if not prompt:
            _post(client, event["channel"], event.get("thread_ts", event["ts"]), build_help_text())
            return
        spawn_prompt_worker(
            client,
            event["channel"],
            event.get("thread_ts", event["ts"]),
            prompt,
        )

    @app.event("message")
    def handle_message(event, client, logger):
        subtype = event.get("subtype")
        raw_text = event.get("text") or ""
        logger.info(
            "message event received: channel=%s channel_type=%s subtype=%s text=%s files=%s",
            event.get("channel"),
            event.get("channel_type"),
            subtype,
            raw_text,
            len(event.get("files") or []),
        )
        if event.get("bot_id"):
            return
        if subtype and subtype != "file_share":
            return

        channel_id = str(event.get("channel") or "")
        channel_type = event.get("channel_type", "")
        is_dm = channel_type == "im" or channel_id.startswith("D")
        thread_ts = event.get("thread_ts")
        has_files = bool(event.get("files"))
        has_bot_mention = bool(MENTION_RE.search(raw_text))

        if is_dm:
            prompt_text = prepare_prompt_or_report_error(client, event, logger, None)
            if not prompt_text:
                return
            logger.info("dm message accepted: channel=%s", channel_id)
            spawn_prompt_worker(client, channel_id, None, prompt_text)
            return

        if has_files and has_bot_mention and not thread_ts:
            prompt_text = prepare_prompt_or_report_error(
                client,
                event,
                logger,
                event.get("ts"),
            )
            if not prompt_text:
                return
            logger.info("channel file mention accepted: channel=%s ts=%s", channel_id, event.get("ts"))
            ensure_in_channel(client, channel_id, logger)
            spawn_prompt_worker(client, channel_id, event.get("ts"), prompt_text)
            return

        if thread_ts:
            key = session_key(channel_id, thread_ts)
            if session_store.get(key) is not None or has_files:
                prompt_text = prepare_prompt_or_report_error(
                    client,
                    event,
                    logger,
                    thread_ts,
                )
                if not prompt_text:
                    return
                logger.info(
                    "channel thread reply accepted: channel=%s thread=%s has_files=%s",
                    channel_id,
                    thread_ts,
                    has_files,
                )
                ensure_in_channel(client, channel_id, logger)
                spawn_prompt_worker(client, channel_id, thread_ts, prompt_text)
                return

    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
