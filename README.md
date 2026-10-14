# LotusWallet/assets

LotusWallet 的**公共只读资源库**。App 需要的、不需要随版本发布的公共信息都放这里 ——
客户端通过 `raw.githubusercontent.com` 拉取并本地缓存，**没有后端服务**。

一次改动，全量用户生效，不必发版。

---

## 目录

| 目录 | 内容 | 谁在读 |
| --- | --- | --- |
| `chains/` | **公链清单**（客户端直接消费） | LotusWallet App |
| `chainlist/` | chainlist 原始数据的转存快照 | 人 / 审计 / 生成脚本 |
| `market/` | **DApp 市场清单**（客户端直接消费）+ 待收录提交 | LotusWallet App |
| `tools/` | 生成与校验脚本（维护者在提交前跑） | 维护者 |
| `blockchains/` | ⚠️ **旧版**代币图标库，保留不动 | 历史遗留 |
| `dapps/` | ⚠️ **旧版** DApp 提交模板，保留不动 | 历史遗留 |

> 历史遗留目录的链接可能已被旧版 App 引用，因此**不删不改**。
> 新数据一律走上面四个新目录。

---

## 1. `chains/` —— 公链清单

客户端「加一条 EVM 链 = 加一条 JSON，零代码」这条架构要求的**数据落点**。

```
chains/
├── _non_evm.json      人工维护的非 EVM 链（BTC / LTC / TRON）——改这里
├── chains.json        ★ 生成产物，客户端消费（不要手工编辑）
└── meta.json          生成时间、来源、条数、sha256
```

`chains.json` 顶层结构：

```json
{
  "version": 1,
  "chains": [
    { "family": "evm",  "id": "bsc", "name": "BNB Smart Chain", "chainId": 56,
      "symbol": "BNB", "decimals": 18, "coinType": 60, "rpcUrls": ["https://..."],
      "explorerTxUrl": "https://bscscan.com/tx/", "supportsEip1559": true,
      "testnet": false, "enabled": true },
    { "family": "utxo", "id": "bitcoin", "name": "Bitcoin", "coinType": 0,
      "addressType": "NATIVE_SEGWIT", "hrp": "bc", "api": "ESPLORA" },
    { "family": "tron", "id": "tron", "name": "TRON", "coinType": 195 }
  ]
}
```

客户端拉取地址：
`https://raw.githubusercontent.com/LotusWallet/assets/main/chains/chains.json`

### 数据从哪来

EVM 部分**全部借用 chainlist**（<https://chainlist.org>，开源社区维护），
由 `tools/sync_chainlist.py` 每天自动同步一次：

1. 抓 `https://chainlist.org/rpcs.json`（备源 `https://chainid.network/chains.json`）
   → 原始数据原样转存到 `chainlist/rpcs.json`（约 1.2 MB，2800+ 条链）；
2. 按白名单（脚本里的 `MAINNET_CHAIN_IDS` / `TESTNET_CHAIN_IDS`）筛出我们支持的链；
3. 清洗 RPC：只留可直接用的公共 `https://` 端点，剔除带 `${API_KEY}` 占位符的付费端点，
   每条链保留前 4 个做故障转移；
4. 输出 `chains/chains.json`。

手动重跑：

```bash
python tools/sync_chainlist.py --fetch                    # 联网抓取
python tools/sync_chainlist.py --from-file rpcs.json      # 用本地快照
python tools/sync_chainlist.py --check                    # 只校验产物
```

### 两条必须守住的不变量

1. **所有 EVM 链的 `coinType` 强制为 60**，不能用 chainlist 里的 slip44。
   chainlist 给 BNB 的 slip44 是 714，但所有 EVM 链的派生路径都固定为
   `m/44'/60'/0'/0/i` —— 只有强行统一，同一助记词才能在全部 EVM 链上得到同一地址。
   这条由 App 侧单测 `ChainConfigTest` 守护。
2. **`id` 一经发布不可更改**。它是客户端数据库里的外键，改了等于把用户的钱包 / 账户
   记录打散。只允许新增 id，不允许重命名（`ID_OVERRIDES` 那份映射就是为此存在的）。

### 怎么加一条链

在 `tools/sync_chainlist.py` 的白名单里加一个 `chainId` → 重跑脚本 → 提交。
`--check` 会校验白名单里的链全部产出成功；RPC 死链 / 被上游废弃的链会报警
（如 `167009 Taiko Hekla` 已被 chainlist 标记 deprecated 并撤掉全部公共 RPC）。

