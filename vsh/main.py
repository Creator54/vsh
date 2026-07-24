import os
import sys

import typer
from loguru import logger

from vsh.core.config import _get_config_path, load_config
from vsh.core.pty_shell import PtyShell
from vsh.core.setup import (
    capture_keybind,
    interactive_setup,
    update_keybind_config,
    update_shell_rc_bind,
)
from vsh.providers import resolve_stt, resolve_thinker, resolve_tts

DEFAULT_VOSK_MODEL = "vosk-model-en-in-0.5"
VERBOSE = False

# Programs invoke $SHELL with `-c`, so handle it before Typer parses the arguments.
if len(sys.argv) >= 3 and sys.argv[1] == "-c":
    try:
        cfg = load_config()
        inner = cfg.shell.inner_shell or os.environ.get("SHELL") or "/bin/bash"
    except Exception:
        inner = os.environ.get("SHELL") or "/bin/bash"

    if "vsh" in inner:
        inner = "/bin/bash"

    os.execv(inner, [inner, "-c"] + sys.argv[2:])


class NoSuchCommandShowsHelp(typer.core.TyperGroup):
    """Show full help instead of a bare 'No such command' error."""

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is None:
            sys.stderr.write(f"Unknown command: '{cmd_name}'\n")
            sys.stderr.write("\n" + ctx.get_help() + "\n")
            sys.exit(0)
        return command


def setup_logger(v: bool):
    global VERBOSE
    VERBOSE = v
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
    voice: bool = typer.Option(False, "--voice", help="Start listening for voice commands immediately."),
    v: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logs."),
    echo: bool = typer.Option(False, "--echo", help="Echo recognized speech without using an AI."),
    no_overlay: bool = typer.Option(
        False, "--no-overlay", help="Hide the voice indicator and use the normal terminal cursor."
    ),
    serve: bool = typer.Option(False, "--serve", help="Expose this live shell on a local-only web server."),
    port: int = typer.Option(8770, "--port", help="Port for --serve (default 8770)."),
):
    """Start an interactive terminal controlled by voice."""
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

    voice_handler = None
    if config.shell.voice_handler:
        from vsh.providers.cli import CliThinker

        voice_handler = CliThinker(command=config.shell.voice_handler, timeout=300)

    thinker = None
    if not voice_handler and config.llm.provider:
        try:
            thinker = resolve_thinker(config.llm.provider, config)
        except Exception as e:
            sys.stderr.write(f"[vsh] Failed to load AI provider '{config.llm.provider}': {e}\n")
            logger.error(f"Failed to load AI provider '{config.llm.provider}': {e}")

    tts_provider = None
    try:
        tts_provider = resolve_tts(config)
    except Exception as e:
        sys.stderr.write(f"[vsh] Failed to load TTS '{config.tts.provider}': {e}\n")
        logger.error(f"Failed to load TTS '{config.tts.provider}': {e}")

    if config.tts.provider not in ("", "none") and not tts_provider:
        logger.warning(f"Unknown or failed TTS provider: {config.tts.provider}")

    pty_shell = PtyShell(
        config,
        thinker,
        verbose=VERBOSE,
        tts_provider=tts_provider,
        voice_handler=voice_handler,
    )

    if serve:
        try:
            from vsh.core.server import serve as serve_http

            serve_http(pty_shell, port=port)
        except Exception as e:
            logger.error(f"Shell bridge failed: {e}")

    try:
        pty_shell.run()
    except Exception as e:
        logger.error(f"Shell crashed: {e}")


@app.command()
def stt(
    file: str = typer.Option(None, "--file", "-f", help="Read from audio file instead of mic"),
):
    """Convert microphone input or an audio file to text."""
    config = load_config()

    stt_provider = resolve_stt(config)
    if not stt_provider:
        from vsh.providers.vosk import VoskSTTProvider

        stt_provider = VoskSTTProvider(config.stt.model or DEFAULT_VOSK_MODEL)
    if file == "-":
        res = stt_provider.transcribe_stream(iter(lambda: sys.stdin.buffer.read(4000), b""))
    elif file:
        import wave

        with wave.open(file, "rb") as f:
            data = f.readframes(f.getnframes())
            rate = f.getframerate()
        res = stt_provider.transcribe_stream([data], rate=rate)
    else:
        if VERBOSE:
            sys.stderr.write("LISTENING\n")
            sys.stderr.flush()
        from vsh.core.audio import MicStream, no_stderr

        with no_stderr(), MicStream(device_index=config.stt.device_index) as s:
            capture = s.capture_phrase(
                threshold=config.stt.vad_threshold,
                silence_limit=config.stt.vad_silence_limit,
                verbose=VERBOSE,
            )
            res = stt_provider.transcribe_stream(iter(capture.chunks)) if capture.accepted else ""
    if res:
        print(res)


@app.command()
def tts(
    text: str = typer.Argument(None),
    save: str = typer.Option(None, "--save", help="Save to WAV file"),
    stream: bool = typer.Option(False, "--stream", help="Write raw audio to standard output"),
):
    """Read text aloud."""
    config = load_config()
    text = text or (not sys.stdin.isatty() and sys.stdin.read().strip())
    if not text:
        logger.error("No input")
        raise typer.Exit(code=1)

    from vsh.core.audio import no_stderr

    with no_stderr():
        tts_provider = resolve_tts(config)
        if not tts_provider:
            from vsh.providers.supertonic import SupertonicTTSProvider

            tts_provider = SupertonicTTSProvider(voice="F1")

    if VERBOSE:
        sys.stderr.write("SPEAKING\n")
        sys.stderr.flush()
    wav = tts_provider.synthesize(text)
    data = (wav * 32767 * 0.9).astype("int16").tobytes()
    from vsh.core.audio import play_audio, save_audio

    if save:
        save_audio(save, data, 44100)
        logger.info(f"Saved: {save}")
    elif stream:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        play_audio(data, 44100, device_index=config.tts.device_index)


@app.command()
def setup(section: str = typer.Argument(None, help="Specific section to configure (e.g. llm, stt, tts, keybind)")):
    """Run the interactive configuration wizard."""
    interactive_setup(section=section)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def wrap(ctx: typer.Context):
    """Run a command or AI assistant inside vsh (for example, vsh wrap aichat)."""
    if not ctx.args:
        logger.error("No command provided. Usage: vsh wrap <command>")
        raise typer.Exit(code=1)

    config = load_config()
    import shlex

    config.shell.inner_shell = os.environ.get("SHELL") or "/bin/bash"
    config.shell.inner_shell_args = [config.shell.inner_shell, "-c", shlex.join(ctx.args)]

    pty_shell = PtyShell(config, verbose=VERBOSE)
    try:
        pty_shell.run()
    except Exception as e:
        logger.error(f"Shell crashed: {e}")


@app.command()
def bind():
    """Interactively set up a new keybind and update shell config."""
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
        update_keybind_config(config_path, keybind_data)
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
        update_shell_rc_bind(rc_file, keybind_data, False)


if __name__ == "__main__":
    app()
