#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LotusWallet/assets —— chainlist 同步 + 链清单生成。

这个脚本做两件事（一次跑完）：

1. **转存**：把 chainlist 的原始数据（`https://chainlist.org/rpcs.json`，
   与 `https://chainid.network/chains.json` 是同一份）原样落到
   `chainlist/rpcs.json`，并写一份 `chainlist/meta.json` 记录来源、时间、大小、sha256。
   目的：即使 chainlist 哪天挂了 / 改了，我们也有一份可复现的快照，且改动可审计。

2. **生成**：把原始数据筛成 LotusWallet 客户端直接消费的 `chains/chains.json`
   （只保留白名单里的链、清洗 RPC、强制 EVM 统一 coinType=60），
   并写 `chains/meta.json`。

非 EVM 链（Bitcoin / Litecoin / TRON）不在 chainlist 里，由 `chains/_non_evm.json`
维护，脚本原样并入 —— 所以 chainlist 同步不会碰它们。

用法
----
    # 联网抓取（正常情况；本机定期同步就是这么跑的）
    python tools/sync_chainlist.py --fetch

    # 用本地缓存文件（无网络时）
    python tools/sync_chainlist.py --from-file /path/to/rpcs.json

    # 只校验产物、不写文件（提交前的检查步骤）
    python tools/sync_chainlist.py --check

退出码：0 成功 / 1 失败。失败必须是「响的」，不能静默产出坏数据 ——
本机定期同步脚本靠这个退出码决定要不要往下发布。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ⚠️ 顺序有讲究：**canonical 源必须排第一**。
#
# 两个源是同一份数据的两副面孔，但质量差得远：
#
#   `chainid.network/chains.json`  —— ethereum-lists/chains 官方数据集，chainlist.org 自己也是
#       从它生成页面的。`rpc` 是**字符串数组**，顺序是社区人工维护的「先能用、先可信」，
#       Ethereum 那条依次是 Infura（带占位符，会被过滤）→ mycryptoapi → Cloudflare → PublicNode。
#
#   `chainlist.org/rpcs.json`     —— 由上面的数据再加工，带 `tracking` 元信息。但它的
#       `rpc` 顺序**噪声很大**：排在最前面的是 `xrpc.cl`、`lb.routeme.sh`、
#       `one.valve.city/rpc/vk_demo/...`（名字里直接写着 demo）这类来源不明的端点。
#       同一个「取前 4 个」的规则，在这个源上会挑出一串明显更差的节点。
#
# 所以：canonical 源优先，chainlist.org 只作兜底。第二项仍然保留价值和意义 ——
# 它带 `tracking` 字段，真的到了要用它的时候 `clean_rpcs` 会把明确跟踪使用者的端点降级。
CHAINLIST_SOURCES = [
    "https://chainid.network/chains.json",
    "https://chainlist.org/rpcs.json",
]

RAW_OUT = REPO / "chainlist" / "rpcs.json"
RAW_META_OUT = REPO / "chainlist" / "meta.json"
CHAINS_OUT = REPO / "chains" / "chains.json"
CHAINS_META_OUT = REPO / "chains" / "meta.json"
NON_EVM_SRC = REPO / "chains" / "_non_evm.json"

# 允许没有 rpcUrls 的链。与 tools/verify.py 的 ALLOW_EMPTY_RPC 保持一致 ——
# 两处若不一致，会出现「verify 通过但 check 失败」这种自相矛盾的状态。
# Chia 的 full-node RPC 需要自签证书，没有可用的公共节点；客户端本版本
# 只做 xch1… 地址编解码与观察地址导入，不查余额。
ALLOW_EMPTY_RPC = {"chia"}

