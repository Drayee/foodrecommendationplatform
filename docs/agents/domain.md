# Domain Docs

**布局结论：single-context（单上下文）。** 本仓库只有一个领域上下文：根目录一份 `GLOSSARY.md`，架构决定放 `docs/adr/`。
不存在 `GLOSSARY-MAP.md`，也没有子项目级的 `GLOSSARY.md`——本仓库不是 monorepo（无 `pnpm-workspace.yaml`、无 `workspaces`、无 `packages/*`）。

> 建词条与写 ADR 都发生在真正需要的时候（由 `/domain-modeling`，经 `/grill-with-docs` 与
> `/improve-codebase-architecture` 触达）。初始化阶段不预建 `GLOSSARY.md`、`GLOSSARY-MAP.md` 或任何子项目 glossary；
> 第一次真正写下词条时再创建 `GLOSSARY.md`。

各工程技能在探索代码库时应如何消费本仓库的领域文档。

## 探索之前，先读这些

- **`GLOSSARY.md`**（仓库根目录），或
- **`GLOSSARY-MAP.md`**（若存在）：它指向每个上下文一份 `GLOSSARY.md`，读与当前主题相关的每一份；
- **`docs/adr/`**：读涉及你即将动工区域的 ADR；multi-context 仓库还要看 `src/<context>/docs/adr/`。

这些文件不存在时**静默跳过**：不要提示缺失，不要主动建议创建。`/domain-modeling` 会在术语或决策真正被敲定时惰性创建。

## 文件结构

单上下文仓库（绝大多数仓库，含本仓库）：

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多上下文仓库（根目录存在 `GLOSSARY-MAP.md` 即为多上下文）：

```
/
├── GLOSSARY-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                  ← 上下文级决策
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## 使用词表里的词汇

输出里出现领域概念时（议题标题、重构提案、假设、测试名），用 `GLOSSARY.md` 定义的术语，不要漂移到词表明确回避的同义词。

需要的概念还不在词表里，是一个信号：要么你在发明项目不用的语言（重新考虑），要么确实有缺口（记下来交给 `/domain-modeling`）。

## 标出 ADR 冲突

输出与已有 ADR 冲突时，明确摆出来，不要静默覆盖：

> _与 ADR-0007（事件溯源订单）冲突，但值得重开，因为……_
