import contextlib
import os
import sys
import wave

import typer
from loguru import logger
from vosk import SetLogLevel

from vsh.core.audio import AudioSignal, MicStream
from vsh.core.config import _get_config_path, interactive_setup, load_config
from vsh.core.pty_shell import PtyShell
from vsh.providers import resolve_stt, resolve_thinker, resolve_tts
from vsh.providers.vosk import VoskSTTProvider

STATE = {"v": False, "in": None, "out": None, "vad_thr": 1000, "vad_sil": 15, "model": "vosk-model-en-in-0.5"}


class NoSuchCommandShowsHelp(typer.core.TyperGroup):
    """Show full help instead of a bare 'No such command' error."""

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is None:
            import click

            click.echo(f"Unknown command: '{cmd_name}'", err=True)
            click.echo("\n" + ctx.get_help())
            ctx.exit(0)
        return command


@contextlib.contextmanager
def no_stderr():
    with open(os.devnull, "w") as f, contextlib.redirect_stderr(f):
        yield


def setup_logger(v: bool):
    STATE["v"] = v
    logger.remove()
    logger.add(sys.stderr, level="INFO" if v else "ERROR", format="<cyan>[vsh]</cyan> {message}")
    SetLogLevel(-1)


class LocalSpeech:
    def __init__(self, stt, tts):
        self.stt, self.tts = stt, tts

    def listen(self, on_phrase=None):
        if STATE["v"]:
            sys.stderr.write("[vsh] LISTENING\n")
            sys.stderr.flush()
        with MicStream(device_index=STATE["in"]) as s:
            return self.stt.transcribe_stream(
                s.live_gen(threshold=STATE["vad_thr"], silence_limit=STATE["vad_sil"], verbose=STATE["v"]),
                on_phrase=on_phrase,
            )

    def say(self, text):
        if text:
            if STATE["v"]:
                sys.stderr.write("[vsh] SPEAKING\n")
                sys.stderr.flush()
            wav = self.tts.synthesize(text)
            data = (wav * 32767 * 0.9).astype("int16").tobytes()
            AudioSignal(data, 44100).play(STATE["out"])


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

    if echo:
        config.llm.provider = "echo"

    thinker = None
    if config.llm.provider:
        try:
            thinker = resolve_thinker(config.llm.provider, config)
        except Exception as e:
            sys.stderr.write(f"[vsh] Failed to load thinker '{config.llm.provider}': {e}\n")
            logger.error(f"Failed to load thinker '{config.llm.provider}': {e}")

    tts_provider = resolve_tts(config)
    if config.tts.provider and not tts_provider:
        logger.warning(f"Unknown TTS provider: {config.tts.provider}")

    pty_shell = PtyShell(config, thinker, verbose=STATE["v"], tts_provider=tts_provider)

    try:
        pty_shell.run()
    except Exception as e:
        logger.error(f"Shell crashed: {e}")


@app.command()
def stt(
    file: str = typer.Option(None, "--file", "-f", help="Read from audio file instead of mic"),
):
    """Speech-to-Text: Convert mic/WAV to text."""
    config = load_config()
    STATE["in"] = config.stt.device_index
    STATE["vad_thr"] = config.stt.vad_threshold

    sys.stderr.write("[vsh] VSH STT active\n")
    stt_provider = resolve_stt(config)
    if not stt_provider:
        stt_provider = VoskSTTProvider(config.stt.model or STATE["model"])
    with no_stderr():
        e = LocalSpeech(stt_provider, None)

    if file == "-":
        res = e.stt.transcribe_stream(iter(lambda: sys.stdin.buffer.read(4000), b""))
    elif file:
        with wave.open(file, "rb") as f:
            sig = AudioSignal(f.readframes(f.getnframes()), f.getframerate(), f.getsampwidth())
        res = e.stt.transcribe_stream([sig.to_rate(16000).data])
    else:
        res = e.listen()
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
    text = text or (not sys.stdin.isatty() and sys.stdin.read().strip())
    if not text:
        logger.error("No input")
        raise typer.Exit(code=1)

    sys.stderr.write("[vsh] VSH TTS active\n")
    with no_stderr():
        tts_provider = resolve_tts(config)
        if not tts_provider:
            # Fallback to a default if None
            from vsh.providers.supertonic import SupertonicTTSProvider

            tts_provider = SupertonicTTSProvider(voice="F1")
        e = LocalSpeech(None, tts_provider)

    wav = e.tts.synthesize(text)
    data = (wav * 32767 * 0.9).astype("int16").tobytes()
    sig = AudioSignal(data, 44100)
    if save:
        sig.save(save)
        logger.info(f"Saved: {save}")
    else:
        if stream:
            sys.stdout.buffer.write(sig.data)
            sys.stdout.buffer.flush()
        else:
            sig.play(STATE["out"])


@app.command()
def setup():
    """Run the interactive configuration wizard."""
    if _get_config_path().exists():
        overwrite = input("Config already exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            sys.stdout.write("Aborted.\n")
            return
    interactive_setup()


@app.command()
def bind():
    """Interactively setup a new keybind and update shell config."""
    import json

    from vsh.core.config import _get_config_path, capture_keybind, update_shell_rc_bind

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
        # We can't just dump TOML easily in python 3.11 without a 3rd party lib if we want to preserve comments
        # But config.toml is usually clean, we'll try to update it safely by string replacement
        content = config_path.read_text()

        # update toggle_listen
        import re

        content = re.sub(r'toggle_listen\s*=\s*".*?"', f"toggle_listen = {json.dumps(keybind_data['name'])}", content)

        # update toggle_listen_triggers or add it if missing
        if "toggle_listen_triggers" in content:
            content = re.sub(
                r"toggle_listen_triggers\s*=\s*\[.*?\]",
                f"toggle_listen_triggers = {json.dumps(keybind_data['triggers'])}",
                content,
                flags=re.DOTALL,
            )
        else:
            # append it under [keybinds]
            content = re.sub(
                r"(\[keybinds\].*?\n)",
                f"\\1toggle_listen_triggers = {json.dumps(keybind_data['triggers'])}\n",
                content,
                flags=re.DOTALL,
            )

        config_path.write_text(content)
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
