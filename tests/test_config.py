import importlib


def test_macos_backend_is_sqlcipher(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    import config
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/local/bin/sqlcipher")
    importlib.reload(config)
    assert config.DB_BACKEND == "sqlcipher"
    assert config.SQLCIPHER_PATH == "/usr/local/bin/sqlcipher"


def test_windows_backend_is_native(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.delenv('WECHAT_DB_BACKEND', raising=False)
    import config
    importlib.reload(config)
    assert config.DB_BACKEND == "sqlcipher_native"
    assert config.DECRYPTED_DIR.endswith("decrypted")


def test_windows_offline_backend_is_explicit(monkeypatch):
    import platform
    import config
    monkeypatch.setattr(platform, 'system', lambda: 'Windows')
    monkeypatch.setenv('WECHAT_DB_BACKEND', 'sqlite3')
    importlib.reload(config)
    assert config.DB_BACKEND == 'sqlite3'


def test_reload_restores_real_platform():
    import config
    importlib.reload(config)  # 还原真实平台，避免污染后续测试
