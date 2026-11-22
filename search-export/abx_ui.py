"""
abx_ui — branded terminal widgets for Abstract Search & Export.

The official Abstract lockup (rendered from the brand SVGs by the kit build), the brand palette, arrow-key
menus, a step tracker and a one-line progress bar. Works on macOS, Linux and Windows 10+, and degrades on
purpose:

  * truecolor terminals get the brand colours and the logo art; 256-colour terminals get close
    approximations; basic terminals get the 16 standard colours; NO_COLOR, TERM=dumb and pipes get none;
  * a terminal whose encoding cannot show the box and bullet glyphs gets ASCII stand-ins;
  * TERM=dumb, pipes, and a Windows console without VT processing get numbered prompts instead of
    arrow-key menus (nothing moves the cursor);
  * ABX_PLAIN=1 forces plain numbered prompts with no colour, no art and no redrawing, which is what a
    screen reader wants.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import codecs
import getpass
import os
import re
import shutil
import sys
import textwrap
import time
import unicodedata

try:
    import abx_brand
except ImportError:                                             # source checkout before the first build
    abx_brand = None


def _isatty(f):
    try:
        return bool(f) and f.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _flag(name):
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


PLAIN = _flag("ABX_PLAIN")
DUMB = os.environ.get("TERM", "").strip().lower() == "dumb"
NO_COLOR = bool(os.environ.get("NO_COLOR"))                     # no-color.org: present and not empty
STDIN_TTY = _isatty(sys.stdin)
STDOUT_TTY = _isatty(sys.stdout)


def _enable_windows_vt():
    """Turn on ANSI escape processing in a Windows console. False when the console cannot do it."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = getattr(ctypes, "windll").kernel32                # Windows only
        handle = k32.GetStdHandle(-11)                           # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:                                  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004 | 0x0001))
    except Exception:                                            # no ctypes, no console, old Windows
        return False


VT_OK = _enable_windows_vt() if (os.name == "nt" and STDOUT_TTY) else True

# CURSOR: escape sequences may be written and the cursor moved (in-place redraws, clearing the screen).
CURSOR = STDOUT_TTY and VT_OK and not DUMB and not PLAIN
# INTERACTIVE: arrow-key menus. Needs a keyboard on stdin as well.
INTERACTIVE = STDIN_TTY and CURSOR


def _color_level():
    if not CURSOR or NO_COLOR:
        return 0
    if os.environ.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit") or os.environ.get("WT_SESSION") or \
            os.environ.get("TERM_PROGRAM", "") in ("iTerm.app", "vscode", "WezTerm", "ghostty", "Hyper"):
        return 24
    if "256" in os.environ.get("TERM", ""):
        return 256
    return 16


COLOR_LEVEL = _color_level()
COLOR = COLOR_LEVEL > 0

# ───────────────────────────── glyphs and encoding ─────────────────────────────

GLYPHS = {"●": "*", "◉": "@", "○": "o", "▸": ">", "█": "#", "░": ".", "▀": "#", "▄": "#", "✓": "+", "✗": "x",
          "…": "...", "→": "->", "←": "<-", "↑": "^", "↓": "v", "│": "|", "━": "=", "─": "-", "┼": "+", "›": ">",
          "·": "-", "—": "-", "–": "-", "★": "*", "≈": "~", "×": "x", "‘": "'", "’": "'", "“": '"', "”": '"',
          " ": " "}
_TT = {ord(k): v for k, v in GLYPHS.items()}


def _stream_can_encode(stream, sample):
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        sample.encode(enc)
        return True
    except (UnicodeError, LookupError):
        return False


def _is_c(v):
    return v.split(".")[0] in ("C", "POSIX") and "utf" not in v.lower()


def _c_locale():
    """LANG=C / POSIX: Python may still pick UTF-8 (PEP 538 coerces LC_CTYPE to C.UTF-8), but the terminal behind
    it usually cannot show it."""
    env = os.environ
    if env.get("LC_ALL"):
        return _is_c(env["LC_ALL"])
    ctype, lang = env.get("LC_CTYPE") or "", env.get("LANG") or ""
    if ctype and ctype.lower() not in ("c.utf-8", "c.utf8"):
        return _is_c(ctype)
    return _is_c(lang) if lang else False


UNICODE = _stream_can_encode(sys.stdout, "".join(GLYPHS)) and not _c_locale() and not PLAIN


def t(s):
    """Text as this terminal can show it (ASCII stand-ins for the glyphs when needed)."""
    s = str(s)
    return s if UNICODE else s.translate(_TT)


def _translit(err):
    bad = err.object[err.start:err.end]
    return "".join(GLYPHS.get(ch, "?") for ch in bad), err.end


codecs.register_error("abx_translit", _translit)


