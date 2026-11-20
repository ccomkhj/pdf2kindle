# pdf2kindle

기술서 PDF를 Kindle에서 읽기 좋은 **리플로우 EPUB**으로 바꿔 주는 변환기입니다.
PDF를 그대로 Kindle에 넣으면 글자가 작고 확대·이동을 반복해야 합니다. 이 도구는 PDF에서
본문·제목·코드·표·그림·각주를 구조로 복원해, 글꼴 크기를 마음대로 바꿔 읽을 수 있는 전자책으로
만듭니다.

- 대상: 텍스트 레이어가 있는(스캔본이 아닌) 기술서·에세이 PDF
- 출력: EPUB 3 (Send to Kindle로 그대로 전송 가능)
- 유료 API 없음. 변환은 전부 로컬에서 실행됩니다.

## 무엇을 해 주나

| 항목 | 처리 |
| --- | --- |
| 본문 | 줄바꿈을 문단으로 다시 합치고, 줄 끝 하이픈은 책 전체 어휘를 보고 판단 (`exam-ple` → `example`, `command-line`은 유지). 쪽을 넘어가는 문단도 이어 붙임 |
| 머리글·바닥글·쪽번호 | 여러 쪽에 반복되는 위치·문구를 찾아 제거 (각주와 본문 첫 줄은 보존) |
| 제목·목차 | PDF 북마크가 있으면 그대로, 없으면 글자 크기로 추정. Kindle "이동" 메뉴용 목차 생성. 인쇄용 목차 쪽과 색인은 제외 |
| 코드 | 글자 위치로 격자를 복원해 들여쓰기와 열 정렬 유지. 화면보다 긴 줄은 이어지는 부분을 들여써서 새 줄과 구분 |
| 표 | 격자가 확실할 때만 실제 표로, 아니면 원본 그대로의 이미지로 (잘못된 칸보다 이미지가 낫다는 원칙) |
| 그림 | 벡터·비트맵 그림 영역을 e-ink 해상도에 맞춰 렌더링, 캡션 연결 |
| 각주 | 쪽 하단 각주를 본문 표식과 연결해 Kindle 팝업 각주로 |
| 링크 | 외부 URL과 문서 내부 상호참조("3.2절 참고")를 모두 유지 |
| 목록·정의 목록·인용·박스 | 구조 그대로 유지 |

글꼴·크기·정렬·색은 CSS에서 강제하지 않습니다. Kindle의 글꼴 설정과 다크 모드가 그대로 동작합니다.

## 설치

```bash
git clone https://github.com/ccomkhj/pdf2kindle && cd pdf2kindle
python -m venv .venv && . .venv/bin/activate
pip install -e .                 # 변환만
pip install -e ".[dev,preview]"  # 테스트와 미리보기까지
playwright install chromium      # 미리보기를 쓸 때만
```

Python 3.10 이상이 필요합니다.

## 사용법

```bash
pdf2kindle book.pdf                       # book.epub 생성
pdf2kindle book.pdf -o out/book.epub --title "책 제목" --author "저자"
pdf2kindle book.pdf --pages 12-340 --profile book.toml
```

