from types import SimpleNamespace

from vsh.core.server import health_payload


def test_health_exposes_the_voice_state_used_by_the_hud():
    voice = {
        "enabled": True,
        "mic_muted": True,
        "phase": "thinking",
        "visual_state": "muted",
    }
    shell = SimpleNamespace(
        shell_name="fish",
        shell_pid=4321,
        shell_state="idle",
        voice_status=lambda: voice,
    )

    assert health_payload(shell) == {
        "status": "ok",
        "shell": "fish",
        "pid": 4321,
        "state": "idle",
        "voice": voice,
    }
