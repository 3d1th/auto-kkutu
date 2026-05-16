"""Chrome에 저장된 특정 도메인 쿠키를 QWebEngineProfile에 주입.

Windows Chrome 쿠키 DB(SQLite, AES-256-GCM 암호화)를 직접 읽음.
- Local State에서 암호화 키 추출 (DPAPI 복호화)
- Network/Cookies SQLite에서 도메인 필터링
- 각 쿠키값 AES-GCM 복호화
- QNetworkCookie로 변환 후 QWebEngineProfile.cookieStore()에 주입

Chrome이 실행 중이어도 임시 파일에 복사해서 처리.
Google 등 다른 도메인 쿠키는 일절 읽지 않음 (SQL WHERE 절로 필터).
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QByteArray, QDateTime, QUrl
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWebEngineCore import QWebEngineProfile


def _copy_shared(src: Path, dst: Path) -> None:
    """Chrome이 잠근 파일도 읽을 수 있도록 FILE_SHARE_* 플래그로 복사."""
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    FILE_SHARE_DELETE = 0x4
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k32.ReadFile.restype = wintypes.BOOL

    handle = k32.CreateFileW(
        str(src),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFileW 실패 (err={ctypes.get_last_error()}) — {src}")
    try:
        chunk = (ctypes.c_ubyte * (64 * 1024))()
        read = wintypes.DWORD()
        with dst.open("wb") as f:
            while True:
                if not k32.ReadFile(handle, chunk, len(chunk), ctypes.byref(read), None):
                    raise OSError(f"ReadFile 실패 (err={ctypes.get_last_error()})")
                if read.value == 0:
                    break
                f.write(ctypes.string_at(chunk, read.value))
    finally:
        k32.CloseHandle(handle)


def _find_chrome_user_data() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA 환경변수 없음")
    p = Path(local) / "Google" / "Chrome" / "User Data"
    if not p.exists():
        raise FileNotFoundError(f"Chrome 사용자 데이터 폴더 없음: {p}")
    return p


def _find_cookies_file(user_data: Path) -> Path:
    candidates = []
    for profile in ["Default"] + [p.name for p in user_data.glob("Profile *")]:
        for sub in [("Network", "Cookies"), ("Cookies",)]:
            candidates.append(user_data / profile / Path(*sub))
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return c
    raise FileNotFoundError(
        f"Chrome 쿠키 파일을 찾지 못함. 확인한 경로:\n" + "\n".join(str(c) for c in candidates)
    )


def _get_chrome_master_key(user_data: Path) -> bytes:
    local_state = user_data / "Local State"
    if not local_state.exists():
        raise FileNotFoundError(f"Chrome Local State 없음: {local_state}")
    data = json.loads(local_state.read_text(encoding="utf-8"))
    encrypted = base64.b64decode(data["os_crypt"]["encrypted_key"])
    if encrypted[:5] != b"DPAPI":
        raise RuntimeError("예상치 못한 key prefix (DPAPI 아님)")
    encrypted = encrypted[5:]
    try:
        import win32crypt
    except ImportError as e:
        raise RuntimeError("pywin32 미설치 (pip install pywin32)") from e
    return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1]


def _decrypt_value(blob: bytes, key: bytes) -> str | None:
    if not blob:
        return ""
    prefix = blob[:3]
    if prefix in (b"v10", b"v11"):
        iv, payload, tag = blob[3:15], blob[15:-16], blob[-16:]
        try:
            from Crypto.Cipher import AES
            return AES.new(key, AES.MODE_GCM, iv).decrypt_and_verify(payload, tag).decode("utf-8", "ignore")
        except Exception:
            return None
    if prefix == b"v20":
        # Chrome 127+ App-Bound Encryption — 외부에서 복호화 불가
        return None
    # 구버전: DPAPI 직접 복호화
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8", "ignore")
    except Exception:
        return None


def _read_cookies(domain: str) -> tuple[list[dict], int, int]:
    """Returns (cookies, v20_skipped_count, decrypt_failed_count)."""
    user_data = _find_chrome_user_data()
    src = _find_cookies_file(user_data)
    key = _get_chrome_master_key(user_data)

    tmp_dir = Path(tempfile.gettempdir())
    tmp = tmp_dir / f"auto-kkutu-cookies-{os.getpid()}.db"
    _copy_shared(src, tmp)
    # SQLite WAL 사이드카도 같이 복사 (있으면)
    extra_copies: list[Path] = []
    for suffix in ("-wal", "-shm", "-journal"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            target = tmp_dir / f"{tmp.name}{suffix}"
            try:
                _copy_shared(side, target)
                extra_copies.append(target)
            except Exception:
                pass

    cookies: list[dict] = []
    v20_skipped = 0
    decrypt_failed = 0
    try:
        conn = sqlite3.connect(str(tmp))
        cur = conn.cursor()
        cur.execute(
            "SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly "
            "FROM cookies WHERE host_key LIKE ?",
            (f"%{domain}%",),
        )
        for host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly in cur.fetchall():
            blob = bytes(encrypted_value) if encrypted_value else b""
            if blob[:3] == b"v20":
                v20_skipped += 1
                continue
            value = _decrypt_value(blob, key)
            if value is None:
                decrypt_failed += 1
                continue
            expires_unix = 0
            if expires_utc:
                # Chrome stores expires_utc as μs since 1601-01-01
                expires_unix = int(expires_utc / 1_000_000 - 11644473600)
            cookies.append({
                "name": name,
                "value": value,
                "domain": host_key,
                "path": path or "/",
                "expires": expires_unix,
                "secure": bool(is_secure),
                "httponly": bool(is_httponly),
            })
        conn.close()
    finally:
        for p in [tmp, *extra_copies]:
            try:
                p.unlink()
            except Exception:
                pass
    return cookies, v20_skipped, decrypt_failed


def find_chrome_exe() -> str | None:
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def _cdp_works(port: int) -> bool:
    """CDP HTTP + WebSocket 핸드셰이크가 우리 origin을 허용하는지 확인."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
            v = json.loads(r.read())
        ws_url = v.get("webSocketDebuggerUrl")
        if not ws_url:
            return False
        import websocket
        ws = websocket.create_connection(ws_url, timeout=2)
        ws.close()
        return True
    except Exception:
        return False


