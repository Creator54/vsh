import os
import sys

from vsh.core.config import load_config
from vsh.core.pty_shell import PtyShell
from vsh.providers import resolve_stt, resolve_thinker, resolve_tts


def _restore_nix_environment():
    variables = (
        "PATH",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON",
        "C_INCLUDE_PATH",
        "LIBRARY_PATH",
        "LD_LIBRARY_PATH",
    )
    for variable in variables:
        original = os.environ.pop(f"VSH_ORIGINAL_{variable}", "")
        if original:
            os.environ[variable] = original
        else:
            os.environ.pop(variable, None)


if os.environ.pop("VSH_NIX_WRAPPER", None):
    # The wrapper needs these while uv builds/launches VSH, but they must not
    # leak into the user's interactive shell or unrelated uv projects.
    _restore_nix_environment()

VERBOSE = False


def _is_vsh_active_on_this_terminal() -> bool:
    active_tty = os.environ.get("VSH_ACTIVE_TTY")
    if not active_tty:
        return False
    try:
        return active_tty == os.ttyname(sys.stdin.fileno())
    except OSError:
        return False


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


def setup_logger(v: bool):
    global VERBOSE
    VERBOSE = v
    from loguru import logger

    logger.remove()
    for level, icon, color in (
        ("DEBUG", "·", "<dim>"),
        ("INFO", "◆", "<cyan>"),
        ("SUCCESS", "✓", "<green>"),
        ("WARNING", "!", "<yellow>"),
        ("ERROR", "✗", "<red>"),
        ("CRITICAL", "✗", "<RED><bold>"),
    ):
        logger.level(level, icon=icon, color=color)
    logger.add(
        sys.stderr,
        level="INFO" if v else "SUCCESS",
        format="<level>{level.icon} {message}</level>",
        colorize=sys.stderr.isatty(),
    )
    try:
        from vosk import SetLogLevel

        SetLogLevel(-1)
    except ImportError:
        pass


_typer_app = None


