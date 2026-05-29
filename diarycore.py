"""Encryption and storage for Diary.

A diary is a single self-contained file: it stores the random salt next to the
encrypted entries, so you can copy it anywhere and open it with just the
password. The location of the active file is remembered in a small config file.
"""

import base64
import json
import os
import shutil

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

VAULT_FORMAT = "diary-vault"
VAULT_VERSION = 1

# scrypt cost parameters for turning the password into a key.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1


def _xdg(var, *fallback):
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), *fallback)


def _config_dir():
    path = os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "diary")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


CONFIG_FILE = os.path.join(_config_dir(), "config.json")

# Suggested place for a new diary when the user doesn't pick one.
DEFAULT_VAULT_DIR = os.path.join(_xdg("XDG_DATA_HOME", ".local", "share"), "diary")
DEFAULT_VAULT_NAME = "diary.vault"


def get_vault_path():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("vault_path") or None
    except Exception:
        return None


def set_vault_path(path):
    data = json.dumps({"vault_path": os.path.abspath(path)}, indent=2)
    _atomic_write(CONFIG_FILE, data.encode("utf-8"))


def vault_ready():
    """True when we have a remembered file that exists and is a real vault."""
    p = get_vault_path()
    return bool(p) and os.path.exists(p) and is_valid_vault(p)


def derive_key(password, salt):
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _atomic_write(path, data):
    # Write to a temp file and rename, so a crash can't leave a half-written
    # diary behind. Mode 600 keeps the file readable by the owner only.
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_vault(path):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict) or obj.get("format") != VAULT_FORMAT:
        raise ValueError("Not a Diary vault file")
    salt = base64.b64decode(obj["salt"])
    token = obj["data"].encode("ascii")
    return salt, token


def _write_vault(salt, token, path):
    obj = {
        "format": VAULT_FORMAT,
        "version": VAULT_VERSION,
        "salt": base64.b64encode(salt).decode("ascii"),
        "data": token.decode("ascii"),
    }
    _atomic_write(path, json.dumps(obj, indent=2).encode("utf-8"))


def is_valid_vault(path):
    try:
        _read_vault(path)
        return True
    except Exception:
        return False


def create_diary(password, path):
    """Create a new empty diary at *path* and make it the active one."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    salt = os.urandom(16)
    fernet = Fernet(derive_key(password, salt))
    token = fernet.encrypt(json.dumps([], ensure_ascii=False).encode("utf-8"))
    _write_vault(salt, token, path)
    set_vault_path(path)
    return fernet


def open_vault(path):
    """Point Diary at an existing vault file (used by 'Open'/'Import')."""
    if not is_valid_vault(path):
        raise ValueError("Not a valid Diary vault file")
    set_vault_path(os.path.abspath(path))


def unlock(password):
    """Return a Fernet for the active vault, or raise InvalidToken if the
    password is wrong."""
    salt, token = _read_vault(get_vault_path())
    fernet = Fernet(derive_key(password, salt))
    fernet.decrypt(token)
    return fernet


def load_entries(fernet):
    path = get_vault_path()
    if not path or not os.path.exists(path):
        return []
    _salt, token = _read_vault(path)
    if not token:
        return []
    return json.loads(fernet.decrypt(token).decode("utf-8"))


def save_entries(fernet, entries):
    # Keep the existing salt so the same password keeps working.
    path = get_vault_path()
    salt, _token = _read_vault(path)
    token = fernet.encrypt(
        json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8"))
    _write_vault(salt, token, path)


def export_vault(dest_path):
    shutil.copyfile(get_vault_path(), dest_path)


def format_ts(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d  %H:%M")
    except ValueError:
        return iso
