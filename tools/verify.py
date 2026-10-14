#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LotusWallet/assets —— 产物结构体检。

在 sync_chainlist.py / build_market.py 之后跑一次，作为最后一道闸
（本机 `repos/sync_assets.py` 就是这么串起来用的）：
产物必须是**能被子客户端直接解析的**，而不是看起来像 JSON 就行。

检查项
------
chains/chains.json
  - 能解析；version 存在；chains 非空
  - id 全局唯一且符合命名规范（小写字母 / 数字 / 连字符）
  - family 必须是客户端已实现的家族之一，且家族必填字段齐全
  - 每条链必须有 rpcUrls，且全部是 https（chia 例外：公网节点没有稳定入口）
  - evm 链必须有 chainId，且 chainId 唯一
  - ⚠️ evm 链的 coinType 必须为 60（派生路径一致性的前提）
  - 非 evm 链必须带 coinType
  - 与 chainlist 原始快照交叉核对：链的 symbol / decimals 没被脚本改错

market/dapps.json
  - 能解析；categories / dapps 非空
  - dapp id 唯一；url 是 https；category 必须在 categories 里

用法
----
    python tools/verify.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHAINS = REPO / "chains" / "chains.json"
CHAINLIST_RAW = REPO / "chainlist" / "rpcs.json"
MARKET = REPO / "market" / "dapps.json"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# 客户端 chain/alt 模块已实现的链家族。新增家族时必须同步这里，
# 否则「客户端解析不了的清单」会被放行。
KNOWN_FAMILIES = {"evm", "utxo", "tron", "ed25519", "filecoin", "chia"}

# 允许 rpcUrls 为空的家族：这些链的公共节点没有公认的稳定入口，
# 客户端对它们也只做账户管理（地址派生/展示），不依赖节点可达性。
# 其余家族必须有节点，否则「链加进来了却查不了余额」是一个隐形故障。
ALLOW_EMPTY_RPC = {"chia"}

# 每个家族必须出现的字段（缺失即客户端解析后行为不确定）。
REQUIRED_FIELDS = {
    "evm": ("chainId",),
    "utxo": ("addressType", "p2pkhVersion", "p2shVersion"),
    "tron": (),
    "ed25519": ("variant", "derivation", "path"),
    "filecoin": ("path", "network"),
    "chia": ("path", "hrp"),
}


def load(path: Path, problems: list[str]):
    if not path.exists():
        problems.append(f"缺少 {path.relative_to(REPO)}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f"{path.relative_to(REPO)} 不是合法 JSON：{exc}")
        return None


def check_chains(problems: list[str]) -> None:
    doc = load(CHAINS, problems)
    if doc is None:
        return

    chains = doc.get("chains") or []
    if not chains:
        problems.append("chains/chains.json 的 chains 为空")
        return

    seen_ids: set[str] = set()
    seen_chain_ids: set[int] = set()

    for c in chains:
        cid = c.get("id") or "<无 id>"
        if not ID_RE.match(cid or ""):
            problems.append(f"链 id 不合规范：{cid!r}（只允许小写字母/数字/连字符）")
        if cid in seen_ids:
            problems.append(f"链 id 重复：{cid}")
        seen_ids.add(cid)

        family = c.get("family")
        if family not in KNOWN_FAMILIES:
            problems.append(f"{cid}: 未知 family {family!r}")
        else:
            for field in REQUIRED_FIELDS[family]:
                if c.get(field) in (None, ""):
                    problems.append(f"{cid}: 家族 {family} 缺少必填字段 {field}")

        if "coinType" not in c:
            problems.append(f"{cid}: 缺少 coinType")

        rpcs = c.get("rpcUrls") or []
        if not rpcs and family not in ALLOW_EMPTY_RPC:
            problems.append(f"{cid}: 没有 rpcUrls")
        for url in rpcs:
            if not str(url).startswith("https://"):
                problems.append(f"{cid}: RPC 不是 https：{url}")

        if family == "evm":
            if "coinType" in c and c["coinType"] != 60:
                problems.append(f"{cid}: EVM 链的 coinType 必须是 60，实际 {c['coinType']}")
            chain_id = c.get("chainId")
            if not isinstance(chain_id, int):
                problems.append(f"{cid}: 缺少合法的 chainId")
            elif chain_id in seen_chain_ids:
                problems.append(f"chainId 重复：{chain_id}")
            else:
                seen_chain_ids.add(chain_id)

    # 与 chainlist 原始快照交叉核对：生成逻辑有没有把 symbol / decimals 弄错
    raw = load(CHAINLIST_RAW, problems)
    if isinstance(raw, list):
        by_id = {e.get("chainId"): e for e in raw if isinstance(e.get("chainId"), int)}
        for c in chains:
            if c.get("family") != "evm":
                continue
            entry = by_id.get(c.get("chainId"))
            if entry is None:
                problems.append(f"{c['id']}: chainlist 原始快照里找不到 chainId={c.get('chainId')}")
                continue
            currency = entry.get("nativeCurrency") or {}
            if currency.get("symbol") and c.get("symbol") != currency["symbol"]:
                # BNB 这类会有历史遗留写法差异，只提示不判错
                print(f"  ~ {c['id']}: symbol {c.get('symbol')} != chainlist {currency['symbol']}",
                      file=sys.stderr)
            expected = currency.get("decimals", 18)
            if c.get("decimals") != expected:
                problems.append(f"{c['id']}: decimals {c.get('decimals')} != chainlist {expected}")


def check_market(problems: list[str]) -> None:
    doc = load(MARKET, problems)
    if doc is None:
        return

    categories = doc.get("categories") or []
    valid = {c.get("id") for c in categories}
    if not valid:
        problems.append("market/dapps.json 没有 categories")

    seen: set[str] = set()
    for d in doc.get("dapps") or []:
        did = d.get("id") or "<无 id>"
        if did in seen:
            problems.append(f"DApp id 重复：{did}")
        seen.add(did)
        if not d.get("name"):
            problems.append(f"{did}: 缺少 name")
        if not str(d.get("url") or "").startswith("https://"):
            problems.append(f"{did}: url 不是 https")
        if d.get("category") not in valid:
            problems.append(f"{did}: category {d.get('category')!r} 不在 categories 里")

    if not seen:
        problems.append("market/dapps.json 的 dapps 为空")


def main() -> int:
    problems: list[str] = []
    check_chains(problems)
    check_market(problems)

    if problems:
        print(f"体检未通过，共 {len(problems)} 个问题：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    chains = json.loads(CHAINS.read_text(encoding="utf-8"))["chains"]
    dapps = json.loads(MARKET.read_text(encoding="utf-8"))["dapps"]
    print(f"体检通过：{len(chains)} 条链、{len(dapps)} 个 DApp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
