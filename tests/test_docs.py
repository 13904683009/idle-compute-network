"""E-3: documentation honesty gate (grep-level).

The docs are part of the deliverable: an overclaiming doc is a defect, not a
typo. These tests pin the honesty markers that MUST exist and forbid the
phrases that would assert capabilities Phase 2 does not have (consensus, DHT,
mDNS/UPnP, verified multi-device, production readiness).

Scope: every Markdown file under docs/ (including docs/phase2/) plus the root
README. Adding a new doc automatically puts it under the forbidden-phrase gate.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_docs() -> list[Path]:
    paths = sorted(DOCS.rglob("*.md"))
    readme = ROOT / "README.md"
    if readme.exists():
        paths.append(readme)
    return paths


FORBIDDEN = [
    # Positive capability claims for things this phase explicitly does NOT do.
    (re.compile(r"(?<!不)(?<!未)支持\s*(共识|DHT|mDNS|UPnP)"), "claims support for a capability we do not have"),
    (re.compile(r"已(实现|支持)\s*(共识|DHT|mDNS|UPnP|NAT 穿透)"), "claims an unimplemented capability as done"),
    (re.compile(r"共识算法"), "claims a consensus algorithm"),
    (re.compile(r"多设备已验证|跨设备已验证|双设备已验证"), "claims verified multi-device operation"),
    (re.compile(r"生产可用|生产就绪"), "claims production readiness"),
]

# Line-level gate: a line that explicitly negates a capability ("不", "未",
# "无", "禁止", "没有", "而非") is documentation of the limitation, not an
# overclaim. This is a pragmatic grep gate, not a semantic parser -- a line
# that both negates and claims would be skipped, which is why the honesty
# markers are also asserted positively further down.
NEGATION_MARKERS = ("不", "未", "无", "禁止", "没有", "而非")


class DocsHonestyTests(unittest.TestCase):
    def test_no_doc_overclaims_unimplemented_capabilities(self) -> None:
        offenders: list[str] = []
        for path in _all_docs():
            for lineno, line in enumerate(_read(path).splitlines(), 1):
                if any(marker in line for marker in NEGATION_MARKERS):
                    continue
                for pattern, reason in FORBIDDEN:
                    match = pattern.search(line)
                    if match:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{lineno}: {reason}: {match.group(0)!r}"
                        )
        self.assertEqual([], offenders, "documentation overclaims:\n" + "\n".join(offenders))

    def test_coordination_doc_states_the_honest_three_way_split(self) -> None:
        text = _read(DOCS / "COORDINATION.md")
        for marker in (
            "协调者权威",
            "工人可验证",
            "明确非去中心化",
            "不是共识",
            "单点故障",
            "无 DHT",
        ):
            self.assertIn(marker, text, f"COORDINATION.md must state {marker!r}")
        # The chain is tamper-evidence, never consensus.
        self.assertIn("防篡改证据", text)

    def test_protocol_doc_documents_the_v2_wire(self) -> None:
        text = _read(DOCS / "PROTOCOL.md")
        for marker in (
            "idle-compute-node-msg-v2",
            "先验签后解析",
            "64 KiB",
            "32 KiB",
            "/idle-compute/1.0.0",
            "LEDGER_SYNC_REQUEST",
            "SEQ_OUT_OF_WINDOW",
            "24 KiB",
        ):
            self.assertIn(marker, text, f"PROTOCOL.md must document {marker!r}")

    def test_threat_model_covers_phase2_threats(self) -> None:
        text = _read(DOCS / "THREAT_MODEL.md")
        for marker in (
            "Ed25519 伪造身份",
            "信封重放",
            "工人镜像被本地篡改",
            "网络窃听",
            "Windows 密钥 ACL",
            "账本尾部删除",
            "未实现",
        ):
            self.assertIn(marker, text, f"THREAT_MODEL.md must cover {marker!r}")

    def test_architecture_documents_the_node_runtime(self) -> None:
        text = _read(DOCS / "ARCHITECTURE.md")
        for marker in ("第二阶段（Phase 2）", "ledger_copy.db", "不共享 DB 文件", "node.py"):
            self.assertIn(marker, text, f"ARCHITECTURE.md must document {marker!r}")

    def test_two_device_status_stays_unverified(self) -> None:
        text = _read(DOCS / "phase2" / "TWO-DEVICE-TEST.md")
        self.assertTrue(
            "待完成" in text or "未验证" in text,
            "TWO-DEVICE-TEST.md must keep the 'not executed' status until it actually runs",
        )

    def test_rate_limiting_limits_are_documented(self) -> None:
        # F-1 is implemented: the limits must be stated with their numbers, and
        # the docs must stay honest about what is still NOT done (F-2..F-4).
        protocol = _read(DOCS / "PROTOCOL.md")
        for marker in ("令牌桶", "20 msg/s", "突发 40", "30 s 隔离", "ratelimit.py"):
            self.assertIn(marker, protocol, f"PROTOCOL.md must document {marker!r}")
        self.assertIn("日志脱敏", protocol)
        self.assertIn("未实现", protocol)
        threat = _read(DOCS / "THREAT_MODEL.md")
        self.assertNotIn("令牌桶 20 msg/s 未实现", threat, "the threat model must not stale-claim F-1 as missing")
        self.assertIn("test_ratelimit.py", threat)


if __name__ == "__main__":
    unittest.main()
