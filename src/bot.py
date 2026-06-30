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

        # 🎯 2. 初始化看守所、失败计数器、未决交易队列（熔断避险机制）
        self.jail_until = {}       # 存储每个对子关禁闭的结束时间戳 {pairId: timestamp}
        self.fail_counters = {}    # 存储每个对子连续链上失败的计数器 {pairId: count}
        self.pending_txs = []      # 存储已经发出但还未确认的交易收据队列 [(tx_hash, pair_id, send_time)]

        # 3. 获取初始可工作的节点，读取钱包信息
        active_w3 = self._get_active_w3()
        self.account = active_w3.eth.account.from_key(PRIVATE_KEY)
        self.address = self.account.address

        # 4. 安全读取并初始化 pairCount
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
        """动态合约绑定，自愈核心"""
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
                borrow_amt = tiers[0]
                
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

    # 🎯 核心升级：异步非阻塞收据追踪，一旦发现某个对子在链上连续 Revert 5次，自动强行熔断
    def _check_pending_receipts(self):
        if not self.pending_txs:
            return

        w3 = self._get_active_w3()
        active_pending = []

        for tx_hash, pair_id, send_time in self.pending_txs:
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                
                # 交易仍在排队中
                if receipt is None:
                    # 容灾：如果在 L2 上发了 60 秒都还没出收据，可能发生卡死，丢弃不再跟踪，防止内存队列膨胀
                    if time.time() - send_time < 60:
                        active_pending.append((tx_hash, pair_id, send_time))
                    else:
                        logger.warning(f"⏳ 交易超时未被定序器打包，放弃跟踪其收据。Hash: {tx_hash.hex()}")
                    continue
                
                # 收到链上执行回执！
                status = receipt.get('status')
                
                if status == 1:
                    logger.info(f"💰 [大吉大利！] 链上套利交易已成功确认并平仓获利！Hash: {tx_hash.hex()}")
                    # 只要有一次成功，立刻清空该对子的失败计数
                    self.fail_counters[pair_id] = 0
                elif status == 0:
                    self.fail_counters[pair_id] = self.fail_counters.get(pair_id, 0) + 1
                    logger.warning(
                        f"❌ [链上回退] 交易在链上执行失败 (Revert) | pairId={pair_id} | "
                        f"连续失败计数: {self.fail_counters[pair_id]}/5 | Hash: {tx_hash.hex()}"
                    )
                    
                    # 🎯 核心熔断判断：触发禁闭 10 分钟 (600秒)
                    if self.fail_counters[pair_id] >= 5:
                        self.jail_until[pair_id] = time.time() + 600
                        self.fail_counters[pair_id] = 0 # 进看守所后，清空计数器
                        logger.error(
                            f"🛑 [!!! 紧急熔断 !!!] 套利对 {pair_id} 连续 5 次开火回退！"
                            f"   👉 极大概率发生滑点剧烈偏移、池子被毁或合约异常！"
                            f"   👉 合约已自动将该套利对拉入【冷冻看守所】隔离 10 分钟！期间绝不进行初筛、不进行开火！"
                        )

            except Exception as e:
                # 发生网络读取错误，保留在队列里，下一轮继续探测
                active_pending.append((tx_hash, pair_id, send_time))

        self.pending_txs = active_pending

    def _send_transaction(self, function_call, pair_id: int, borrow_amount: int, profit_normalized: float, direction_str: str, gas_limit: int = 1200000):
        w3 = self._get_active_w3()
        
        try:
            nonce = w3.eth.get_transaction_count(self.address, 'pending')
        except Exception as e:
            logger.error(f"❌ [网络异常] 无法获取 Nonce，准备触发节点主备自愈切换: {e}")
            raise e

        try:
            gas_price = w3.eth.gas_price
            max_fee_per_gas = int(gas_price * 2.0)
            max_priority_fee_per_gas = w3.to_wei(0.01, 'gwei')

            tx = function_call.build_transaction({
                'chainId': 42161,
                'from': self.address,
                'nonce': nonce,
                'gas': gas_limit,     
                'maxFeePerGas': max_fee_per_gas,
                'maxPriorityFeePerGas': max_priority_fee_per_gas,
            })

            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            logger.info(
                f"🚀 [大吉大利，雷霆发射！] 闪电贷套利交易已直接打入 Arbitrum 定序器！\n"
                f"   👉 [交易详情] pairId={pair_id} ({direction_str}) | 借款数量={borrow_amount} | "
                f"预计纯利润={profit_normalized:.6f} | 交易Hash: {tx_hash.hex()}"
            )
            
            # 🎯 登记到未决交易队列中，以便在后台异步监测它的执行结果，防止死循环空枪放血
            self.pending_txs.append((tx_hash, pair_id, time.time()))
            return tx_hash
        except Exception as e:
            logger.error(f"❌ [开火失败] 交易在签名或发送阶段发生错误，放弃本次发射: {e}")
            return None

    def run(self):
        logger.info("📡 雷达扫描已开启，正在进行全量初筛/超频盲冲检测...")
        while True:
            try:
                # 🎯 核心优化：每次循环的最开头，先异步结算所有排队中的交易结果，更新看守所名单
                self._check_pending_receipts()

                result = self.contract.functions.checkAllOpportunities().call()
                best_tiers, best_profits, directions = result

                executed = False
                for i in range(self.pair_count):
                    # 🎯 核心优化：检查当前套利对是否被关了禁闭
                    jail_time = self.jail_until.get(i, 0)
                    if time.time() < jail_time:
                        # 尚在 10 分钟禁闭期内，完全忽略不予理睬，0 开枪消耗！
                        continue

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

                    # 2. 复筛过滤
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

                    # 3. 极速直发开火
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