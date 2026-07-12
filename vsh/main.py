import os
import sys

import typer
from loguru import logger

from vsh.core.config import _get_config_path, interactive_setup, load_config
from vsh.core.pty_shell import PtyShell
from vsh.providers import resolve_stt, resolve_thinker, resolve_tts

STATE = {"v": False, "in": None, "out": None, "vad_thr": 1000, "vad_sil": 15, "model": "vosk-model-en-in-0.5"}

# --- SHELL PROXY INTERCEPT ---
# If vsh is used as $SHELL, programs like nvim and tmux will execute `vsh -c "cmd"`.
# Typer will crash on -c. We must intercept it at the module level before Typer boots.
if len(sys.argv) >= 3 and sys.argv[1] == "-c":
    try:
        from vsh.core.config import load_config

        cfg = load_config()
        inner = cfg.shell.inner_shell or os.environ.get("SHELL") or "/bin/bash"
    except Exception:
        inner = os.environ.get("SHELL") or "/bin/bash"

    if "vsh" in inner:
        inner = "/bin/bash"

    # Completely replace current process with the inner shell
    os.execv(inner, [inner, "-c"] + sys.argv[2:])
# -----------------------------


class NoSuchCommandShowsHelp(typer.core.TyperGroup):
    """Show full help instead of a bare 'No such command' error."""

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is None:
            import sys

            sys.stderr.write(f"Unknown command: '{cmd_name}'\n")
            sys.stderr.write("\n" + ctx.get_help() + "\n")
            sys.exit(0)
        return command


def setup_logger(v: bool):
    STATE["v"] = v
    logger.remove()
    logger.add(sys.stderr, level="INFO" if v else "ERROR", format="{message}")
    try:
        from vosk import SetLogLevel

        SetLogLevel(-1)
    except ImportError:
        pass


