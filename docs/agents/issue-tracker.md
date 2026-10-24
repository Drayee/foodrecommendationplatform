# Issue tracker: GitHub

本仓库的议题与规格都存在 GitHub Issues 里：**`Drayee/foodrecommendationplatform`**。
所有操作走 `gh` CLI（在仓库目录内执行时会自动推断远端仓库，无需手写 `-R`）。

## 约定

- **建票**：`gh issue create --title "..." --body "..."`；多行正文用 heredoc（bash）或 `--body-file`。
- **读票**：`gh issue view <number> --comments`，需要标签时一并取。
- **列票**：
  ```bash
  gh issue list --state open \
    --json number,title,body,labels,comments \
    --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'
  ```
  配合 `--label` / `--state` 过滤。
- **评论**：`gh issue comment <number> --body "..."`
- **打 / 摘标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

## PR 作为请求入口

**PRs as a request surface: no.**（若本仓库把外部 PR 当作功能请求，改成 `yes`；`/triage` 会读这一行。）

置为 `yes` 后，PR 与议题走同一套标签与状态，用 `gh pr` 对应命令：

- 读 PR：`gh pr view <number> --comments`、`gh pr diff <number>`
- 列出待 triage 的外部 PR：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只保留 `authorAssociation` 为 `CONTRIBUTOR` / `FIRST_TIME_CONTRIBUTOR` / `NONE` 的（丢掉 `OWNER` / `MEMBER` / `COLLABORATOR`）
- 评论 / 打标签 / 关闭：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`

GitHub 的 issue 与 PR 共用一个编号空间，裸写的 `#42` 可能是两者之一：先 `gh pr view 42`，失败再 `gh issue view 42`。

## 技能说「发布到议题跟踪器」时

建一个 GitHub issue。

## 技能说「取回对应票据」时

执行 `gh issue view <number> --comments`。

## Wayfinding 操作（`/wayfinder` 使用）

**地图（map）** 是一个打了 `wayfinder:map` 标签的议题，**工单**是它的子议题。本仓库所用标签见 `triage-labels.md`。

- **建地图**：`gh issue create --label wayfinder:map`，正文含 Destination / Notes / Decisions so far / Not yet specified。
- **建子票**：用 GitHub sub-issues 关联到地图（`gh api` 调 sub-issues 端点）；未启用时，把子票加进地图正文的任务列表，并在子票正文首行写 `Part of #<map>`。标签用 `wayfinder:<type>`：`research` / `prototype` / `grilling` / `task`。
- **阻塞边**：优先用 GitHub 原生依赖
  ```bash
  gh api --method POST repos/Drayee/foodrecommendationplatform/issues/<child>/dependencies/blocked_by \
    -F issue_id=<blocker-db-id>
  ```
  `<blocker-db-id>` 是阻塞者的**数据库 id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`），不是 `#number`。
  不可用时退化为子票正文首行 `Blocked by: #<n>, #<n>`。
- **前沿查询**：列出地图的未关闭子票，剔除有未关闭阻塞者或有 assignee 的，按地图顺序取第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`（本轮第一个写操作）。
- **结案**：`gh issue comment <n> --body "<答案>"` → `gh issue close <n>` → 把结论指针追加到地图的 Decisions so far。
