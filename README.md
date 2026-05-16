# Auto Kkutu

[kkutu.co.kr](https://kkutu.co.kr/) 끝말잇기 자동 검색 보조 도구. PyQt6로 임베드된 게임 화면을 감시하고, 제시어가 뜨면 통합 단어장(약 627,000개)에서 후보를 자동 조회·표시한다. 단어를 더블클릭하면 게임 입력창에 채워넣고 화면의 "전송" 버튼을 이미지 매칭으로 찾아 클릭.

## 주요 기능

- **임베드 웹뷰** — kkutu.co.kr을 한 GUI 안에 띄움 (`QWebEngineView`)
- **자동 제시어 감지** — `.jjo-display.ellipse`를 500ms 간격으로 폴링, 변할 때마다 자동 검색
- **두음법칙 처리** — `이(리)` 같은 표기 파싱해서 양쪽 글자 모두 검색
- **한방단어 우선 / 긴단어 우선** — 정렬 모드 토글
- **사용자 단어장** — `word/user.txt`에 추가/삭제 (통합 인덱스 자동 무효화)
- **Chrome 세션 임포트** — 구글 로그인 차단 우회용. 디버그 모드 Chrome → CDP로 쿠키+localStorage 추출 → 임베드 브라우저 주입
- **자동 전송** — 단어 더블클릭 → JS로 입력창 채우기 → 화면에서 "전송" 버튼 이미지 매칭 → 클릭

## 요구 사항

- Windows 10/11
- Python 3.11+
- Chrome (세션 임포트용, 선택)

## 설치

```powershell
pip install -r requirements.txt
```

의존성:
- `PyQt6`, `PyQt6-WebEngine` — GUI + 임베드 브라우저
- `pycryptodome`, `pywin32` — Chrome 쿠키 복호화
- `websocket-client` — Chrome DevTools Protocol
- `pyautogui`, `opencv-python`, `pillow` — 화면 이미지 매칭

## 실행

```powershell
python main.py
```

## 폴더 구조

```
auto-kkutu/
├── main.py              # GUI + 감시 + 결과 표시
├── wordbank.py          # 단어장 통합 인덱스 (4개 소스 머지)
├── chrome_import.py     # Chrome 쿠키/localStorage 임포트 (디스크 + CDP)
├── image_click.py       # 화면 이미지 매칭 + 클릭
├── send_button.png      # "전송" 버튼 템플릿 (직접 캡쳐해서 저장)
├── word/                # 단어 데이터
│   ├── rocket.json      #   로켓 단어 모음
│   ├── word.json        #   기본 단어장
│   ├── words.json       #   확장 단어장
│   ├── words.txt        #   평면 단어 리스트
│   └── user.txt         #   사용자 추가 단어 (자동 생성)
├── account/             # 로그인 세션 (자동 생성, 영구 저장)
└── build/               # Nuitka 빌드 산출물
```

## 첫 사용 (로그인)

kkutu가 구글 로그인을 요구하는데, QtWebEngine은 "지원되지 않는 브라우저"로 차단됩니다. 우회 방법:

1. `main.py` 실행
2. 패널의 **"1) Chrome 디버그 모드 실행"** 클릭
   - 앱 전용 user-data-dir(`account/chrome-debug-profile/`)로 Chrome이 새 창에 뜸
   - `--remote-debugging-port=9222 --remote-allow-origins=*` 자동 적용
3. 새 Chrome 창에서 kkutu.co.kr 접속 → 구글 로그인 (Chrome이라 통과)
4. 패널의 **"2) kkutu 세션 가져오기"** 클릭
   - CDP로 쿠키 + localStorage 추출 → 임베드 브라우저에 주입 → 자동 새로고침
5. `account/storage/`에 영구 저장 — 다음 실행부터 자동 로그인

## "전송" 버튼 이미지 준비

자동 전송 기능을 쓰려면 `send_button.png`가 필요합니다:

1. 게임 화면에서 **Win + Shift + S** → 영역 캡쳐
2. "전송" 버튼만 정확히 크롭 (테두리 포함)
3. `send_button.png` 이름으로 프로젝트 루트(또는 exe 옆)에 저장

매칭은 OpenCV grayscale 템플릿 매칭, 기본 신뢰도 0.8.

## 빌드 (Nuitka)

### 폴더형 (standalone, ~477 MB)

```powershell
chcp 65001
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONLEGACYWINDOWSFSENCODING = "0"

python -m nuitka `
    --standalone `
    --enable-plugin=pyqt6 `
    --include-data-dir=word=word `
    --windows-console-mode=disable `
    --output-dir=build `
    --assume-yes-for-downloads `
    --disable-ccache `
    --disable-cache=all `
    --remove-output `
    main.py
```

→ `build/main.dist/` 폴더 전체를 배포

### 단일 exe (onefile, ~124 MB)

위 명령에서 `--standalone` → `--onefile`로 교체. 실행 시 temp에 압축 해제됨.

### `send_button.png` 번들

빌드 시 같이 포함하려면 `--include-data-files=send_button.png=send_button.png` 추가. 또는 빌드 후 exe 옆에 별도로 두면 됨.

## 단축 검색 흐름

```
게임에서 제시어 표시
  ↓ (500ms 폴링)
.jjo-display.ellipse 읽음
  ↓
형식 검증 ('이' or '이(리)')
  ↓ (유효하면)
parse_start_chars → ['이', '리']
  ↓
각 글자별 wordbank.get_all_words_prioritized
  ↓
[한방단어] + [일반] 섹션으로 표시
  ↓ (단어 더블클릭)
TYPE_JS: input.value 채우기 + input/keyup 이벤트
  ↓
image_click: 화면에서 send_button.png 찾기 → 클릭
```

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| Chrome 쿠키 읽기 실패 (err=32) | Chrome 실행 중 잠금 → CDP 방식 사용 ("1) 디버그 모드 실행") |
| Chrome 쿠키 읽기 실패 (ABE) | Chrome 127+ App-Bound 암호화 → CDP 사용해야 함 |
| CDP 403 Forbidden | 디버그 Chrome에 `--remote-allow-origins=*` 없음 → 닫고 "1)" 다시 누르기 |
| 쿠키 임포트 후 로그인 안 됨 | localStorage 임포트 누락 가능 → 디버그 Chrome 탭에서 kkutu.co.kr 열어둔 채로 "2)" |
| 이미지 클릭 실패 | `send_button.png`가 없거나, 화면 DPI 스케일이 캡쳐 시와 다름 |
| Nuitka 빌드 mbcs 에러 | 환경변수 `PYTHONUTF8=1` + 캐시 비활성화 |

## 주의 사항

- 자동 매크로는 kkutu.co.kr 약관 위반 가능성이 있음. 본인 책임 하에 사용.
- `account/` 폴더에는 로그인 쿠키가 평문으로 저장됨 → `.gitignore`로 제외됨
- 이미지 클릭은 OS 좌표 기반이라 윈도우가 화면에 보여야 동작 (가려지면 다른 위치 클릭)
