"""WeChat MCP Server — configuration and platform detection."""
import os
import platform
import shutil

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

KEY_FILE = os.path.join(SKILL_DIR, "key.txt")
CONTACTS_FILE = os.path.join(SKILL_DIR, "contacts.json")

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

if IS_MACOS:
    DB_BACKEND = "sqlcipher"
    SQLCIPHER_PATH = (
        os.environ.get("WECHAT_SQLCIPHER_PATH")
        or shutil.which("sqlcipher")
        or "/opt/homebrew/bin/sqlcipher"
    )
    WECHAT_DATA_GLOB = os.path.expanduser(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
        "xwechat_files/*/db_storage"
    )
elif IS_WINDOWS:
    KEY_FILE = os.path.join(SKILL_DIR, 'key_windows.txt')
    DB_BACKEND = os.environ.get('WECHAT_DB_BACKEND', 'sqlcipher_native')
    if DB_BACKEND not in ('sqlcipher_native', 'sqlite3'):
        raise ValueError('WECHAT_DB_BACKEND must be sqlcipher_native or sqlite3 on Windows')
    # decrypt_all.py 解密产物根目录（明文库，保留 {wxid}/db_storage 结构）
    DECRYPTED_DIR = os.path.join(SKILL_DIR, "decrypted")
    # 原始加密库（在线查询及提 key/全量解密的来源）
    WECHAT_DATA_GLOB = os.path.expanduser(
        "~/Documents/xwechat_files/*/db_storage"
    )
    if os.environ.get('WECHAT_DATA_ROOT'):
        root = os.path.expandvars(os.path.expanduser(os.environ['WECHAT_DATA_ROOT']))
        WECHAT_DATA_GLOB = os.path.join(root, '*', 'db_storage')
else:
    raise RuntimeError(f"Unsupported platform: {platform.system()}")
