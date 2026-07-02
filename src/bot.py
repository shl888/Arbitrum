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
    PAIR_MIN_PROFIT_OVERRIDE,  # 🆕 导入每组套利对的专属独立门槛字典
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

        # 🆕 3. 贷源决策脑：存储强行改走 Aave 备弹仓的套利对 {pairId: False}
        self.use_balancer_override = {}

        # 4. 获取初始可工作的节点，读取钱包信息
        active_w3 = self._get_active_w3()
        self.account = active_w3.eth.account.from_key(PRIVATE_KEY)
        self.address = self.account.address

        # 5. 安全读取并初始化 pairCount
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

    # 🆕 6. 贷源心跳探针：零 Gas 实时嗅探 Balancer 银行的库存
    def _probe_lender_liveness(self):
        logger.info("🏥 正在对主银行（Balancer Vault）进行实时库存与心跳健康探测...")
        w3 = self._get_active_w3()
        
        # 物理代币与金库地址
        weth_addr = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
        wbtc_addr = Web3.to_checksum_address("0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f")
        balancer_vault = Web3.to_checksum_address("0xBA12222222228d8Ba445958a75A0704d566BF2C8")
        
        # 极简 ERC20 只读 ABI
        min_erc20_abi = [
            {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
        ]
        
        try:
            weth_contract = w3.eth.contract(address=weth_addr, abi=min_erc20_abi)
            wbtc_contract = w3.eth.contract(address=wbtc_addr, abi=min_erc20_abi)
            
            # 读取金库的真实可用储备
            vault_weth = weth_contract.functions.balanceOf(balancer_vault).call()
            vault_wbtc = wbtc_contract.functions.balanceOf(balancer_vault).call()
            
            logger.info(f"  [金库状态] 💚 Balancer WETH 库存: {vault_weth / 10**18:.2f} | WBTC 库存: {vault_wbtc / 10**8:.4f}")
            
            # 如果金库发生不可抗力导致余额干涸（比如小于 0.1 WETH），直接启动全局自愈拉黑，强制走 Aave！
            if vault_weth < 10**17:
                logger.warning("  ⚠️ 警告：Balancer 金库 WETH 库存异常干涸！系统已触发全局预防拉黑，改走 Aave V3 备弹仓！")
                for i in range(self.pair_count):
                    self.use_balancer_override[i] = False
            else:
                # 状态良好，确保清空历史拉黑
                self.use_balancer_override.clear()
                
        except Exception as e:
            logger.warning(f"  ⚠️ 金库心跳探测发生异常 (节点可能瞬时延迟): {e}。系统将默认信赖当前通道状态。")

    # 🎯 自检诊断雷达：自检阶段同样使用动态 contract，保障 9 组参数配置全部通畅
    def _run_diagnostics(self):
        # 🆕 先行触发银行主动心跳嗅探
        self._probe_lender_liveness()
        
        logger.info("⚙️ 正在启动'装填全自检诊断雷达'，逐一测试各套利对的链上连通性...")
        healthy_count = 0
        for i in range(self.pair_count):
            try:
                pair_config = self.contract.functions.pairs(i).call()
                pool_a, pool_b = pair_config[0], pair_config[1]
                if pool_a == "0x0000000000000000000000000000000000000000":
                    continue # 完美包容空指针情况
                protocol_a, protocol_b = pair_config[2], pair_config[3]
                fee_a, fee_b = pair_config[6], pair_config[7]
                token_borrow, token_alt = pair_config[8], pair_config[9]
                
                tiers = PAIR_BORROW_TIERS.get(i)
                if not tiers:
                    logger.error(f"  [套利对 {i}] ❌ 异常：config.py 未配置 PAIR_BORROW_TIERS")
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
                logger.warning(f"  👉 解决办法：请检查配置，并在 Remix 重新调用 setPairConfig 覆盖！")
        
        logger.info(f"📊 自检完成：共检测到配置的有效套利对中，有 {healthy_count} 组处于完美健康状态。")

    # 🎯 动态解剖器：非阻塞地在最新状态（latest）执行 eth_call 重放，并支持 Hex data 自动解密
    def _get_revert_reason(self, tx_hash) -> str:
        w3 = self._get_active_w3()
        try:
            tx = w3.eth.get_transaction(tx_hash)
            if not tx or 'blockNumber' not in tx:
                return "无法获取该笔失败交易的链上数据"
                
            replay_tx = {
                'to': tx.get('to'),
                'from': tx.get('from'),
                'value': tx.get('value', 0),
                'data': tx.get('input', '0x'),
                'gas': tx.get('gas'),
                'nonce': tx.get('nonce')
            }
            
            if tx.get('gasPrice') is not None:
                replay_tx['gasPrice'] = tx.get('gasPrice')
            elif tx.get('maxFeePerGas') is not None:
                replay_tx['maxFeePerGas'] = tx.get('maxFeePerGas')
                replay_tx['maxPriorityFeePerGas'] = tx.get('maxPriorityFeePerGas')

            replay_tx = {k: v for k, v in replay_tx.items() if v is not None}

            w3.eth.call(replay_tx, 'latest')
            return "在链下重放模拟中成功（极其诡异，建议检查是否为偶发性滑点）"
        except Exception as e:
            # 🎯 提取并解密原始 json-rpc 回滚 Hex Data
            raw_data = None
            if hasattr(e, 'data'):
                raw_data = e.data
            elif isinstance(e.args, tuple) and len(e.args) > 0 and isinstance(e.args[0], dict):
                raw_data = e.args[0].get('data')

            if raw_data:
                if isinstance(raw_data, str) and raw_data.startswith('0x'):
                    try:
                        if raw_data.startswith('0x08c379a0'):
                            decoded = w3.codec.decode(['string'], bytes.fromhex(raw_data[10:]))
                            return f"解密底层报错: {decoded[0]}"
                        else:
                            return f"原始报错 Hex: {raw_data}"
                    except Exception as decode_err:
                        return f"解析报错Hex失败: {raw_data} | Error: {decode_err}"
                return f"底层异常原始数据: {raw_data}"

            err_msg = str(e)
            if "execution reverted:" in err_msg:
                return err_msg.split("execution reverted:")[-1].strip()
            return err_msg

    # 🎯 异步非阻塞收据追踪，一旦发现某个对子在链上 Revert 2次，自动熔断看守
    def _check_pending_receipts(self):
        if not self.pending_txs:
            return

        w3 = self._get_active_w3()
        active_pending = []

        for tx_hash, pair_id, send_time in self.pending_txs:
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                
                if receipt is None:
                    if time.time() - send_time < 60:
                        active_pending.append((tx_hash, pair_id, send_time))
                    else:
                        logger.warning(f"⏳ 交易超时未被定序器打包，放弃跟踪。Hash: {tx_hash.hex()}")
                    continue
                
                status = receipt.get('status')
                
                if status == 1:
                    logger.info(f"💰 [大吉大利！] 链上套利交易已成功确认并平仓获利！Hash: {tx_hash.hex()}")
                    self.fail_counters[pair_id] = 0
                elif status == 0:
                    self.fail_counters[pair_id] = self.fail_counters.get(pair_id, 0) + 1
                    revert_reason = self._get_revert_reason(tx_hash)
                    
                    logger.warning(
                        f"❌ [链上回退] 交易在链上执行失败 (Revert) | pairId={pair_id} |\n"
                        f"   👉 [详情] 连续失败计数: {self.fail_counters[pair_id]}/2 | 报错原因: {revert_reason} | Hash: {tx_hash.hex()}"
                    )
                    
                    # 🆕 觉醒自愈：如果解剖出是 Balancer 银行出的硬伤，立刻在下一轮强行拉黑，不关套利对禁闭！
                    if "BAL" in revert_reason or "Balancer" in revert_reason:
                        self.use_balancer_override[pair_id] = False
                        logger.error(
                            f"🔄 [雷达动态调整] 检测到 Balancer 银行硬伤回滚: {revert_reason}！\n"
                            f"   👉 系统已强行拉黑该对子 {pair_id} 的 Balancer 接口，下一轮扫描直接强制走 Aave 备用弹仓发射！"
                        )
                        self.fail_counters[pair_id] = 0 # 既然定位并排除了病因，清空连续失败数，下轮继续打！
                    
                    # 触发 10 分钟禁闭熔断
                    elif self.fail_counters[pair_id] >= 2:
                        self.jail_until[pair_id] = time.time() + 600
                        self.fail_counters[pair_id] = 0
                        logger.error(
                            f"🛑 [!!! 紧急熔断 !!!] 套利对 {pair_id} 连续 2 次非银行回退（滑点问题）！\n"
                            f"   👉 已自动将该套利对拉入【冷冻看守所】隔离 10 分钟！"
                        )

            except Exception as e:
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
            
            self.pending_txs.append((tx_hash, pair_id, time.time()))
            return tx_hash
        except Exception as e:
            logger.error(f"❌ [开火失败] 交易在签名或发送阶段发生错误，放弃本次发射: {e}")
            return None

    def _process_pair_matrix(self, pair_id: int, profits_true: list, profits_false: list) -> bool:
        """
        🎯 核心决策模块：全量矩阵分析
        针对第 pair_id 组套利对的双向利润，从【档位0到最大档位】顺序扫描，
        寻找第一个满足“安全优先（滑点最小）”且跑赢【该对子自定义专属/全局默认】门槛的档位进行击发！
        """
        if not profits_true or not profits_false:
            return False

        # 🆕 动态精准控险：优先读取该套利对专属的自定义门槛
        threshold = PAIR_MIN_PROFIT_OVERRIDE.get(pair_id)
        
        # 🛡️ 极速防漏空阻断：如果该对子在 config.py 中漏配了，则给出一个绝对安全的默认门槛
        if threshold is None:
            threshold_type = PAIR_THRESHOLD_TYPE.get(pair_id, 'WETH')
            threshold = 0.0003 if threshold_type == 'WETH' else 0.000008

        # 1. 优先扫描检测正向 (True: A → B)
        for tier_idx, profit_raw in enumerate(profits_true):
            if profit_raw == 0:
                continue
            
            profit_normalized = self._normalize_profit(profit_raw, pair_id)
            if profit_normalized >= threshold:
                direction_str = 'A→B'
                logger.info(
                    f"🔥 [矩阵复筛通过] 发现高价值套利机会！ | pairId={pair_id} | "
                    f"锁定【最小安全档位 {tier_idx}】 | 预计利润={profit_normalized:.6f} (对齐独立门槛={threshold:.6f}) | 方向={direction_str}"
                )
                return self._fire_arbitrage(pair_id, tier_idx, True, profit_normalized, direction_str)

        # 2. 其次扫描检测反向 (False: B → A)
        for tier_idx, profit_raw in enumerate(profits_false):
            if profit_raw == 0:
                continue
            
            profit_normalized = self._normalize_profit(profit_raw, pair_id)
            if profit_normalized >= threshold:
                direction_str = 'B→A'
                logger.info(
                    f"🔥 [矩阵复筛通过] 发现高价值套利机会！ | pairId={pair_id} | "
                    f"锁定【最小安全档位 {tier_idx}】 | 预计利润={profit_normalized:.6f} (对齐独立门槛={threshold:.6f}) | 方向={direction_str}"
                )
                return self._fire_arbitrage(pair_id, tier_idx, False, profit_normalized, direction_str)

        return False

    def _fire_arbitrage(self, pair_id: int, tier_idx: int, direction: bool, profit_normalized: float, direction_str: str) -> bool:
        tiers = PAIR_BORROW_TIERS.get(pair_id)
        if not tiers or tier_idx >= len(tiers):
            logger.error(f"❌ 资金档位读取越界: pairId={pair_id}, tierIdx={tier_idx}")
            return False
        borrow_amount = tiers[tier_idx]

        # 🆕 觉醒动态贷源：默认走 Balancer(True)，一旦其在诊断或上一轮被拉黑，下一轮起直接变成 False 走 Aave 开火！
        use_balancer = self.use_balancer_override.get(pair_id, True)

        tx_hash = self._send_transaction(
            function_call=self.contract.functions.executeArbitrage(
                pair_id,
                borrow_amount,
                direction,
                use_balancer  # 👈 动态决策
            ),
            pair_id=pair_id,
            borrow_amount=borrow_amount,
            profit_normalized=profit_normalized,
            direction_str=direction_str
        )
        
        # 降级容灾：如果在本地/前置构建阶段被拦截（依然作为最后防线保留）
        if tx_hash is None:
            current_jail_time = self.jail_until.get(pair_id, 0)
            if time.time() < current_jail_time:
                return False
                
            logger.info(
                f"🔄 [贷源自愈降级] pairId={pair_id} | 签名/前置校验遇阻，已将 Balancer 列入冷宫并改走 Aave 重试！"
            )
            # 顺手把 Balancer 强制拉黑
            self.use_balancer_override[pair_id] = False
            
            self._send_transaction(
                function_call=self.contract.functions.executeArbitrage(
                    pair_id,
                    borrow_amount,
                    direction,
                    False  # 改走 Aave
                ),
                pair_id=pair_id,
                borrow_amount=borrow_amount,
                profit_normalized=profit_normalized,
                direction_str=direction_str
            )
        return True

    def run(self):
        logger.info("📡 雷达扫描已开启，正在进行全量二维矩阵初筛/超频盲冲检测...")
        
        # 🆕 新增：每隔大约 1 小时，自动在后台悄悄重置并重新做一次“金库健康心跳嗅探”
        last_heartbeat_time = time.time()
        
        while True:
            try:
                # 🎯 每次扫描前，先异步结算所有排队中的交易结果，更新看守所名单
                self._check_pending_receipts()

                # 🆕 动态定时体检：每隔 1 小时，主动重新测一下 Balancer 和 Aave 是否健康
                if time.time() - last_heartbeat_time > 3600:
                    self._probe_lender_liveness()
                    last_heartbeat_time = time.time()

                # 🎯 获取全量利润矩阵（54 种可能性的纯数据）
                matrix = self.contract.functions.checkAllOpportunities().call()

                executed = False
                for i in range(self.pair_count):
                    jail_time = self.jail_until.get(i, 0)
                    if time.time() < jail_time:
                        continue

                    if i >= len(matrix):
                        continue

                    profits_true = matrix[i][0]
                    profits_false = matrix[i][1]

                    # 🎯 进入矩阵决策分析核心进行计算、复筛与击发
                    if self._process_pair_matrix(i, profits_true, profits_false):
                        executed = True
                        # 🎯【终极防踩踏安全锁】：打一枪就跑！
                        # 只要在一轮内成功开火了任意一组，立刻 break 终止！100% 物理避免同区块 Nonce 冲突锁死！
                        break

                if not executed:
                    sys.stdout.write('.')
                    sys.stdout.flush()

                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                logger.info("\n🚫 用户手动中断，套利机器人已安全下线。")
                break
            except Exception as e:
                logger.error(f"🚨 主循环异常: {e}")
                self._switch_node()
                time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    bot = ArbitrageBot()
    bot.run()
    