# ------------------------------------------------------------------ 白名单
# 主网：钱包默认展示、用户真正会用的链。
# 加链 = 在这里加一个 chainId（chainlist 有数据即可），然后重跑本脚本。
MAINNET_CHAIN_IDS = [
    1,            # Ethereum
    56,           # BNB Smart Chain
    137,          # Polygon PoS
    8453,         # Base
    10,           # OP Mainnet
    42161,        # Arbitrum One
    43114,        # Avalanche C-Chain
    59144,        # Linea
    534352,       # Scroll
    324,          # zkSync Era
    250,          # Fantom Opera
    100,          # Gnosis Chain
    8217,         # Kaia（原 Klaytn）
    1284,         # Moonbeam
    1285,         # Moonriver
    25,           # Cronos
    1088,         # Metis
    42220,        # Celo
    1313161554,   # Aurora
    1666600000,   # Harmony
    42262,        # Oasis Emerald
    199,          # BitTorrent Chain
    66,           # OKX Chain (OKC)
    128,          # HECO
    321,          # KCC
    167000,       # Taiko
    81457,        # Blast
    34443,        # Mode
    7777777,      # Zora
    1116,         # Core
]

# 测试网：供开发/验证使用，客户端由「显示测试网」开关控制可见性。
TESTNET_CHAIN_IDS = [
    11155111,     # Sepolia
    11155420,     # OP Sepolia
    84532,        # Base Sepolia
    421614,       # Arbitrum Sepolia
    80002,        # Polygon Amoy
    97,           # BSC Testnet
    17000,        # Holesky
    43113,        # Avalanche Fuji
    10200,        # Gnosis Chiado
    1666700000,   # Harmony Testnet
]

# 曾经在白名单里、后来被上游废弃的链，记在这里以免有人再手滑加回来。
# 167009 Taiko Hekla：chainlist 已标记 deprecated 并撤掉全部公共 RPC。

# 每条链保留多少个公共 RPC（chainlist 大体按可用性排序，取前几个做故障转移）
MAX_RPC = 4

# 可读 id 覆盖。chainlist 的 shortName 有些很费解（arb1 / oeth / pol …）。
# ⚠️ id 会成为客户端数据库里的外键，**一经发布不可更改** —— 只能增，不能改。
ID_OVERRIDES = {
    1: "ethereum",
    56: "bsc",
    137: "polygon",
    10: "optimism",
    42161: "arbitrum",
    43114: "avalanche",
    8453: "base",
    59144: "linea",
    534352: "scroll",
    324: "zksync-era",
    250: "fantom",
    100: "gnosis",
    8217: "kaia",
    1284: "moonbeam",
    1285: "moonriver",
    25: "cronos",
    1088: "metis",
    42220: "celo",
    1313161554: "aurora",
    1666600000: "harmony",
    42262: "oasis-emerald",
    199: "bittorrent",
    66: "okx-chain",
    128: "heco",
    321: "kcc",
    167000: "taiko",
    81457: "blast",
    34443: "mode",
    7777777: "zora",
    1116: "core",
    11155111: "sepolia",
    11155420: "optimism-sepolia",
    84532: "base-sepolia",
    421614: "arbitrum-sepolia",
    80002: "polygon-amoy",
    97: "bsc-testnet",
    17000: "holesky",
    167009: "taiko-hekla",
    43113: "avalanche-fuji",
    10200: "gnosis-chiado",
    1666700000: "harmony-testnet",
}

_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


# ------------------------------------------------------------------ 工具
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_opener() -> urllib.request.OpenerDirector:
    """显式控制代理与 TLS —— 不吃环境变量的暗亏。

    shell 里被注入的 `HTTPS_PROXY`（本机是 `http://127.0.0.1:53117`）会改坏请求体，
    GitHub 那边会报随机的 `malformed request`。抓 chainlist 是 GET，症状没 PUT 那么明显，
    但既然本机已经确认 `PUBLISH_PROXY=http://127.0.0.1:8800` 这条链路是稳的，
    就在这里统一用同一个变量，避免「有的脚本走这条、有的走那条」。

    没设 `PUBLISH_PROXY` 时显式传空字典，**屏蔽环境代理**走直连 ——
    行为可预测，比「看环境里有什么就用什么」强。
    """
    context = ssl.create_default_context()
    # 本链路到 1.2 才稳：TLS 1.3 的握手会被中间设备打断（SSLEOFError）。
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    proxy = os.environ.get("PUBLISH_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        urllib.request.HTTPSHandler(context=context),
    )


_OPENER = _build_opener()
FETCH_RETRIES = 4