class _GlyphWriter:
    """Wraps stdout/stderr when the terminal cannot show the glyphs: every write is transliterated, and any
    other character the encoding cannot carry becomes '?' instead of a UnicodeEncodeError."""

    def __init__(self, raw):
        self._raw = raw

    def write(self, s):
        return self._raw.write(str(s).translate(_TT))

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _install_ascii():
    for name in ("stdout", "stderr"):
        raw = getattr(sys, name, None)
        if raw is None or isinstance(raw, _GlyphWriter):
            continue
        try:
            raw.reconfigure(errors="abx_translit")
        except (AttributeError, ValueError, OSError, LookupError):
            pass
        setattr(sys, name, _GlyphWriter(raw))


if not UNICODE or not _stream_can_encode(sys.stderr, "".join(GLYPHS)):
    _install_ascii()

# ───────────────────────────── size ─────────────────────────────


def term_size():
    """Measured on every call (terminals resize). The real window wins over the COLUMNS variable."""
    for f in (sys.__stdout__, sys.__stdin__, sys.__stderr__):
        if f is None:
            continue
        try:
            sz = os.get_terminal_size(f.fileno())
            if sz.columns > 0:
                return sz
        except (AttributeError, ValueError, OSError):
            continue
    return shutil.get_terminal_size((80, 24))


def cols():
    return max(20, term_size().columns)


def rows():
    return max(10, term_size().lines)


def width():
    """Width for wrapped text and rules: the window, capped for readability."""
    return max(20, min(110, cols() - 1))


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s):
    """Visible width: escape sequences removed, wide characters counted twice."""
    return sum(_cw(ch) for ch in _ANSI.sub("", t(s)))


def clip(s, w=None):
    """Cut s to w visible columns (default: one less than the window) so a line never wraps. Escape sequences
    are kept intact and closed."""
    s = t(s)
    w = cols() - 1 if w is None else w
    if vlen(s) <= w:
        return s
    ell = t("…")
    room = max(0, w - len(ell))
    out, n, i, esc = [], 0, 0, False
    while i < len(s):
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group(0))
            esc = True
            i = m.end()
            continue
        c = _cw(s[i])
        if n + c > room:
            break
        out.append(s[i])
        n += c
        i += 1
    return "".join(out) + ("\033[0m" if esc else "") + ell


def pad(s, w):
    """Left-justify to w visible columns (clipping when longer)."""
    s = clip(s, w)
    return s + " " * max(0, w - vlen(s))


# ───────────────────────────── colour ─────────────────────────────

# The shared Abstract dark tokens: brand pink / mid / deep, the three accents,
# text-muted for secondary text, border-strong for rules, text-primary for white.
PINK, MID, DEEP = (255, 33, 107), (232, 0, 93), (194, 0, 76)
TEALC, AMBERC, BLUEC = (1, 230, 157), (245, 198, 30), (46, 155, 240)
GREY, DIMC, WHITE = (143, 138, 148), (94, 89, 100), (255, 255, 255)
# The shared no-go red: failures and errors only. Pink is the brand and an action colour, never a status.
RED = (255, 92, 92)

# Basic 16-colour stand-ins for the brand palette (chosen by meaning, not by distance).
_BASIC = {PINK: 95, MID: 95, DEEP: 35, TEALC: 92, AMBERC: 93, BLUEC: 94, GREY: 90, DIMC: 90, WHITE: 97, RED: 91}
_CUBE = [0, 95, 135, 175, 215, 255]


def _to256(rgb):
    def near(v):
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))
    r, g, b = (near(c) for c in rgb)
    cube = 16 + 36 * r + 6 * g + b
    cube_rgb = (_CUBE[r], _CUBE[g], _CUBE[b])
    avg = sum(rgb) // 3
    gi = max(0, min(23, round((avg - 8) / 10)))
    grey_rgb = (8 + 10 * gi,) * 3

    def dist(a):
        return sum((a[k] - rgb[k]) ** 2 for k in range(3))
    return 232 + gi if dist(grey_rgb) < dist(cube_rgb) else cube


def _to16(rgb):
    if rgb in _BASIC:
        return _BASIC[rgb]
    table = {30: (0, 0, 0), 31: (205, 49, 49), 32: (13, 188, 121), 33: (229, 229, 16), 34: (36, 114, 200),
             35: (188, 63, 188), 36: (17, 168, 205), 37: (229, 229, 229), 90: (102, 102, 102), 91: (241, 76, 76),
             92: (35, 209, 139), 93: (245, 245, 67), 94: (59, 142, 234), 95: (214, 112, 214), 96: (41, 184, 219),
             97: (255, 255, 255)}
    return min(table, key=lambda k: sum((table[k][i] - rgb[i]) ** 2 for i in range(3)))


