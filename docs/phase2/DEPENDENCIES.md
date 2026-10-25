# Phase 2 依赖清单

原则：每个新增依赖必须"确有必要的项目依赖"、有维护、可复现（钉版 + 锁定全依赖树）、
来源明确、与 Python 3.12/Windows 兼容性有真实证据。

## 顶层依赖（requirements.txt）

| 包 | 版本 | 用途 | 来源/理由 | 证据 |
| --- | --- | --- | --- | --- |
| libp2p | ==0.8.1 | 节点传输层（TCP/多路复用/协议协商/流） | PyPI；2026-10-22 发布，0.6/0.7/0.8 均在 2026 年内（有维护） | logs/idle-compute/libp2p-eval/EVALUATION.md（双 host 回环收发冒烟通过） |
| trio | ==0.30.0 | libp2p 要求的异步运行时（**必须钉版**：默认解析装 0.34.0 会导致 libp2p 运行时崩溃，因其声明 trio>=0.26.0 无上限） | PyPI | 同上（0.34.0 与 0.30.0 对比实测） |
| cryptography | （锁定版本见 lock 文件；libp2p 传递依赖解析结果） | Ed25519 节点身份签名（如需自有模块）；libp2p 自身亦依赖 | 系统已装 48.0.0；venv 内由 libp2p 解析（50.0.2） | 同上 venv 安装输出 |

## 完整锁定（requirements.lock.txt）

`pip freeze` 全量锁定（约 40+ 传递依赖：grpcio、aioquic、coincurve、blake3、
multiaddr 等）。重新搭建环境：`python -m venv .venv && .venv/Scripts/pip install -r requirements.lock.txt`。

## 环境约定

- `.venv/`：项目虚拟环境（含 libp2p/trio）；**所有测试与节点进程统一用
  `.venv/Scripts/python` 运行**，避免系统 Python 与 venv 版本漂移。
- Phase 1 的 57 项标准库测试在 .venv 下必须继续全过（回归基线）。
- 新增依赖不得改变既有行为；若 .venv 下基线测试失败，视为阻塞，先解决再继续。

## 明确不采用的方案（证据）

- PyPI `py-libp2p`：无匹配发行版（pip 实测 `No matching distribution found`）。
- 标准库 TCP 自研传输：**保留为单模块应急预案**（Libp2pTransport 适配器接口不变时
  可替换），不作为默认——指令要求评估 libp2p，评估已通过（D-003）。
- PyNaCl / 纯 Python ed25519 包：无维护或需额外安装，cryptography 已满足需求。
