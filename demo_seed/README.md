# 成竹 Keyless 演示数据

载入：

```bash
python scripts/load_demo.py --force
```

随后启动后端和前端，打开任务 `task_demo_catl_byd_debate`。该任务的当前运行是
`run_demo_catl_byd_h1_2025`，无需配置文本或视觉模型密钥。

该演示使用“宁德时代 vs 比亚迪”的合成夹具展示证据辩论界面。公司名称和证券代码仅用于界面辨识；所有数值均为模拟输入，并非真实公司披露，不可用于现实判断。

演示中的预期裁决：

- 一个 H1 同口径 Claim 通过 Auditor 并被接受；
- 一个 H1 与 Q1 混比 Claim 被确定性拒绝；
- 一个观点在反证后撤回；
- 一个观点维持未决；
- 运行数据库中的真实 LLM 调用数为 0。

重新生成并校验：

```bash
python scripts/build_debate_demo_seed.py
python scripts/build_debate_demo_seed.py --check
```
