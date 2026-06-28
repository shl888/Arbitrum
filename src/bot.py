import time
import sys
import logging
import os
from logging.handlers import RotatingFileHandler
from web3 import Web3
from web3.middleware import geth_poa_middleware
from config import (
    RPC_URL_1,
    RPC_URL_2,
    PRIVATE_KEY,
    CONTRACT_ADDRESS,
    MIN_PROFIT_THRESHOLD_WETH,
    MIN_PROFIT_THRESHOLD_WBTC,
    CHECK_INTERVAL,
    PRECISION,
    PAIR_PRECISION,
    PAIR_THRESHOLD_TYPE,
    PAIR_BORROW_TIERS,
    CONTRACT_ABI,
)

# ==================== 日志配置 ====================
LOG_DIR = os.getenv('LOG_DIR', '/app/logs')
os.makedirs(LOG_DIR, exist_ok=True)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'bot.log'),
    maxBytes=10 * 1024 * 1024,
    backupCount=5
)
file_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[console_handler, file_handler]
)
logger = logging.getLogger(__name__)


class ArbitrageBot:
    def __init__(self):
        # 1. 初始化主、备节点 Web3 实例
        self.w3_primary = self._init_web3(RPC_URL_1)
        self.w3_secondary = self._init_web3(RPC_URL_2)
        self.use_primary = True

        # 2. 获取初始可工作的节点，读取钱包信息
        active_w3 = self._get_active_w3()
        self.account = active_w3.eth.account.from_key(PRIVATE_KEY)
        self.address = self.account.address

        # 3. 安全读取并初始化 pairCount
        try:
            self.pair_count = self.contract.functions.pairCount().call()
        except Exception as e:
            logger.error(f"❌ 启动失败：无法读取 pairCount，请确保合约地址和 ABI 正确: {e}")
            sys.exit(1)

        logger.info(f"🎉 Bot 启动成功 | 账户地址: {self.address} | 套利对数量: {self.pair_count}")
        if self.pair_count == 0:
            logger.warning("⚠️ 警告：未检测到套利对配置，请先调用 setPairConfig 注入子弹！")

    @property
    def contract(self):
        """
        🎯 核心 Bug 修复：利用 @property 属性黑魔法实现“动态合约绑定”。
        每次调用 self.contract 时，都会自动使用当前最健康、已切换的活动 w3 实例来创建合约对象。
        彻底规避 Web3.py 合约实例的“单节点终身制绑定死锁”！
        """
        active_w3 = self._get_active_w3()
        return active_w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI
        )

    @staticmethod
    def _init_web3(rpc_url):
        if not rpc_url:
            return None
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            return w3
        except Exception as e:
            logger.warning(f"节点位置初始化失败: {rpc_url} | {e}")
            return None

    def _get_active_w3(self):
        """获取当前标记的活动节点"""
        if self.use_primary and self.w3_primary:
            return self.w3_primary
        if self.w3_secondary:
            return self.w3_secondary
        if self.w3_primary:
            return self.w3_primary
        raise ConnectionError("❌ 致命错误：主备 RPC 节点全部失效！")

    def _switch_node(self):
        """被动触发主备容灾切换"""
        if self.use_primary:
            if self.w3_secondary:
                logger.warning("⚠️ 主节点连接异常，正在极速切换到备用节点...")
                self.use_primary = False
            else:
                logger.error("⚠️ 主节点异常，但未配置有效的备用节点，继续硬撑使用主节点...")
        else:
            if self.w3_primary:
                logger.warning("⚠️ 备用节点异常，正在尝试切回主节点...")
                self.use_primary = True

    def _normalize_profit(self, profit: int, pair_id: int) -> float:
        precision_key = PAIR_PRECISION.get(pair_id, 'WETH')
        decimals = PRECISION.get(precision_key, 10**18)
        return profit / decimals

    def _send_transaction(self, function_call, gas_multiplier: float = 1.2):
        w3 = self._get_active_w3()
        nonce = w3.eth.get_transaction_count(self.address, 'pending')
        
        # 估算并打包
        gas_estimate = function_call.estimate_gas({'from': self.address})
        gas_limit = int(gas_estimate * gas_multiplier)
        gas_price = w3.eth.gas_price

        tx = function_call.build_transaction({
            'chainId': 42161,
            'from': self.address,
            'nonce': nonce,
            'gas': gas_limit,
            'gasPrice': gas_price,
        })

        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"🚀 闪电贷套利开火成功！Hash: {tx_hash.hex()}")
        return tx_hash

    def run(self):
        logger.info("📡 雷达扫描已开启，正在进行超频盲冲检测...")
        while True:
            try:
                # 🎯 动态调用：此处会动态、实时地使用最健康的节点进行 check 
                result = self.contract.functions.checkAllOpportunities().call()
                best_tiers, best_profits, directions = result

                executed = False
                for i in range(self.pair_count):
                    profit_raw = best_profits[i]
                    if profit_raw == 0:
                        continue

                    profit_normalized = self._normalize_profit(profit_raw, i)

                    # 根据套利对类型选择对应的利润阈值
                    threshold_type = PAIR_THRESHOLD_TYPE.get(i, 'WETH')
                    if threshold_type == 'WETH':
                        threshold = MIN_PROFIT_THRESHOLD_WETH
                    else:
                        threshold = MIN_PROFIT_THRESHOLD_WBTC

                    if profit_normalized < threshold:
                        continue

                    logger.info(
                        f"🔥 发现高换手套利利润！ | pairId={i} | 最佳档位={best_tiers[i]} | "
                        f"估算净利润={profit_normalized:.6f} | 方向={'A→B' if directions[i] else 'B→A'}"
                    )

                    tier_idx = best_tiers[i]
                    if tier_idx == 99:
                        continue

                    tiers = PAIR_BORROW_TIERS.get(i)
                    if not tiers or tier_idx >= len(tiers):
                        logger.error(f"❌ 档位读取越界: pairId={i}, tierIdx={tier_idx}")
                        continue
                    borrow_amount = tiers[tier_idx]

                    self._send_transaction(
                        self.contract.functions.executeArbitrage(
                            i,
                            borrow_amount,
                            directions[i],
                            True
                        )
                    )
                    executed = True

                if not executed:
                    sys.stdout.write('.')
                    sys.stdout.flush()

                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                logger.info("\n🚫 用户手动中断，套利机器人已安全下线。")
                break
            except Exception as e:
                logger.error(f"🚨 主循环异常: {e}")
                #                  被动触发节点切换，让程序自适应自愈，绝对不崩盘！
                self._switch_node()
                time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    bot = ArbitrageBot()
    bot.run()