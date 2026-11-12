"""Key names: the pyautogui names Reparo's GUI agent sends, mapped to Playwright key names."""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_KEY_TABLE: Mapping[str, str] = {
    # Modifiers
    "ctrl": "Control",
    "control": "Control",
    "ctl": "Control",
    "ctrlleft": "ControlLeft",
    "lctrl": "ControlLeft",
    "ctrlright": "ControlRight",
    "rctrl": "ControlRight",
    "shift": "Shift",
    "shiftleft": "ShiftLeft",
    "lshift": "ShiftLeft",
    "shiftright": "ShiftRight",
    "rshift": "ShiftRight",
    "alt": "Alt",
    "option": "Alt",
    "opt": "Alt",
    "altleft": "AltLeft",
    "lalt": "AltLeft",
    "altright": "AltRight",
    "ralt": "AltRight",
    "altgr": "AltRight",
    "win": "Meta",
    "winleft": "MetaLeft",
    "winright": "MetaRight",
    "windows": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "super": "Meta",
    # Editing and whitespace
    "enter": "Enter",
    "return": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": " ",
    "spacebar": " ",
    "backspace": "Backspace",
    "bksp": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    # Navigation
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "uparrow": "ArrowUp",
    "downarrow": "ArrowDown",
    "leftarrow": "ArrowLeft",
    "rightarrow": "ArrowRight",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pgup": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "pgdown": "PageDown",
    "home": "Home",
    "end": "End",
    # Locks
    "capslock": "CapsLock",
    "caps_lock": "CapsLock",
    "numlock": "NumLock",
    "num_lock": "NumLock",
    "scrolllock": "ScrollLock",
    "scroll_lock": "ScrollLock",
    # Function keys
    **{f"f{n}": f"F{n}" for n in range(1, 25)},
    # Symbols spelled out
    "minus": "-",
    "dash": "-",
    "hyphen": "-",
    "plus": "+",
    "equal": "=",
    "equals": "=",
    "comma": ",",
    "period": ".",
    "dot": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "quote": "'",
    "apostrophe": "'",
    "backtick": "`",
    "grave": "`",
    "tilde": "~",
}
"""pyautogui key names (and the spellings Reparo's reference robot accepts) to Playwright's."""


def map_key(name: str, table: Mapping[str, str]) -> str:
    """One key's Playwright name. A single character passes through; others are looked up
    case-insensitively, and a name not in the table is left for Playwright to judge."""
    if len(name) == 1:
        return name
    return table.get(name.lower(), name)


def split_hotkey(hotkey: str) -> list[str]:
    """Split a hotkey on spaces, or on a `+` or `-` joining two key names.

    As Reparo's reference robot does: `"ctrl a"`, `"ctrl+shift+s"` and `"ctrl + shift + s"`
    all work, and a `+` or `-` that is itself the key (`"ctrl +"`, `"ctrl+-"`) is kept.
    """
    parts: list[str] = []
    chunks = hotkey.split()
    for position, chunk in enumerate(chunks):
        if chunk == "+" and 0 < position < len(chunks) - 1:
            continue  # "ctrl + shift + s": a spaced "+" between keys joins them
        if len(chunk) == 1:
            parts.append(chunk)
            continue
        current = ""
        for index, char in enumerate(chunk):
            is_separator = (
                char in "+-"
                and current != ""
                and index + 1 < len(chunk)
                and current[-1] not in "+-"
            )
            if is_separator:
                parts.append(current)
                current = ""
            else:
                current += char
        if current:
            parts.append(current)
    return parts