---

## 2. `market/` —— DApp 市场清单

替代旧版「服务端 DApp 接口」的静态只读清单。

```
market/
├── curated.json          团队维护的精选条目（含分类定义）——改这里
├── submissions/          社区提交，每个 DApp 一个 JSON
│   ├── README.md         收录标准与字段说明
│   ├── _TEMPLATE.json    提交模板
│   └── <id>.json         已收录的提交
└── dapps.json            ★ 生成产物，客户端消费（不要手工编辑）
```

客户端拉取地址：
`https://raw.githubusercontent.com/LotusWallet/assets/main/market/dapps.json`

```bash
python tools/build_market.py            # 重新生成 dapps.json
python tools/build_market.py --check    # 校验产物与源数据一致
```

生成时会把条目里的 `chains` 与 `chains/chains.json`、`category` 与
`curated.json` 的分类交叉校验 —— 写错了直接失败，不会把坏数据推上去。

**下架也走同一条路**：从 `curated.json` 或 `submissions/` 删掉条目、重新生成，
客户端下次刷新即生效，不需要用户升级 App。

---

## 3. `tools/`

| 脚本 | 作用 |
| --- | --- |
| `sync_chainlist.py` | 转存 chainlist + 生成 `chains/chains.json`；`--check` 校验 |
| `build_market.py` | 生成 `market/dapps.json`；`--check` 校验 |
| `verify.py` | 对所有产物做一次结构体检（提交前最后一步） |

全部是 Python 3.9+ 标准库，**没有第三方依赖** —— 零安装成本，也保证几年后还能跑起来。

### 定时同步怎么做（不使用 GitHub Actions）

链清单是外部数据，没人改代码也会漂，因此需要定期重跑。**这件事在开发机上做**：

```bash
python tools/sync_chainlist.py          # 需要能访问 chainlist.org
python tools/verify.py
# 有变化再提交：走 repos/publish.py assets --yes
```

按项目约定，GitHub 只用于存放与分发，**不使用它的部署/打包能力**，
所以这里没有 `.github/workflows/`。定时触发由开发机上的计划任务承担。

---

## 4. 扩展位：`tokens/`（尚未启用）

客户端已有代币表（`TokenEntity`），但「添加代币」目前只能手填合约地址。
将来做「常见代币列表」时，在 `tokens/tokens.json` 落一份**按链分组**的代币清单即可：

```json
{ "version": 1, "updatedAt": "…",
  "tokens": { "ethereum": [
    { "address": "0xdAC17F…", "symbol": "USDT", "name": "Tether USD",
      "decimals": 6, "standard": "ERC20", "iconUrl": "…" } ] } }
```

候选上游（同样是公开数据源）：LI.FI 的 `https://li.quest/v1/tokens`、
Uniswap 的 `https://tokens.uniswap.org`。

> **地址必须来自上游抓取，不允许手抄。** 手工抄错一个字符，用户就把币转丢了。

---

## 5. 旧版目录（保留，不作为新数据源）

### `blockchains/`

沿用 trustwallet/assets 风格：`blockchains/<链名>/<合约地址>/{info.json, logo.png}`。
社区提交代币图标时复制 `blockchains/ethereum/0xdAC17F…/` 改即可。

### `dapps/`

旧版 DApp 提交模板（`sample_dapp.json`）。字段设计落后于现在客户端消费的
`market/dapps.json`，**新提交请走 `market/submissions/`**。

---

## 6. 数据来源与署名

| 数据 | 来源 | 许可 |
| --- | --- | --- |
| 公链元数据 / RPC | [chainlist](https://chainlist.org)（<https://github.com/DefiLlama/chainlist>） | 见上游仓库 |
| 代币图标 | 社区提交，见 `blockchains/` | 各自项目所有 |
| DApp 条目 | 团队收录 + 社区提交 | 本仓库 |

本仓库对上述数据做的是**原样转存与格式转换**，未改变内容。转存快照保留在
`chainlist/` 下，附 `meta.json` 记录来源、抓取时间与 sha256，便于随时回溯比对。

## 7. Disclaimer

任何人都可以提交条目，但这**不代表** LotusWallet 与该项目存在合作关系。团队会在
仔细审核后拒绝被认定为诈骗或欺诈的项目，并保留随时修改收录标准的权利。
刷量行为（包括但不限于向随机地址大量空投代币）会导致条目被标记为 spam 并移除。

**本仓库不托管任何私钥、助记词或用户数据。**
