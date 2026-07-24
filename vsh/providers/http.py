import copy
import json
import shlex
import urllib.error
import urllib.parse
import urllib.request


class HttpThinker:
    """Send prompts to OpenAI, Ollama, Anthropic, or Gemini-compatible servers."""

    FORMATS = {
        "openai": {
            "headers": {
                "Content-Type": "application/json",
                "Authorization": "Bearer {api_key}",
            },
            "body": {
                "model": "{model}",
                "messages": [{"role": "user", "content": "{prompt}"}],
            },
            "response_path": "choices.0.message.content",
        },
        "ollama": {
            "headers": {"Content-Type": "application/json"},
            "body": {
                "model": "{model}",
                "prompt": "{prompt}",
                "stream": False,
            },
            "response_path": "response",
        },
        "anthropic": {
            "headers": {
                "Content-Type": "application/json",
                "x-api-key": "{api_key}",
                "anthropic-version": "2023-06-01",
            },
            "body": {
                "model": "{model}",
                "messages": [{"role": "user", "content": "{prompt}"}],
                "max_tokens": 1024,
            },
            "response_path": "content.0.text",
        },
        "gemini": {
            "headers": {"Content-Type": "application/json"},
            "body": {
                "contents": [{"role": "user", "parts": [{"text": "{prompt}"}]}],
            },
            "response_path": "candidates.0.content.parts.0.text",
        },
    }

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        model: str = "",
        format: str = "openai",
        response_path: str = "",
        **kwargs,
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.format_spec = copy.deepcopy(self.FORMATS.get(format, self.FORMATS["openai"]))
        if response_path:
            self.format_spec["response_path"] = response_path

    def _substitute(self, obj, prompt: str):
        """Recursively substitute {api_key}, {model}, {prompt} into dicts and strings."""
        if isinstance(obj, str):
            return obj.replace("{api_key}", self.api_key).replace("{model}", self.model).replace("{prompt}", prompt)
        if isinstance(obj, dict):
            return {k: self._substitute(v, prompt) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._substitute(item, prompt) for item in obj]
        return obj

    def _extract(self, data: dict, path: str):
        """Extract value from nested dict using dot-path (e.g. choices.0.message.content)."""
        current = data
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
            if current is None:
                return None
        return current

    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "echo 'I did not catch that.'\n"

        headers = self._substitute(self.format_spec["headers"], prompt)
        body = self._substitute(self.format_spec["body"], prompt)
        payload = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = self._extract(data, self.format_spec["response_path"])
                if text is None:
                    return "echo 'Empty response from API'\n"
                return str(text).strip()
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:200]
            return f"echo {shlex.quote(f'HTTP {e.code}: {body_text}')}\n"
        except urllib.error.URLError as e:
            return f"echo {shlex.quote(f'Connection error: {e.reason}')}\n"
        except TimeoutError:
            return "echo 'Request timed out'\n"
        except json.JSONDecodeError:
            return "echo 'API returned invalid JSON'\n"
