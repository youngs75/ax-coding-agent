#!/bin/bash
# 제출용 ZIP 생성 스크립트
# 사용법: ./make-submission.sh
# 결과: submission-ax-coding-agent.zip

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ZIP_NAME="submission-ax-coding-agent.zip"

echo "프로젝트: $PROJECT_DIR"
echo "출력 파일: $PROJECT_DIR/$ZIP_NAME"

# 기존 zip 제거
rm -f "$PROJECT_DIR/$ZIP_NAME"

# Python zipfile 모듈로 압축 (zip 명령 미설치 환경 대응)
cd "$PROJECT_DIR"
python3 - <<'PYEOF'
import zipfile
import os
import sys

PROJECT_DIR = os.path.abspath(".")
PROJECT_NAME = os.path.basename(PROJECT_DIR)
ZIP_PATH = os.path.join(PROJECT_DIR, "submission-ax-coding-agent.zip")

# 제외할 디렉토리/파일 패턴
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ax-agent",
    ".claude",
    "node_modules",
    "dist",
    "build",
}
EXCLUDE_FILES_EXACT = {
    ".env",
    "submission-ax-coding-agent.zip",
    "e2e_langfuse_traces.md",
    "e2e_output.txt",
}
EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".egg-info",
}

def should_skip(path: str) -> bool:
    parts = path.split(os.sep)
    for p in parts:
        if p in EXCLUDE_DIRS:
            return True
        if p in EXCLUDE_FILES_EXACT:
            return True
        for ext in EXCLUDE_EXTENSIONS:
            if p.endswith(ext):
                return True
    # memory_store의 DB 파일 제외
    if "memory_store" in parts and (path.endswith(".db") or ".db-" in path):
        return True
    return False

added = 0
with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for root, dirs, files in os.walk(PROJECT_DIR):
        # 디렉토리 필터링 (in-place 수정으로 os.walk 조기 종료)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, PROJECT_DIR)
            if should_skip(rel_path):
                continue
            # 아카이브 내부 경로는 PROJECT_NAME/... 형태로
            arcname = os.path.join(PROJECT_NAME, rel_path)
            zf.write(full_path, arcname)
            added += 1

size = os.path.getsize(ZIP_PATH)
print(f"  포함 파일 수: {added}")
print(f"  크기: {size / 1024 / 1024:.2f} MB")
PYEOF

echo ""
# 검증
python3 - <<'PYEOF'
import zipfile, sys
zip_path = "submission-ax-coding-agent.zip"
with zipfile.ZipFile(zip_path, "r") as zf:
    names = zf.namelist()

# .env 포함 여부 검증
env_files = [n for n in names if n.endswith("/.env") or n.split("/")[-1] == ".env"]
if env_files:
    print(f"⚠ 경고: .env 파일 포함됨: {env_files}")
    sys.exit(1)

# README.md 포함 여부
readme = [n for n in names if n.endswith("/README.md") and n.count("/") == 1]
if not readme:
    print("⚠ 경고: 루트 README.md 없음")
    sys.exit(1)

# Dockerfile 포함 여부
dockerfile = [n for n in names if n.endswith("/Dockerfile") and n.count("/") == 1]
if not dockerfile:
    print("⚠ 경고: Dockerfile 없음")
    sys.exit(1)

# .env.example 포함 여부
env_example = [n for n in names if n.endswith("/.env.example")]
if not env_example:
    print("⚠ 경고: .env.example 없음 (사용자가 .env 만들 수 없음)")
    sys.exit(1)

print("✓ 안전 검증 통과")
print("  - .env 제외됨")
print("  - README.md 포함됨")
print("  - Dockerfile 포함됨")
print("  - .env.example 포함됨")
PYEOF

echo ""
echo "✓ 제출 준비 완료: submission-ax-coding-agent.zip"