def _get_typer_app():
    global _typer_app
    if _typer_app is not None:
        return _typer_app

    import typer
    from loguru import logger

    class NoSuchCommandShowsHelp(typer.core.TyperGroup):
        """Show full help instead of a bare 'No such command' error."""

        def get_command(self, ctx, cmd_name):
            command = super().get_command(ctx, cmd_name)
            if command is None:
                sys.stderr.write(f"Unknown command: '{cmd_name}'\n")
                sys.stderr.write("\n" + ctx.get_help() + "\n")
                ctx.exit(2)
            return command

    app_obj = typer.Typer(
        cls=NoSuchCommandShowsHelp,
        add_completion=False,
        no_args_is_help=False,
        rich_markup_mode=None,
        invoke_without_command=True,
    )

    @app_obj.callback(invoke_without_command=True)
    def main(
        ctx: typer.Context,
        voice: bool = typer.Option(False, "--voice", help="Start listening for voice commands immediately."),
        v: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logs."),
        echo: bool = typer.Option(False, "--echo", help="Echo recognized speech without using an AI."),
        no_overlay: bool = typer.Option(
            False, "--no-overlay", help="Hide the voice indicator and use the normal terminal cursor."
        ),
        serve: bool = typer.Option(False, "--serve", help="Expose read-only shell status on a local-only web server."),
        port: int = typer.Option(8770, "--port", help="Port for --serve (default 8770)."),
    ):
        """Start an interactive terminal controlled by voice."""
        setup_logger(v)
        if _is_vsh_active_on_this_terminal():
            logger.warning("Already running inside VSH on this terminal")
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
                logger.error(f"Failed to load AI provider '{config.llm.provider}': {e}")

        pty_shell = PtyShell(
            config,
            thinker,
            verbose=VERBOSE,
            voice_handler=voice_handler,
            server_port=port if serve else None,
        )

        try:
            pty_shell.run()
        except Exception as e:
            logger.error(f"Shell crashed: {e}")

    @app_obj.command()
    def stt(
        file: str = typer.Option(None, "--file", "-f", help="Read from audio file instead of mic"),
    ):
        """Convert microphone input or an audio file to text."""
        config = load_config()

        stt_provider = resolve_stt(config)
        if not stt_provider:
            typer.echo("Error: Speech recognition provider not configured. Run 'vsh setup' to configure.", err=True)
            raise typer.Exit(code=1)
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
                logger.info("Listening")
            from vsh.core.audio import MicStream
            from vsh.core.microphone import acquire_microphone, release_microphone

            try:
                microphone_lease = acquire_microphone()
            except OSError as error:
                typer.echo(f"Failed to acquire microphone: {error}", err=True)
                raise typer.Exit(code=1) from error
            if microphone_lease is None:
                typer.echo("Microphone is already in use by another VSH session.", err=True)
                raise typer.Exit(code=1)
            try:
                with MicStream(device_index=config.stt.device_index) as s:
                    capture = s.capture_phrase(
                        threshold=config.stt.vad_threshold,
                        silence_limit=config.stt.vad_silence_limit,
                        verbose=VERBOSE,
                    )
                    res = stt_provider.transcribe_stream(iter(capture.chunks)) if capture.accepted else ""
            finally:
                release_microphone(microphone_lease)
        if res:
            print(res)

    @app_obj.command()
    def tts(
        text: str = typer.Argument(None),
        save: str = typer.Option(None, "--save", help="Save to WAV file"),
        stream: bool = typer.Option(False, "--stream", help="Write raw audio to standard output"),
    ):
        """Read text aloud."""
        config = load_config()
        input_text: str | None = text or (sys.stdin.read().strip() if not sys.stdin.isatty() else None)
        if not input_text:
            logger.error("No input")
            raise typer.Exit(code=1)
        text = input_text

        from vsh.core.audio import no_stderr

        with no_stderr():
            tts_provider = resolve_tts(config)
            if not tts_provider:
                from vsh.providers.supertonic import SupertonicTTSProvider

                tts_provider = SupertonicTTSProvider(voice="F1")

        if VERBOSE:
            logger.info("Speaking")
        from vsh.core.audio import play_audio, save_audio, synthesize_pcm16

        data, rate = synthesize_pcm16(tts_provider, text)

        if save:
            save_audio(save, data, rate)
            logger.success(f"Saved · {save}")
        elif stream:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            play_audio(data, rate, device_index=config.tts.device_index)

    @app_obj.command()
    def setup(section: str = typer.Argument(None, help="Specific section to configure (e.g. llm, stt, tts, keybind)")):
        """Run the interactive configuration wizard."""
        from vsh.core.setup import interactive_setup

        interactive_setup(section=section)

    @app_obj.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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

    @app_obj.command()
    def bind():
        """Interactively set up a new keybind and update shell config."""
        from vsh.core.config import _get_config_path
        from vsh.core.setup import (
            _setup_lock,
            capture_keybind,
            get_default_rc,
            update_keybind_setup,
        )

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

        from InquirerPy import inquirer

        update_rc = inquirer.confirm(
            message="Update your shell config (.bashrc/.zshrc) to launch vsh with this keybind?", default=True
        ).execute()

        rc_file = ""
        if update_rc:
            import shutil

            default_shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"
            default_rc = get_default_rc(default_shell)
            rc_file = inquirer.text(message="Shell config file:", default=default_rc).execute()

        with _setup_lock(config_path):
            if not config_path.exists():
                sys.stdout.write("Config file was removed during setup. Please run 'vsh setup' first.\n")
                return
            try:
                update_keybind_setup(config_path, keybind_data, rc_file)
                sys.stdout.write("Updated config.toml with new keybind.\n")
            except Exception as e:
                sys.stdout.write(f"Failed to update keybind: {e}\n")

    globals()["app"] = app_obj
    globals()["main"] = main
    globals()["stt"] = stt
    globals()["tts"] = tts
    globals()["setup"] = setup
    globals()["wrap"] = wrap
    globals()["bind"] = bind
    _typer_app = app_obj
    return _typer_app


def __getattr__(name: str):
    if name in ("app", "main", "stt", "tts", "setup", "wrap", "bind"):
        _get_typer_app()
        return globals()[name]
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        if _is_vsh_active_on_this_terminal():
            sys.stderr.write("Already running inside VSH on this terminal\n")
            sys.exit(0)
        config = load_config()
        voice_handler = None
        if config.shell.voice_handler:
            from vsh.providers.cli import CliThinker

            voice_handler = CliThinker(command=config.shell.voice_handler, timeout=300)

        thinker = None
        if not voice_handler and config.llm.provider:
            try:
                thinker = resolve_thinker(config.llm.provider, config)
            except Exception as e:
                sys.stderr.write(f"Failed to load AI provider '{config.llm.provider}': {e}\n")

        pty_shell = PtyShell(
            config,
            thinker,
            verbose=False,
            voice_handler=voice_handler,
        )
        try:
            pty_shell.run()
        except Exception as e:
            sys.stderr.write(f"Shell crashed: {e}\n")
        os._exit(0)
    else:
        _get_typer_app()()
