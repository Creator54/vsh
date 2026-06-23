import warnings

# ponytail: silence deprecation noise at source
warnings.filterwarnings("ignore", category=DeprecationWarning)
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

from loguru import logger  # noqa: E402
from vosk import KaldiRecognizer, Model  # noqa: E402


class VoskSTTProvider:
    """Vosk Offline Speech-to-Text provider."""

    DEFAULT_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-en-in-0.5.zip"
    DEFAULT_MODEL_NAME = "vosk-model-en-in-0.5"

    def __init__(self, model_name: str = None, model_url: str = None):
        self.model_name = model_name or self.DEFAULT_MODEL_NAME
        self.model_url = model_url or self.DEFAULT_MODEL_URL
        # Use XDG-compatible path so models work regardless of install method
        model_path = str(Path.home() / ".local" / "share" / "vsh" / "models" / self.model_name)

        self._ensure_model(model_path, self.model_url)
        self.model = Model(model_path)

    def _ensure_model(self, model_path: str, model_url: str):
        if not os.path.exists(model_path):
            os.makedirs(os.path.dirname(model_path), exist_ok=True)
            logger.info(f"Downloading model {self.model_name}...")
            tmp_zip = model_path + ".tmp.zip"
            import ssl
            import sys
            import urllib.request

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(model_url, context=ctx) as r, open(tmp_zip, "wb") as f:
                total_size = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 128 * 1024
                while True:
                    chunk = r.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        percent = int((downloaded / total_size) * 100)
                        sys.stderr.write(
                            f"\rDownloading model {self.model_name}... {percent}% ({downloaded / (1024 * 1024):.1f}MB / {total_size / (1024 * 1024):.1f}MB)"
                        )
                        sys.stderr.flush()
                if total_size:
                    sys.stderr.write("\n")
            logger.info("Extracting...")
            shutil.unpack_archive(tmp_zip, os.path.dirname(model_path))
            os.remove(tmp_zip)
            logger.success("Model ready.")

    def transcribe_stream(self, audio_stream: Iterator[bytes], on_phrase=None, rate: int = 16000) -> str:
        rec, res = KaldiRecognizer(self.model, rate), []
        chunk_count = 0
        for chunk in audio_stream:
            chunk_count += 1
            if rec.AcceptWaveform(chunk):
                t = json.loads(rec.Result()).get("text", "")
                if t:
                    logger.debug(f"Vosk result: {t}")
                    res.append(t)
                    if on_phrase:
                        on_phrase(t)
            else:
                p = json.loads(rec.PartialResult()).get("partial", "")
                if p:
                    sys.stderr.write(f"\r\033[K• {p}")
                    sys.stderr.flush()

        if self.model_name:
            sys.stderr.write("\r\033[K")  # Clear partials
        logger.debug(f"Vosk stream finished. Total chunks: {chunk_count}")
        f = json.loads(rec.FinalResult()).get("text", "")
        if f:
            logger.debug(f"Vosk final: {f}")
            res.append(f)
            if on_phrase:
                on_phrase(f)
        return " ".join(filter(None, res))