만들어진 `.epub`을 [Send to Kindle](https://www.amazon.com/sendtokindle)(웹·앱·이메일)로 보내면 됩니다.

변환이 끝나면 요약이 출력됩니다. 경고(`!`)가 있으면 읽어 보세요.

```
'GAWK: Effective AWK Programming' -> gawk.epub
  pages                599
  chapters             27
  blocks               {'Code': 1061, 'Figure': 26, 'Heading': 467, 'ListItem': 755, 'Paragraph': 5186, 'Table': 6}
  footnotes            122
  footnotes_linked     121
  internal_links       1920
  ! 1 footnote(s) had no matching marker in the text; they are listed unlinked.
```

주요 옵션 (`pdf2kindle convert --help`):

| 옵션 | 뜻 |
| --- | --- |
| `--pages`, `--skip-pages` | 변환할/뺄 쪽. 예: `"1-3,10,200-"` |
| `--keep-index`, `--keep-toc-pages` | 색인, 인쇄용 목차 쪽을 남김 |
| `--tables auto\|html\|image` | 표 처리 방식 |
| `--no-footnotes` | 각주 연결을 끄고 본문 흐름에 둠 |
| `--cover auto\|none\|파일` | 표지 (기본: 첫 쪽) |
| `--report r.json` | 변환 요약을 JSON으로 저장 |
| `--debug-pdf d.pdf` | 줄마다 무엇으로 판정했는지 그려 넣은 PDF 저장 |

## 책마다 다듬기

PDF는 만든 도구마다 생김새가 달라서, 결과가 어색하면 아래 순서로 원인을 찾고 프로필로 고칩니다.

1. **살펴보기** — `pdf2kindle inspect book.pdf`
   본문 글꼴, 고정폭으로 인식된 글꼴, 북마크, 머리글 감지 결과를 보여 줍니다.
2. **판정 확인** — `pdf2kindle book.pdf --debug-pdf debug.pdf`
   원본 위에 줄마다 판정(본문·제목·코드·각주·머리글·그림…)을 색으로 표시합니다.
   코드가 본문으로 잡혔는지, 머리글이 남았는지 한눈에 보입니다.
3. **미리보기** — `pdf2kindle preview book.epub --device paperwhite`
   Kindle 화면 크기와 쪽 넘김으로 렌더링한 PNG를 만듭니다 (`paperwhite`, `kindle`, `scribe`).
   Chromium 기반이라 실제 기기와 똑같지는 않지만 코드 줄바꿈, 그림 크기, 제목 위계를 판단하기엔 충분합니다.
4. **프로필 작성** — [`examples/book.toml`](examples/book.toml)을 복사해 고칩니다.

```toml
mono_fonts = ["LetterGothic"]        # 고정폭 표시가 없는 코드 글꼴
drop_lines = ["Licensed to .*"]      # 쪽마다 찍힌 문구 제거
crop = [0.06, 0.0, 0.05, 0.0]        # 감지되지 않은 머리글·바닥글 영역 무시
skip_pages = "3-6"
tables = "image"                     # 이 책의 표는 전부 이미지로
```

## 동작 원리

OCR이나 레이아웃 모델로 글자를 "다시 읽지" 않습니다. 텍스트 레이어가 있는 PDF에는 정확한 글자와
글꼴·좌표가 이미 들어 있으므로, 그것을 그대로 쓰고 구조만 추론합니다. 그래서 오탈자가 생기지 않습니다.

```
PDF ─ pdfdoc ─▶ 줄(글자별 좌표·글꼴) ─ stats ─▶ 본문 크기·행간·여백·머리글
    ─ regions ─▶ 그림·표·박스 영역
    ─ layout ─▶ 제목 / 문단 / 코드 / 목록 / 각주 블록
    ─ convert ─▶ 쪽 넘김 병합, 하이픈·각주·링크 해결  ─ epub ─▶ EPUB 3
```

| 모듈 | 역할 |
| --- | --- |
| `pdfdoc.py` | PyMuPDF로 글자 단위 좌표를 읽어 시각적 줄로 묶음 |
| `stats.py` | 본문 글꼴·행간·여백 측정, 머리글·바닥글 감지 |
| `regions.py` | 그림(벡터·비트맵), 표, 박스 영역 |
| `layout.py` | 줄을 제목·문단·코드·목록·각주로 분류 |
| `convert.py` | 전체 파이프라인과 쪽 간 병합, 하이픈·각주·링크 처리 |
| `epub.py`, `kindle.css` | EPUB 작성과 Kindle용 스타일 |
| `debug.py`, `preview.py` | 진단 도구 |

## 검증

- `pytest` — 자체 제작한 샘플 PDF(Chromium 출력, 북마크 유/무)로 제목·문단·코드·목록·표·그림·박스를 확인합니다.
- 개발 중 TeX·Sphinx·Chromium으로 만든 35~599쪽 문서 5종을 변환해 모두 EPUBCheck 오류 0건을 확인했습니다.
- **실제 Kindle 기기에서는 아직 확인하지 못했습니다.** CSS는 Kindle이 지원하는 범위로 보수적으로 썼고,
  코드 블록은 CSS가 무시돼도 들여쓰기가 남도록 만들었지만, 기기에서 어색한 점이 보이면
  `code_wrap = "plain"`으로 바꿔 보세요.

## 한계

- **스캔 PDF**: 텍스트 레이어가 없는 쪽은 쪽 이미지로 들어가고 경고가 나옵니다. 먼저
  [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) 같은 도구로 OCR을 거치세요.
- **다단(2단) 조판**: 지원하지 않습니다. 기술서 본문은 대부분 1단이고, 2단이 흔한 색인은 기본으로 제외됩니다.
- **수식**: 별도 처리가 없어 수식이 많은 책은 수식이 글자 나열로 깨집니다.
- **한국어 PDF**: 줄 끝에서 단어 중간이 끊긴 경우 띄어쓰기를 복원하지 못해 불필요한 공백이 생길 수 있습니다.
- **복잡한 표**: 칸이 여러 줄로 접힌 표는 이미지로 들어갑니다 (`--tables html`로 강제 가능).
- **ML 레이아웃 모델 미포함**: 다단·수식·스캔본에는 Docling, Marker 같은 오픈소스 모델이 도움이 되지만,
  현재는 포함돼 있지 않습니다.

## 라이선스 관련

PDF 파싱에 [PyMuPDF](https://pymupdf.readthedocs.io/)(AGPL-3.0)를 사용합니다. 개인적으로 쓰는 데는
문제가 없지만, 이 코드를 배포하거나 서비스로 제공하려면 AGPL 조건을 확인하세요.
