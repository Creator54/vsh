import typer
from rich.console import Console
from loguru import logger
from vsh.core.provider import update_state, VSHState, Thinker
from vsh.core.audio import MicStream, play_audio, save_audio
from vsh.providers import STT_PROVIDERS, TTS_PROVIDERS

app = typer.Typer(help="vsh: Voice Shell - Orchestrate STT and TTS providers.")
console = Console()

class EchoThinker(Thinker):
    """Simple thinker that echos back with a prefix."""
    def ask(self, prompt: str) -> str:
        return f"VSH: {prompt}" if prompt.strip() else "I didn't catch that."

THINKERS = {
    "echo": EchoThinker,
}

@app.command()
def stt(
    file: str = typer.Option(None, "--file", "-f", help="Path to an audio file to transcribe"),
    provider: str = typer.Option("vosk", help="STT provider to use")
):
    """Transcribe speech to text."""
    if provider not in STT_PROVIDERS:
        logger.error(f"Unknown provider '{provider}'")
        raise typer.Exit(1)

    update_state(VSHState.LISTENING)
    instance = STT_PROVIDERS[provider]()
    
    if file:
        logger.info(f"Transcribing file: {file} using {provider}...")
        result = instance.transcribe_file(file)
    else:
        logger.info(f"Starting live transcription using {provider}...")
        with MicStream() as stream:
            result = instance.transcribe_stream(stream.live_gen(silence_limit=15, timeout=45))
    
    logger.success(f"Result: {result}")
    update_state(VSHState.IDLE)

@app.command()
def tts(
    text: str = typer.Argument(..., help="Text to synthesize into speech"),
    provider: str = typer.Option("supertonic", help="TTS provider to use"),
    save: str = typer.Option(None, "--save", help="Path to save audio file")
):
    """Synthesize text to speech."""
    if provider not in TTS_PROVIDERS:
        logger.error(f"Unknown provider '{provider}'")
        raise typer.Exit(1)

    update_state(VSHState.SPEAKING)
    logger.info(f"Synthesizing text: '{text}' using {provider}...")
    
    instance = TTS_PROVIDERS[provider]()
    audio = instance.synthesize(text)
    
    logger.success(f"Generated {len(audio)/44100:.2f}s of audio.")
    
    if save:
        save_audio(audio, save)
        logger.info(f"Audio saved to: {save}")
    else:
        play_audio(audio)
    
    update_state(VSHState.IDLE)

@app.command()
def duplex(
    stt_provider: str = typer.Option("vosk", help="STT provider"),
    tts_provider: str = typer.Option("supertonic", help="TTS provider"),
    thinker: str = typer.Option("echo", help="Thinking provider")
):
    """Start the full voice-in, voice-out orchestrated loop."""
    if stt_provider not in STT_PROVIDERS or tts_provider not in TTS_PROVIDERS or thinker not in THINKERS:
        logger.error("Unknown provider")
        raise typer.Exit(1)

    stt_inst = STT_PROVIDERS[stt_provider]()
    tts_inst = TTS_PROVIDERS[tts_provider]()
    think_inst = THINKERS[thinker]()

    logger.info(f"Starting duplex mode (STT: {stt_provider}, TTS: {tts_provider}, Thinker: {thinker})")
    
    try:
        while True:
            update_state(VSHState.LISTENING)
            with MicStream() as stream:
                user_text = stt_inst.transcribe_stream(stream.live_gen())
            
            if not user_text.strip():
                logger.debug("Silence detected")
                continue

            console.print(f"[bold blue]User:[/bold blue] {user_text}")
            
            update_state(VSHState.THINKING)
            assistant_text = think_inst.ask(user_text)
            console.print(f"[bold green]Assistant:[/bold blue] {assistant_text}")
            
            update_state(VSHState.SPEAKING)
            audio = tts_inst.synthesize(assistant_text)
            play_audio(audio)
            
            update_state(VSHState.IDLE)
            
            # ponytail: single turn verification
            break
            
    except KeyboardInterrupt:
        logger.warning("Stopping duplex mode...")
        update_state(VSHState.IDLE)

if __name__ == "__main__":
    app()
