import os
import json
from dotenv import load_dotenv

load_dotenv()

# RPC 节点
RPC_URL_1 = os.getenv('ARBITRUM_RPC_URL_1')
RPC_URL_2 = os.getenv('ARBITRUM_RPC_URL_2')
PRIVATE_KEY = os.getenv('OPERATOR_PRIVATE_KEY')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS')

# 最小利润门槛（币本位）
# 取自至少能够覆盖gas费，可自定义。
# 这是服务器程序，对套利合约的初筛结果，进行复筛用的，因为初筛的通过标准，只是理论利润大于0，但是未必值得交易，所以有了复筛，通过复筛门槛的套利机会，会被直接执行真实交易。
# WETH 本位（第1-6组）：0.0003 WETH ≈ 0.5 美元
# WBTC 本位（第7-9组）：0.000008 WBTC ≈ 0.5 美元
MIN_PROFIT_THRESHOLD_WETH = float(os.getenv('MIN_PROFIT_THRESHOLD_WETH', 0.0006))
MIN_PROFIT_THRESHOLD_WBTC = float(os.getenv('MIN_PROFIT_THRESHOLD_WBTC', 0.000016))

CHECK_INTERVAL = float(os.getenv('CHECK_INTERVAL', 1.2))

# 代币精度映射（用于归一化 bestProfits）
PRECISION = {
    'WETH': 10**18,
    'WBTC': 10**8,
}

# 每组套利对对应的精度类型
PAIR_PRECISION = {
    0: 'WETH',  # WETH/ARB
    1: 'WETH',  # WETH/LINK
    2: 'WETH',  # WETH/GMX
    3: 'WETH',  # WETH/AAVE
    4: 'WETH',  # weETH/WETH
    5: 'WETH',  # PENDLE/WETH
    6: 'WBTC',  # WBTC/EVA
    7: 'WBTC',  # WBTC/cbBTC
    8: 'WBTC',  # WBTC/tBTC
}

# 每组套利对对应的利润币种类型
PAIR_THRESHOLD_TYPE = {
    0: 'WETH',
    1: 'WETH',
    2: 'WETH',
    3: 'WETH',
    4: 'WETH',
    5: 'WETH',
    6: 'WBTC',
    7: 'WBTC',
    8: 'WBTC',
}

# 在 Python 端对齐 setPairConfig 中配置的真实物理档位，这里的档位是套利合约所使用的参数副本，是用来映射，用来解决套利合约失忆，记不住初筛，精筛时所用档位的机制的问题。必须与套利合约所用的参数一致。
"""
换算公式
```
借款金额计算基数：小池子的最小库存币的库存美元额度
借款金额的3个档位的比例：
5万以下，0.5％-1％-1.5％
5--10万，1％-1.5％-2％
10万以上，1％-2％-3％
借款金额（美元）= 计算基数（美元）* 对应比例
借款数量 = 美元金额 / 代币价格
借款的最小单位整数 = 借款数量 × 精度
```
· WETH / PENDLE / weETH：精度 18 位，归一化值直接以 wei 为单位。
· WBTC：精度 8 位，归一化值以 satoshi 为单位。
"""

PAIR_BORROW_TIERS = {
     0: [125786163522012580, 251572327044025150, 377358490566037760],  # 4万，0.5％-1％-1.5％ WETH
     1: [628930817610062800, 1257861635220125800, 1886792452830188500],  # 10万，1％-2％-3％ WETH
     2: [125786163522012580, 251572327044025150, 377358490566037760],  # 4万，0.5％-1％-1.5％ WETH
     3: [31446540880503140, 62893081761006290, 94339622641509440],   # 1万，0.5％-1％-1.5％ WETH
     4: [125786163522012580, 251572327044025150, 377358490566037760],   # 4万，0.5％-1％-1.5％ WETH
     5: [2201257861635220200, 4402515723270440250, 6603773584905660370],   # 35万，1％-2％-3％ WETH
     6: [1345100, 2017650, 2690200],                                 # 8万，1％-1.5％-2％ WBTC
     7: [252200, 504410, 756600],                              # 3万，0.5％-1％-1.5％ WBTC
     8: [2690200, 5380410, 8070610]                               # 16万，1％-2％-3％ WBTC
}

# 自动读取同目录下的 abi.json
current_dir = os.path.dirname(os.path.abspath(__file__))
abi_path = os.path.join(current_dir, 'abi.json')
try:
    with open(abi_path, 'r', encoding='utf-8') as f:
        CONTRACT_ABI = json.load(f)
except Exception as e:
    raise FileNotFoundError(f"无法加载 {abi_path}，请确保已将 Remix 的 ABI 导出放入：{e}")