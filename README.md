# Coders Design

Penpot 2.18.5를 Coders Core의 독립 워크스페이스에서 실행하는 통합 시제품입니다.

## 구현

- Coders가 검증한 사용자·리소스·역할 헤더를 사용합니다. 브라우저의 Penpot 쿠키·Bearer 토큰은 신원으로 사용하지 않습니다.
- Penpot 내부 등록·로그인 API로 사용자를 만들고, 세션 쿠키는 서버 메모리에만 보관합니다. 사용자 ID와 워크스페이스 ID를 함께 사용해 계정을 구분합니다.
- 소유자가 최초 공간을 설정합니다. 멤버는 같은 팀의 편집자로 연결하며, Core 소유자는 팀 관리자로 동기화합니다. 멤버의 최초 접속 전에는 소유자 설정이 필요합니다.
- 문서는 PostgreSQL에, 이미지·폰트·임시 내보내기 파일은 Core S3 저장소에 보관합니다. 자산 요청은 Penpot의 권한 검사 뒤 서버 안에서 저장소 응답을 중계합니다.
- exporter의 다운로드 주소는 현재 앱 출처의 상대 경로로 변환합니다. 내부 S3 주소와 사전 서명 URL을 브라우저에 노출하지 않습니다.
- 프론트, JVM 백엔드, Node exporter, 연결 서버를 하나의 HTTP 이미지에 포함합니다. 프로세스 하나가 종료되면 전체 앱을 종료하고 Core가 재시작하도록 합니다.

앱은 Core 뒤에서 실행해야 합니다. 신원 헤더를 임의로 보낼 수 있는 공개 서버에 직접 배포하지 마세요. 공개 가입·비밀번호 변경·별도 초대·팀 삭제는 연결 서버에서 차단하며, 멤버와 접근 권한은 Core에서 관리합니다. 초기 버전의 Core 멤버는 모두 편집자입니다. 앱 이용료는 아직 부과하지 않습니다.

## 필요한 Core 버전

일반 HTTP 앱 규약과 Versity Gateway 객체 저장소를 포함한 Core가 필요합니다. 추가로 `browser.frames: true` 지원이 필요합니다. Penpot의 `/rasterizer.html` 내부 iframe을 위해 CSP `frame-src`와 `frame-ancestors` 양쪽에 `'self'`를 허용하는 변경입니다. Core 커밋 `6ce5a0a` (`codex/same-origin-frames`)에 이 규약과 같은 출처·외부 출처 iframe 격리 브라우저 검사가 반영되었습니다. 공개 개발 가이드에도 옵션이 명시돼 있습니다. 기존 [제안 패치](integration/core-frames.patch)는 이 변경의 기반으로 보관하며, 새 Core에 다시 적용할 필요가 없습니다. 아직 이 옵션이 없는 Core에서는 manifest 검증이 거부됩니다.

## 배포

1. 공개 GitHub 저장소와 공개 GHCR 패키지를 준비합니다. `.github/workflows/coders-build.yml`이 고정 커밋 이미지와 GitHub 빌드 증명을 게시합니다.
2. 워크스페이스 비밀정보 `DESIGN_AUTH_SECRET`에 32자 이상의 충분히 긴 무작위 값을 등록합니다. 이 값은 Penpot의 마스터 키와 내부 계정 비밀번호 파생에 사용하므로 워크스페이스 수명 동안 유지해야 합니다. 값 변경에 대한 계정 마이그레이션은 아직 구현하지 않았습니다.
3. `coders.json`의 자원 요청을 운영 한도와 비교합니다. 현재 2 vCPU, 2 GiB 메모리, 512 MiB 임시 공간, 요청·응답 각각 16 MiB, 이미지 상한 2 GiB입니다. 단일 이미지 기본 동작을 확인한 시작 설정이며, 동시 사용자 부하 검증은 남아 있습니다.
4. 소유자가 최초 접속해 팀을 만듭니다. 이후 Core에서 초대한 멤버가 접속합니다.
5. 검증한 앱 ID에 `design.coders.kr`을 등록합니다.

기본 Docker 빌드:

```bash
docker build -t coders-design:local .
```

업스트림 이미지는 linux/amd64 manifest digest로 고정했습니다. 통합 코드는 업스트림 소스 재빌드 없이 동작하며, MPL 고지와 원본 소스 위치는 `NOTICE` 및 `licenses/`에 있습니다. 현재 외관은 편집기 제목과 공간 이름을 변경한 수준이며 Penpot UI의 추가 브랜드 작업은 남아 있습니다.

