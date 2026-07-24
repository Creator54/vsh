import json
import shutil
import subprocess
import threading
from collections.abc import Callable

from loguru import logger


class JsonStreamDecoder:
    def __init__(self):
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def feed(self, chunk: str) -> list:
        self._buffer += chunk
        values = []
        while self._buffer:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            try:
                value, end = self._decoder.raw_decode(self._buffer)
            except json.JSONDecodeError:
                if len(self._buffer) > 16 * 1024 * 1024:
                    self._buffer = ""
                break
            values.append(value)
            self._buffer = self._buffer[end:]
        return values


class PipeWireState:
    def __init__(self):
        self._default_source = None
        self._sources = {}

    @property
    def mic_muted(self) -> bool | None:
        for source in self._sources.values():
            if source.get("name") == self._default_source:
                return source.get("muted")
        return None

    def update(self, objects) -> bool | None:
        if not isinstance(objects, list):
            return self.mic_muted

        for item in objects:
            if not isinstance(item, dict):
                continue
            object_id = item.get("id")
            if item.get("type") == "PipeWire:Interface:Metadata":
                self._update_metadata(item.get("metadata"))

            info = item.get("info")
            if not isinstance(info, dict) or not isinstance(object_id, int):
                continue
            props = info.get("props")
            if isinstance(props, dict) and props.get("media.class") == "Audio/Source":
                source = self._sources.setdefault(object_id, {})
                if isinstance(props.get("node.name"), str):
                    source["name"] = props["node.name"]

            source = self._sources.get(object_id)
            if source is not None:
                muted = self._props_mute(info.get("params"))
                if muted is not None:
                    source["muted"] = muted

        return self.mic_muted

    def _update_metadata(self, entries):
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("key") != "default.audio.source":
                continue
            value = entry.get("value")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = None
            self._default_source = value.get("name") if isinstance(value, dict) else None

    @staticmethod
    def _props_mute(params) -> bool | None:
        if not isinstance(params, dict):
            return None
        props = params.get("Props")
        if not isinstance(props, list):
            return None
        for prop in props:
            if isinstance(prop, dict) and isinstance(prop.get("mute"), bool):
                return prop["mute"]
        return None


class PipeWireMicMonitor(threading.Thread):
    def __init__(self, callback: Callable[[bool | None], None]):
        super().__init__(name="PipeWireMicMonitor", daemon=True)
        self._callback = callback
        self._command = shutil.which("pw-dump")
        self._stop_event = threading.Event()
        self._process = None

    @property
    def available(self) -> bool:
        return self._command is not None

    def stop(self):
        self._stop_event.set()
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def run(self):
        if not self._command:
            self._callback(None)
            return

        last_state = object()
        while not self._stop_event.is_set():
            decoder = JsonStreamDecoder()
            state = PipeWireState()
            try:
                self._process = subprocess.Popen(
                    [self._command, "--monitor"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                if self._process.stdout is None:
                    raise OSError("pw-dump stdout is unavailable")
                for line in self._process.stdout:
                    if self._stop_event.is_set():
                        break
                    for document in decoder.feed(line):
                        current = state.update(document)
                        if current != last_state:
                            last_state = current
                            self._callback(current)
            except OSError as error:
                logger.debug("PipeWire microphone monitor unavailable: {}", error)
            finally:
                process = self._process
                self._process = None
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()

            if self._stop_event.wait(1):
                break
            if last_state is not None:
                last_state = None
                self._callback(None)