def fetch(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            # 带 UA 便于 chainlist 侧识别流量来源，也避免被默认 UA 拦。
            "User-Agent": "LotusWallet-assets-sync/1.0 (+https://github.com/LotusWallet/assets)",
            "Accept": "application/json",
        },
    )
    last: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            with _OPENER.open(request, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise  # 有响应说明链路是通的，重试没意义
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if attempt < FETCH_RETRIES:
                print(f"  第 {attempt} 次失败（{exc}），重试……", file=sys.stderr)
                time.sleep(1.5 * attempt)
    raise last  # type: ignore[misc]


def load_chainlist(args) -> tuple[bytes, str]:
    """返回 (原始字节, 来源 URL)；失败时抛出异常由 main 统一处理。"""
    if args.from_file:
        path = Path(args.from_file)
        if not path.exists():
            raise SystemExit(f"找不到本地文件：{path}")
        return path.read_bytes(), f"file:{path.name}"

    last_err: Exception | None = None
    for url in CHAINLIST_SOURCES:
        try:
            print(f"抓取 {url} ……")
            data = fetch(url)
            if len(data) < 10_000:
                raise ValueError(f"返回体过小（{len(data)} 字节），疑似被劫持或出错")
            print(f"  收到 {len(data):,} 字节")
            return data, url
        except Exception as exc:  # noqa: BLE001 —— 逐个源尝试，最后统一报错
            print(f"  失败：{exc}", file=sys.stderr)
            last_err = exc
    raise SystemExit(f"全部 chainlist 源均不可用：{last_err}")


def _rpc_entries(raw: list) -> list[tuple[str, str]]:
    """把 chainlist 的 `rpc` 字段归一成 [(url, tracking)]。

    ⚠️ 这个字段的格式**变过**，是本仓库踩过最贵的一个坑：

      - 老版本：`"rpc": ["https://a", "https://b"]` —— 字符串数组
      - 新版本：`"rpc": [{"url": "https://a", "tracking": "none"}, …]` —— 对象数组

    旧代码只认字符串（`if not isinstance(url, str): continue`），于是上游一改格式，
    **每一条链的 RPC 都被判成「不是字符串」丢掉**，40 条白名单链一条都产不出来，
    产物从 49 条链缩成 9 条（只剩非 EVM）—— 而 `verify.py` 照样报「体检通过」。

    所以这里两种都收：只要拿得到 URL，格式怎么变都不至于静默归零。
    """
    out: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            # 老格式没有 tracking 信息，用 unknown 占位
            out.append((item, "unknown"))
        elif isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str):
                out.append((url, str(item.get("tracking") or "unknown").lower()))
    return out


def clean_rpcs(raw: list) -> list[str]:
    """挑出可直接使用的公共 HTTPS RPC，去掉带占位符的付费端点与 ws://。

    排序上把 `tracking != "yes"` 的排在前面：标了 `tracking: yes` 的端点明确会记录
    使用者，对一个把隐私当卖点的钱包不该是首选。

    但这是**排序**、不是过滤 —— 有些链只有带 tracking 的公共端点，
    一刀切会让这条链直接消失，那比用一个不理想的节点糟得多。
    """
    seen: set[str] = set()
    candidates: list[tuple[bool, int, str]] = []
    for index, (url, tracking) in enumerate(_rpc_entries(raw)):
        if not url.startswith("https://"):
            continue
        if _PLACEHOLDER.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        # False 排前面；同组内保持上游给出的原始顺序（那是它们的可用性排序）
        candidates.append((tracking == "yes", index, url))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [url for _tracked, _index, url in candidates[:MAX_RPC]]


def explorer_urls(entry: dict) -> tuple[str | None, str | None]:
    for ex in entry.get("explorers") or []:
        base = (ex.get("url") or "").rstrip("/")
        if not base.startswith("https://"):
            continue
        tx = ex.get("txPage") or (base + "/tx/")
        addr = ex.get("addressPage") or (base + "/address/")
        return tx, addr
    return None, None


