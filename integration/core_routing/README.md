# Core 인증·라우팅 통합 검사

Core `6ce5a0a`의 테스트 DB와 세션·브라우저 라우터·Gateway를 거쳐 실제 Penpot
컨테이너에 요청합니다. 사용자·리소스·역할 헤더를 테스트에서 직접 주입하는 기존
smoke 검사와 달리, Core가 검증한 사용자 정보를 연결 서버에 전달하는 경계를 검사합니다.

검사 범위는 비로그인·신원 위조 차단, 소유자/멤버 구분과 문서 공유, 쿼리 전달,
renderer CSP, Origin 검사, WebSocket 접속자 알림, 멤버 권한 회수입니다.

## 실행 조건

- 일치하는 Core 체크아웃과 해당 개발 의존성, Docker가 필요합니다.
- `DESIGN_CORE_TEST_URL`은 **새 테스트 DB에 연결한 별도 로컬 Penpot 컨테이너**의
  주소여야 합니다. 기존 개발 문서나 운영 워크스페이스를 연결하지 마세요.
- Core 원장을 외부 DB로 지정하지 않습니다. Core fixture가 임시 PostgreSQL을
  만들고 삭제합니다.
- 각 실행은 새로운 Core 리소스를 만듭니다. Penpot DB도 매번 새로 준비해야 합니다.

Core 체크아웃을 작업 디렉토리로 지정해 실행합니다.

```bash
PYTHONPATH="$PWD" DESIGN_CORE_TEST_URL=http://127.0.0.1:19666 \
  uv run pytest --import-mode=importlib -q -s \
  /absolute/path/to/coders-design/integration/core_routing
```

보고서는 Design 저장소의 `artifacts/core-routing.json`에 기록됩니다.
일반 `pytest`에는 포함하지 않는 별도 통합 검사입니다.

## 검증의 한계

컨테이너 생성·자원 계측은 Core fixture를 사용하고, 실제 HTTP/WebSocket 전송만
실행 중인 Penpot에 연결합니다. DB·Redis·객체 저장소·비밀정보는 로컬 컨테이너에
미리 공급하므로 Core fixture manifest에서는 `services`와 `secrets`를 제외합니다.
운영 Knative의 서비스 준비·비밀정보 주입·과금과 실제 OIDC 로그인은 이 검사의
범위에 포함하지 않습니다. 운영 런타임과 같이 WebSocket 연결별 handshake 헤더는
다음 연결로 복사하지 않습니다.
