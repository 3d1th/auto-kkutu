import re
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWebEngineCore import (
    QWebEngineProfile,
    QWebEnginePage,
    QWebEngineSettings,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import json as _json

import wordbank
from chrome_import import (
    get_chrome_localstorage,
    import_chrome_cookies,
    launch_chrome_for_debug,
)
from image_click import click_image_on_screen, resolve_asset

KKUTU_URL = "https://kkutu.co.kr/"
WORDLIST_PATH = wordbank.USER_FILE  # word/user.txt
ACCOUNT_DIR = Path(__file__).with_name("account")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# 게임 화면에 표시되는 제시어 ('.jjo-display.ellipse')만 감시
WATCH_JS = r"""
(function () {
    var el = document.querySelector(".jjo-display.ellipse");
    return el ? (el.textContent || '').trim() : null;
})();
"""

# input[style*='float: left'] 에 값을 한 번에 채우고 input+keyup 이벤트만 발사.
# 버튼 클릭은 Python에서 화면 이미지 스캔으로 처리.
SEND_BUTTON_IMAGE = "send_button.png"  # 프로젝트 루트(또는 exe 옆)에 두기

TYPE_JS = r"""
(function () {
    const word = %s;
    const input = document.querySelector("input[style*='float: left']");
    if (!input) return { ok: false, reason: 'no input (style*=float: left)' };

    input.focus();

    let typed = false;
    if (!input.value) {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, word);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        typed = true;
    }
    return { ok: true, word: word, typed: typed };
})();
"""


_HANGUL = r"[가-힣ㄱ-ㅎㅏ-ㅣ]"
_STARTER_RE = re.compile(
    rf"^\s*{_HANGUL}\s*"
    rf"(?:\(\s*{_HANGUL}(?:\s*,\s*{_HANGUL})*\s*\))?"
    rf"\s*$"
)


def is_valid_starter(display: str) -> bool:
    """시작단어 형식 검증: '이' 또는 '이(리)' 또는 '이(리,니)' 만 허용."""
    return bool(_STARTER_RE.match(display or ""))


def parse_start_chars(display: str) -> list[str]:
    """게임 표시 텍스트를 시작 글자 후보 리스트로 변환.

    예시:
      '이'        → ['이']
      '이(리)'    → ['이', '리']
      '여(려)'    → ['여', '려']
      '롱(농,)'   → ['롱', '농']  (콤마/공백 허용)
    """
    s = (display or "").strip()
    if not s:
        return []
    chars: list[str] = []
    # 괄호 밖 첫 글자
    main = s[0]
    if main not in "()":
        chars.append(main)
    # 괄호 안 글자들
    for inner in re.findall(r"\(([^)]+)\)", s):
        for token in re.split(r"[\s,]+", inner):
            token = token.strip()
            if token:
                chars.append(token[0])
    # 중복 제거 (순서 유지)
    seen: set[str] = set()
    out: list[str] = []
    for c in chars:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


class KkutuWatcher(QWidget):
    """`.jjo-display.ellipse` 변화를 감지해서 detected 시그널을 발사."""

    detected = pyqtSignal(str)  # 새 제시어 (raw text, '이(리)' 같은 형태 포함)
    status = pyqtSignal(str)

    killer_priority: bool = True
    long_priority: bool = False
    typing_battle_mode: bool = False

    def __init__(self, view: QWebEngineView, parent=None):
        super().__init__(parent)
        self.view = view
        self.last_word: str | None = None

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self):
        page = self.view.page()
        if page is None:
            return
        page.runJavaScript(WATCH_JS, self._on_word)

    def _on_word(self, word):
        if not isinstance(word, str):
            return
        word = word.strip()
        if not word:
            return
        if word == self.last_word:
            return
        self.last_word = word
        self.detected.emit(word)
        self.status.emit(f"감지: '{word}'")


