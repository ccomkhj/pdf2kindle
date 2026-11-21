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
| 코드 | 글자 위치로 격자를 복원해 들여쓰기와 열 정렬 유지. 화면보다 긴 줄은 이어지는 부분을 들여써서 새 줄과 구분. PDF가 고정폭이라고 표시하지 않은 글꼴(Consolas 등)도 이름과 글자 폭으로 판별 |
| 수식 | 별행 수식(여러 줄 연립식, 행렬, 분수 포함)은 원본 그대로의 이미지로 보존. 본문 속 변수의 아래첨자·위첨자는 글자로 유지 |
| 표 | 격자가 확실할 때만 실제 표로, 아니면 원본 그대로의 이미지로 (잘못된 칸보다 이미지가 낫다는 원칙) |
| 그림 | 벡터·비트맵 그림 영역을 e-ink 해상도에 맞춰 렌더링. "Figure N:" 캡션이 있으면 그 위의 조각들을 한 그림으로 묶음 |
| 여백 주석 | 바깥 여백의 주석을 본문 줄에서 분리해, 옆에 있던 문단 뒤에 작은 글씨로 배치 |
| 한국어 | 공백 글리프가 없는 PDF에서 글자 간격으로 띄어쓰기 복원. 줄 끝에서 끊긴 단어는 책 전체 어휘를 보고 이어 붙임 |
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
  blocks               {'Code': 1064, 'Figure': 19, 'Heading': 467, 'ListItem': 757, 'Paragraph': 5135, 'Table': 15}
  footnotes            122
  footnotes_linked     121
  internal_links       1914
  ! 1 footnote(s) had no matching marker in the text; they are listed unlinked.
```

주요 옵션 (`pdf2kindle convert --help`):

| 옵션 | 뜻 |
| --- | --- |
| `--pages`, `--skip-pages` | 변환할/뺄 쪽. 예: `"1-3,10,200-"` |
| `--keep-index`, `--keep-toc-pages` | 색인, 인쇄용 목차 쪽을 남김 |
| `--tables auto\|html\|image` | 표 처리 방식 |
| `--no-footnotes` | 각주 연결을 끄고 본문 흐름에 둠 |
| `--profile book.toml` | 책별 설정 (`equations = false`로 수식 이미지화를 끌 수도 있음) |
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
    ─ finishing ─▶ 쪽 넘김 병합, 하이픈·각주·링크 해결  ─ epub ─▶ EPUB 3
```

| 모듈 | 역할 |
| --- | --- |
| `pdfdoc.py` | PyMuPDF로 글자 단위 좌표를 읽어 시각적 줄로 묶음 |
| `stats.py` | 본문 글꼴·행간·여백 측정, 머리글·바닥글 감지 |
| `regions.py` | 그림(벡터·비트맵), 표, 박스 영역 |
| `layout.py` | 줄을 제목·문단·코드·목록·각주로 분류 |
| `judge.py` | 줄 하나에 대한 판정(코드인가, 제목 글자 크기인가, 캡션인가)을 한 곳에서 답함 |
| `convert.py` | PDF를 열고 쪽마다 위 단계를 진행 |
| `finishing.py` | 책 단위 후처리: 제목 단계 확정, 색인 제거, 캡션, 쪽 간 병합, 각주, 줄 끝 단어, 내부 링크. PDF 없이 블록만으로 동작 |
| `epub.py`, `kindle.css` | EPUB 작성과 Kindle용 스타일 |
| `debug.py`, `preview.py` | 진단 도구 |

## 검증

- `pytest` — 자체 제작한 샘플 PDF(Chromium 출력, 북마크 유/무)와 단위 테스트로 제목·문단·코드·목록·표·그림·박스,
  고정폭 글꼴 추론, 한국어 띄어쓰기, 표 열 감지 등을 확인합니다.
- 저자·출판사가 직접 무료로 공개한 책 10권으로 "변환 → 결과 평가 → 변환기 개선"을 반복했습니다.
  제작 도구가 서로 달라(Word, LaTeX, pandoc, Typst, LuaTeX) 책마다 다른 문제가 드러났습니다.

