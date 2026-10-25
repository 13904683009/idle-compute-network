# EXECUTION-CHECKPOINT

- 项目：分布式闲置算力网络（Phase 2 搭建任务）
- 检查点时间：2026-10-26
- 里程碑：规划落地 + 阶段 B（Ed25519 节点身份）完成
- 对应提交：`50fb629`（phase2 分支，仅本地，未推送）

---

## 1. 阶段目标及实际完成情况

**目标**：规划仲裁结果落地到任务看板/决策日志，并完成阶段 B（Ed25519 节点身份）的编码、测试、回归验证后提交。

**实际完成**：
- 规划落地：仲裁计划（`logs/idle-compute/phase2-plan-arbiter.json`，18 项最终决策、20 项任务、12 项风险）已完整写入 `docs/phase2/TASK-BOARD.md`（任务行 + 验收标准 + 波次编排）与 `docs/phase2/DECISION-LOG.md`（D-002 修订：单一 libp2p Ed25519 密钥对、node_id=peer id；D-004：18 项决策摘要）。
- B-1 `identity.py`：NodeIdentity + Ed25519Signer（32B 种子文件 O_EXCL 竞态安全、node_id = libp2p peer id、内嵌公钥验签、cryptography 交叉验证）+ 17 测试。
- B-2 `crypto.py` v2 载荷域（node-msg-v2/result-v2/credit-v2）+ `signing.py` 双策略（Ed25519Policy / HmacLegacyPolicy，LocalSigner 标注 LEGACY-ONLY）+ 12 测试。
- B-3 `db.py` schema v2 附加迁移（sig_algo 列默认 hmac-sha256、node_state/claims/client_refs 表、可注入验签策略）；`ledger.py` 逐行按方案验签（hmac 行在 Ed25519 路径下结构化 CONFLICT，不静默通过）、export_since 批量导出、verify_and_append 镜像追加 + 32 测试。
- B-4 `cli.py` identity/peers 命令（仅 --config、只输出 node_id、密钥字节永不打印）+ 6 测试。
- 回归：57 项 Phase 1 既有测试原样通过；总计 **115 项测试全部通过**；compileall 通过；libp2p 导入仅出现在 identity.py（+ 允许的 net_transport.py 占位，尚未创建）；无 asyncio。

## 2. 已修改、新增和删除的文件清单

**修改（11）**：
- `.gitignore`（+`.venv/`）
- `idle_compute/cli.py`、`idle_compute/coordinator.py`、`idle_compute/crypto.py`、`idle_compute/db.py`、`idle_compute/ledger.py`、`idle_compute/worker.py`
- `tests/test_cli.py`、`tests/test_crypto.py`、`tests/test_ledger.py`、`tests/test_store.py`

**新增（9）**：
- `idle_compute/identity.py`、`idle_compute/signing.py`
- `tests/test_identity.py`、`tests/test_signing.py`
- `docs/phase2/TASK-BOARD.md`、`docs/phase2/DECISION-LOG.md`、`docs/phase2/DEPENDENCIES.md`
- `requirements.txt`、`requirements.lock.txt`

**删除**：无。

提交统计：20 files changed, 2025 insertions(+), 41 deletions(-)。

## 3. 测试命令、测试结果及失败项

- 命令：`.venv/Scripts/python -m unittest discover -s tests -v`
- 结果：`Ran 115 tests ... OK`，退出码 0，**无失败项**
- 编译：`.venv/Scripts/python -m compileall -q idle_compute` → OK
- 冒烟：`python -m idle_compute identity --config var/n1/node.json` 两次 → 同一 node_id（种子文件复用），密钥字节不出现在输出
- 证据日志：`C:\Users\linxi\AI-Work\logs\idle-compute\phase2-B-tests.log`（完整 verbose 输出）

## 4. Git 分支、工作区状态、未提交修改及变更摘要

