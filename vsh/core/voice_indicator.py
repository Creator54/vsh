import base64
import os
import re
import secrets
import select
import sys
import time

from loguru import logger

from vsh.core.voice_input import VoiceState

CURSOR_RESET = b"\033]112\a\033[0 q"

_GRAPHICS_RESPONSE = rb"\x1b_Gi={image_id}(?:,[^;]*)?;[ -~]*\x1b\\"
_DEVICE_ATTRIBUTES_RESPONSE = re.compile(rb"\x1b\[\?[0-9;]*c")
_STATE_COLORS = {
    VoiceState.MUTED: (255, 130, 50),
    VoiceState.IDLE: (0, 210, 255),
    VoiceState.LISTENING: (255, 0, 180),
    VoiceState.TRANSCRIBING: (0, 210, 255),
    VoiceState.THINKING: (255, 174, 0),
    VoiceState.TYPING: (50, 220, 100),
    VoiceState.SPEAKING: (50, 220, 100),
}
_CURSOR_SHAPES = {
    VoiceState.MUTED: b"\033[4 q",
    VoiceState.IDLE: b"\033[6 q",
    VoiceState.LISTENING: b"\033[1 q",
    VoiceState.TRANSCRIBING: b"\033[3 q",
    VoiceState.THINKING: b"\033[4 q",
    VoiceState.TYPING: b"\033[6 q",
    VoiceState.SPEAKING: b"\033[5 q",
}