class WordbookPanel(QWidget):
    def __init__(self, watcher: KkutuWatcher, web_view: QWebEngineView, profile: QWebEngineProfile, parent=None):
        super().__init__(parent)
        self.watcher = watcher
        self.web_view = web_view
        self.profile = profile

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("단어장")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        self.state_label = QLabel("상태: 대기 중")
        self.state_label.setStyleSheet("color: #555; padding: 2px;")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        self.watcher.status.connect(self.state_label.setText)
        self.watcher.detected.connect(self._on_detected)

        # 로그인 상태
        self.login_label = QLabel()
        self._refresh_login_label()
        layout.addWidget(self.login_label)

        chrome_row = QHBoxLayout()
        launch_btn = QPushButton("1) Chrome 디버그 모드 실행")
        launch_btn.setToolTip(
            "앱 전용 user-data-dir로 Chrome을 --remote-debugging-port=9222로 실행.\n"
            "기존 Chrome과 분리된 깨끗한 창에서 kkutu에 로그인하세요."
        )
        launch_btn.clicked.connect(self._on_launch_chrome)
        import_btn = QPushButton("2) kkutu 세션 가져오기")
        import_btn.setToolTip(
            "디버그 Chrome에서 kkutu 로그인을 끝낸 뒤 누르세요.\n"
            "Chrome DevTools Protocol로 쿠키만 이쪽 브라우저로 복사합니다."
        )
        import_btn.clicked.connect(self._on_import_chrome)
        chrome_row.addWidget(launch_btn)
        chrome_row.addWidget(import_btn)
        layout.addLayout(chrome_row)

        layout.addWidget(self._divider())

        # 시작단어 자동 조회
        starter_label = QLabel("시작단어 조회 (자동 감지)")
        starter_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(starter_label)

        mode_row = QHBoxLayout()
        self.killer_toggle = QCheckBox("한방단어 우선")
        self.killer_toggle.setChecked(True)
        self.killer_toggle.toggled.connect(self._on_mode_toggled)
        self.long_toggle = QCheckBox("긴단어 우선")
        self.long_toggle.setChecked(False)
        self.long_toggle.toggled.connect(self._on_mode_toggled)
        mode_row.addWidget(self.killer_toggle)
        mode_row.addWidget(self.long_toggle)
        layout.addLayout(mode_row)

        battle_row = QHBoxLayout()
        self.battle_toggle = QCheckBox("타자대결 모드 (제시어 맨 앞 단어 자동 입력)")
        self.battle_toggle.setChecked(False)
        self.battle_toggle.toggled.connect(self._on_mode_toggled)
        battle_row.addWidget(self.battle_toggle)
        layout.addLayout(battle_row)

        self.detected_label = QLabel("감지된 제시어: —")
        self.detected_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #225; padding: 4px; background: #eef;"
        )
        layout.addWidget(self.detected_label)

        self.starter_result = QListWidget()
        self.starter_result.setMinimumHeight(360)
        self.starter_result.itemDoubleClicked.connect(self._on_item_doubleclicked)
        layout.addWidget(self.starter_result, 2)

        layout.addWidget(self._divider())

        # 사용자 단어장 (word/user.txt)
        wb_label = QLabel("내 단어장 (word/user.txt)")
        wb_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(wb_label)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("검색 / 추가할 단어")
        self.search_box.textChanged.connect(self._on_search)
        layout.addWidget(self.search_box)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("추가")
        del_btn = QPushButton("삭제")
        add_btn.clicked.connect(self._on_add)
        del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        self.count_label = QLabel("0개")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.count_label)

        self._all_words: list[str] = []
        self._last_chars: list[str] = []
        self._last_battle_word: str | None = None
        self._pending_localstorage: dict | None = None
        self.web_view.loadFinished.connect(self._maybe_apply_localstorage)
        self._load_wordlist()

    def _divider(self) -> QLabel:
        d = QLabel()
        d.setStyleSheet("border-top: 1px solid #ccc; margin: 4px 0;")
        d.setFixedHeight(1)
        return d

    def _refresh_login_label(self):
        storage = ACCOUNT_DIR / "storage"
        if storage.exists() and any(storage.iterdir()):
            self.login_label.setText("✓ 저장된 세션 있음 (자동 로그인됨)")
            self.login_label.setStyleSheet("color: #2a7; padding: 2px;")
        else:
            self.login_label.setText("○ 세션 없음 — 한 번 로그인하면 자동 저장")
            self.login_label.setStyleSheet("color: #a72; padding: 2px;")

    # ---- 단어장 ----
    def _load_wordlist(self):
        self._all_words = sorted(wordbank.load_user_words())
        self._refresh_wordlist()

    def _refresh_wordlist(self, filter_text: str = ""):
        self.list_widget.clear()
        items = [w for w in self._all_words if filter_text in w] if filter_text else self._all_words
        self.list_widget.addItems(items)
        self.count_label.setText(f"{len(self._all_words)}개")

    def _on_search(self, text: str):
        self._refresh_wordlist(text.strip())

    def _on_add(self):
        word = self.search_box.text().strip()
        if not word:
            return
        if wordbank.add_user_word(word):
            self._all_words.append(word)
            self._all_words.sort()
            self.search_box.clear()
            self._refresh_wordlist()
            self.state_label.setText(f"추가됨: {word} → word/user.txt")
            # 새 단어가 현재 감지된 시작 글자에 해당하면 결과 즉시 갱신
            if self._last_chars and word[:1] in self._last_chars:
                self._rerun_lookup()
        else:
            self.state_label.setText(f"이미 단어장에 있음: {word}")

    def _on_delete(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        word = item.text()
        if wordbank.remove_user_word(word):
            if word in self._all_words:
                self._all_words.remove(word)
            self._refresh_wordlist(self.search_box.text().strip())
            self.state_label.setText(f"삭제됨: {word}")
            if self._last_chars and word[:1] in self._last_chars:
                self._rerun_lookup()

    # ---- 모드 토글 ----
    def _on_mode_toggled(self, _on: bool):
        KkutuWatcher.killer_priority = self.killer_toggle.isChecked()
        KkutuWatcher.long_priority = self.long_toggle.isChecked()
        KkutuWatcher.typing_battle_mode = self.battle_toggle.isChecked()
        if not self.battle_toggle.isChecked():
            self._last_battle_word = None
        self._rerun_lookup()

    # ---- 자동 감지 → 조회 ----
    def _on_detected(self, raw: str):
        # 타자대결 모드: 공백으로 자른 첫 단어를 자동 입력
        if self.battle_toggle.isChecked():
            self._handle_typing_battle(raw)
            return

        if not is_valid_starter(raw):
            self.detected_label.setText(
                f"감지: '{raw}' — 시작단어 형식 아님 (이전 결과 유지)"
            )
            self.detected_label.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #888; padding: 4px; background: #f4f4f4;"
            )
            # 이전 결과(starter_result, _last_chars) 그대로 유지
            return
        chars = parse_start_chars(raw)
        self._last_chars = chars
        if not chars:
            return
        self.detected_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #225; padding: 4px; background: #eef;"
        )
        if len(chars) > 1:
            self.detected_label.setText(
                f"감지된 제시어: '{raw}'  →  두음법칙 {', '.join(chars)} 모두 검색"
            )
        else:
            self.detected_label.setText(f"감지된 제시어: '{raw}'")
        self._show_results(chars)

    BATTLE_MIN_WORDS = 4  # 이만큼 이상 단어가 표시될 때만 타자대결로 인정

    def _handle_typing_battle(self, raw: str):
        text = (raw or "").strip()
        if not text:
            return
        words = text.split()
        if len(words) < self.BATTLE_MIN_WORDS:
            self.detected_label.setText(
                f"타자대결 대기: '{text}' ({len(words)}단어 < {self.BATTLE_MIN_WORDS})"
            )
            self.detected_label.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #888; padding: 4px; background: #f4f4f4;"
            )
            # 단어 수가 부족하면 last_battle_word 초기화해서 나중에 다시 등장하면 새 단어로 인식
            self._last_battle_word = None
            return
        first = words[0]
        if not first or first == self._last_battle_word:
            return
        self._last_battle_word = first
        self.detected_label.setText(
            f"타자대결: '{first}' 자동 입력 (전체 {len(words)}단어)"
        )
        self.detected_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #c00; padding: 4px; background: #fee;"
        )
        js = TYPE_JS % _json.dumps(first, ensure_ascii=False)
        self.web_view.page().runJavaScript(
            js, lambda r: self._after_typing(first, r)
        )

    def _rerun_lookup(self):
        if self._last_chars:
            self._show_results(self._last_chars)

    def _show_results(self, chars: list[str]):
        self.starter_result.clear()
        MAX_PER_CHAR = 500
        any_results = False
        for ch in chars:
            count = self._render_char(ch, MAX_PER_CHAR, prefix=f"[{ch}] ")
            if count:
                any_results = True
        if not any_results:
            self.starter_result.addItem("결과 없음")

    def _render_char(self, ch: str, max_count: int, prefix: str = "") -> int:
        """한 글자에 대한 결과를 starter_result에 추가하고 표시된 단어 수 반환."""
        long_first = self.long_toggle.isChecked()
        order_tag = "긴단어순" if long_first else "짧은단어순"

        if self.killer_toggle.isChecked():
            killers, rest = wordbank.get_all_words_prioritized(ch)
            if long_first:
                killers = list(reversed(killers))
                rest = list(reversed(rest))
            total = len(killers) + len(rest)
            if total == 0:
                self._add_header(f"━ {prefix}없음 ━")
                return 0
            shown = 0
            if killers:
                self._add_header(f"━ {prefix}한방단어 ({len(killers)}개, {order_tag}) ━")
                for w in killers[:max_count]:
                    self.starter_result.addItem(w)
                    shown += 1
            remaining = max_count - shown
            if rest and remaining > 0:
                self._add_header(f"━ {prefix}일반 ({len(rest)}개, {order_tag}) ━")
                for w in rest[:remaining]:
                    self.starter_result.addItem(w)
                    shown += 1
            if total > max_count:
                self.starter_result.addItem(
                    f"… {prefix}총 {total:,}개 중 상위 {max_count}개만 표시"
                )
            return shown
        else:
            words = wordbank.get_all_words(ch)
            if long_first:
                words = list(reversed(words))
            if not words:
                self._add_header(f"━ {prefix}없음 ━")
                return 0
            self._add_header(f"━ {prefix}전체 ({len(words)}개, {order_tag}) ━")
            for w in words[:max_count]:
                self.starter_result.addItem(w)
            if len(words) > max_count:
                self.starter_result.addItem(
                    f"… {prefix}총 {len(words):,}개 중 상위 {max_count}개만 표시"
                )
            return min(len(words), max_count)

    def _add_header(self, text: str):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(Qt.GlobalColor.darkGray)
        self.starter_result.addItem(item)

    def _on_item_doubleclicked(self, item: QListWidgetItem):
        word = item.text() or ""
        if not word or word.startswith("━") or word.startswith("…") or (word.startswith("[") and word.endswith("] 없음")):
            return
        if not word.strip() or word == "결과 없음":
            return
        js = TYPE_JS % _json.dumps(word, ensure_ascii=False)
        self.web_view.page().runJavaScript(
            js, lambda r: self._after_typing(word, r)
        )

    def _after_typing(self, word: str, type_result):
        # JS 콜백은 JS 실행 완료 후에 호출되므로 바로 클릭
        self._scan_and_click(word)

    def _scan_and_click(self, word: str):
        template = resolve_asset(SEND_BUTTON_IMAGE)
        ok, msg = click_image_on_screen(template, confidence=0.8)
        if ok:
            self.state_label.setText(f"전송 완료: {word} ({msg})")
        else:
            self.state_label.setText(f"이미지 클릭 실패: {msg}")

    def _on_launch_chrome(self):
        debug_profile = ACCOUNT_DIR / "chrome-debug-profile"
        ok, msg = launch_chrome_for_debug(debug_profile)
        if ok:
            self.state_label.setText(
                f"{msg} — 그 Chrome에서 kkutu 로그인 후 [2)] 버튼을 누르세요"
            )
        else:
            self.state_label.setText(f"Chrome 디버그 실행 실패: {msg}")

    def _on_import_chrome(self):
        count, err = import_chrome_cookies(self.profile, "kkutu.co.kr")
        if err:
            self.state_label.setText(f"세션 가져오기 실패: {err}")
            return

        ls_data, ls_err = get_chrome_localstorage("kkutu.co.kr")
        if ls_err:
            self.state_label.setText(
                f"쿠키 {count}개 OK / localStorage 실패: {ls_err}"
            )
        else:
            self._pending_localstorage = ls_data
            self.state_label.setText(
                f"쿠키 {count}개 + localStorage {len(ls_data)}개 — 적용 중"
            )

        self.web_view.setUrl(QUrl(KKUTU_URL))
        self._refresh_login_label()

    def _maybe_apply_localstorage(self, ok: bool):
        if not ok:
            return
        data = self._pending_localstorage
        if not data:
            return
        self._pending_localstorage = None  # 1회만 적용
        lines = [
            f"try{{localStorage.setItem({_json.dumps(k)}, {_json.dumps(str(v))});}}catch(e){{}}"
            for k, v in data.items()
        ]
        if not lines:
            return
        js = "\n".join(lines) + "\nsetTimeout(()=>location.reload(), 200);"
        self.web_view.page().runJavaScript(js)
        self.state_label.setText(f"localStorage {len(data)}개 적용 후 재로드")