def unique_id(short_name: str, chain_id: int, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", (short_name or "").lower()).strip("-")
    if not base:
        base = "chain"
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}-{chain_id}" if n == 2 else f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def build_evm(entries: list[dict], used_ids: set[str]) -> list[dict]:
    by_id = {e["chainId"]: e for e in entries if isinstance(e.get("chainId"), int)}
    chains: list[dict] = []

    for chain_id, is_testnet in (
        [(cid, False) for cid in MAINNET_CHAIN_IDS]
        + [(cid, True) for cid in TESTNET_CHAIN_IDS]
    ):
        entry = by_id.get(chain_id)
        if entry is None:
            print(f"  ! chainlist 中找不到 chainId={chain_id}，已跳过", file=sys.stderr)
            continue

        rpcs = clean_rpcs(entry.get("rpc"))
        if not rpcs:
            print(f"  ! chainId={chain_id} ({entry.get('name')}) 没有可用的公共 HTTPS RPC，已跳过",
                  file=sys.stderr)
            continue

        currency = entry.get("nativeCurrency") or {}
        tx_url, addr_url = explorer_urls(entry)
        features = {f.get("name") for f in entry.get("features") or []}

        chain_key = ID_OVERRIDES.get(chain_id)
        if chain_key is None or chain_key in used_ids:
            chain_key = unique_id(entry.get("shortName") or "", chain_id, used_ids)
        else:
            used_ids.add(chain_key)

        chains.append({
            "family": "evm",
            "id": chain_key,
            "name": entry.get("name") or f"Chain {chain_id}",
            "symbol": currency.get("symbol") or "ETH",
            "chainId": chain_id,
            # ⚠️ 强制 60，不能用 chainlist 的 slip44。
            # 例如 BNB 的 slip44 是 714，但所有 EVM 链的派生路径都固定为
            # m/44'/60'/0'/0/i，这样同一助记词才能在全部 EVM 链上得到同一地址
            # —— 这是「加链零代码」成立的前提。
            # 客户端有单测守护这条不变量（ChainConfigTest）。
            "coinType": 60,
            "decimals": currency.get("decimals", 18),
            "rpcUrls": rpcs,
            "explorerTxUrl": tx_url,
            "explorerAddressUrl": addr_url,
            "supportsEip1559": "EIP1559" in features,
            "testnet": is_testnet,
            "enabled": True,
        })

    return chains


def build_chains(raw_bytes: bytes) -> tuple[dict, dict]:
    """从 chainlist 原始数据构建客户端清单，返回 (chains 文档, meta)。"""
    entries = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(entries, list):
        raise SystemExit("chainlist 数据不是数组，数据结构可能已变更")

    non_evm_doc = json.loads(NON_EVM_SRC.read_text(encoding="utf-8"))
    non_evm = non_evm_doc["chains"]
    used_ids = {c["id"] for c in non_evm}

    evm = build_evm(entries, used_ids)
    mainnets = [c for c in evm if not c["testnet"]]
    testnets = [c for c in evm if c["testnet"]]

    missing = (set(MAINNET_CHAIN_IDS) - {c["chainId"] for c in evm}) | (
        set(TESTNET_CHAIN_IDS) - {c["chainId"] for c in evm}
    )
    if missing:
        print(f"  ! 白名单中有 {len(missing)} 条链没产出：{sorted(missing)}", file=sys.stderr)

    doc = {
        "version": 1,
        "chains": non_evm + evm,
    }
    meta = {
        "generatedAt": now_iso(),
        "generator": "tools/sync_chainlist.py",
        "chainlistEntries": len(entries),
        "counts": {
            "total": len(doc["chains"]),
            "evmMainnet": len(mainnets),
            "evmTestnet": len(testnets),
            "nonEvm": len(non_evm),
        },
        "whitelist": {
            "mainnet": len(MAINNET_CHAIN_IDS),
            "testnet": len(TESTNET_CHAIN_IDS),
        },
        "skippedChainIds": sorted(missing),
    }
    return doc, meta


