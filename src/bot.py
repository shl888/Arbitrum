import time
import sys
import logging
from web3 import Web3
from web3.middleware import geth_poa_middleware
from config import (
    RPC_URL_1,
    RPC_URL_2,
    PRIVATE_KEY,
    CONTRACT_ADDRESS,
    MIN_PROFIT_THRESHOLD,
    CHECK_INTERVAL,
    PRECISION,
    PAIR_PRECISION,
    PAIR_BORROW_TIERS,
    CONTRACT_ABI,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ArbitrageBot:
    def __init__(self):
        # 初始化主、备 Web3 实例
        self.w3_primary = self._init_web3(RPC_URL_1)
        self.w3_secondary = self._init_web3(RPC_URL_2)
        
        # 标志位：当前活动的是哪一个节点，默认是主节点 (True代表主，False代表备)
        self.use_primary = True
        
        # 获取当前可工作的节点，用于合约初始化，防止启动时主节点卡顿导致崩盘
        active_w3 = self._get_active_w3()
        self.contract = active_w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI
        )
        self.account = active_w3.eth.account.from_key(PRIVATE_KEY)
        self.address = self.account.address
        
        # 安全调用 pairCount 获取
        try:
            self.pair_count = self.contract.functions.pairCount().call()
        except Exception as e:
            logger.error(f"无法读取 pairCount，请确保合约地址和 ABI 正确: {e}")
            sys.exit(1)

        logger.info(f"Bot 启动成功 | 账户地址: {self.address} | 套利对数量: {self.pair_count}")
        if self.pair_count == 0:
            logger.warning("⚠️ 未检测到套利对配置，请先调用 setPairConfig 注入子弹！")

    @staticmethod
    def _init_web3(rpc_url):
        if not rpc_url:
            return None
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            return w3
        except Exception as e:
            logger.warning(f"节点初始化失败: {rpc_url} | {e}")
            return None

    def _get_active_w3(self):
        """
        🎯 核心优化：被动式容灾切换。
        平时直接返回活动实例，绝不在主循环中调用耗费额度的 w3.is_connected()！
        """
        if self.use_primary and self.w3_primary:
            return self.w3_primary
        if self.w3_secondary:
            return self.w3_secondary
        if self.w3_primary:
            return self.w3_primary
        raise ConnectionError("❌ 致命错误：主备 RPC 节点全部失效！")

    def _switch_node(self):
        """当发生网络请求异常时，被动触发切换"""
        if self.use_primary:
            if self.w3_secondary:
                logger.warning("⚠️ 主节点异常，正在切换到备用节点...")
                self.use_primary = False
            else:
                logger.error("⚠️ 主节点异常，但未配置有效的备用节点，继续尝试使用主节点...")
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
        
        # 1. 获取 Nonce（对 L2 来说，pending 非常安全）
        nonce = w3.eth.get_transaction_count(self.address, 'pending')
        
        # 2. 估算 Gas，并乘以安全系数防止 Out Of Gas 回滚
        gas_estimate = function_call.estimate_gas({'from': self.address})
        gas_limit = int(gas_estimate * gas_multiplier)
        gas_price = w3.eth.gas_price

        # 🎯 核心 Bug 修复：必须调用 build_transaction 才能把 executeArbitrage 的 calldata 打包进交易！
        tx = function_call.build_transaction({
            'chainId': 42161,
            'from': self.address,
            'nonce': nonce,
            'gas': gas_limit,
            'gasPrice': gas_price,
        })

        # 3. 签名并极速打入 Arbitrum 定序器
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"🚀 闪电贷套利交易已成功开火！Hash: {tx_hash.hex()}")
        return tx_hash

    def run(self):
        logger.info("📡 监听雷达已开启，正在进行超频盲冲检测...")
        while True:
            try:
                w3 = self._get_active_w3()
                
                # 1. 调用 checkAllOpportunities 获取链下 0 Gas 利润模拟
                result = self.contract.functions.checkAllOpportunities().call()
                best_tiers, best_profits, directions = result

                executed = False
                for i in range(self.pair_count):
                    profit_raw = best_profits[i]
                    if profit_raw == 0:
                        continue
                    
                    # 归一化利润
                    profit_normalized = self._normalize_profit(profit_raw, i)
                    if profit_normalized < MIN_PROFIT_THRESHOLD:
                        continue

                    logger.info(
                        f"🔥 发现高换手套利利润！ | pairId={i} | 最佳档位={best_tiers[i]} | "
                        f"估算净利润={profit_normalized:.6f} | 方向={'A→B' if directions[i] else 'B→A'}"
                    )

                    tier_idx = best_tiers[i]
                    if tier_idx == 99:
                        continue

                    # 🎯 核心 Bug 修复：从本地 config 配置中安全提取对应档位，彻底规避 Solidity 元组越界
                    tiers = PAIR_BORROW_TIERS.get(i)
                    if not tiers or tier_idx >= len(tiers):
                        logger.error(f"❌ 档位读取越界: pairId={i}, tierIdx={tier_idx}")
                        continue
                    borrow_amount = tiers[tier_idx]

                    # 发起套利执行
                    self._send_transaction(
                        self.contract.functions.executeArbitrage(
                            i,
                            borrow_amount,
                            directions[i],
                            True # 默认优先走 Balancer 0% 息渠道
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
                # 被动触发节点切换，让程序自适应自愈，绝对不崩盘！
                self._switch_node()
                time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    bot = ArbitrageBot()
    bot.run()
    