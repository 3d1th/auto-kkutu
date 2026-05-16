import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

WORD_DIR = Path(__file__).with_name("word")

ROCKET_FILE = WORD_DIR / "rocket.json"
WORD_FILE = WORD_DIR / "word.json"
WORDS_FILE = WORD_DIR / "words.json"
WORDS_TXT = WORD_DIR / "words.txt"
USER_FILE = WORD_DIR / "user.txt"


def _load_json(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_rocket() -> dict[str, list[str]]:
    return _load_json(ROCKET_FILE)


@lru_cache(maxsize=1)
def load_word() -> dict[str, list[str]]:
    return _load_json(WORD_FILE)


@lru_cache(maxsize=1)
def load_words() -> dict[str, list[str]]:
    return _load_json(WORDS_FILE)


@lru_cache(maxsize=1)
def load_words_txt() -> list[str]:
    if not WORDS_TXT.exists():
        return []
    return [w.strip() for w in WORDS_TXT.read_text(encoding="utf-8").splitlines() if w.strip()]


@lru_cache(maxsize=1)
def load_user_words() -> list[str]:
    """사용자가 GUI에서 추가한 단어들 (word/user.txt)."""
    if not USER_FILE.exists():
        return []
    return [w.strip() for w in USER_FILE.read_text(encoding="utf-8").splitlines() if w.strip()]


def _invalidate():
    load_user_words.cache_clear()
    load_all.cache_clear()


def add_user_word(word: str) -> bool:
    """word/user.txt에 단어 추가. 이미 통합 인덱스에 있으면 False."""
    word = word.strip()
    if not word:
        return False
    existing = load_all().get(word[:1], [])
    if word in existing:
        return False
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USER_FILE.open("a", encoding="utf-8") as f:
        f.write(word + "\n")
    _invalidate()
    return True


def remove_user_word(word: str) -> bool:
    """word/user.txt에서 단어 삭제. (원본 JSON/words.txt는 건드리지 않음)."""
    word = word.strip()
    if not word or not USER_FILE.exists():
        return False
    current = load_user_words()
    if word not in current:
        return False
    new = [w for w in current if w != word]
    USER_FILE.write_text("\n".join(new) + ("\n" if new else ""), encoding="utf-8")
    _invalidate()
    return True


@lru_cache(maxsize=1)
def load_all() -> dict[str, list[str]]:
    """word/ 폴더의 4개 파일을 모두 합쳐 {시작글자: 정렬된 중복제거 단어 리스트}로 반환."""
    bucket: dict[str, set[str]] = defaultdict(set)

    for src in (load_rocket(), load_word(), load_words()):
        for key, words in src.items():
            if not key:
                continue
            first = key[:1]
            for w in words:
                ww = w.strip() if isinstance(w, str) else ""
                if ww:
                    bucket[first].add(ww)

    for w in load_words_txt():
        bucket[w[:1]].add(w)

    for w in load_user_words():
        bucket[w[:1]].add(w)

    return {k: sorted(v, key=lambda s: (len(s), s)) for k, v in bucket.items()}


def _first_char(s: str) -> str:
    return s.strip()[:1]


def get_all_words(start: str) -> list[str]:
    """모든 단어 소스(rocket/word/words json + words.txt)에서 중복 제거 후 반환."""
    key = _first_char(start)
    if not key:
        return []
    return list(load_all().get(key, []))


def is_killer_word(word: str) -> bool:
    """한방단어 = 끝 글자로 시작하는 단어가 단어장에 하나도 없음."""
    if not word:
        return False
    return not load_all().get(word[-1])


def get_all_words_prioritized(start: str) -> tuple[list[str], list[str]]:
    """(한방단어 목록, 일반단어 목록)으로 분리해서 반환. 각각 길이순 정렬됨."""
    words = get_all_words(start)
    if not words:
        return [], []
    index = load_all()
    killers: list[str] = []
    rest: list[str] = []
    for w in words:
        if not w:
            continue
        if not index.get(w[-1]):
            killers.append(w)
        else:
            rest.append(w)
    return killers, rest


def get_starter_word(start: str, *, prefer: str = "shortest") -> str | None:
    candidates = get_all_words(start)
    if not candidates:
        return None
    if prefer == "shortest":
        return min(candidates, key=len)
    if prefer == "longest":
        return max(candidates, key=len)
    return candidates[0]


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    idx = load_all()
    print(f"전체 키 수: {len(idx)}")
    total = sum(len(v) for v in idx.values())
    print(f"총 단어 (중복 제거): {total:,}")
    for ch in ["이", "로", "고", "꽃", "ㄱ"]:
        killers, rest = get_all_words_prioritized(ch)
        print(f"\n'{ch}': 한방 {len(killers)}개 / 일반 {len(rest)}개")
        print(f"  한방 앞 5개: {killers[:5]}")
        print(f"  일반 앞 5개: {rest[:5]}")