class VoiceIndicator:
    """Show voice state without occupying terminal cells."""

    def __init__(self, configured_mode: str, vad_threshold: int):
        self.configured_mode = configured_mode
        self.mode = "none"
        self.cols = 80
        self.pending_input = b""
        self.energy = 0
        self.threshold = vad_threshold
        self._image_id = secrets.randbelow(2**31 - 1) + 1
        self._placement_id = 1
        self._graphics_visible = False
        self._anim_frame = 0
        self._typing_until = 0.0
        self._alternate_screen = False
        self._control_tail = b""

    def select_mode(self, tty_available: bool) -> None:
        configured = self.configured_mode.lower()
        if configured not in ("auto", "kitty", "none"):
            logger.warning("Unknown overlay mode {!r}; using auto", configured)
            configured = "auto"
        if configured == "none" or not tty_available:
            self.mode = "none"
        elif configured == "kitty" or self._probe_graphics_support():
            self.mode = "graphics"
        else:
            self.mode = "cursor"

    def take_pending_input(self) -> bytes:
        pending, self.pending_input = self.pending_input, b""
        return pending

    def resize(self, cols: int) -> None:
        self.cols = cols

    def update_volume(self, energy: int, threshold: int) -> None:
        self.energy = energy
        self.threshold = threshold

    def render(self, state: VoiceState | str | None) -> None:
        state = VoiceState(state) if state is not None else None
        if self.mode == "cursor":
            self._render_cursor(state)
        elif self.mode == "graphics":
            self._render_graphics(state)

    def user_started_typing(self) -> None:
        self._typing_until = time.monotonic() + 0.6
        if self.mode == "graphics":
            self._delete_graphics()
        elif self.mode == "cursor":
            self._render_cursor(VoiceState.TYPING)

    def tick(self, state: VoiceState | str | None) -> None:
        state = VoiceState(state) if state is not None else None
        if self.mode == "graphics":
            self._render_graphics(state)
        elif self.mode == "cursor" and self._typing_until and time.monotonic() >= self._typing_until:
            self._typing_until = 0.0
            self._render_cursor(state)

    def track_output(self, data: bytes) -> None:
        stream = self._control_tail + data
        for match in re.finditer(rb"\x1b\[\?(1049|1047|47)(h|l)", stream):
            entering = match.group(2) == b"h"
            if entering != self._alternate_screen:
                self._alternate_screen = entering
                if entering and self.mode == "graphics":
                    self._delete_graphics()
        self._control_tail = stream[-16:]

    def restore(self) -> None:
        if self.mode == "graphics":
            self._delete_graphics()
        elif self.mode == "cursor":
            sys.stdout.buffer.write(CURSOR_RESET)
            sys.stdout.buffer.flush()

    def _probe_graphics_support(self, timeout: float = 0.25) -> bool:
        image_id = str(self._image_id).encode()
        query = b"\033_Gi=" + image_id + b",s=1,v=1,a=q,t=d,f=24;AAAA\033\\\033[c"
        sys.stdout.buffer.write(self._terminal_sequence(query))
        sys.stdout.buffer.flush()

        response = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([sys.stdin.fileno()], [], [], remaining)
            if not ready:
                break
            chunk = os.read(sys.stdin.fileno(), 1024)
            if not chunk:
                break
            response.extend(chunk)
            if _DEVICE_ATTRIBUTES_RESPONSE.search(response):
                break

        graphics_response = re.compile(_GRAPHICS_RESPONSE.replace(b"{image_id}", image_id))
        graphics_match = graphics_response.search(response)
        attributes_match = _DEVICE_ATTRIBUTES_RESPONSE.search(response)
        supported = bool(graphics_match and (not attributes_match or graphics_match.start() < attributes_match.start()))

        pending = graphics_response.sub(b"", bytes(response))
        self.pending_input += _DEVICE_ATTRIBUTES_RESPONSE.sub(b"", pending)
        return supported

    @staticmethod
    def _terminal_sequence(sequence: bytes) -> bytes:
        if os.environ.get("TMUX"):
            # tmux passthrough requires every embedded escape byte to be doubled.
            return b"\033Ptmux;" + sequence.replace(b"\033", b"\033\033") + b"\033\\"
        return sequence

    def _render_cursor(self, state: VoiceState | None) -> None:
        if state is None:
            sequence = CURSOR_RESET
        else:
            color = _STATE_COLORS.get(state, _STATE_COLORS[VoiceState.IDLE])
            sequence = f"\033]12;#{color[0]:02x}{color[1]:02x}{color[2]:02x}\a".encode() + _CURSOR_SHAPES.get(
                state, CURSOR_RESET
            )
        sys.stdout.buffer.write(sequence)
        sys.stdout.buffer.flush()

    def _icon_pixels(self, state: VoiceState, phase: int) -> bytes:
        color = _STATE_COLORS.get(state, _STATE_COLORS[VoiceState.IDLE])
        pixels = bytearray(16 * 16 * 4)

        def put(x: int, y: int, alpha: int = 255):
            if 0 <= x < 16 and 0 <= y < 16:
                offset = (y * 16 + x) * 4
                pixels[offset : offset + 4] = (*color, alpha)

        def hline(x1: int, x2: int, y: int, alpha: int = 255):
            for x in range(x1, x2 + 1):
                put(x, y, alpha)

        def vline(x: int, y1: int, y2: int, alpha: int = 255):
            for y in range(y1, y2 + 1):
                put(x, y, alpha)

        if state == VoiceState.MUTED:
            step = phase % 4
            left_alpha = 230 if step < 2 else 130
            right_alpha = 130 if step < 2 else 230
            vline(6, 5, 10, left_alpha)
            vline(9, 5, 10, right_alpha)
        elif state == VoiceState.IDLE:
            put(7, 7)
            put(8, 7)
            put(7, 8)
            put(8, 8)
            rings = (
                ((5, 7), (10, 7), (7, 5), (7, 10)),
                ((4, 7), (11, 7), (7, 4), (7, 11), (5, 5), (10, 5), (5, 10), (10, 10)),
                ((3, 7), (12, 7), (7, 3), (7, 12), (4, 4), (11, 4), (4, 11), (11, 11)),
                ((2, 7), (13, 7), (7, 2), (7, 13), (3, 3), (12, 3), (3, 12), (12, 12)),
            )
            for x, y in rings[phase % len(rings)]:
                put(x, y, 170 if phase < 3 else 120)
        elif state == VoiceState.LISTENING:
            threshold = max(1, self.threshold)
            level = 1 if self.energy <= threshold * 2.5 else 2 if self.energy <= threshold * 5 else 3
            bar_heights = (3, 6, 9, 6, 3)
            for index, x in enumerate(range(3, 13, 2)):
                height = min(9, bar_heights[(index + phase) % len(bar_heights)] + level)
                top = 8 - height // 2
                vline(x, top, top + height - 1)
        elif state == VoiceState.TRANSCRIBING:
            wave_heights = (2, 5, 8, 3, 6, 10, 4, 7, 3, 9, 5, 2)
            for index, x in enumerate(range(2, 14)):
                height = wave_heights[(index + phase) % len(wave_heights)]
                top = 8 - height // 2
                vline(x, top, top + height - 1)
        elif state == VoiceState.THINKING:
            for index, x in enumerate((4, 8, 12)):
                alpha = 255 if index == phase % 3 else 100
                for pixel_x in (x - 1, x):
                    for pixel_y in (7, 8):
                        put(pixel_x, pixel_y, alpha)
        elif state == VoiceState.TYPING:
            hline(2, 13, 4)
            hline(2, 13, 12)
            vline(2, 5, 11)
            vline(13, 5, 11)
            for x in (4, 7, 10):
                put(x, 7)
            for x in (4, 6, 8, 10):
                put(x, 9)
            hline(5, 10, 11)
            if phase % 2:
                vline(11, 8, 10)
        elif state == VoiceState.SPEAKING:
            vline(3, 7, 9)
            vline(4, 6, 10)
            vline(5, 5, 11)
            vline(6, 4, 12)
            arcs = (
                ((9, 6), (10, 7), (10, 9), (9, 10)),
                ((11, 4), (12, 5), (13, 7), (13, 9), (12, 11), (11, 12)),
            )
            for arc in arcs[: 1 + phase % 2]:
                for x, y in arc:
                    put(x, y)

        return bytes(pixels)

    def _graphics_delete_sequence(self) -> bytes:
        return f"\033_Ga=d,d=I,i={self._image_id},p={self._placement_id},q=2;\033\\".encode()

    def _delete_graphics(self) -> None:
        if not self._graphics_visible:
            return
        sys.stdout.buffer.write(self._terminal_sequence(self._graphics_delete_sequence()) + b"\033[?25h")
        sys.stdout.buffer.flush()
        self._graphics_visible = False

    def _render_graphics(self, state: VoiceState | None) -> None:
        if self.cols < 2 or self._alternate_screen or time.monotonic() < self._typing_until:
            self._delete_graphics()
            return

        if state is None:
            self._delete_graphics()
            return

        phase = self._anim_frame
        self._anim_frame += 1
        animation_phase = (
            phase % 4
            if state == VoiceState.MUTED
            else phase % 12
            if state == VoiceState.TRANSCRIBING
            else phase % 3
            if state == VoiceState.THINKING
            else phase % 4
            if state in (VoiceState.IDLE, VoiceState.LISTENING)
            else phase % 2
        )
        payload = base64.standard_b64encode(self._icon_pixels(state, animation_phase))
        transmit_control = f"a=t,f=32,s=16,v=16,i={self._image_id},q=2".encode()
        place_control = f"a=p,i={self._image_id},p={self._placement_id},c=2,r=1,C=1,z=1,q=2".encode()
        col = max(1, self.cols - 1)
        cursor_visibility = b"\033[?25h" if state == VoiceState.MUTED else b"\033[?25l"
        transmit = b"\033_G" + transmit_control + b";" + payload + b"\033\\"
        place = f"\033[s\033[1;{col}H".encode() + b"\033_G" + place_control + b";" + b"\033\\\033[u" + cursor_visibility
        clear = self._terminal_sequence(self._graphics_delete_sequence()) if self._graphics_visible else b""
        sys.stdout.buffer.write(clear + self._terminal_sequence(transmit) + self._terminal_sequence(place))
        sys.stdout.buffer.flush()
        self._graphics_visible = True
