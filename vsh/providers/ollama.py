import json
import shlex
import urllib.error
import urllib.request

from vsh.core.provider import Thinker


class OllamaThinker(Thinker):
    """Local LLM via Ollama. Passes the user's transcript through without prompt engineering."""

    def __init__(self, model: str = "llama3", endpoint: str = "http://localhost:11434/api/generate"):
        self.model = model
        self.endpoint = endpoint

    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "echo 'I did not catch that.'\n"

        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response", "").strip()
        except urllib.error.URLError as e:
            return f"echo {shlex.quote(f'Ollama unavailable: {e}')}\n"
        except json.JSONDecodeError:
            return "echo 'Ollama returned bad JSON'\n"
        except TimeoutError:
            return "echo 'Ollama request timed out'\n"