app = typer.Typer(
    cls=NoSuchCommandShowsHelp,
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    voice: bool = typer.Option(False, "--voice", help="Start shell with microphone hot."),
    v: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logs."),
    echo: bool = typer.Option(False, "--echo", help="Run in diagnostic echo mode without LLMs."),
    no_overlay: bool = typer.Option(
        False, "--no-overlay", help="Disable the voice HUD overlay entirely (pure passthrough)."
    ),
    serve: bool = typer.Option(False, "--serve", help="Expose this live shell to kai over HTTP."),
    port: int = typer.Option(8770, "--port", help="Port for --serve (default 8770)."),
):
    """Voice Shell — Default action is to start the interactive terminal wrapper."""
    setup_logger(v)
    if os.environ.get("VSH_ACTIVE"):
        sys.stderr.write("[vsh] Already running inside vsh. Exiting.\n")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is not None:
        return

    config = load_config()

    if voice:
        config.shell.voice_on_start = True

    if no_overlay:
        config.shell.overlay_mode = "none"

    if echo:
        config.llm.provider = "echo"

    thinker = None
    if config.llm.provider:
        try:
            thinker = resolve_thinker(config.llm.provider, config)
        except Exception as e:
            sys.stderr.write(f"[vsh] Failed to load thinker '{config.llm.provider}': {e}\n")
            logger.error(f"Failed to load thinker '{config.llm.provider}': {e}")

    tts_provider = None
    try:
        tts_provider = resolve_tts(config)
    except Exception as e:
        sys.stderr.write(f"[vsh] Failed to load TTS '{config.tts.provider}': {e}\n")
        logger.error(f"Failed to load TTS '{config.tts.provider}': {e}")

    if config.tts.provider and not tts_provider:
        logger.warning(f"Unknown or failed TTS provider: {config.tts.provider}")

    pty_shell = PtyShell(config, thinker, verbose=STATE["v"], tts_provider=tts_provider)

    # KAI Integration: Auto-serve if running inside herdr
    pane_id = os.environ.get("HERDR_PANE_ID")
    sess_file = None
    if pane_id:
        import socket
        import uuid
        import json
        import time
        from pathlib import Path
        
        serve = True
        if port == 8770:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            
        sess_file = Path.home() / ".kai" / "vsh_sessions.json"
        try:
            sess_file.parent.mkdir(parents=True, exist_ok=True)
            recs = {}
            if sess_file.exists():
                with open(sess_file, "r") as f:
                    recs = json.load(f)
            recs[pane_id] = {
                "session_id": uuid.uuid4().hex[:12],
                "pane_id": pane_id,
                "label": "",
                "cwd": os.environ.get("HERDR_ACTIVE_PANE_CWD", os.getcwd()),
                "vsh_port": port,
                "created": time.time(),
            }
            with open(sess_file, "w") as f:
                json.dump(recs, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to register herdr session: {e}")

    if serve:
        from vsh.core.server import serve as serve_http

        serve_http(pty_shell, port=port)

    try:
        pty_shell.run()
    except Exception as e:
        logger.error(f"Shell crashed: {e}")
    finally:
        if pane_id and sess_file and sess_file.exists():
            try:
                import json
                with open(sess_file, "r") as f:
                    recs = json.load(f)
                if pane_id in recs:
                    del recs[pane_id]
                    with open(sess_file, "w") as f:
                        json.dump(recs, f, indent=2)
            except Exception:
                pass


@app.command()
def stt(
    file: str = typer.Option(None, "--file", "-f", help="Read from audio file instead of mic"),
):
    """Speech-to-Text: Convert mic/WAV to text."""
    config = load_config()
    STATE["in"] = config.stt.device_index
    STATE["vad_thr"] = config.stt.vad_threshold
    STATE["vad_sil"] = config.stt.vad_silence_limit

    stt_provider = resolve_stt(config)
    if not stt_provider:
        from vsh.providers.vosk import VoskSTTProvider

        stt_provider = VoskSTTProvider(config.stt.model or STATE["model"])
    if file == "-":
        res = stt_provider.transcribe_stream(iter(lambda: sys.stdin.buffer.read(4000), b""))
    elif file:
        import wave

        with wave.open(file, "rb") as f:
            data = f.readframes(f.getnframes())
            rate = f.getframerate()
        res = stt_provider.transcribe_stream([data], rate=rate)
    else:
        if STATE["v"]:
            sys.stderr.write("LISTENING\n")
            sys.stderr.flush()
        from vsh.core.audio import MicStream, no_stderr

        with no_stderr(), MicStream(device_index=STATE["in"]) as s:
            res = stt_provider.transcribe_stream(
                s.live_gen(threshold=STATE["vad_thr"], silence_limit=STATE["vad_sil"], verbose=STATE["v"])
            )
    if res:
        print(res)


@app.command()
def tts(
    text: str = typer.Argument(None),
    save: str = typer.Option(None, "--save", help="Save to WAV file"),
    stream: bool = typer.Option(False, "--stream", help="Output raw bytes to stdout"),
):
    """Text-to-Speech: Read text aloud."""
    config = load_config()
    STATE["out"] = config.tts.device_index
    text = text or (not sys.stdin.isatty() and sys.stdin.read().strip())
    if not text:
        logger.error("No input")
        raise typer.Exit(code=1)

    from vsh.core.audio import no_stderr

    with no_stderr():
        tts_provider = resolve_tts(config)
        if not tts_provider:
            # Fallback to a default if None
            from vsh.providers.supertonic import SupertonicTTSProvider

            tts_provider = SupertonicTTSProvider(voice="F1")

    if STATE["v"]:
        sys.stderr.write("SPEAKING\n")
        sys.stderr.flush()
    wav = tts_provider.synthesize(text)
    data = (wav * 32767 * 0.9).astype("int16").tobytes()
    from vsh.core.audio import play_audio, save_audio

    if save:
        save_audio(save, data, 44100)
        logger.info(f"Saved: {save}")
    else:
        if stream:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            play_audio(data, 44100, device_index=STATE["out"])


@app.command()
def setup(section: str = typer.Argument(None, help="Specific section to configure (e.g. llm, stt, tts, keybind)")):
    """Run the interactive configuration wizard."""
    interactive_setup(section=section)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def wrap(ctx: typer.Context):
    """Wrap a specific command/LLM inside vsh (e.g. vsh wrap aichat)."""
    if not ctx.args:
        logger.error("No command provided. Usage: vsh wrap <command>")
        raise typer.Exit(code=1)

    config = load_config()
    import shlex

    # Execute the command inside the user's default shell
    config.shell.inner_shell = os.environ.get("SHELL") or "/bin/bash"
    config.shell.inner_shell_args = [config.shell.inner_shell, "-c", shlex.join(ctx.args)]

    # We disable the internal LLM when wrapping an external one to avoid double-processing
    thinker = None
    # We also disable TTS since direct injection mode doesn't synthesize speech
    tts_provider = None

    pty_shell = PtyShell(config, thinker, verbose=STATE["v"], tts_provider=tts_provider)
    try:
        pty_shell.run()
    except Exception as e:
        logger.error(f"Shell crashed: {e}")


@app.command()
def bind():
    """Interactively setup a new keybind and update shell config."""
    import json

    from vsh.core.config import capture_keybind, update_shell_rc_bind

    sys.stdout.write("Keybind Setup Wizard\n")

    keybind_data = None
    while True:
        kb = capture_keybind()
        if not kb:
            sys.stdout.write("Aborted.\n")
            return

        from InquirerPy import inquirer

        if inquirer.confirm(message=f"You pressed {kb['name']}. Use this keybind?", default=True).execute():
            keybind_data = kb
            break

    sys.stdout.write(f"\nSelected keybind: {keybind_data['name']}\n")

    config_path = _get_config_path()
    if not config_path.exists():
        sys.stdout.write("Config file not found. Please run 'vsh setup' first.\n")
        return

    try:
        # Round-trip through tomllib so the edit is robust against different spacing,
        # comments, and table ordering. We only regenerate the [keybinds] table; the
        # rest of the file is preserved verbatim.
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)

        kb = dict(data.get("keybinds", {}))
        kb["toggle_listen"] = keybind_data["name"]
        kb["toggle_listen_triggers"] = keybind_data["triggers"]
        data["keybinds"] = kb

        def _dump_value(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, list | tuple):
                return "[" + ", ".join(_dump_value(x) for x in v) + "]"
            return json.dumps(v)

        lines = config_path.read_text().splitlines()
        out = []
        i = 0
        n = len(lines)
        in_keybinds = False
        kb_replaced = False
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if stripped == "[keybinds]":
                # Emit the regenerated keybinds table and skip until the next section
                out.append("[keybinds]")
                for k, v in kb.items():
                    out.append(f"{k} = {_dump_value(v)}")
                kb_replaced = True
                in_keybinds = True
                i += 1
                continue
            if in_keybinds:
                # Stop skipping once we hit the next table header
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_keybinds = False
                    out.append(line)
                    i += 1
                    continue
                else:
                    i += 1
                    continue
            out.append(line)
            i += 1

        # If no [keybinds] section existed, append one
        if not kb_replaced:
            if out and out[-1].strip():
                out.append("")
            out.append("[keybinds]")
            for k, v in kb.items():
                out.append(f"{k} = {_dump_value(v)}")

        config_path.write_text("\n".join(out) + "\n")
        sys.stdout.write("Updated config.toml with new keybind.\n")

    except Exception as e:
        sys.stdout.write(f"Failed to update config.toml: {e}\n")
        return

    from InquirerPy import inquirer

    update_rc = inquirer.confirm(
        message="Update your shell config (.bashrc/.zshrc) to launch vsh with this keybind?", default=True
    ).execute()

    if update_rc:
        import shutil

        default_shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        if "fish" in default_shell:
            default_rc = "~/.config/fish/config.fish"
        elif "zsh" in default_shell:
            default_rc = "~/.zshrc"
        else:
            default_rc = "~/.bashrc"

        rc_file = inquirer.text(message="Shell config file:", default=default_rc).execute()
        update_shell_rc_bind(rc_file, keybind_data)


if __name__ == "__main__":
    app()