- 分支：`phase2`（由 dev@e3e6d5a 分出）
- HEAD：`50fb629` Phase 2 B（提交作者身份为仓库级占位配置 `Idle Compute Local <idle-compute@localhost>`，与任何 GitHub 账户无关）
- 工作区状态：除本检查点文件 `EXECUTION-CHECKPOINT.md`（新建、未提交）外**干净**，无未提交的代码修改
- **无任何 remote**，从未推送过；本机无 GitHub 凭据（详见第 7 节，切换账户不影响本仓库）
- 变更摘要：阶段 B 全部实现 + 规划文档 + 依赖清单（libp2p==0.8.1、trio==0.30.0、cryptography==50.0.2 钉版）

## 5. 正在执行、尚未开始和被阻塞的任务

- 正在执行：无（已到检查点，暂停领取新任务）
- 已完成：阶段 A（A-1..A-4）、阶段 B（B-1..B-4）、规划落地
- 尚未开始：C-1..C-4（传输层）、D-1..D-3（应用协议）、E-1..E-3（账本同步）、F-1..F-4（安全加固）、G-1..G-2（集成验收）、H（独立审计）、I（最终报告）
- 被阻塞：无

## 6. 下一步任务的准确执行入口、依赖条件和预期结果

- 入口：读取 `docs/phase2/TASK-BOARD.md` 阶段 C 行 + `logs/idle-compute/phase2-plan-arbiter.json` 相关决策（wire 协议 /idle-compute/1.0.0、4 字节 BE 帧、64KiB/32KiB 上限、深度 16、pull 拓扑等）
- 波次（WAVE 0，可并行）：C-1 `net_transport.py`（Libp2pTransport 适配器 + 双真实进程冒烟 + 导入遏制测试）‖ C-2 `protocol.py`（信封/帧编解码/类型注册表/先验签后解析）‖ C-4 `transport_tcp.py`（纯标准库 TCP 回退）
- 随后（WAVE 1）：C-3 `config.py` + `node.py` 运行时，依赖 C-1+C-2+B-1
- 依赖条件：identity.py（B-1）、signing.py/crypto.py（B-2）、db.py（B-3）；venv 已含 libp2p==0.8.1 + trio==0.30.0；libp2p 导入仍须仅限 identity.py + net_transport.py
- 预期结果：C 阶段全部验收标准达成（见 TASK-BOARD C 行），测试全绿后提交并进入 WAVE 2（C-3、B-4 已完成，实际为 C-3 + D 前置）

## 7. 后台进程、子 Agent、异步任务及外部操作的状态

- 后台 Bash：无
- 子 Agent / 工作流：无运行中；规划工作流 wf_e43542ca-921 已完结（输出已落地）
- 外部操作：无对外网络请求、无部署、无外部服务
- GitHub 账户事项（用户指示，非本任务阻塞）：用户有新旧两个 GitHub 账户，后续想将本项目接到**新账户**。本机检查结果：无 remote、无存储的 git/github 凭据（credential.helper=manager 为系统级默认但无任何条目）、无 .git-credentials、无 ~/.ssh、无 Token 环境变量、gh CLI 未安装。切换 = 未来直接建新仓库 + 加 origin + 首次推送时以新账户登录即可；提交作者身份是中性占位，不受影响。**未执行任何账户操作。**

## 8. 数据持久化和恢复注意事项

- 回滚点：`dev@e3e6d5a`；Phase 2 工作在 phase2 分支，回滚 = `git checkout dev`，不丢任何 Phase 1 成果
- 全历史备份 bundle（含双分支）：`C:\Users\linxi\AI-Work\backups\idle-compute-phase2-20261026\idle-compute-dev-e3e6d5a.bundle`（39,948 字节，已再次验证存在）
- `var/`（种子密钥、SQLite DB、rendezvous 文件）、`logs/`、`*.db`、`*.key` 均 gitignored——运行时数据不进版本库，重建安全
- 节点身份种子：`var/<node>/identity.key` 恰 32 字节，删除 = 节点身份永久更换（不可逆，勿随意删）
- 工人侧 DB 为派生数据（worker_tasks/ledger_copy/node_state/meta），可安全删除后由协调者重建（E-2 会验证）
- 恢复时不执行 git reset --hard / clean -fd；当前无未提交改动，无未保存数据
