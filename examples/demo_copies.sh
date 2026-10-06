#!/usr/bin/env bash
# 实体放映拷贝登记与场次分配调用示例。启动服务后执行：bash examples/demo_copies.sh
#
# 演示主线：
#   登记影片拷贝配置（片长 120 分钟 + 跨场馆转运 45 分钟）与两份拷贝
#   → 配置/编号冲突与"已有核准流水后不可再登记"门槛
#   → 核准三场并分配拷贝：同场馆重叠 409（附冲突场次）、跨场馆转运边界恰好可用
#   → 查询拷贝占用与冲突预检 → 同拷贝重试返回现状
#   → 改签到冲突时段被拒 COPY_CONFLICT（排片/分配/额度不变）→ 改签到可用时段
#   → 换拷贝原子替换 → 已配置拷贝的影片未分配时领开场码被拒 → 分配后领取成功
#   → 取消场次释放分配
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-copy-demo"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 登记拷贝：首份同时固定影片级配置（片长120分钟、跨场馆转运45分钟） =="
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 120, "transfer_minutes": 45, "copy_code": "DEMO-COPY-A"
}' | pretty
echo "-- 第二份拷贝必须重复相同配置 --"
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 120, "transfer_minutes": 45, "copy_code": "DEMO-COPY-B"
}' | pretty

echo
echo "== 2. 登记校验：配置不一致 409 COPY_PROFILE_MISMATCH；编号冲突 409 COPY_CODE_CONFLICT =="
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 90, "transfer_minutes": 45, "copy_code": "DEMO-COPY-C"
}' | pretty
curl -s -X POST "$BASE/films/other-film/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 120, "transfer_minutes": 45, "copy_code": "DEMO-COPY-A"
}' | pretty
echo "-- 同编号同参数重登：返回既有记录 replay=true --"
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 120, "transfer_minutes": 45, "copy_code": "DEMO-COPY-A"
}' | pretty

echo
echo "== 3. 录入授权（两个场馆）并核准三个场次 =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 10, \"note\": \"拷贝演示-场馆1\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-2\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 10, \"note\": \"拷贝演示-场馆2\"
}" | pretty
for kv in "copy-demo-1 hall-1 2030-06-01T10:00:00Z" \
          "copy-demo-2 hall-1 2030-06-01T11:30:00Z" \
          "copy-demo-3 hall-2 2030-06-01T12:45:00Z"; do
  set -- $kv
  curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
    \"film_id\": \"$FILM\", \"venue_id\": \"$2\",
    \"start_time\": \"$3\", \"idempotency_key\": \"$1\"
  }" | pretty
done

echo
echo "== 4. 影片已有核准流水后再登记拷贝：422 FILM_HAS_APPROVED_VERIFICATIONS =="
curl -s -X POST "$BASE/films/$FILM/copies" -H 'Content-Type: application/json' -d '{
  "duration_minutes": 120, "transfer_minutes": 45, "copy_code": "DEMO-COPY-D"
}' | pretty

echo
echo "== 5. 分配：copy-demo-1 -> DEMO-COPY-A（hall-1 占用 [10:00, 12:00)） =="
curl -s -X POST "$BASE/verifications/copy-demo-1/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"DEMO-COPY-A"}' | pretty

echo
echo "== 6. 同场馆重叠：copy-demo-2（11:30 与 [10:00,12:00) 重叠）-> 409 COPY_CONFLICT，不落账 =="
curl -s -X POST "$BASE/verifications/copy-demo-2/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"DEMO-COPY-A"}' | pretty
echo "-- copy-demo-2 仍未分配（assigned=false） --"
curl -s "$BASE/verifications/copy-demo-2/copy-assignment" | pretty

echo
echo "== 7. 跨场馆转运边界：copy-demo-3 在 hall-2 12:45，距 12:00 恰好 45 分钟 -> 可用 =="
curl -s -X POST "$BASE/verifications/copy-demo-3/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"DEMO-COPY-A"}' | pretty

echo
echo "== 8. 查询拷贝占用（按当前排片派生 [开场, 开场+片长)）与冲突预检 =="
curl -s "$BASE/copies/DEMO-COPY-A" | pretty
echo "-- 预检：DEMO-COPY-A 在 hall-1 11:00 候选 -> 冲突列表含 copy-demo-1 --"
curl -s "$BASE/copies/DEMO-COPY-A/conflicts?venue_id=hall-1&start_time=2030-06-01T11:00:00Z" | pretty

echo
echo "== 9. 同场次同拷贝重试返回现状（replay=true），不重复落账 =="
curl -s -X POST "$BASE/verifications/copy-demo-1/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"DEMO-COPY-A"}' | pretty

echo
echo "== 10. 改签检验拷贝：copy-demo-3 改到 11:00（与 DEMO-COPY-A 上 copy-demo-1 冲突） =="
echo "    -> decision=rejected, reject_reason=COPY_CONFLICT，排片/分配/额度不变"
curl -s -X POST "$BASE/verifications/copy-demo-3/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2030-06-01T11:00:00Z","operation_key":"copy-demo-3-move-1",
       "reason":"尝试提前"}' | pretty
echo "-- 改签到 15:00（与 10:00 场间隔充足）-> 成功，占用自动跟随新时段 --"
curl -s -X POST "$BASE/verifications/copy-demo-3/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2030-06-01T15:00:00Z","operation_key":"copy-demo-3-move-2",
       "reason":"改到下午场"}' | pretty
curl -s "$BASE/copies/DEMO-COPY-A" | pretty

echo
echo "== 11. 换拷贝原子替换：copy-demo-1 从 DEMO-COPY-A 换到 DEMO-COPY-B =="
curl -s -X POST "$BASE/verifications/copy-demo-1/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"DEMO-COPY-B"}' | pretty

echo
echo "== 12. 已配置拷贝的影片：未分配时领开场码 422 COPY_ASSIGNMENT_REQUIRED =="
curl -s -X POST "$BASE/verifications/copy-demo-2/opening-code" | pretty
echo "-- copy-demo-2 分配 DEMO-COPY-A（11:30-13:30 与 15:00 场跨馆间隔充足）后领取成功 --"
curl -s -X POST "$BASE/verifications/copy-demo-2/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"DEMO-COPY-A"}' | pretty
curl -s -X POST "$BASE/verifications/copy-demo-2/opening-code" | pretty

echo
echo "== 13. 取消 copy-demo-1：分配随取消释放，DEMO-COPY-B 占用清空 =="
curl -s -X POST "$BASE/verifications/copy-demo-1/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"拷贝调度调整"}' | pretty
curl -s "$BASE/copies/DEMO-COPY-B" | pretty

echo
echo "演示完成。"
