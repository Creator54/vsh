import warnings

# Silence deprecation warnings at the source.
warnings.filterwarnings("ignore", category=DeprecationWarning)
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

from loguru import logger  # noqa: E402
from vosk import KaldiRecognizer, Model  # noqa: E402


# ponytail: minimal direct vosk model resolution without hardcoded model defaults
class VoskSTTProvider:
    """Vosk Offline Speech-to-Text provider."""

    def __init__(self, model_name: str | None = None, model_url: str | None = None):
        if not model_name:
            raise ValueError("No Vosk model name configured. Run 'vsh setup' to select a model.")
        self.model_name = model_name
        self.model_url = model_url

        # Use XDG-compatible path so models work regardless of install method
        model_path = str(Path.home() / ".local" / "share" / "vsh" / "models" / self.model_name)
        if not os.path.exists(model_path):
            if not self.model_url:
                raise ValueError(
                    f"Vosk model '{self.model_name}' is not installed at '{model_path}' and no download URL is configured."
                )
            self._ensure_model(model_path, self.model_url)
        self.model = Model(model_path)

    def _ensure_model(self, model_path: str, model_url: str):
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        logger.info(f"Downloading model {self.model_name}...")
        tmp_zip = model_path + ".tmp.zip"
        import urllib.request

        with urllib.request.urlopen(model_url, timeout=30) as r, open(tmp_zip, "wb") as f:
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