| # | 책 | 제작 도구 | 이 책에서 드러나 고친 것 |
| --- | --- | --- | --- |
| 1 | Approaching (Almost) Any Machine Learning Problem | Word → Quartz | 고정폭 표시가 없는 코드 글꼴 판별, 대시 목록, 표지 글자가 1단계 제목이 되던 문제 |
| 2 | Think DSP | LaTeX | 별행 수식 이미지 보존, 아래첨자·위첨자, 영역 병합 버그 |
| 3 | Mathematics for Machine Learning | LaTeX | 여백 주석 분리, 여러 줄 수식·행렬, 번호 붙은 소절 제목 승격 |
| 4 | Natural Language Processing (Eisenstein) | LaTeX | 캡션 기준으로 그림 묶기, 수식 번호로 수식 인식, 책 제목 추정 |
| 5 | Applied Statistics with R | pandoc + XeLaTeX | 위치가 바뀌는 쪽번호, 수학용 유니코드 글자, 글꼴 경계에서 빠진 공백 |
| 6 | Julia Data Science | Typst | 둥근 배경의 코드가 그림으로 바뀌던 문제, 가운데 정렬 표의 칸 배치 |
| 7 | Stan User's Guide | pandoc + LaTeX | 인쇄용 목차 감지, 본문 절이 색인으로 오인돼 삭제되던 문제 |
| 8 | Algorithms (Erickson) | LaTeX | 목록 항목이 다음 문단을 삼키던 문제 |
| 9 | Competitive Programmer's Handbook | LaTeX | 북마크가 제목 앞 쪽을 가리킬 때의 목차 연결 |
| 10 | 씹어먹는 C | LuaTeX | 한국어 띄어쓰기 복원, 줄 끝에서 끊긴 한국어 단어 |

- 위 10권과 초기 샘플 5종 모두 최종 코드로 다시 변환해 EPUBCheck 오류 0건을 확인했습니다.
- **실제 Kindle 기기에서는 아직 확인하지 못했습니다.** CSS는 Kindle이 지원하는 범위로 보수적으로 썼고,
  코드 블록은 CSS가 무시돼도 들여쓰기가 남도록 만들었지만, 기기에서 어색한 점이 보이면
  `code_wrap = "plain"`으로 바꿔 보세요.

## 한계

- **스캔 PDF**: 텍스트 레이어가 없는 쪽은 쪽 이미지로 들어가고 경고가 나옵니다. 먼저
  [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) 같은 도구로 OCR을 거치세요.
- **다단(2단) 조판**: 지원하지 않습니다. 기술서 본문은 대부분 1단이고, 2단이 흔한 색인은 기본으로 제외됩니다.
- **수식**: 별행 수식은 이미지라서 글꼴 크기 설정을 따르지 않고, 다크 모드에서 흰 바탕으로 보입니다.
  본문 속 수식은 글자로 남기므로 분수·근호처럼 복잡한 인라인 수식은 모양이 단순해집니다.
  수식이 매우 많은 책은 파일이 커집니다(수식 1,300개인 책이 약 24MB).
- **한국어 줄 끝 처리**: 줄 끝에서 끊긴 단어를 이을지는 책 안의 다른 용례로 판단하므로, 책에 한 번만 나오는
  단어는 사이에 공백이 남을 수 있습니다.
- **복잡한 표**: 칸이 여러 줄로 접힌 표는 이미지로 들어갑니다 (`--tables html`로 강제 가능).
- **어긋난 북마크**: PDF 북마크가 실제 제목과 여러 쪽 떨어져 있으면 목차 항목이 제목 대신 근처 위치로 연결됩니다.
- **ML 레이아웃 모델 미포함**: 다단·스캔본에는 Docling, Marker 같은 오픈소스 모델이 도움이 되지만,
  현재는 포함돼 있지 않습니다.

## 라이선스 관련

PDF 파싱에 [PyMuPDF](https://pymupdf.readthedocs.io/)(AGPL-3.0)를 사용합니다. 개인적으로 쓰는 데는
문제가 없지만, 이 코드를 배포하거나 서비스로 제공하려면 AGPL 조건을 확인하세요.
