# 运行手册 fieldmap

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18202/api/health
```

宿主端口 `18202`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18202/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。
7. `/recovery` 点「登记恢复点」「冻结最新点」，填表发起回滚，验证未冻结 409、中断后恢复。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `fieldmap-data` 挂着 `/data/fieldmap.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| POST | `/api/recovery/points` | 把批次历史登记为恢复点（幂等） |
| POST | `/api/recovery/points/{point_id}/freeze` | 冻结恢复点，固定结构指纹 |
| GET | `/api/recovery/points` | 恢复点清单与最近冻结序号 |
| GET | `/api/recovery/boundary` | 查某消费方相对故障发布的恢复边界（只读） |
| POST | `/api/recovery/rollbacks` | 发起/重试/恢复回滚，按迁移 ID 幂等 |
| POST | `/api/recovery/rollbacks/{order_id}/interrupt` | 把未完成回滚单标记中断 |
| GET | `/api/recovery/rollbacks` | 回滚单清单 |
| GET | `/api/recovery/rollbacks/{order_id}` | 回滚单详情含事件流 |
| GET | `/api/export/recovery/{order_id}.md` | 恢复单 Markdown |
| GET | `/api/export/recovery/{order_id}/blockers.csv` | 阻断原因 CSV |
| GET | `/api/export/recovery/{order_id}/events.csv` | 回滚事件流 CSV |

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 409 `RECOVERY_BASIS_UNFROZEN` | 恢复点未冻结就请求回滚 | 先调冻结接口，再用同一迁移 ID 重试（沿用原单） |
| 409 `RECOVERY_POINT_NOT_FOUND` | 目标序号没有恢复点 | 先登记恢复点，或改用存在的发布序号 |
| 409 `RECOVERY_NO_COMPLETE_POINT` | 消费方在目标发布前没有完整快照 | 不共用他人回滚点；补齐该消费方或调整目标序号 |
| 409 `RECOVERY_SHARED_MISMATCH` | 共享报表引用跨发布、结构混用 | 把报表纳入同一发布恢复点，先修断链再回滚 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 | 先在 `/batches` 点「跑一次」 |

## 7 回滚处置

1. 切换出问题后，在 `/recovery` 先「登记恢复点」，确认每条发布一个点、序号不重复。
2. 对作为恢复依据的发布点「冻结」；未冻结的点发起回滚只会得到 409，单据状态为 `blocked` 且事件留痕。
3. 发起回滚时带 `migration_id`、目标序号、消费方与共享报表。服务为每个消费方定最后完整版边界，并校验共享报表只引用同次发布。
4. 同一 `migration_id` 的重试与中断恢复是幂等的：同一张回滚单、同一个恢复点，事件流只追加。中断后用相同参数再调一次即从断点续跑，已完成步骤不重放。
5. 页面、恢复单 Markdown、阻断 CSV 读的是同一份单据数据，恢复点与阻断原因必须一致；出现分叉先查库，不要在展示层重算。

## 8 端口与并发

同一批其他项目占用 18200-18209；本服务固定 18202。同时起多个栈时用批目录里的 `scripts/slot.sh` 限流。