## 개발·검증

```bash
uv sync --locked
uv run ruff check design scripts tests
uv run ruff format --check design scripts tests
uv run pytest -q
```

로컬 통합 검증은 실제 Penpot 백엔드·프론트·exporter, PostgreSQL, Redis, Versity Gateway와 연결 서버를 사용합니다. `scripts/image_root.py`는 디스크가 부족한 개발 환경에서 공식 이미지 레이어를 일회용 메모리 파일시스템에 추출하기 위한 보조 도구입니다. Docker의 빌드·배포 도구를 대체하지 않습니다.

로컬 테스트용 연결 서버는 `127.0.0.1:19664`에서 실행합니다. 테스트는 로컬 Core 신원 헤더를 직접 주입합니다. 기본 브라우저 접속은 401을 반환하는 것이 정상입니다.

```bash
uv run python scripts/smoke.py
uv run --with playwright python scripts/browser_smoke.py
uv run python scripts/check_runtime.py
```

`browser_smoke.py`는 두 사용자 창을 열어 소유자가 그린 도형이 멤버 화면에 추가되는지 검사합니다. `DESIGN_TEST_CSP=1`은 Core 정책과 내부 iframe 허용안을 적용한 브라우저 검사입니다. Playwright의 localhost 응답 가로채기에 따른 Local Network Access 오탐은 이 로컬 테스트에서만 해제합니다. `DESIGN_TEST_CSP_FILE=artifacts/core-csp.txt`를 지정하면 Core의 `content_security_policy()`가 생성한 정책 파일을 그대로 적용합니다. `check_runtime.py`는 저장된 리비전, 이미지 원본 왕복, PNG·SVG 내보내기를 검사합니다.

## 현재 검증 범위와 남은 작업

실제 단일 Docker 이미지에서 UID 65534·읽기 전용 루트·2 vCPU·2 GiB 메모리·512 MiB 임시 공간을 적용해 두 사용자 연결·문서 공유·편집 동기화·DB 저장·이미지 저장·PNG/SVG 내보내기를 확인했습니다. 멤버가 오프라인인 동안 추가한 도형도 재연결 후 복구됐습니다. 수정한 Core CSP를 적용한 브라우저 검사에서 페이지 예외나 HTTP 오류는 없었습니다. 새 서버가 기존 문서와 사용자를 복구하는 것도 확인했습니다.

연결 서버 단위 테스트 5개와 Core 호환성 테스트 7개가 통과했습니다. 추가로 [Core 인증·라우팅 통합 검사](integration/core_routing/README.md)에서 실제 Penpot에 대한 소유자·멤버 연결, 신원 헤더 위조 차단, 문서 공유, WebSocket 접속자 알림과 멤버 권한 회수 후 HTTP·새 WebSocket 차단을 확인했습니다. 이 검사는 격리된 Core 원장과 런타임 fixture를 사용합니다. 운영 Core를 거치는 앱 접속·300초 WebSocket 교체·권한 회수·잔액 부족 종료, 폰트 업로드, 백업·복원과 부하 검증은 남아 있습니다. 로컬 테스트 데이터는 별도 DB·파일 저장소 컨테이너의 임시 파일시스템에 있으며 해당 데이터 컨테이너를 중지하면 사라집니다.

공개 [GitHub 저장소](https://github.com/cykim8811/coders-design)와 GHCR 이미지 게시를 완료했습니다. 최초 게시 버전은 `a9d3a56dc1f90a957da9e3095d64c6abe668821b`, 이미지 digest는 `sha256:ba198463e17e5366281b1d7ab525f4600b00989088b84123ff8c2032fd9ea47b`입니다. 익명 이미지 접근과 빌드 출처 증명을 확인했으며 운영 Core의 준비 상태는 `ready`입니다. [Core 접속·워크스페이스 연결](https://core-v2.coders.kr/github/cykim8811/coders-design/a9d3a56dc1f90a957da9e3095d64c6abe668821b) 뒤 `DESIGN_AUTH_SECRET`을 설정해야 합니다. `design.coders.kr` 도메인 등록은 아직 수행하지 않았습니다.

2026-10-29 재검증: Core `6ce5a0a`에서 생성한 CSP를 실제 Penpot 컨테이너에 적용한 두 사용자 브라우저 검사와 PNG/SVG 내보내기를 통과했습니다. 이 검사는 운영 Core의 인증·라우팅 경유 검증과 구분합니다.