def _fg_code(rgb):
    if COLOR_LEVEL == 24:
        return "38;2;%d;%d;%d" % tuple(rgb)
    if COLOR_LEVEL == 256:
        return "38;5;%d" % _to256(rgb)
    i = _nearest16(*rgb)                                  # the same brand mapping as the logo (pink = red)
    return str(30 + (i % 8) + (60 if i >= 8 else 0))


def fg(rgb, s):
    return f"\033[{_fg_code(rgb)}m{s}\033[0m" if COLOR_LEVEL else str(s)


def bold(s):
    return f"\033[1m{s}\033[0m" if CURSOR else str(s)


def highlight(s):
    """The selected row of a menu."""
    if COLOR_LEVEL == 24:
        return f"\033[48;2;60;12;32m {s} \033[0m"
    if COLOR_LEVEL == 256:
        return f"\033[48;5;52m {s} \033[0m"
    if CURSOR:
        return f"\033[7m {s} \033[0m"
    return f"[{s}]"


def pink(s): return fg(PINK, s)                                  # noqa: E704
def teal(s): return fg(TEALC, s)                                 # noqa: E704
def amber(s): return fg(AMBERC, s)                               # noqa: E704
def blue(s): return fg(BLUEC, s)                                 # noqa: E704
def dim(s): return fg(GREY, s)                                   # noqa: E704
def red(s): return fg(RED, s)                                    # noqa: E704


STATUS = {"PASS": teal, "FAIL": red, "WARN": amber, "INFO": blue, "INCONCLUSIVE": amber}


def _mix(a, b, f):
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def gradient(text):
    """The title in solid brand pink. (A pink-to-deep gradient was dropped: its last letters fell below 4.5:1 on the
    background, as in the browser app.)"""
    if not COLOR_LEVEL:
        return str(text)
    return f"\033[1;{_fg_code(PINK)}m{text}\033[0m"


def bar(frac, w):
    """A gradient bar w cells wide, filled to frac (0..1). frac None = unknown yet."""
    if frac is None:
        return fg(DIMC, t("░") * w)
    n = max(0, min(w, round(frac * w)))
    if COLOR_LEVEL in (24, 256):
        filled = "".join(fg(_mix(PINK, DEEP, i / max(1, w - 1)), "█") for i in range(n))
    else:
        filled = pink("█" * n)
    return t(filled + fg(DIMC, "░" * (w - n)))


# ───────────────────────────── screen ─────────────────────────────

def rule(ch="─"):
    print(fg(DIMC, t(ch) * width()))


def clear():
    if CURSOR:
        print("\033[2J\033[H", end="")


_RGB_SGR = re.compile(r"\x1b\[(38|48);2;(\d+);(\d+);(\d+)m")
_BASIC16 = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
            (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
            (255, 0, 255), (0, 255, 255), (255, 255, 255)]


