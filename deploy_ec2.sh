#!/usr/bin/env bash
# ============================================================
# EC2 배포 스크립트
# - 사전 빌드된 이미지(chokyeongjin/recommendation-campaign-system:latest)로 기동
# - metadata DDL 적용 + RAG 적재까지 한 번에 수행
#
# 사용법:
#   ./deploy_ec2.sh              # 전체 배포 (DDL 적용 포함)
#   ./deploy_ec2.sh --skip-ddl   # DDL 재적용 없이 기동/적재만
#
# 사전 조건:
#   - 같은 폴더에 docker-compose.ec2.yml, .env 가 있어야 한다
#   - 이미지가 로컬에 pull/build 되어 있어야 한다
# ============================================================
set -euo pipefail

COMPOSE_FILE="docker-compose.ec2.yml"
DC="docker compose -f ${COMPOSE_FILE}"
DDL_PATH_IN_IMAGE="docs/data/metadata_ddl.sql"
PG_USER="postgres"
PG_DB="campaign_db"

SKIP_DDL=0
for arg in "$@"; do
  case "$arg" in
    --skip-ddl) SKIP_DDL=1 ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
  esac
done

# 0) 사전 점검 -------------------------------------------------
if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "[ERROR] ${COMPOSE_FILE} 을 찾을 수 없습니다. 프로젝트 루트에서 실행하세요." >&2
  exit 1
fi
if [[ ! -f ".env" ]]; then
  echo "[ERROR] .env 파일이 없습니다. (env_file 로 참조됨)" >&2
  exit 1
fi

echo "==> [1/6] 인프라 + python 컨테이너 기동 (qdrant, postgres, python)"
${DC} up -d qdrant postgres python

echo "==> [2/6] postgres 헬스 대기"
until ${DC} exec -T postgres pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; do
  echo "    ...postgres 준비 대기중"
  sleep 2
done
echo "    postgres 준비 완료"

# 3) metadata DDL 적용 ---------------------------------------
if [[ "${SKIP_DDL}" -eq 0 ]]; then
  echo "==> [3/6] metadata DDL 적용 (${DDL_PATH_IN_IMAGE} -> ${PG_DB})"
  echo "    주의: DROP TABLE ... CASCADE 로 기존 메타 테이블이 재생성됩니다."
  ${DC} exec -T python cat "${DDL_PATH_IN_IMAGE}" \
    | ${DC} exec -T postgres psql -v ON_ERROR_STOP=1 -U "${PG_USER}" -d "${PG_DB}"
  echo "    DDL 적용 완료"
else
  echo "==> [3/6] DDL 적용 건너뜀 (--skip-ddl)"
fi

echo "==> [4/6] RAG 적재 (init_rag_collections.py --recreate)"
${DC} run --rm python python init_rag_collections.py --recreate

echo "==> [5/6] RAG 적재 상태 점검 (check_rag_collections.py --strict)"
${DC} run --rm python python check_rag_collections.py --strict

echo "==> [6/6] API + 프론트엔드 기동"
${DC} up -d api frontend

echo "==> API 헬스 체크 대기"
for i in $(seq 1 20); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    echo "    API 정상 (http://localhost:8000/health)"
    break
  fi
  echo "    ...API 기동 대기중 (${i}/20)"
  sleep 3
  if [[ "${i}" -eq 20 ]]; then
    echo "[WARN] API 헬스 체크가 시간 내에 통과하지 못했습니다. 로그 확인:" >&2
    echo "       ${DC} logs --tail=50 api" >&2
  fi
done

echo "==> 프론트엔드 헬스 체크 대기"
for i in $(seq 1 20); do
  if curl -sf http://localhost:80 >/dev/null 2>&1; then
    echo "    프론트엔드 정상 (http://localhost:80)"
    break
  fi
  echo "    ...프론트엔드 기동 대기중 (${i}/20)"
  sleep 3
  if [[ "${i}" -eq 20 ]]; then
    echo "[WARN] 프론트엔드 헬스 체크가 시간 내에 통과하지 못했습니다. 로그 확인:" >&2
    echo "       ${DC} logs --tail=50 frontend" >&2
  fi
done

echo ""
echo "배포 완료."
echo "  - 화면:   http://<EC2-PUBLIC-IP>/        (보안그룹 80 인바운드 필요)"
echo "  - API:    http://<EC2-PUBLIC-IP>:8000/health"
echo "  - 메타 테이블 확인:"
echo "      ${DC} exec -T postgres psql -U ${PG_USER} -d ${PG_DB} -c '\\dt'"