def _port_listening(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
            return True
    except Exception:
        return False


def launch_chrome_for_debug(
    user_data_dir: Path, port: int = 9222, start_url: str = "https://kkutu.co.kr/"
) -> tuple[bool, str]:
    """앱 전용 user-data-dir로 Chrome을 디버그 모드 실행.
    Returns (성공 여부, 메시지)."""
    exe = find_chrome_exe()
    if not exe:
        return False, "Chrome 실행파일을 찾지 못함"

    # 이미 그 포트에 우리가 쓸 수 있는 디버그 Chrome이 있으면 재실행 불필요
    if _cdp_works(port):
        return True, f"이미 포트 {port}에 디버그 Chrome 떠 있음 — 그대로 사용"

    # 포트는 응답하는데 CDP 핸드셰이크가 거부됨 = 구버전 flag로 띄워진 Chrome
    if _port_listening(port):
        return False, (
            f"포트 {port}에 Chrome이 있지만 CDP 핸드셰이크가 거부됨 (403).\n"
            f"이전에 띄운 디버그 Chrome 창을 닫고 다시 [1)]을 누르세요."
        )

    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={port}",
                f"--remote-allow-origins=*",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                start_url,
            ],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008,  # DETACHED_PROCESS
        )
    except Exception as e:
        return False, f"Chrome 실행 실패: {e}"

    # CDP가 정상 동작할 때까지 대기
    deadline = time.time() + 12
    while time.time() < deadline:
        if _cdp_works(port):
            return True, f"Chrome 디버그 모드 실행 완료 (포트 {port})"
        time.sleep(0.3)
    return True, "Chrome은 실행됐는데 CDP 응답이 지연됨 — 몇 초 후 [2)]을 시도하세요"


def _get_cookies_via_cdp(domain: str, port: int = 9222) -> list[dict]:
    """Chrome DevTools Protocol로 쿠키 조회. Chrome이 메모리에서 복호화된 값을 직접 줌."""
    try:
        import websocket  # websocket-client
    except ImportError as e:
        raise RuntimeError("websocket-client 미설치 (pip install websocket-client)") from e

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            version = json.loads(r.read())
    except Exception as e:
        raise RuntimeError(
            f"CDP 연결 실패 — Chrome을 다음처럼 실행한 뒤 다시 시도하세요:\n"
            f'  chrome.exe --remote-debugging-port={port}\n'
            f"(에러: {e})"
        )

    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("CDP webSocketDebuggerUrl 응답에 없음")

    ws = websocket.create_connection(ws_url, timeout=5)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        deadline = time.time() + 6
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"CDP 에러: {msg['error']}")
                all_cookies = msg.get("result", {}).get("cookies", [])
                return [c for c in all_cookies if domain in (c.get("domain") or "")]
    finally:
        ws.close()
    raise RuntimeError("CDP 응답 타임아웃")


