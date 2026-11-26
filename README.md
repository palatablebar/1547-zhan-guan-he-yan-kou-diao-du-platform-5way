# 展馆核验口调度台

场馆票务值班长用这套工作台处理展馆入场人数泊松预测与核验增岗。进入工作台可调整本班复核额度，查看每张半小时批次的预测、命中规则和处置，导出单班可复算记录。换场或验电临时关停入口时，每个关停事件生成一张可签收的迁移单，把受影响批次转到未关停入口，并按解除后的余量裁决全量或分批迁回。

## 本地运行

```sh
docker compose up -d --build
```

访问 `http://127.0.0.1:18646`。样例是可复算的模拟班次，涵盖正常历史、较迟回执、最新冲突版、缺测单和临时分区。

```sh
docker compose exec -T app python -m venuegate.entry evaluate --capacity 8 --strategy focus
docker compose exec -T app python -m venuegate.entry evaluate --input sample_data/entry_rotations_sample.json --capacity 9
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

[字段和时间窗](docs/SPEC.md) · [模块与回归表](docs/DEPTH.md) · [HTTP 与自备数据](docs/RUNBOOK.md)
