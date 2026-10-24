# Triage Labels

技能用五个「规范 triage 角色」说话。本文件把这些角色映射到本仓库 issue tracker 里实际使用的标签字符串。
标签已在本仓库（`Drayee/foodrecommendationplatform`）创建完毕，技能直接使用，**不要重复创建**。

## 五角色（triage）

| 技能里的角色              | 本仓库的标签        | 含义                             |
| ------------------------- | ------------------- | -------------------------------- |
| `needs-triage`            | `needs-triage`      | 维护者需要评估这条议题           |
| `needs-info`              | `needs-info`        | 等待报告者补充信息               |
| `ready-for-agent`         | `ready-for-agent`   | 规格齐全，可交给 AFK agent 执行  |
| `ready-for-human`         | `ready-for-human`   | 需要人类实现                     |
| `wontfix`                 | `wontfix`           | 不会处理（GitHub 默认标签，沿用）|

技能提到某个角色时（例如「打上 AFK-ready 的 triage 标签」），用上表右列对应的字符串。
要换词汇时，只改右列。

## Wayfinder 标签（`/wayfinder` 使用）

| 标签                  | 含义                                       |
| --------------------- | ------------------------------------------ |
| `wayfinder:map`       | 探索地图，一次探索的规范产物（顶层议题）   |
| `wayfinder:research`  | 地图工单：查一手资料                       |
| `wayfinder:prototype` | 地图工单：一次性原型验证                   |
| `wayfinder:grilling`  | 地图工单：拷问式访谈敲定决策               |
| `wayfinder:task`      | 地图工单：确定性执行任务                   |

地图本体与工单都用上表标签；工单类型四选一，不叠加多个 `wayfinder:<type>`。
