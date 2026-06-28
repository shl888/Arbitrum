# Arbitrage Bot - Arbitrum 套利机器人

## 简介
自动监控 Arbitrum 链上 10 组套利对，通过 Balancer V2 0% 闪电贷执行套利。

## 环境变量
| 变量名 | 说明 |
|--------|------|
| `ARBITRUM_RPC_URL_1` | 主 RPC 节点地址 |
| `ARBITRUM_RPC_URL_2` | 备用 RPC 节点地址 |
| `OPERATOR_PRIVATE_KEY` | 操作者钱包私钥 (0x开头) |
| `CONTRACT_ADDRESS` | 已部署的套利合约地址 |
| `MIN_PROFIT_THRESHOLD` | 最小利润门槛 (默认 0.0001 ETH) |
| `CHECK_INTERVAL` | 检测间隔秒数 (默认 1.2) |

## 运行
```bash
# 本地运行
pip install -r requirements.txt
python src/bot.py

# Docker 运行
docker-compose up -d
