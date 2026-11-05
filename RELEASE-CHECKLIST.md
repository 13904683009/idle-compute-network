# 公开发布检查清单（GitHub）+ 团队分发包

> 本清单是**发布前人工检查**与**用户手动发布步骤**。本仓库未设置 remote、未 push；
> 以下 GitHub 操作由仓库所有者自行完成。

## 1. 已完成的自动检查（本次提交）

| 检查 | 结果 |
| --- | --- |
| HEAD 与工作区 | `9541126` + 本提交；仅 `PROJECT-STATUS-AND-HANDOFF.md` 未跟踪（保留、未改） |
| 完整测试 | 479/479 通过（见本轮最终日志） |
| compileall | `idle_compute tests benchmarks` 通过 |
| 源码安全门禁 | `tests/test_source_safety.py` 未修改，通过 |
| 文档门禁 | `tests/test_docs.py` 未修改，通过 |
| 秘密扫描（tracked 文本） | 未发现 API key/token/私钥/凭据/邮箱；仅测试与文档中的私网/回环地址（非秘密） |
| 个人绝对路径 | 已清理（README 的旧 `C:\Users\...` 已移除）；全仓库 grep 无 `/home/`、`C:\Users\` |
| 数据库/日志/密钥入库 | 无；`.gitignore` 覆盖 `var/`、`*.db`、`*.db-*`、`*.key`、`*.log`、`.venv/` |
| 依赖 | `requirements.txt` 固定 `libp2p==0.8.1`、`trio==0.30.0`、`cryptography==50.0.2`；完整锁定见 `requirements.lock.txt` |
| 文档准确性 | README 已按 D1–D5 现实重写；`docs/phase2/TWO-DEVICE-TEST.md` 标注为过期历史文档 |

## 2. 发布前仍需**人工**完成的决定

1. **许可证**：已添加 **MIT License**（`LICENSE`，Copyright 2026 Idle Compute Network contributors）；
   如团队需要 Apache-2.0，请在发布前与所有者确认后再替换。
2. **仓库名/可见性**：由所有者决定 public/private；团队模拟建议先 private，跑通后再 public。
3. **历史提交信息**：包含阶段报告与内部审计措辞；确认团队可接受后再发布。
4. **分支**：当前分支 `phase2`；GitHub 默认分支由所有者选择（建议发布分支另建，如 `release/team-sim`）。
5. **敏感内容复核**：发布前再跑一次：
   ```bash
   git grep -n -E 'sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY'
   git grep -n -E 'C:\\Users\\|/home/|/Users/'
   ```
   若有命中，停止发布并人工确认（不要在本清单输出具体值）。

## 3. 用户手动 GitHub 步骤（本代理不执行）

```bash
# 1) 在 GitHub 创建空仓库（不要初始化 README）
# 2) 在本地仓库添加 remote（由所有者执行）
git remote add origin https://github.com/<owner>/<repo>.git
# 3) 确认工作区与测试后推送（由所有者执行）
git status
git push -u origin phase2        # 或发布分支
```

> 本代理**不会**创建仓库、设置 remote 或 push；上述命令仅供所有者参考。

## 4. 团队成员获取与自检

```bash
git clone https://github.com/<owner>/<repo>.git idle-compute-network
cd idle-compute-network
git rev-parse HEAD
python -m venv .venv && .venv/Scripts/activate   # Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -q          # 期望 OK（479）
```

然后按 **`TEAM-SIMULATION-GUIDE.md`** 执行两台设备模拟，并填写第 7 节结果表。

## 5. 发布时**不得**声称的内容

- 两台真实设备已验证（除非团队确实执行并提交证据）。
- 链上注册表、共识、代币、真实支付（均未实现）。
- DHT / Gossip / Relay / mDNS / NAT 穿透（均未实现）。
- 生产可用或已完成安全审计。
- D5 `LocalRegistryAdapter` 已参与调度（**尚未接入**节点/匹配器）。

## 6. 已知风险（发布说明中应保留）

- 单 coordinator 权威，无去中心化调度；coordinator 是单点。
- D4 验收矩阵仍有 PARTIAL/NOT TESTED（租约竞态、节点级错投/广播、worker 重启/断线恢复）。
- 任务 payload 明文落库/上线；链路加密仅覆盖 libp2p 传输（本指南用 TCP，链路不加密）。
- 仅白名单 `numeric_batch_v1`；不是通用计算平台。
- Windows 下 `0600` 权限无法强制，密钥机密性依赖目录 ACL。
