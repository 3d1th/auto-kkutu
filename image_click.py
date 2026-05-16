"""화면에서 템플릿 이미지를 찾아 그 위치를 클릭."""
from __future__ import annotations

import sys
from pathlib import Path


def resolve_asset(name: str) -> Path:
    """exe 옆 → 스크립트 옆 → 빌드 임시폴더 순으로 탐색."""
    candidates = []
    try:
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(exe_dir / name)
    except Exception:
        pass
    candidates.append(Path(__file__).with_name(name))
    # Nuitka onefile: sys._MEIPASS 비슷한 위치
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / name)
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # 없으면 첫 후보 반환 (에러 메시지용)


def click_image_on_screen(
    template_path: Path, confidence: float = 0.8, grayscale: bool = True
) -> tuple[bool, str]:
    """화면에서 template_path 이미지를 찾아 그 중심을 클릭.
    Returns (성공 여부, 메시지)."""
    try:
        import pyautogui
    except ImportError:
        return False, "pyautogui 미설치"

    if not template_path.exists():
        return False, f"템플릿 없음: {template_path}"

    try:
        loc = pyautogui.locateCenterOnScreen(
            str(template_path), confidence=confidence, grayscale=grayscale
        )
    except Exception as e:
        # pyautogui.ImageNotFoundException 포함 — 버전마다 클래스 위치 달라서 광범위 catch
        return False, f"이미지 검색 실패: {type(e).__name__}: {e}"

    if not loc:
        return False, "화면에서 이미지 못 찾음"

    try:
        pyautogui.click(loc.x, loc.y)
    except Exception as e:
        return False, f"클릭 실패: {e}"
    return True, f"클릭 ({loc.x}, {loc.y})"
