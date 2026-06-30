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
        temp_contract = active_w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI
        )
        try:
            self.pair_count = temp_contract.functions.pairCount().call()
        except Exception as e:
            logger.error(f"❌ 启动失败：无法读取 pairCount，请确保合约地址和 ABI 正确: {e}")
            sys.exit(1)

        logger.info(f"🎉 Bot 启动成功 | 账户地址: {self.address} | 套利对数量: {self.pair_count}")
        if self.pair_count == 0:
            logger.warning("⚠️ 警告：未检测到套利对配置，请先调用 setPairConfig 注入套利对参数！")
        else:
            # 启动自检诊断雷达
            self._run_diagnostics()

    @property
    def contract(self):
        """
        🎯 属性黑魔法：实现“动态合约绑定”。
        每次调用 self.contract 时，都会自动使用当前最健康、已切换的活动 w3 实例来创建合约对象。
        彻底规避 Web3.py 合约实例的“单节点制绑定死锁”！
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

    # 🎯 自检诊断雷达：自检阶段同样使用动态 contract，保障 10 组参数配置全部通畅
    def _run_diagnostics(self):
        logger.info("⚙️ 正在启动'装填全自检诊断雷达'，逐一测试 10 组套利对的链上连通性...")
        healthy_count = 0
        for i in range(self.pair_count):
            try:
                pair_config = self.contract.functions.pairs(i).call()
                pool_a, pool_b = pair_config[0], pair_config[1]
                protocol_a, protocol_b = pair_config[2], pair_config[3]
                fee_a, fee_b = pair_config[6], pair_config[7]
                token_borrow, token_alt = pair_config[8], pair_config[9]
                
                tiers = PAIR_BORROW_TIERS.get(i)
                if not tiers:
                    logger.error(f"  [套利对 {i}] ❌ 异常：Python 端 config.py 未配置该套利对的 PAIR_BORROW_TIERS")
                    continue
                borrow_amt = tiers[0]  # 用第一档测试即可
                
                output_alt = self.contract.functions.getOutputAmount(
                    pool_a, protocol_a, token_borrow, token_alt, borrow_amt, fee_a
                ).call()
                
                self.contract.functions.getOutputAmount(
                    pool_b, protocol_b, token_alt, token_borrow, output_alt, fee_b
                ).call()
                
                healthy_count += 1
                logger.info(f"  [套利对 {i}] ✅ 状态健康，参数配置已就位！")
            except Exception as e:
                logger.error(f"  [套利对 {i}] ❌ 状态异常！该套利对在链上调用会发生 Revert！报错原因: {e}")
                logger.warning(f"  👉 解决办法：请检查你在 Remix 写入第 {i} 组数据时，是否把池子地址或代币地址输错了。请直接在 Remix 里对 pairId={i} 重新调用 setPairConfig 进行覆盖，无需重新部署合约！")
        
        logger.info(f"📊 自检完成：共 {self.pair_count} 组套利对，其中 {healthy_count} 组处于完美健康状态。")

    def _send_transaction(self, function_call, pair_id: int, borrow_amount: int, profit_normalized: float, direction_str: str, gas_limit: int = 1200000):
        """
        🚀 极速直发模式 (Direct Fire Mode)：
        一刀切除耗时 100ms 的 estimate_gas (精筛) 步骤，
        直接使用预设的 120 万 Gas 限制（EVM未消耗完的部分会自动退还，不需担心多扣费），
        在【复筛】通过后，以最快速度直接签名并发射，实现雷霆一击！
        """
        w3 = self._get_active_w3()
        
        # 1. 尝试获取 Nonce (如果连不上，向上抛出触发节点自愈切换)
        try:
            nonce = w3.eth.get_transaction_count(self.address, 'pending')
        except Exception as e:
            logger.error(f"❌ [网络异常] 无法获取 Nonce，准备触发节点主备自愈切换: {e}")
            raise e

        # 2. 🎯 极限升级：彻底切除拖慢速度的 estimate_gas 精筛流程，直接以 0 延迟构建真实交易
        try:
            gas_price = w3.eth.gas_price

            tx = function_call.build_transaction({
                'chainId': 42161,
                'from': self.address,
                'nonce': nonce,
                'gas': gas_limit,      # 🎯 直接强行硬编码 Gas 上限，0 毫秒延迟，未用完的 Gas 会在执行后退回
                'gasPrice': gas_price,
            })

            # 3. 🎯 修复：将 signed_tx.raw_transaction 完美修改为 Web3.py 标准的驼峰命名 signed_tx.rawTransaction！
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            logger.info(
                f"🚀 [大吉大利，雷霆发射！] 闪电贷套利交易已直接打入 Arbitrum 定序器！\n"
                f"   👉 [交易详情] pairId={pair_id} ({direction_str}) | 借款数量={borrow_amount} | "
                f"预计纯利润={profit_normalized:.6f} | 交易Hash: {tx_hash.hex()}"
            )
            return tx_hash
        except Exception as e:
            logger.error(f"❌ [开火失败] 交易在签名或发送阶段发生未知错误: {e}")
            raise e

    def run(self):
        # 初筛时，会对全量套利对，双向，3个不同档位的资金进行利润检测。不考虑实际滑点
        logger.info("📡 雷达扫描已开启，正在进行全量初筛/超频盲冲检测...")
        while True:
            try:
                # 1. 🎯 初筛：调用 checkAllOpportunities
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

                    # 🎯 2. 复筛：本地通过最小利润阈值进行过滤，保障利润覆盖 Gas 并过滤微小噪音
                    if profit_normalized < threshold:
                        continue

                    direction_str = 'A→B' if directions[i] else 'B→A'
                    logger.info(
                        f"🔥 [复筛通过] 发现高价值套利机会！ | pairId={i} | 最佳资金档位={best_tiers[i]} | "
                        f"估算利润={profit_normalized:.6f} | 方向={direction_str}"
                    )

                    tier_idx = best_tiers[i]
                    if tier_idx == 99:
                        continue

                    tiers = PAIR_BORROW_TIERS.get(i)
                    if not tiers or tier_idx >= len(tiers):
                        logger.error(f"❌ 资金档位读取越界: pairId={i}, tierIdx={tier_idx}")
                        continue
                    borrow_amount = tiers[tier_idx]

                    # 🎯 3. 开火：直接雷霆开枪，不经过 estimate_gas 精筛，在合约内用安全 revert 兜底
                    self._send_transaction(
                        function_call=self.contract.functions.executeArbitrage(
                            i,
                            borrow_amount,
                            directions[i],
                            True
                        ),
                        pair_id=i,
                        borrow_amount=borrow_amount,
                        profit_normalized=profit_normalized,
                        direction_str=direction_str
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
                # 只有真正的 RPC 崩溃，才会触发主备自动切换
                self._switch_node()
                time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    bot = ArbitrageBot()
    bot.run()