def _xterm256(r, g, b):
    """Nearest xterm-256 colour (6x6x6 cube or grey ramp)."""
    def cube(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    ci = 16 + 36 * cube(r) + 6 * cube(g) + cube(b)
    levels = [0, 95, 135, 175, 215, 255]
    cr, cg, cb = levels[cube(r)], levels[cube(g)], levels[cube(b)]
    grey = round(((r + g + b) / 3 - 8) / 10)
    grey = max(0, min(23, grey))
    gv = 8 + 10 * grey
    d_cube = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    d_grey = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return ci if d_cube <= d_grey else 232 + grey


def _nearest16(r, g, b):
    """16 colours: keep the brand legible rather than nearest-by-distance (which splits Abstract pink between red
    and purple). Pinks become red (bright for the lighter end), light pixels bright white, dark ones black."""
    if r > 150 and g < 120 and b < 170:
        return 9 if r >= 235 else 1
    if r > 170 and g > 170 and b > 170:
        return 15
    if max(r, g, b) < 60:
        return 0
    return min(range(16), key=lambda k: (_BASIC16[k][0] - r) ** 2 + (_BASIC16[k][1] - g) ** 2 + (_BASIC16[k][2] - b) ** 2)


def _art_for_terminal(line):
    """The lockup is stored in 24-bit colour. Terminals that only do 256 or 16 colours (macOS Terminal, many SSH
    sessions) get the same drawing with each colour mapped to the nearest one they have."""
    if COLOR_LEVEL == 24:
        return line

    def sub(m):
        layer, r, g, b = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if COLOR_LEVEL == 256:
            return f"\x1b[{layer};5;{_xterm256(r, g, b)}m"
        i = _nearest16(r, g, b)
        base = (30 if layer == "38" else 40) + (i % 8) + (60 if i >= 8 else 0)
        return f"\x1b[{base}m"
    return _RGB_SGR.sub(sub, line)


def logo(compact=False):
    """The official Abstract lockup, drawn with half blocks. Shown on any colour terminal (24-bit, 256 or 16
    colours, mapped to the nearest colours available) that is at least 64 columns wide and can display the half
    blocks; otherwise the text word mark."""
    art = getattr(abx_brand, "MARK" if compact else "LOCKUP", None) if abx_brand else None
    if art and COLOR_LEVEL and UNICODE and cols() >= (16 if compact else 64) and \
            _stream_can_encode(sys.stdout, "█▀▄"):
        for line in art:
            print("  " + _art_for_terminal(line))
    else:
        print("  " + pink(bold("A")) + " " + bold("abstract"))


# -- launch mode (hidden) -------------------------------------------------------------------------------------------
# The same presentation layer as the browser app: a flight tag in the header, a star rule
# instead of the plain rule (same single line), and a launch animation of the official mark. It never changes a
# number, a prompt or an export. ABX_LAUNCH=1 turns it on; the main menu turns it on when you type "launch".
# It does nothing at all with NO_COLOR, ABX_PLAIN, TERM=dumb or when output is not a terminal.
_LAUNCH = [os.environ.get("ABX_LAUNCH", "").strip() == "1"]


def launch_possible():
    return bool(CURSOR and COLOR_LEVEL and not PLAIN and not NO_COLOR and STDOUT_TTY)


def launch_mode(on=None):
    """Read (no argument) or set launch mode. Returns whether it is on now (always False where it cannot show)."""
    if on is not None:
        _LAUNCH[0] = bool(on)
    return _LAUNCH[0] and launch_possible()


def star_rule():
    """One line, like rule(): dim dots with a few pink and blue stars."""
    w = width()
    out = []
    for i in range(w):
        k = (i * 2654435761 >> 7) % 101                  # a fixed scatter: the same sky on every screen
        if k == 3:
            out.append(fg(PINK, t("✦")))
        elif k in (11, 59):
            out.append(fg(BLUEC, t("·")))
        elif k % 13 == 0:
            out.append(fg(GREY, t("·")))
        else:
            out.append(" ")
    print("".join(out))


def rocket_launch(seconds=2.2):
    """The official mark rising off the pad, with a T-3 count. The mark is only moved whole: the exhaust is drawn
    below it, never over it. Does nothing where launch mode cannot show, or when ABX_REDUCED_MOTION=1 (then it
    prints the final frame: the mark and one line). Ctrl+C skips it."""
    if not launch_possible():
        return False
    art = list(getattr(abx_brand, "MARK", None) or []) if abx_brand else []
    if not art or not UNICODE or cols() < 40 or rows() < 16 or not _stream_can_encode(sys.stdout, "█▀▄░▒▓"):
        print("  " + gradient("Launch mode on") + dim("  · it changes only how the screens look"))
        return True
    if _flag("ABX_REDUCED_MOTION"):
        for line in art:
            print("  " + _art_for_terminal(line))
        print("  " + gradient("Launch mode on") + dim("  · it changes only how the screens look"))
        return True
    h = rows()
    pad = max(8, h - len(art) - 6)
    exhaust = [fg(AMBERC, "▓▓"), fg(PINK, "▒▒"), fg(DEEP, "░░")]
    words = [("T-3", "Fuelling"), ("T-2", "Ignition"), ("T-1", "Clamps release"), ("Away", "Clear of the tower")]
    frames = max(12, int(seconds * 14))
    centre = max(2, (cols() - 18) // 2)
    try:
        print("\033[?25l", end="")
        for f in range(frames):
            k = f / (frames - 1)
            lift = 0 if k < 0.45 else int(((k - 0.45) / 0.55) ** 2 * (pad + len(art)))
            top = pad - lift
            word = words[min(3, int(k * 4))]
            out = ["\033[2J\033[H", "\n  " + gradient(word[0]) + "  " + dim(word[1]) + "\n"]
            for row in range(h - 3):
                i = row - top
                if 0 <= i < len(art):
                    out.append(" " * centre + _art_for_terminal(art[i]) + "\n")
                elif len(art) <= i < len(art) + 3 and k >= 0.15:
                    plume = exhaust[i - len(art)] * (1 + int(k * 2))
                    out.append(" " * (centre + 6) + plume + "\033[0m\n")
                else:
                    out.append("\n")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            time.sleep(seconds / frames)
    except KeyboardInterrupt:
        pass
    finally:
        print("\033[0m\033[?25h\033[2J\033[H", end="")
        sys.stdout.flush()
    return True


def header(app_line, tenant=None, compact=False, sanitize: Callable[[Any], str] = str):
    """The top of every screen. sanitize: the function that makes server text safe (C.sanitize). Plain text when
    stdout is not a terminal or ABX_PLAIN=1: the branded header is for terminals only."""
    if PLAIN or not STDOUT_TTY:
        print()
        print(f"Abstract Search & Export {app_line}")
        if tenant:
            print("Connected to " + sanitize(tenant.get("name")) + " (" + sanitize(tenant.get("vendor_account_id"))
                  + ")" + (", production" if tenant.get("production") else ""))
        else:
            print("Not connected")
        print("-" * 20)
        return
    clear()
    print()
    logo(compact)                                  # the full lockup on every screen (both arts are 6 lines tall)
    print()
    title = "S E A R C H   &   E X P O R T" if cols() >= 56 else "SEARCH & EXPORT"
    tag = ("   " + fg(PINK, "· launch mode")) if launch_mode() else ""
    print(clip("  " + gradient(title) + "   " + dim(app_line) + tag))
    if tenant:
        env = pink("production") if tenant.get("production") else dim(sanitize(tenant.get("base_url")))
        print(clip(f"  {teal('●')} {bold(sanitize(tenant.get('name')))} {dim(sanitize(tenant.get('vendor_account_id')))}"
                   f"  ·  {env}"))
    else:
        print(f"  {amber('●')} {amber('not connected')}")
    if launch_mode():
        star_rule()
    else:
        rule("━")


def steps_bar(steps, current):
    """● done  ◉ current  ○ to do. Compact when the window is narrow."""
    if PLAIN:
        print(f"Step {current + 1} of {len(steps)}: {steps[current]}")
        return

    def build(compact):
        parts = []
        for i, name in enumerate(steps):
            if i < current:
                parts.append(teal("●") + ("" if compact else " " + dim(name)))
            elif i == current:
                parts.append(pink("◉ ") + bold(name))
            else:
                parts.append(fg(GREY, "○" + ("" if compact else " " + name)))
        return fg(GREY, " ─ " if not compact else " ").join(parts)
    line = build(False)
    if vlen(line) + 2 > cols() - 1:
        line = build(True)
    print(clip("  " + line))
    print("  " + dim(f"step {current + 1} of {len(steps)}"))
    rule()


def say(text, indent=2, style=None):
    """Wrapped paragraphs. style (e.g. dim) is applied after wrapping, so colour codes never count as width."""
    w = max(20, width() - indent)
    for para in t(text).split("\n"):
        if not para.strip():
            print()
            continue
        for ln in textwrap.wrap(para, w, break_on_hyphens=False, break_long_words=False):
            print(" " * indent + (style(ln) if style else ln))


def _msg(mark, msg, indent=4):
    w = max(20, width() - indent)
    body = textwrap.fill(t(msg), w, subsequent_indent=" " * indent, break_on_hyphens=False, break_long_words=False)
    print("  " + mark + body)


def ok(msg):
    print()
    _msg(teal(t("✓") + " "), msg)


def err(msg):
    print()
    _msg(red(t("✗") + " "), msg)


def note(msg):
    _msg(amber("! "), msg)


def info(msg):
    _msg(blue("i "), msg)


# ───────────────────────────── input ─────────────────────────────

class Back(Exception):
    """User asked to go back one step."""


class Home(Exception):
    """User asked for the main menu."""


class EndOfInput(KeyboardInterrupt):
    """stdin ended (a piped script ran out of answers). Stops the program; never loops."""


class Quit(KeyboardInterrupt):
    """The user pressed q in a menu."""


def _readline(prompt):
    try:
        return input(t(prompt))
    except EOFError:
        print()
        raise EndOfInput()


class NoTerminal(ValueError):
    """A secret was asked for but stdin is not a terminal, so it could not be read without echoing it.
    A ValueError, so every front end already shows it as a customer message and recovers."""


def is_mintty(env=None, os_name=None, stdin_tty=None):
    """Native Windows Python inside Git Bash / MSYS2 mintty: MSYSTEM is set and stdin is a pipe, not a Windows
    console, so neither hidden input nor arrow keys work there. winpty gives it a real console. The inputs are
    parameters so the check can be tested on any platform."""
    env = os.environ if env is None else env
    os_name = os.name if os_name is None else os_name
    stdin_tty = STDIN_TTY if stdin_tty is None else stdin_tty
    return os_name == "nt" and bool(env.get("MSYSTEM")) and not stdin_tty


WINPTY_HINT = "In Git Bash (mintty), run the tool through winpty: winpty python abstract_search.py"


def no_terminal_message(mintty=None):
    msg = "No terminal to read the API key from safely: set ABSTRACT_API_KEY, or run ssh with -t"
    if is_mintty() if mintty is None else mintty:
        msg += ". " + WINPTY_HINT
    return msg


def require_secret_terminal():
    """PORT-23: refuse before any prompt when a secret cannot be read safely (ssh without -t, a pipe, mintty).
    Reading it with input() would echo the key on the remote terminal."""
    if not STDIN_TTY:
        raise NoTerminal(no_terminal_message())


def ask(prompt, default=None, secret=False, nav=True):
    d = f" {dim('[' + str(default) + ']')}" if default not in (None, "") else ""
    p = f"  {pink('›')} {prompt}{d}: "
    if secret:
        require_secret_terminal()                                # never reads the line, so it is never echoed
        try:
            v = getpass.getpass(t(p))
        except EOFError:
            print()
            raise EndOfInput()
    else:
        v = _readline(p)                                         # piped: read the answer from stdin
    v = v.strip()
    if nav and v in ("<", ":back", ":b"):
        raise Back()
    if nav and v in (":menu", ":m"):
        raise Home()
    return v if v else (default if default is not None else "")


def confirm(prompt, default=True):
    if INTERACTIVE:
        try:
            k = select(prompt, [(True, "Yes"), (False, "No")], default=0 if default else 1, back=False, home=False)
        except Back:
            return False
        return bool(k)
    d = "Y/n" if default else "y/N"
    while True:
        v = ask(f"{prompt} ({d})", nav=False).lower()
        if not v:
            return default
        if v[0] in "yn":
            return v[0] == "y"
        print("  " + amber("answer y or n"))


class _Keys:
    """Holds the terminal in cbreak mode for one menu (not per key) and reads single keys from the fd.

    cbreak, not raw: ICANON and ECHO off, ISIG and output processing left on, so Ctrl+C still raises
    KeyboardInterrupt and newlines still return the carriage. The attributes are built by hand so every
    Python version behaves the same. The saved settings are restored in __exit__, which also runs when
    Ctrl+C interrupts a read."""

    def __init__(self):
        self.fd: Any = None
        self.old: Any = None

    def __enter__(self):
        if os.name == "nt":
            return self
        import termios
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        new = termios.tcgetattr(self.fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)
        new[6] = list(new[6])
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            except (OSError, ValueError):
                pass
            self.old = None
        return False

    def _ready(self, timeout):
        import select as _select
        r, _, _ = _select.select([self.fd], [], [], timeout)
        return bool(r)

    def _byte(self):
        b = os.read(self.fd, 1)
        if not b:
            raise EndOfInput()
        return b

    def read(self):
        """'up' | 'down' | 'enter' | 'back' | 'home' | 'quit' | 'first' | 'last' | 'pgup' | 'pgdn' | a char."""
        if os.name == "nt":
            return self._read_windows()
        b = self._byte()
        if b == b"\x1b":
            if not self._ready(0.06):
                return "back"                                    # a lone Esc
            b2 = self._byte()
            if b2 not in (b"[", b"O"):
                return ""                                        # Alt+key: ignored
            seq = b""
            while self._ready(0.06):
                c = self._byte()
                seq += c
                if 0x40 <= c[0] <= 0x7E:
                    break
            # Right arrow chooses, like Enter (UX-48: shown in the key hint as "Enter/→ choose").
            return {"A": "up", "B": "down", "C": "enter", "D": "back", "H": "first", "F": "last",
                    "1~": "first", "7~": "first", "4~": "last", "8~": "last", "5~": "pgup",
                    "6~": "pgdn"}.get(seq.decode("ascii", "replace"), "")
        if b[0] >= 0xC0:                                         # swallow the rest of a UTF-8 character
            for _ in range(1 if b[0] < 0xE0 else (2 if b[0] < 0xF0 else 3)):
                if self._ready(0.02):
                    self._byte()
            return ""
        return self._plain(b.decode("latin-1"))

    def _read_windows(self):
        import msvcrt
        ch = getattr(msvcrt, "getwch")()                    # Windows only
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "K": "back", "M": "enter", "G": "first", "O": "last", "I": "pgup",
                    "Q": "pgdn"}.get(getattr(msvcrt, "getwch")(), "")
        if ch == "\x1b":
            return "back"
        return self._plain(ch)

    @staticmethod
    def _plain(ch):
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x04":
            raise EndOfInput()
        if ch in ("\x7f", "\x08", "b", "B"):
            return "back"
        if ch in ("m", "M"):
            return "home"
        if ch in ("q", "Q"):
            return "quit"
        if ch == "k":
            return "up"
        if ch == "j":
            return "down"
        return ch


def select(title, items, default=0, back=True, hint=None, home=True, words=()):
    """A menu. items: [(key, label)]; returns the chosen key.
    Interactive: ↑/↓ move, Enter or → chooses, ← / Esc / b go back, m opens the main menu, q quits.
    Otherwise numbered prompts (pipes, TERM=dumb, ABX_PLAIN=1). words: hidden words that, typed while the menu is
    up, return "word:<word>" (the main menu's "launch")."""
    if not items:
        raise Back()
    if not INTERACTIVE:
        return _select_numbered(title, items, back, hint, home)
    idx = max(0, min(default, len(items) - 1))
    top, drawn = 0, 0
    keys_help = t("↑↓ move · Enter/→ choose" + (" · ← back" if back else "") + (" · m menu" if home else "")
                  + " · q quit")
    typed = ""
    print("\033[?25l", end="")                                   # hide the cursor while the menu is up
    try:
        with _Keys() as keys:
            while True:
                view = min(len(items), max(3, rows() - 16))
                if idx < top:
                    top = idx
                if idx >= top + view:
                    top = idx - view + 1
                lines = [clip(f"  {bold(title)}")]
                if hint:
                    lines.append(clip("  " + dim(hint)))
                for i in range(top, min(len(items), top + view)):
                    label = str(items[i][1])
                    if i == idx:
                        lines.append(clip(f"  {pink('▸')} " + highlight(label)))
                    else:
                        lines.append(clip(f"    {label}"))
                more = []
                if top > 0:
                    more.append("↑ more")
                if top + view < len(items):
                    more.append(f"↓ {len(items) - top - view} more")
                lines.append(clip("  " + dim(t("   ".join(more) + ("   " if more else "")) + keys_help)))
                if drawn:
                    print(f"\033[{drawn}F\033[J", end="")
                print("\n".join(lines), flush=True)
                drawn = len(lines)
                k = keys.read()
                if k == "up":
                    idx = (idx - 1) % len(items)
                elif k == "down":
                    idx = (idx + 1) % len(items)
                elif k == "first":
                    idx = 0
                elif k == "last":
                    idx = len(items) - 1
                elif k == "pgup":
                    idx = max(0, idx - view)
                elif k == "pgdn":
                    idx = min(len(items) - 1, idx + view)
                elif k == "enter":
                    return items[idx][0]
                elif k == "back" and back:
                    raise Back()
                elif k == "home" and home:
                    raise Home()
                elif k == "quit":
                    raise Quit()
                elif len(k) == 1 and k.isdigit() and 1 <= int(k) <= len(items):
                    idx = int(k) - 1
                if words and len(k) == 1 and k.isalpha():
                    typed = (typed + k.lower())[-16:]
                    hit = next((w for w in words if typed.endswith(w)), None)
                    if hit:
                        return "word:" + hit
    finally:
        print("\033[?25h", end="", flush=True)


def _select_numbered(title, items, back, hint, home):
    print()
    print("  " + bold(title))
    if hint:
        print("  " + dim(hint))
    for i, (_, label) in enumerate(items, 1):
        print(f"   {pink(str(i).rjust(2))}  {label}")
    extra = ([" 0 back"] if back else []) + (["m main menu"] if home else [])
    if extra:
        print("   " + dim(",  ".join(extra)))
    while True:
        v = _readline(f"  {pink('›')} choose 1-{len(items)}: ").strip()
        if back and v in ("0", "b", "<"):
            raise Back()
        if home and v in ("m", ":m", ":menu"):
            raise Home()
        if v in ("q", ":q"):
            raise Quit()
        if v.isdigit() and 1 <= int(v) <= len(items):
            return items[int(v) - 1][0]
        print("  " + amber("pick a number from the list"))


def pause(msg="press Enter to continue"):
    """Wait for Enter on a keyboard. A piped script is never made to answer a pause."""
    if not STDIN_TTY:
        return
    try:
        input(t("  " + dim(msg + "… ")))
    except EOFError:
        raise EndOfInput()


def wrap_visible(line, w):
    """Wrap one line to w VISIBLE columns. Colour codes take no room on screen, so they are never counted (textwrap
    counts them as characters and broke coloured bars and tables early). Words are kept whole where they fit."""
    if vlen(line) <= w:
        return [line]
    out, cur, cw = [], "", 0
    for tok in re.split(r"( +)", line):                          # words and the runs of spaces between them
        if not tok:
            continue
        tw = vlen(tok)
        if cw + tw <= w:
            cur, cw = cur + tok, cw + tw
            continue
        if tok.strip() == "":                                     # spaces at a break are dropped
            out.append(cur)
            cur, cw = "", 0
            continue
        if cur.strip():
            out.append(cur)
            cur, cw = "", 0
        for part in re.split(r"(\x1b\[[0-9;]*m)", tok):        # a word longer than the line: split it, codes kept
            if part.startswith("\x1b["):
                cur += part
                continue
            for ch in part:
                if cw + _cw(ch) > w:
                    out.append(cur)
                    cur, cw = "", 0
                cur, cw = cur + ch, cw + _cw(ch)
    if cur or not out:
        out.append(cur)
    return out


def page(text):
    """Show long text a screen at a time (Enter for more, q to stop). Unpaged when nobody can press a key."""
    lines = t(text).splitlines()
    if not STDIN_TTY:
        print("\n".join(lines))
        return
    w = max(20, cols() - 1)                                      # wrap first, so a page is exactly one screen
    lines = [part for ln in lines for part in wrap_visible(ln, w)]
    n = max(8, rows() - 3)
    for i in range(0, len(lines), n):
        print("\n".join(lines[i:i + n]))
        if i + n < len(lines):
            try:
                v = input(t(dim("  — Enter for more, q to stop — ")))
            except EOFError:
                raise EndOfInput()
            if v.strip().lower() == "q":
                return


# ───────────────────────────── progress ─────────────────────────────

def fmt_elapsed(s):
    s = int(s or 0)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_bytes(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class Progress:
    """One progress line, redrawn in place, with log lines scrolling above it. Without cursor control it
    prints a plain progress line when the stage or the percentage moves, or every 15 seconds."""

    def __init__(self):
        self.shown = False
        self.bar_line = ""
        self.last = (None, None, 0.0)

    def _line(self, pct, text):
        frac = None if pct is None else max(0.0, min(1.0, pct / 100.0))
        ptxt = "  --  " if pct is None else f"{pct:5.1f}%"
        if cols() < 72:                                          # narrow: the numbers matter more than the bar
            return clip(f"  {bold(ptxt)}  {dim(text)}")
        bw = max(8, min(30, cols() // 4))
        return clip(f"  {bar(frac, bw)} {bold(ptxt)}  {dim(text)}")

    def update(self, pct, text, stage=None):
        if CURSOR:
            self.bar_line = self._line(pct, text)
            print("\r\033[K" + self.bar_line, end="", flush=True)
            self.shown = True
            return
        step = None if pct is None else int(pct // 10)
        now = time.time()
        if (stage, step) != self.last[:2] or now - self.last[2] >= 15:
            print(t(f"  progress: {'--' if pct is None else f'{pct:.1f}%'}  {text}"), flush=True)
            self.last = (stage, step, now)

    def line(self, s):
        """A log line above the bar."""
        if CURSOR and self.shown:                                  # one write: the bar never disappears
            print("\r\033[K" + clip(s) + "\n" + self.bar_line, end="", flush=True)
        else:
            print(clip(s) if CURSOR else t(s), flush=True)

    def finish(self):
        if CURSOR and self.shown:
            print()
        self.shown = False


def table(rows_, cols_, flatten, cell, maxw=None, max_rows=None):
    """A table of untrusted values. cell() sanitises every value and every header before it is measured."""
    maxw = maxw or width() - 2
    if not rows_:
        print("  " + dim("(no events)"))
        return
    shown = rows_[:max_rows] if max_rows else rows_
    flat = [flatten(r) if isinstance(r, dict) else {} for r in shown]
    heads = {c: t(cell(c)) for c in cols_}
    vals = [{c: t(cell(f.get(c))) for c in cols_} for f in flat]
    colw = {c: min(40, max([vlen(heads[c])] + [vlen(v[c]) for v in vals])) for c in cols_}
    floor = {c: min(colw[c], 24 if c in ("@timestamp", "id") else 8) for c in cols_}   # the time stays readable
    cols_ = list(cols_)
    while sum(colw[c] for c in cols_) + 3 * (len(cols_) - 1) > maxw:
        shrinkable = [c for c in cols_ if colw[c] > floor[c]]
        if not shrinkable:
            break
        big = max(shrinkable, key=lambda c: colw[c])
        colw[big] -= 1
    dropped = 0
    while len(cols_) > 1 and sum(colw[c] for c in cols_) + 3 * (len(cols_) - 1) > maxw:
        cols_.pop()
        dropped += 1
    sep = fg(DIMC, t(" │ "))
    print("  " + sep.join(bold(pad(heads[c], colw[c])) for c in cols_))
    print("  " + fg(DIMC, t("─┼─").join(t("─") * colw[c] for c in cols_)))
    for v in vals:
        print("  " + sep.join(pad(v[c], colw[c]) for c in cols_))
    notes = []
    if dropped:
        notes.append(f"{dropped} more column(s) not shown at this width")
    if max_rows and len(rows_) > max_rows:
        notes.append(f"{len(rows_) - max_rows} more event(s) not shown")
    if notes:
        print("  " + dim("(" + "; ".join(notes) + ")"))
