import typer, sys, json, pyaudio, wave, re, contextlib, os, warnings
from loguru import logger
from pathlib import Path
from vosk import Model, KaldiRecognizer, SetLogLevel
from supertonic import TTS as STTTS

# ponytail: silence deprecation noise at source
warnings.filterwarnings("ignore", category=DeprecationWarning)
import audioop

STATE = {"v": False, "in": None, "out": None, "vad_thr": 800, "vad_sil": 20, "model": "vosk-model-en-in-0.5"}

@contextlib.contextmanager
def no_stderr():
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(sys.stderr.fileno())
        os.dup2(devnull, sys.stderr.fileno())
        yield
    finally:
        os.dup2(old_stderr, sys.stderr.fileno())
        os.close(devnull); os.close(old_stderr)

def setup(v: bool, i: int = None, o: int = None, vt: int = 800, vs: int = 20, m: str = None):
    STATE.update({"v": v, "in": i, "out": o, "vad_thr": vt, "vad_sil": vs})
    if m: STATE["model"] = m
    logger.remove(); logger.add(sys.stderr, level="INFO" if v else "ERROR", format="<cyan>[vsh]</cyan> {message}")
    SetLogLevel(-1)

class AudioSignal:
    def __init__(self, data: bytes, rate: int, width: int = 2):
        self.data, self.rate, self.width = data, rate, width
    def to_rate(self, target: int):
        if self.rate == target: return self
        d, _ = audioop.ratecv(self.data, self.width, 1, self.rate, target, None)
        return AudioSignal(d, target, self.width)
    def play(self):
        with no_stderr():
            pa = pyaudio.PyAudio()
            # ponytail: switch to paInt16 (matches width=2) to fix distortion
            s = pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate, output=True, output_device_index=STATE["out"])
        s.write(self.data); s.stop_stream(); s.close(); pa.terminate()
    def save(self, p: str):
        with wave.open(p, 'wb') as f: f.setnchannels(1); f.setsampwidth(self.width); f.setframerate(self.rate); f.writeframes(self.data)

class MicStream:
    def __init__(self, rate=16000, chunk=1024):
        self.rate, self.chunk, self._q = rate, chunk, []
        with no_stderr(): self._pa = pyaudio.PyAudio()
    def __enter__(self):
        with no_stderr():
            self._s = self._pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate, input=True, 
                input_device_index=STATE["in"], frames_per_buffer=self.chunk, stream_callback=self._callback)
        return self
    def _callback(self, d, f, t, s): self._q.append(d); return None, pyaudio.paContinue
    def __exit__(self, *a):
        self._s.stop_stream(); self._s.close(); self._pa.terminate(); self._q.append(None)
    def live_gen(self, silence=None, timeout=50, thr=None):
        sil, vt, sc = silence or STATE["vad_sil"], thr or STATE["vad_thr"], 0
        while True:
            if not self._q: continue
            c = self._q.pop(0)
            if c is None: break
            yield c
            if audioop.rms(c, 2) > vt: sc = 0
            else: sc += 1
            if sc > sil: break

class VoskAdapter:
    def __init__(self, m_name=None):
        p = Path.cwd() / "models" / (m_name or STATE["model"])
        if not p.exists(): logger.error(f"Model {p.name} not found"); raise typer.Exit(1)
        self.m = Model(str(p))
    def transcribe(self, stream, live=False, rate=16000, width=2, on_phrase=None):
        rec, res, st = KaldiRecognizer(self.m, 16000), [], None
        for c in stream:
            if rate != 16000: c, st = audioop.ratecv(c, width, 1, rate, 16000, st)
            if rec.AcceptWaveform(c):
                t = json.loads(rec.Result())["text"]
                if t:
                    if STATE["v"]: sys.stderr.write("\r\033[K")
                    res.append(t); logger.info(f"Phrase: {t}")
                    if on_phrase: on_phrase(t)
            elif live and STATE["v"]:
                p = json.loads(rec.PartialResult())["partial"]
                if p: sys.stderr.write(f"\r\033[K• {p}"); sys.stderr.flush()
        if live and STATE["v"]: sys.stderr.write("\r\033[K")
        f = json.loads(rec.FinalResult())["text"]
        if f and on_phrase: on_phrase(f)
        return " ".join(filter(None, res + [f]))

class SupertonicAdapter:
    def __init__(self, voice="F1"): self.e = STTTS(auto_download=True); self.v = self.e.get_voice_style(voice_name=voice)
    def synthesize(self, text):
        for s in filter(None, re.split(r'(?<=[.!?]) +', text)):
            wav, _ = self.e.synthesize(text=s, voice_style=self.v, total_steps=8)
            # ponytail: gain reduction (0.9) to prevent clipping, convert to int16 bytes
            data = (wav * 32767 * 0.9).astype("int16").tobytes()
            yield AudioSignal(data, 44100, 2)

class LocalSpeech:
    def __init__(self, stt, tts): self.stt, self.tts = stt, tts
    def listen(self, sil=None, to=50, thr=None, on_phrase=None):
        sys.stderr.write("[vsh] LISTENING\n"); sys.stderr.flush()
        with MicStream() as s:
            return self.stt.transcribe(s.live_gen(sil, to, thr), live=True, on_phrase=on_phrase)
    def say(self, text):
        if text:
            sys.stderr.write("[vsh] SPEAKING\n"); sys.stderr.flush()
            for s in self.tts.synthesize(text): s.play()

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
def stt(file: str=typer.Option(None, "--file", "-f"), i: int=typer.Option(None, "--in"), m: str=typer.Option(None, "--model"), rate: int=16000):
    """Audio -> Text"""
    setup(STATE["v"], i, None, 800, 20, m); e = LocalSpeech(VoskAdapter(), None)
    if file == "-": res = e.stt.transcribe(iter(lambda: sys.stdin.buffer.read(4000), b""), rate=rate)
    elif file: 
        with wave.open(file, 'rb') as f: sig = AudioSignal(f.readframes(f.getnframes()), f.getframerate(), f.getsampwidth())
        res = e.stt.transcribe([sig.to_rate(16000).data])
    else: res = e.listen()
    if res: print(res)

@app.command()
def tts(text: str=typer.Argument(None), o: int=typer.Option(None, "--out"), save: str=None, voice: str="F1", stream: bool=False):
    """Text -> Audio"""
    setup(STATE["v"], None, o); text = text or (not sys.stdin.isatty() and sys.stdin.read().strip())
    if not text: raise typer.Exit(logger.error("No input") or 1)
    e = LocalSpeech(None, SupertonicAdapter(voice=voice)); sigs = list(e.tts.synthesize(text))
    if save: AudioSignal(b"".join(s.data for s in sigs), sigs[0].rate, sigs[0].width).save(save); logger.info(f"Saved: {save}")
    else: 
        for s in sigs:
            if stream: sys.stdout.buffer.write(s.data)
            else: s.play()
        if stream: sys.stdout.buffer.flush()

@app.command()
def duplex(i: int=typer.Option(None, "--in"), o: int=typer.Option(None, "--out"), vt: int=800, vs: int=20, m: str=None, voice: str="F1"):
    """Audio -> Text -> Audio"""
    setup(STATE["v"], i, o, vt, vs, m); e = LocalSpeech(VoskAdapter(), SupertonicAdapter(voice=voice))
    try:
        while True:
            e.listen(on_phrase=lambda t: (print(t), e.say(t)))
    except KeyboardInterrupt: sys.stderr.write("\r\033[K[vsh] Stopped\n")

if __name__ == "__main__": app()
