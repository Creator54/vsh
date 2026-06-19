# conftest.py — pre-mock heavy hardware deps so tests can import vsh modules
# without requiring portaudio native libraries.
import sys
from unittest.mock import MagicMock

_mods = ("pyaudio", "supertonic", "vosk")
for _m in _mods:
    sys.modules.setdefault(_m, MagicMock(name=_m))
