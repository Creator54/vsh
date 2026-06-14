import typer, sys, json, pyaudio, wave, re, contextlib, os, warnings
from loguru import logger
from pathlib import Path
from vosk import SetLogLevel
from vsh.core.audio import AudioSignal, MicStream
from vsh.providers.vosk import VoskSTTProvider
from vsh.providers.supertonic import SupertonicTTSProvider

STATE = {"v": False, "in": None, "out": None, "vad_thr": 800, "vad_sil": 20, "model": "vosk-model-en-in-0.5"}

@contextlib.contextmanager
def no_stderr():
    with open(os.devnull, "w") as f, contextlib.redirect_stderr(f): yield

def setup(v: bool, i: int = None, o: int = None, vt: int = 800, vs: int = 20, m: str = None):
    STATE.update({"v": v, "in": i, "out": o, "vad_thr": vt, "vad_sil": vs, "model": m or STATE["model"]})
    logger.remove(); logger.add(sys.stderr, level="INFO" if v else "ERROR", format="<cyan>[vsh]</cyan> {message}")
    SetLogLevel(-1)

class LocalSpeech:
    def __init__(self, stt, tts): self.stt, self.tts = stt, tts
    def listen(self, on_phrase=None):
        with no_stderr():
            sys.stderr.write("[vsh] LISTENING\n"); sys.stderr.flush()
            with MicStream(device_index=STATE["in"]) as s:
                return self.stt.transcribe_stream(s.live_gen(threshold=STATE["vad_thr"], silence_limit=STATE["vad_sil"], verbose=STATE["v"]), on_phrase=on_phrase)
    def say(self, text):
        if text:
            sys.stderr.write("[vsh] SPEAKING\n"); sys.stderr.flush()
            # SupertonicTTSProvider returns a numpy array
            wav = self.tts.synthesize(text)
            data = (wav * 32767 * 0.9).astype("int16").tobytes()
            AudioSignal(data, 44100).play(STATE["out"])

app = typer.Typer(add_completion=False, no_args_is_help=True)

@app.callback()
def main(v: bool=typer.Option(False, "--verbose", "-v")): setup(v)

@app.command()
def list_devices():
    """List local audio hardware."""
    p = pyaudio.PyAudio()
    for i in range(p.get_device_count()): print(f"[{i}] {p.get_device_info_by_index(i)['name']}")
    p.terminate()

@app.command()
def stt(file: str=typer.Option(None, "--file", "-f"), i: int=typer.Option(None, "--in"), m: str=typer.Option(None, "--model"), rate: int=16000, vt: int=400):
    """Audio -> Text"""
    setup(STATE["v"], i, None, vt, 20, m); e = LocalSpeech(VoskSTTProvider(m), None)
    if file == "-": 
        with no_stderr(): res = e.stt.transcribe_stream(iter(lambda: sys.stdin.buffer.read(4000), b""))
    elif file: 
        with wave.open(file, 'rb') as f: sig = AudioSignal(f.readframes(f.getnframes()), f.getframerate(), f.getsampwidth())
        res = e.stt.transcribe_stream([sig.to_rate(16000).data])
    else: res = e.listen()
    if res: print(res)

@app.command()
def tts(text: str=typer.Argument(None), o: int=typer.Option(None, "--out"), save: str=None, voice: str="F1", stream: bool=False):
    """Text -> Audio"""
    setup(STATE["v"], None, o); text = text or (not sys.stdin.isatty() and sys.stdin.read().strip())
    if not text: raise typer.Exit(logger.error("No input") or 1)
    e = LocalSpeech(None, SupertonicTTSProvider(voice=voice))
    wav = e.tts.synthesize(text)
    data = (wav * 32767 * 0.9).astype("int16").tobytes()
    sig = AudioSignal(data, 44100)
    if save: sig.save(save); logger.info(f"Saved: {save}")
    else: 
        if stream: sys.stdout.buffer.write(sig.data); sys.stdout.buffer.flush()
        else: sig.play(STATE["out"])

@app.command()
def duplex(i: int=typer.Option(None, "--in"), o: int=typer.Option(None, "--out"), vt: int=800, vs: int=20, m: str=None, voice: str="F1"):
    """Audio -> Text -> Audio"""
    setup(STATE["v"], i, o, vt, vs, m); e = LocalSpeech(VoskSTTProvider(m), SupertonicTTSProvider(voice=voice))
    try:
        while True:
            e.listen(on_phrase=lambda t: (print(t), e.say(t)))
    except KeyboardInterrupt: sys.stderr.write("\r\033[K[vsh] Stopped\n")

from vsh.core.config import load_config
from vsh.core.pty_shell import PtyShell
from vsh.providers import THINKER_PROVIDERS, STT_PROVIDERS, TTS_PROVIDERS

@app.command()
def setup_config():
    """Setup vsh shell integration."""
    p = Path.home() / ".config" / "vsh" / "config.toml"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[shell]\nvoice_on_start = false\n\n[keybinds]\ntoggle_listen = \"ctrl+\\\\\"\n\n[stt]\nprovider = \"vosk\"\n\n[tts]\nprovider = \"supertonic\"\n")
        print(f"Created {p}")
    print("\nAdd to ~/.bashrc or terminal config:\n  exec vsh shell\n")

@app.command()
def shell(inner_shell: str=typer.Option(None, "--shell", "-s", help="Override inner shell"), 
          voice: bool=typer.Option(False, "--voice", help="Start with voice enabled"),
          llm: str=typer.Option(None, "--llm", help="Thinker provider to use"),
          stt: str=typer.Option(None, "--stt", help="STT provider to use"),
          tts: str=typer.Option(None, "--tts", help="TTS provider to use"),
          i: int=typer.Option(None, "--in", help="Audio input device index")):
    """Start vsh as a PTY shell wrapper."""
    setup(STATE["v"])
    config = load_config()
    
    if inner_shell: config.shell.inner_shell = inner_shell
    if voice: config.shell.voice_on_start = voice
    if llm: config.llm.provider = llm
    if stt: config.stt.provider = stt
    if tts: config.tts.provider = tts
    if i is not None: config.stt.device_index = i
        
    thinker = None
    if config.llm.provider and config.llm.provider in THINKER_PROVIDERS:
        thinker = THINKER_PROVIDERS[config.llm.provider]()
    elif config.llm.provider:
        logger.warning(f"Unknown LLM provider: {config.llm.provider}")

    tts_provider = None
    if config.tts.provider and config.tts.provider in TTS_PROVIDERS:
        tts_provider = TTS_PROVIDERS[config.tts.provider]()
    elif config.tts.provider:
        logger.warning(f"Unknown TTS provider: {config.tts.provider}")
        
    pty_shell = PtyShell(config, thinker, verbose=STATE["v"], tts_provider=tts_provider)
    
    # If voice_on_start is true, we simulate the first toggle
    if config.shell.voice_on_start:
        pty_shell._toggle_listening()
        
    try:
        pty_shell.run()
    except Exception as e:
        logger.error(f"Shell crashed: {e}")
        # Terminal gets restored by finally block in run()

if __name__ == "__main__": app()
