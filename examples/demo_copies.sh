#!/usr/bin/env bash
# 实体放映拷贝（physical copies）登记 / 分配 / 占用冲突完整调用示例。
# 启动服务后执行：bash examples/demo_copies.sh  （BASE_URL 环境变量可改地址）
#
# 背景：同片实体放映拷贝数量有限，即便授权额度足够，同一份拷贝也无法同时
# 出现在两个场馆。每份拷贝按当前排片占用 [开场, 开场+片长)：同场馆时段不
# 得重叠（左闭右开，端点相接可用）；跨场馆两场之间须留足"片长 + 转运"的
# 间隔（双向检查，恰好满足边界可用）。
#
# 演示主线（片长 118 分钟、跨场馆转运 45 分钟）：
#   1. 影片尚无核准流水时登记两份拷贝；元数据不一致 / 编号重复被 422 拒绝
#   2. 核准 8 个 2030 年未来场次（授权口径与以前完全一样，不感知拷贝）
#   3. 分配 print-1：同场同码重试返现状（replay=true）
#   4. 跨场馆恰好满足边界可用（21:43）；提前一分钟 COPY_CONFLICT
#   5. 另一日期的同场馆：端点相接可用（20:58），提前一分钟重叠 422
#   6. 只读冲突探针 /copies/conflicts（可排除本场自身模拟改签）
#   7. 原子换拷贝：print-2 -> print-1 成功（replaced=true）
#   8. 占用查询：GET /copies/{code} 与 /occupancies（end_time/ready_time）
#   9. 已配置拷贝的影片领开场码前须有有效分配（COPY_NOT_ASSIGNED）
#  10. 取消场次释放拷贝（响应带 copy_code），释放后槽位可被另一场使用
#  11. 改签在同一事务重检拷贝：冲突时 decision=rejected/COPY_CONFLICT，
#      排片、分配、额度都不变；冲突时间不可领码/不受授权影响
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-copy-demo"
HA="hall-cop-a"
HB="hall-cop-b"

echo "== 0. 健康检查 + 两个场馆的授权（授权/池额度口径与拷贝无关，各自独立） =="
curl -s "$BASE/health" | pretty
for V in "$HA" "$HB"; do
  curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
    \"film_id\": \"$FILM\", \"venue_id\": \"$V\",
    \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2040-01-01T00:00:00Z\",
    \"max_sessions\": 20, \"note\": \"拷贝演示-$V\"
  }" | pretty
done

echo
echo "== 1. 登记两份同片拷贝：片长 118 分钟、跨场馆转运 45 分钟，编号全库唯一 =="
curl -s -X POST "$BASE/copies" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"copy_code\": \"print-1\",
  \"duration_minutes\": 118, \"transport_minutes\": 45,
  \"note\": \"主拷贝\"
}" | pretty
curl -s -X POST "$BASE/copies" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"copy_code\": \"print-2\",
  \"duration_minutes\": 118, \"transport_minutes\": 45,
  \"note\": \"备份拷贝\"
}" | pretty
echo "-- 片长不一致：422 COPY_METADATA_MISMATCH --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/copies" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"copy_code\": \"print-3\",
  \"duration_minutes\": 100, \"transport_minutes\": 45
}"
echo "-- 拷贝编号重复：422 COPY_CODE_TAKEN（别的影片也不能用同一编号） --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/copies" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"other-film\", \"copy_code\": \"print-1\",
  \"duration_minutes\": 90, \"transport_minutes\": 10
}"
echo "-- 拷贝列表（可按 film_id 过滤） --"
curl -s "$BASE/copies?film_id=$FILM" | pretty

echo
echo "== 2. 核准 8 个 2030 年未来场次（单场核验接口/响应完全不变，不感知拷贝） =="
# 06-01：cd-1 A 馆 19:00（占 [19:00,20:58)，运抵 B 馆 21:43）
#         cd-2 B 馆 21:43（跨场馆恰好边界）/ cd-3 B 馆 21:42（差 1 分钟）
# 06-02：cd-8 A 馆 19:00；cd-4 A 馆 20:58（端点相接）/ cd-5 A 馆 20:57（重叠）
# 06-03：cd-6 A 馆 19:00（换拷贝演示）；06-04：cd-7 B 馆 21:43（取消后复用）
verify() {  # $1=key $2=venue $3=start
  curl -s -o /dev/null -X POST "$BASE/verifications" \
    -H 'Content-Type: application/json' \
    -d "{\"film_id\":\"$FILM\",\"venue_id\":\"$2\",\"start_time\":\"$3\",\"idempotency_key\":\"$1\"}"
}
verify "cd-1" "$HA" "2030-06-01T19:00:00Z"
verify "cd-2" "$HB" "2030-06-01T21:43:00Z"
verify "cd-3" "$HB" "2030-06-01T21:42:00Z"
verify "cd-8" "$HA" "2030-06-02T19:00:00Z"
verify "cd-4" "$HA" "2030-06-02T20:58:00Z"
verify "cd-5" "$HA" "2030-06-02T20:57:00Z"
verify "cd-6" "$HA" "2030-06-03T19:00:00Z"
verify "cd-7" "$HB" "2030-06-04T21:43:00Z"
echo "8 场全部核准（授权上限 20）；拷贝分配是独立的下一步。"

echo
echo "== 3. 为 cd-1 分配 print-1；同场同码重试返回现状（replay=true，不重复落账） =="
curl -s -X POST "$BASE/verifications/cd-1/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty
curl -s -X POST "$BASE/verifications/cd-1/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty

echo
echo "== 4. 跨场馆转运：cd-2 在 21:43 恰好是 20:58 + 45min 边界 -> 可用 =="
curl -s -X POST "$BASE/verifications/cd-2/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty
echo "-- cd-3 提前到 21:42：422 COPY_CONFLICT (cross_venue_transport)，附冲突场次，冲突不落账 --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/cd-3/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}'

echo
echo "== 5. 同场馆半开区间（次日，先给 cd-8 分配 print-1）：cd-4 20:58 与 [19:00,20:58) 端点相接可用 =="
curl -s -o /dev/null -X POST "$BASE/verifications/cd-8/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}'
curl -s -X POST "$BASE/verifications/cd-4/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty
echo "-- cd-5 在 20:57 重叠 1 分钟：422 COPY_CONFLICT (same_venue_overlap) --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/cd-5/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}'

echo
echo "== 6. 只读冲突探针 /copies/conflicts（不写任何分配；可排除本场自身模拟改签） =="
echo "-- cd-5 的时段打 print-1：与 cd-8 同场馆重叠 --"
curl -s -X POST "$BASE/copies/conflicts" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"copy_code\": \"print-1\",
  \"venue_id\": \"$HA\", \"start_time\": \"2030-06-02T20:57:00Z\"
}" | pretty
echo "-- 模拟 cd-1 改签时自身先释放：exclude 后其 19:00 时段不再与自己冲突（与 cd-2 恰好边界） --"
curl -s -X POST "$BASE/copies/conflicts" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"copy_code\": \"print-1\",
  \"venue_id\": \"$HA\", \"start_time\": \"2030-06-01T19:00:00Z\",
  \"exclude_idempotency_key\": \"cd-1\"
}" | pretty

echo
echo "== 7. 原子换拷贝：cd-6 先占空闲的 print-2，再换成 print-1（另一日期无冲突） =="
curl -s -X POST "$BASE/verifications/cd-6/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-2"}' | pretty
echo "-- 换到 print-1：replaced=true，原 print-2 立即释放，全程同一事务 --"
curl -s -X POST "$BASE/verifications/cd-6/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty
echo "-- 再请求 print-1（同场同码重试）：replay=true，不产生新的替换 --"
curl -s -X POST "$BASE/verifications/cd-6/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty
echo "-- 若新拷贝有冲突（试换给 06-02 20:57 的 cd-5）：422 且 print-1 原占用不变 --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications/cd-5/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-2"}'
echo "-- cd-5 从未分配成功，状态为 assigned=false --"
curl -s "$BASE/verifications/cd-5/copy-assignment" | pretty

echo
echo "== 8. 占用查询：每份拷贝的占用按当前排片计算，给出 end_time/ready_time =="
curl -s "$BASE/copies/print-1" | pretty
echo "-- print-2 已在换拷贝时释放，占用为空 --"
curl -s "$BASE/copies/print-2/occupancies" | pretty
echo "-- GET /verifications/{key} 的当前排片视图也带 copy_code / copy_released --"
curl -s "$BASE/verifications/cd-1" | pretty

echo
echo "== 9. 已配置拷贝的影片：领开场码前必须有有效分配，否则 422 COPY_NOT_ASSIGNED =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/cd-3/opening-code"
echo "-- cd-1 已分配 print-1，可正常领码（每场仅一个码） --"
curl -s -X POST "$BASE/verifications/cd-1/opening-code" | pretty

echo
echo "== 10. 取消 cd-2：响应带 copy_code=print-1，拷贝占用立即释放（分配行保留留痕） =="
curl -s -X POST "$BASE/verifications/cd-2/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"片方调整，该场取消"}' | pretty
echo "-- print-1 used_sessions 少 1；06-04 B 馆 21:43 的 cd-7 现在可拿 print-1（无其他占用） --"
curl -s -X POST "$BASE/verifications/cd-7/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-1"}' | pretty
echo "-- 已取消场次的分配状态：released=true（行保留），但不计入占用，且不可再分配 --"
curl -s "$BASE/verifications/cd-2/copy-assignment" | pretty
curl -s -o /dev/null -w '再分配已取消场次 -> HTTP %{http_code} (SESSION_CANCELLED)\n' \
  -X POST "$BASE/verifications/cd-2/copy-assignment" \
  -H 'Content-Type: application/json' -d '{"copy_code":"print-2"}'

echo
echo "== 11. 改签在同一事务内重检拷贝（cd-7 已分配 print-1、未领码） =="
echo "-- cd-7 当前 B 馆 06-04 21:43；改到 06-02 B 馆 20:00：与 print-1 上 cd-4(A馆 20:58
   结束 22:56, 运抵 B 馆 23:41) 转运不达标 -> decision=rejected, reject_reason=
   COPY_CONFLICT，排片/分配/授权与池额度全部不变 --"
curl -s -X POST "$BASE/verifications/cd-7/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2030-06-02T20:00:00Z","operation_key":"cd-7-move-bad"}' | pretty
echo "-- 同操作键重放：replay=true，冲突快照一致，不重复判定 --"
curl -s -X POST "$BASE/verifications/cd-7/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2030-06-02T20:00:00Z","operation_key":"cd-7-move-bad"}' | pretty
echo "-- 当前排片仍是 06-04 21:43、仍持有 print-1 --"
curl -s "$BASE/verifications/cd-7" | pretty
echo "-- 改到无冲突的 2030-09-01 21:43：approved，分配 print-1 自动跟随新时刻 --"
curl -s -X POST "$BASE/verifications/cd-7/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2030-09-01T21:43:00Z","operation_key":"cd-7-move-ok"}' | pretty
curl -s "$BASE/verifications/cd-7" | pretty
curl -s "$BASE/copies/print-1/occupancies" | pretty

echo
echo "演示完成。未配置拷贝的影片所有旧行为保持不变（无需任何拷贝分配即可领码/核销）。"