def _inject_cookies(
    profile: QWebEngineProfile, domain: str, cookies: list[dict]
) -> tuple[int, list[str]]:
    store = profile.cookieStore()
    page_url = QUrl(f"https://{domain}/")
    count = 0
    names: list[str] = []
    for c in cookies:
        try:
            name = (c.get("name") or "").encode("utf-8", "ignore")
            value = (c.get("value") or "").encode("utf-8", "ignore")
            if not name:
                continue
            qc = QNetworkCookie(QByteArray(name), QByteArray(value))
            qc.setDomain(c.get("domain") or domain)
            qc.setPath(c.get("path") or "/")
            if c.get("secure"):
                qc.setSecure(True)
            if c.get("httponly") or c.get("httpOnly"):
                qc.setHttpOnly(True)
            # SameSite 보존 (modern QWebEngine은 SameSite 검사를 함)
            ss = (c.get("sameSite") or "").lower()
            if ss in ("strict",):
                qc.setSameSitePolicy(QNetworkCookie.SameSite.Strict)
            elif ss in ("lax",):
                qc.setSameSitePolicy(QNetworkCookie.SameSite.Lax)
            elif ss in ("none",):
                qc.setSameSitePolicy(QNetworkCookie.SameSite.None_)
            exp = c.get("expires")
            if exp and exp > 0:
                qc.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(exp)))
            store.setCookie(qc, page_url)
            count += 1
            names.append(c.get("name") or "")
        except Exception:
            continue
    return count, names


def get_chrome_localstorage(domain: str, port: int = 9222) -> tuple[dict, str | None]:
    """디버그 Chrome에서 해당 도메인 탭의 localStorage를 통째로 dump.
    Returns (dict, 에러 메시지 or None)."""
    try:
        import websocket
    except ImportError:
        return {}, "websocket-client 미설치"

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:
            tabs = json.loads(r.read())
    except Exception as e:
        return {}, f"CDP 탭 목록 조회 실패: {e}"

    tab = next(
        (t for t in tabs if t.get("type") == "page" and domain in (t.get("url") or "")),
        None,
    )
    if not tab:
        return {}, f"디버그 Chrome에 {domain} 탭이 없음 — 거기서 사이트 열어두세요"

    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return {}, "탭의 webSocketDebuggerUrl 없음"

    try:
        ws = websocket.create_connection(ws_url, timeout=5)
    except Exception as e:
        return {}, f"탭 WebSocket 연결 실패: {e}"
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": (
                    "JSON.stringify("
                    "  Object.fromEntries("
                    "    Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])"
                    "  )"
                    ")"
                ),
                "returnByValue": True,
            },
        }))
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                break
            if msg.get("id") == 1:
                if "error" in msg:
                    return {}, f"localStorage 평가 실패: {msg['error']}"
                val = msg.get("result", {}).get("result", {}).get("value")
                if not val:
                    return {}, None
                try:
                    return json.loads(val), None
                except Exception:
                    return {}, "localStorage JSON 파싱 실패"
    finally:
        ws.close()
    return {}, "localStorage 응답 타임아웃"


def import_chrome_cookies(
    profile: QWebEngineProfile, domain: str = "kkutu.co.kr"
) -> tuple[int, str | None]:
    """지정 도메인 쿠키만 Chrome에서 읽어 QWebEngineProfile에 주입.
    1) 디스크 직접 복호화 시도 (Chrome 종료 필요, Chrome 127+ ABE 쿠키는 못 읽음)
    2) 실패 시 CDP로 시도 (Chrome --remote-debugging-port 필요, ABE 우회됨)
    Returns (성공 개수, 에러 메시지 or None)."""
    disk_err = None
    v20_skipped = 0
    try:
        cookies, v20_skipped, decrypt_failed = _read_cookies(domain)
        if cookies:
            count, _ = _inject_cookies(profile, domain, cookies)
            if count:
                return count, None
    except Exception as e:
        disk_err = e

    # 디스크 방식 실패 — CDP로 fallback
    try:
        cdp_cookies = _get_cookies_via_cdp(domain)
    except Exception as e:
        parts = []
        if disk_err:
            parts.append(f"디스크: {disk_err}")
        if v20_skipped:
            parts.append(f"ABE로 보호된 쿠키 {v20_skipped}개 — 디스크에서는 못 읽음")
        parts.append(f"CDP: {e}")
        return 0, "  /  ".join(parts)

    if not cdp_cookies:
        return 0, f"CDP 연결은 됐지만 {domain} 쿠키가 없음 — 그 Chrome에서 먼저 로그인하세요"

    count, _names = _inject_cookies(profile, domain, cdp_cookies)
    return count, None if count else "CDP 쿠키 주입 실패"
