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
# WETH 本位（第1-7组）：0.00006 WETH ≈ 0.1 美元
# WBTC 本位（第8-10组）：0.0000016 WBTC ≈ 0.1 美元
MIN_PROFIT_THRESHOLD_WETH = float(os.getenv('MIN_PROFIT_THRESHOLD_WETH', 0.00006))
MIN_PROFIT_THRESHOLD_WBTC = float(os.getenv('MIN_PROFIT_THRESHOLD_WBTC', 0.0000016))

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
    4: 'WETH',  # RAIN/WETH
    5: 'WETH',  # weETH/WETH
    6: 'WETH',  # PENDLE/WETH
    7: 'WBTC',  # WBTC/EVA
    8: 'WBTC',  # WBTC/cbBTC
    9: 'WBTC',  # WBTC/tBTC
}

# 每组套利对对应的利润币种类型
PAIR_THRESHOLD_TYPE = {
    0: 'WETH',
    1: 'WETH',
    2: 'WETH',
    3: 'WETH',
    4: 'WETH',
    5: 'WETH',
    6: 'WETH',
    7: 'WBTC',
    8: 'WBTC',
    9: 'WBTC',
}

# 在 Python 端对齐 setPairConfig 中配置的真实物理档位，这里的档位是套利合约所使用的参数副本，是用来映射，用来解决套利合约失忆，记不住初筛，精筛时所用档位的机制的问题。必须与套利合约所用的参数一致。
"""
换算公式
```
借款数量 = 美元金额 / 代币价格
借款的最小单位整数 = 借款数量 × 精度
```
· WETH / PENDLE / weETH：精度 18 位，归一化值直接以 wei 为单位。
· WBTC：精度 8 位，归一化值以 satoshi 为单位。
"""

PAIR_BORROW_TIERS = {
     0: [503144654088050300, 754716981132075500, 1257861635220125800],  # 0.5031, 0.7547, 1.2579 WETH
     1: [1257861635220125800, 1886792452830188700, 3144654088050314500],  # 1.2579, 1.8868, 3.1447 WETH
     2: [503144654088050300, 754716981132075500, 1257861635220125800],  # 0.5031, 0.7547, 1.2579 WETH
     3: [125786163522012580, 188679245283018870, 314465408805031450],   # 0.1258, 0.1887, 0.3145 WETH
     4: [628930817610062900, 943396226415094300, 1572327044025157200],     # 0.6289, 0.9434, 1.5723 WETH
     5: [503144654088050300, 754716981132075500, 1257861635220125800],   # 0.5031, 0.7547, 1.2579 WETH
     6: [4402515723270440250, 6603773584905660377, 11006289308176100630],   # 4.4025, 6.6038, 11.0063 WETH
     7: [2690206, 4035309, 6725516],                                 # 0.0269, 0.0404, 0.0673 WBTC
     8: [1008827, 1513241, 2522068],                              # 0.0101, 0.0151, 0.0252 WBTC
     9: [5380412, 8070617, 13451030]                               # 0.0538, 0.0807, 0.1345 WBTC
}

# 自动读取同目录下的 abi.json
current_dir = os.path.dirname(os.path.abspath(__file__))
abi_path = os.path.join(current_dir, 'abi.json')
try:
    with open(abi_path, 'r', encoding='utf-8') as f:
        CONTRACT_ABI = json.load(f)
except Exception as e:
    raise FileNotFoundError(f"无法加载 {abi_path}，请确保已将 Remix 的 ABI 导出放入：{e}")