def build_profile() -> QWebEngineProfile:
    """영구 프로필 = account/ 폴더. 한 번 로그인 후 다음 실행부터 자동 로그인."""
    storage = ACCOUNT_DIR / "storage"
    cache = ACCOUNT_DIR / "cache"
    storage.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile("auto-kkutu", None)
    profile.setHttpUserAgent(USER_AGENT)
    profile.setPersistentStoragePath(str(storage))
    profile.setCachePath(str(cache))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    s = profile.settings()
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
    return profile


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto Kkutu")
        self.resize(1500, 900)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.profile = build_profile()
        self.web_view = QWebEngineView()
        page = QWebEnginePage(self.profile, self.web_view)
        self.web_view.setPage(page)
        self.web_view.setUrl(QUrl(KKUTU_URL))
        splitter.addWidget(self.web_view)

        self.watcher = KkutuWatcher(self.web_view)
        self.panel = WordbookPanel(self.watcher, self.web_view, self.profile)
        splitter.addWidget(self.panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1050, 450])

        self.setCentralWidget(splitter)
        self._build_toolbar()

    def _build_toolbar(self):
        tb = self.addToolBar("nav")
        tb.setMovable(False)

        back = QAction("◀", self)
        back.triggered.connect(self.web_view.back)
        tb.addAction(back)

        fwd = QAction("▶", self)
        fwd.triggered.connect(self.web_view.forward)
        tb.addAction(fwd)

        reload_act = QAction("새로고침", self)
        reload_act.triggered.connect(self.web_view.reload)
        tb.addAction(reload_act)

        home_act = QAction("홈", self)
        home_act.triggered.connect(lambda: self.web_view.setUrl(QUrl(KKUTU_URL)))
        tb.addAction(home_act)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
