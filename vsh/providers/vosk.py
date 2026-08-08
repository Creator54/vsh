import warnings

# Silence deprecation warnings at the source.
warnings.filterwarnings("ignore", category=DeprecationWarning)
import fcntl  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
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
        model_path = Path.home() / ".local" / "share" / "vsh" / "models" / self.model_name
        self._ensure_model(model_path, self.model_url)
        self.model = Model(str(model_path))

    def _ensure_model(self, model_path: str | Path, model_url: str | None):
        model_path = Path(model_path)
        parent = model_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = parent / f".{model_path.name}.lock"
        staging = parent / f".{model_path.name}.installing"

        with lock_path.open("a+") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.info(f"Waiting for another VSH session to install {self.model_name}...")
                fcntl.flock(lock, fcntl.LOCK_EX)

            shutil.rmtree(staging, ignore_errors=True)
            if model_path.exists():
                return
            if not model_url:
                raise ValueError(
                    f"Vosk model '{self.model_name}' is not installed at '{model_path}' and no download URL is configured."
                )

            extracted = staging / "extracted"
            archive = staging / "model.zip"
            extracted.mkdir(parents=True)
            try:
                self._download_model(archive, model_url)
                logger.info("Extracting...")
                shutil.unpack_archive(archive, extracted)
                staged_model = extracted / model_path.name
                if not staged_model.is_dir():
                    raise ValueError(f"Downloaded archive does not contain {model_path.name!r}")
                os.replace(staged_model, model_path)
                logger.success("Model ready.")
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def _download_model(self, archive: Path, model_url: str):
        logger.info(f"Downloading model {self.model_name}...")
        import urllib.request

        with urllib.request.urlopen(model_url, timeout=30) as r, archive.open("wb") as f:
            chunk_size = 128 * 1024
            while True:
                chunk = r.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)

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
        logger.debug(f"Vosk stream finished. Total chunks: {chunk_count}")
        f = json.loads(rec.FinalResult()).get("text", "")
        if f:
            logger.debug(f"Vosk final: {f}")
            res.append(f)
            if on_phrase:
                on_phrase(f)
        return " ".join(filter(None, res))