def write_json(path: Path, obj) -> bytes:
    # 统一 LF、2 空格缩进、结尾换行 —— 让 diff 最小、可 review。
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    data = text.replace("\r\n", "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def do_check() -> int:
    """只校验已有产物：解析得动、白名单齐全、EVM coinType 全为 60。"""
    problems: list[str] = []
    if not CHAINS_OUT.exists():
        problems.append("缺少 chains/chains.json")
    else:
        doc = json.loads(CHAINS_OUT.read_text(encoding="utf-8"))
        chains = doc["chains"]
        ids = [c["id"] for c in chains]
        if len(ids) != len(set(ids)):
            problems.append("chains/chains.json 存在重复 id")
        for c in chains:
            family = c.get("family")
            if family == "evm" and c.get("coinType") != 60:
                problems.append(f"EVM 链 {c['id']} 的 coinType 不是 60")
            # Chia 没有可用的公共 JSON-RPC：它靠 Chia 自家的 full-node RPC（自签证书），
            # 客户端本版本也只做地址编解码与观察地址导入，因此允许留空。
            # 其余家族必须至少有一个节点，否则「链在列表里但永远查不到余额」。
            if not c.get("rpcUrls") and c["id"] not in ALLOW_EMPTY_RPC:
                problems.append(f"链 {c['id']} 没有 RPC")
        present = {c.get("chainId") for c in chains if c.get("family") == "evm"}
        for cid in MAINNET_CHAIN_IDS + TESTNET_CHAIN_IDS:
            if cid not in present:
                problems.append(f"白名单 chainId={cid} 缺失")
    if not RAW_OUT.exists():
        problems.append("缺少 chainlist/rpcs.json")

    if problems:
        print("校验失败：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("校验通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 chainlist 并生成 LotusWallet 链清单")
    parser.add_argument("--fetch", action="store_true", help="从 chainlist 联网抓取（默认行为）")
    parser.add_argument("--from-file", metavar="PATH", help="使用本地 chainlist JSON 缓存")
    parser.add_argument("--check", action="store_true", help="只校验已有产物，不写文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容摘要")
    args = parser.parse_args()

    if args.check:
        return do_check()

    raw_bytes, source = load_chainlist(args)
    raw_sha = sha256_bytes(raw_bytes)

    # 内容没变就不动文件：让「定期同步」在没有变化时保持仓库安静，
    # 也避免每天产生一次无意义的提交。
    if RAW_OUT.exists() and sha256_bytes(RAW_OUT.read_bytes()) == raw_sha:
        print("chainlist 内容与上次一致，转存文件保持不变")
    elif args.dry_run:
        print(f"[dry-run] 将写入 {RAW_OUT.relative_to(REPO)}（{len(raw_bytes):,} 字节）")
    else:
        RAW_OUT.parent.mkdir(parents=True, exist_ok=True)
        RAW_OUT.write_bytes(raw_bytes if raw_bytes.endswith(b"\n") else raw_bytes + b"\n")
        print(f"已转存 {RAW_OUT.relative_to(REPO)}（{len(raw_bytes):,} 字节）")

    doc, meta = build_chains(raw_bytes)
    meta["chainlist"] = {
        "source": source,
        "fetchedAt": now_iso(),
        "sha256": raw_sha,
        "bytes": len(raw_bytes),
    }
    meta["chainsSha256"] = sha256_bytes(
        (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )

    if args.dry_run:
        print(f"[dry-run] chains：共 {meta['counts']['total']} 条，"
              f"主网 {meta['counts']['evmMainnet']}，测试网 {meta['counts']['evmTestnet']}")
        return 0

    write_json(CHAINS_OUT, doc)
    write_json(CHAINS_META_OUT, meta)
    if not args.dry_run:
        write_json(RAW_META_OUT, {
            "source": source,
            "fetchedAt": now_iso(),
            "bytes": len(raw_bytes),
            "sha256": raw_sha,
            "mirroredFile": "rpcs.json",
            "note": "chainlist 原始数据原样转存；不要手工编辑此文件。",
        })

    print(f"已生成 {CHAINS_OUT.relative_to(REPO)}：共 {meta['counts']['total']} 条"
          f"（EVM 主网 {meta['counts']['evmMainnet']} / 测试网 {meta['counts']['evmTestnet']}"
          f" / 非 EVM {meta['counts']['nonEvm']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
