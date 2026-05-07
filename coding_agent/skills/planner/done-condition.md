---
name: done-condition
applies_to: [planner]
summary: planner 는 task 분해 후 DONE_CONDITION.md 를 *반드시* 작성한다 — harness 가 이 파일과 워크스페이스를 기계적으로 대조해 stack misalignment 등을 차단
---

# DONE_CONDITION.md 작성 (필수)

## 왜 필요한가
v21 회귀 — 사용자가 React 를 선택했는데 coder 가 Vue 컴포넌트를 작성하고
"완료" 마킹. orchestrator LLM 은 stack misalignment 를 알아채지 못함.
DONE_CONDITION.md 는 *기계가 읽을 수 있는 합의문* 으로, sufficiency.gate 가
이 파일을 워크스페이스와 grep 비교해 위반을 즉시 LOW 로 분류한다.

이 skill 은 planner 의 *모든* task 분해 호출에 적용된다 (PRD/SPEC 만 쓰고
끝나는 호출은 예외 — 단, 기술 스택이 결정된 시점이라면 작성 권장).

## 작성 위치
워크스페이스 루트 또는 ``docs/`` 아래에 ``DONE_CONDITION.md`` 파일명으로
저장. 다음 후보 경로가 자동 인식된다:

- ``DONE_CONDITION.md``
- ``done_condition.md``
- ``docs/DONE_CONDITION.md``
- ``docs/done_condition.md``

## 필수 섹션

### `## Framework Choice`
사용자가 결정했거나 planner 가 합의한 기술 선택을 *명시*. 다음 형식:

```markdown
## Framework Choice
- Frontend: React 18 (NOT Vue, Angular, Svelte)
- Backend: Node.js 20 + Express (NOT Python, Java, Go)
- Database: PostgreSQL (NOT MySQL, MongoDB, SQLite)
- Test framework: Jest (frontend), Vitest 가능
- Package manager: pnpm
```

### `## Forbidden Patterns`
*가장 중요한 섹션*. 위 framework 선택과 *불일치* 하는 파일/디렉토리 glob
패턴을 bullet 으로 나열. sufficiency.gate 가 이 패턴들을 워크스페이스에서
검색해 *하나라도 매치* 되면 즉시 LOW 분류 → critic → fixer 위임.

```markdown
## Forbidden Patterns
- *.vue (React was chosen)
- *.svelte (React was chosen)
- **/requirements.txt (Node.js was chosen, not Python)
- **/Cargo.toml (Node.js was chosen)
- **/pom.xml (Node.js was chosen)
- **/go.mod (Node.js was chosen)
```

bullet 은 ``- pattern`` 또는 ``* pattern`` 형식, 패턴 뒤 괄호 메모는 자유.

### `## Required Tests`
프로젝트 종류에 따라 *반드시* 통과해야 할 명령어. verifier 가 이 명령들을
실행하고 exit code 0 을 확인.

```markdown
## Required Tests
- `pnpm test` (must pass with exit 0)
- `pnpm lint` (must pass with exit 0)
- `pnpm build` (must pass with exit 0)
```

### `## Per-Task Expected Files` (선택)
task 별로 생성될 파일 경로. coder/verifier 가 task 완료를 주장할 때
이 파일들이 실재하는지 점검 가능.

```markdown
## Per-Task Expected Files
- TASK-1.1.1: src/auth/dto/login-request.dto.ts, tests/auth/login-request.dto.test.ts
- TASK-1.1.2: src/auth/jwt.service.ts, tests/auth/jwt.service.test.ts
```

## 작성 시점
1. PRD 작성 후 + 기술 스택 확정 후 (사용자 ``ask_user_question`` 답변 직후)
2. task 분해 직후 (todo_ledger.md 와 함께)
3. 분해를 다시 하라는 사용자 요청 / critic replan 이 오면 갱신

## task 분해 결과 → write_todos 직접 호출 (v22.2 부터)

분해된 task 목록을 *반드시* ``write_todos`` 도구로 직접 등록한다.
이전에는 별도 ledger SubAgent 가 등록했지만 v22.2 부터 planner 가
직접 호출 — 핸드오프 한 단계 사라짐 + LLM 자유 의지 의존 제거.

```
write_todos(todos=[
    {"id": "TASK-1.1", "content": "사용자 등록 API 구현 (이메일/비밀번호 + bcrypt)", "status": "pending"},
    {"id": "TASK-1.2", "content": "JWT 로그인 API", "status": "pending"},
    ...
])
```

**규칙**:
- task id 는 `TASK-N.M` 또는 `TASK-NN` 형식 (auto-advance 가 description 에서
  이 패턴을 추출)
- content 는 사용자가 워크플로 화면에서 보게 될 텍스트 — 구체적으로
- status 는 모두 `pending` 으로 등록 (orchestrator/coder 가 진행하며 자동 갱신)
- 분해 작업 직후 *같은 planner invocation* 안에서 호출 — 별도 task() 위임 불필요

## 형식 자유도
- 위 4개 섹션 (Framework Choice / Forbidden Patterns / Required Tests /
  Per-Task Expected Files) 의 *순서* 는 자유. 헤더 텍스트는 고정.
- 추가 섹션 (예: `## Notes`, `## Out of Scope`) 자유 허용. harness 는
  Forbidden Patterns 헤더만 식별.
- bullet 패턴은 glob 형식 (``*.ext``, ``**/dir/*``).

## 잘못된 예

### ❌ glob 형식이 아님
```markdown
## Forbidden Patterns
- 사용하지 마세요: Vue   ← glob 형식 아님 — 매치 안 됨
- pure text                ← glob 아님
```

### ❌ 자연어 조건이 섞인 패턴 (v25 회귀의 직접 원인)
```markdown
## Forbidden Patterns
- **/requirements.txt must exist (Python backend)
  ← ``must exist`` 는 *반대 의미* (필수 파일을 forbidden 으로 등록)
- **/package.json (if containing non-frontend dependencies)
  ← ``if`` 조건은 harness 가 평가 못 함
- *.vue should not appear when React was chosen
  ← ``should not`` 자연어 — bullet 자체가 forbidden 의도가 아님
- *.py 가 있어야 함
  ← 반대 의미. 필수 파일을 forbidden 으로 등록하면 안 됨
```

**규칙**: `## Forbidden Patterns` 의 모든 bullet 은 *순수 glob* 이어야
한다. bullet 은 sufficiency.gate 가 *기계적* 으로 워크스페이스에 매치
시도하는 입력 — 자연어 조건이 섞이면 매치 시도가 무의미해지거나
*반대 의미* 가 된다. 같은 줄에 ``must`` / ``should`` / ``if`` / ``when`` /
``unless`` / ``except`` / ``필수`` / ``있어야`` / ``없어야`` 같은 조건어를
넣지 말 것. bullet 끝 괄호 메모는 허용 (`- *.vue (React was chosen)`) —
glob 자체가 forbidden 의도를 표현해야 한다.

### ❌ 헤더 텍스트 다름
```markdown
## Banned Files               ← 헤더 텍스트 다름 — 인식 안 됨
- *.vue
```
