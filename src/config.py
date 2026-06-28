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

# 每组套利对对应的利润阈值类型
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

# 在 Python 端对齐 setPairConfig 中配置的真实物理档位
PAIR_BORROW_TIERS = {
     0: [10000000000000000, 30000000000000000, 50000000000000000],  # 0.01, 0.03, 0.05 WETH
#     1: [10000000000000000, 30000000000000000, 50000000000000000],  # 0.01, 0.03, 0.05 WETH
#     2: [10000000000000000, 30000000000000000, 50000000000000000],  # 0.01, 0.03, 0.05 WETH
#     3: [5000000000000000, 10000000000000000, 20000000000000000],   # 0.005, 0.01, 0.02 WETH
#     4: [1000000000000000, 3000000000000000, 5000000000000000],     # 0.001, 0.003, 0.005 WETH
#     5: [5000000000000000, 10000000000000000, 20000000000000000],   # 0.005, 0.01, 0.02 WETH
#     6: [5000000000000000, 10000000000000000, 20000000000000000],   # 0.005, 0.01, 0.02 WETH
#     7: [5000000, 10000000, 20000000],                                 # 0.005, 0.01, 0.02 WBTC
#     8: [5000000, 10000000, 20000000],                              # 0.05, 0.1, 0.2 WBTC
#     9: [5000000, 10000000, 20000000]                               # 0.05, 0.1, 0.2 WBTC
}

# 自动读取同目录下的 abi.json
current_dir = os.path.dirname(os.path.abspath(__file__))
abi_path = os.path.join(current_dir, 'abi.json')
try:
    with open(abi_path, 'r', encoding='utf-8') as f:
        CONTRACT_ABI = json.load(f)
except Exception as e:
    raise FileNotFoundError(f"无法加载 {abi_path}，请确保已将 Remix 的 ABI 导出放入：{e}")