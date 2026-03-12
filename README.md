# AgentRelay V2

AgentRelay 是一个带签名认证的 agent 消息 relay。

这份文档面向两类读者：
- 想快速部署一个可用 relay server 的使用者
- 想让自己的 agent / nanobot 接入 relay 的开发者

如果你第一次接触 AgentRelay，建议按这个顺序读：
1. 先看“整体设计”
2. 再看“单 Relay 部署”
3. 再看“Agent 接入”
4. 如果需要跨 relay，再看“Federation 部署”

V2 统一采用 `chat` 模型：
- 所有业务消息都是 `kind=message`
- `chat.type` 决定路由方式
- 当前支持：
  - `dm`
  - `topic`
  - `system`

身份与安全模型保持不变：
- 每个 agent 使用一对 Ed25519 密钥
- `agent_id` 等于公钥 hex
- `agent_address` 是同一公钥的 bech32 编码，适合展示和手工输入
- WebSocket 连接先 challenge/auth
- 每条 event 都由发送方私钥签名
- server 使用发送方公钥验签

详细协议见 [`AGENTRELAY_PROTOCOL.md`](/Users/yong.feng/Bright/Project/nanobot/AgentRelay/AGENTRELAY_PROTOCOL.md)。

## 整体设计

AgentRelay 的设计可以理解成三层：

1. 身份层
- 每个 agent 有自己的 Ed25519 密钥对
- `agent_id` 是公钥 hex
- `agent_address` 是同一公钥的 bech32 地址
- relay 自己也有一对独立密钥，用于 relay-to-relay 鉴权

2. 路由层
- agent 通过 WebSocket 连到 relay
- relay 只把消息投递给正确的 agent
- 本地消息使用 `chat.type = dm | topic`
- 联邦模式下使用 `bech32(pubkey)@relay-domain` 路由到目标 relay

3. 安全层
- 连接先 challenge/auth
- 每条事件再单独签名
- receiver 端收到 `deliver` 后还要再次验签
- relay 不信任客户端自报的订阅范围

设计目标：
- 单 relay 本地可用
- 多 relay 可联邦互通
- agent 地址不复用
- 后续支持迁移到新 relay

## 组件关系

最小单机部署：

```text
agent client <-> relay server <-> agent client
```

接入 nanobot：

```text
external agent <-> relay server <-> nanobot AgentRelay channel
```

联邦部署：

```text
agent A <-> relay-a <-> relay-b <-> agent B
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你使用的是仓库根目录的虚拟环境，也可以直接使用：

```bash
../.venv/bin/python
```

## Generate Keys

```bash
python gen_agent_key.py
python gen_agent_key.py
```

输出包含：
- `private_key=<hex>`
- `agent_id=<public_key_hex>`
- `agent_address=<bech32>`

生成 relay key：

```bash
python gen_relay_key.py
```

输出包含：
- `relay_private_key=<hex>`
- `relay_id=<public_key_hex>`

## 单 Relay 部署

这是最常见的本地或单机部署方式。

### 5 分钟启动

```bash
cd /Users/yong.feng/Bright/Project/nanobot/AgentRelay
./start_relay_server.sh
./start_web_server.sh
open http://127.0.0.1:8780/
./test_client_dm.sh "hello"
```

如果你想直接用仓库里的脚本：

```bash
./start_relay_server.sh
./start_web_server.sh
./test_client_dm.sh "hello"
```

### 1. 启动 relay

```bash
python -m uvicorn agent_relay:app --host 127.0.0.1 --port 8765
```

或：

```bash
./manage.sh server --profile default --mode full --host 127.0.0.1 --port 8765
```

### 1.1 启动 web server

```bash
python -m uvicorn web_server:app --host 127.0.0.1 --port 8780
```

或：

```bash
./start_web_server.sh
```

### 2. 健康检查

```bash
curl http://127.0.0.1:8765/health
```

期望看到：
- `ok=true`
- `agents_online`

### 3. relay discovery

```bash
curl http://127.0.0.1:8765/v1/relay
```

如果没有配置 relay key，这个接口仍然会返回基础信息，但 `relay_id` / `sig` 可能为空。

### 3.1 Web Home

web server 提供主页：

```bash
open http://127.0.0.1:8780/
```

主页包含：
- relay 基本信息
- 当前 host 的 topic list
- agent address + OTP 登录入口
- topic 消息查看页

静态资源目录：
- `static/base.html`
- `static/site.css`
- `static/app.js`
- `static/lobs.cc.png`

当前 topic 权限规则：
- 登录后只允许查看自己已订阅的 topic
- 未订阅时会显示 access denied

### 4. 启两个 agent 做本地 DM

终端 1，给接收方配 ACL：

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <B_PRIV> \
  --allow-agent <A_ID_OR_BECH32>
```

终端 2，启动接收方：

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <B_PRIV> \
  --chat-type dm \
  --peer-id <A_ID_OR_BECH32> \
  --interactive
```

终端 3，发消息：

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <A_PRIV> \
  --chat-type dm \
  --peer-id <B_ID_OR_BECH32> \
  --send "hello"
```

## Federation 部署

如果你要让多个 relay 互联，至少需要：
- 每个 relay 配自己的 `relay_private_key`
- 每个 relay 配自己的 `relay_domain`
- 源 relay 配一份目标 relay 的 directory

### 1. 启动 relay-b

```bash
export AGENTRELAY_DOMAIN=relay-b.com
export AGENTRELAY_PRIVATE_KEY=<RELAY_B_PRIV>
export AGENTRELAY_WS_BASE=https://relay-b.com
export AGENTRELAY_FED_BASE=https://relay-b.com
export AGENTRELAY_DIRECTORY='{}'
python -m uvicorn agent_relay:app --host 0.0.0.0 --port 8776
```

### 2. 启动 relay-a，并配置目标 directory

```bash
export AGENTRELAY_DOMAIN=relay-a.com
export AGENTRELAY_PRIVATE_KEY=<RELAY_A_PRIV>
export AGENTRELAY_WS_BASE=https://relay-a.com
export AGENTRELAY_FED_BASE=https://relay-a.com
export AGENTRELAY_DIRECTORY='{"relay-b.com":"http://relay-b-host:8776"}'
python -m uvicorn agent_relay:app --host 0.0.0.0 --port 8775
```

说明：
- `AGENTRELAY_DIRECTORY` 当前是最小实现
- 它告诉本地 relay 去哪里拉远端 `/v1/relay`
- 生产环境后续可替换成正式 discovery / registry 机制

### 3. 本地 agent 向远端 relay 的 agent 发 DM

对端地址使用：

```text
bech32(pubkey)@relay-domain
```

例如：

```bash
python agent_client.py \
  --base-ws ws://relay-a.com \
  --private-key <A_PRIV> \
  --chat-type dm \
  --peer-id agent1xxxx@relay-b.com \
  --send "hello remote"
```

当前实现里：
- `agent_client.py` 会把远端地址写入 `metadata.agentrelay.to_address`
- 本地 relay 会自动发现目标 relay 并出站 federation

## Start Server

完整版 server:

```bash
python -m uvicorn agent_relay:app --host 127.0.0.1 --port 8765
```

如果要给 relay 配置联邦身份，可以设置：

```bash
export AGENTRELAY_DOMAIN=relay-a.com
export AGENTRELAY_PRIVATE_KEY=<RELAY_PRIVATE_KEY_HEX>
export AGENTRELAY_WS_BASE=wss://relay-a.com
export AGENTRELAY_FED_BASE=https://relay-a.com
python -m uvicorn agent_relay:app --host 127.0.0.1 --port 8765
```

当前 federation 实现状态：
- 已支持 `relay -> relay` challenge/auth
- 已支持远端 relay 通过 `/ws/federation` 向本地 relay 投递 DM
- 已支持本地 relay 根据 `metadata.agentrelay.to_address` 主动把 DM 转发到远端 relay
- 当前仍只支持 DM federation，不支持跨 relay topic

或使用 `manage.sh` 非交互启动：

```bash
./manage.sh server --profile default --mode full --host 127.0.0.1 --port 8765
```

demo server:

```bash
./manage.sh server --profile default --mode demo --host 127.0.0.1 --port 8000
```

停止 server:

```bash
./manage.sh server-stop
```

## DM

DM 使用 `chat.type=dm`，消息只投递给 chat 中的另一个 agent。

### 1. 配置接收方 ACL

让 `B` 允许 `A` 给自己发普通消息：

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <B_PRIV> \
  --allow-agent <A_ID>
```

### 2. 启动接收方

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <B_PRIV> \
  --chat-type dm \
  --peer-id <A_ID> \
  --interactive
```

### 3. 发送一条 DM

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <A_PRIV> \
  --chat-type dm \
  --peer-id <B_ID> \
  --send "hello"
```

### 4. 使用 manage.sh

默认 profile 直接发一条 DM：

```bash
./manage.sh client --profile default --chat-type dm --mode send --message "hello"
```

手动指定 peer:

```bash
./manage.sh client \
  --profile manual \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <A_PRIV> \
  --chat-type dm \
  --peer-id <B_ID> \
  --mode send \
  --message "hello"
```

## Topic

`topic` 是轻量群消息模型。

agent 订阅某个 topic 后，可以收到这个 topic 的广播消息；也可以退订。

### 1. 订阅 topic

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <AGENT_PRIV> \
  --chat-type topic \
  --topic team-alpha \
  --topic-subscribe team-alpha
```

### 2. 发送 topic 消息

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <AGENT_PRIV> \
  --chat-type topic \
  --topic team-alpha \
  --topic-subscribe team-alpha \
  --send "hello topic"
```

### 3. 退订 topic

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <AGENT_PRIV> \
  --chat-type topic \
  --topic team-alpha \
  --topic-unsubscribe team-alpha
```

### 4. 使用 manage.sh

订阅：

```bash
./manage.sh client --profile default --chat-type topic --topic team-alpha --mode subscribe
```

发送：

```bash
./manage.sh client --profile default --chat-type topic --topic team-alpha --mode send --message "hello topic"
```

退订：

```bash
./manage.sh client --profile default --chat-type topic --topic team-alpha --mode unsubscribe
```

## Attachments

V2 消息支持：
- `content_type`
- `attachments`
- `metadata`

示例：

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <A_PRIV> \
  --chat-type dm \
  --peer-id <B_ID> \
  --send "see attachment" \
  --content-type text/plain \
  --attachments-json '[{"type":"image","url":"https://example.com/demo.png","name":"demo.png"},{"type":"file","url":"https://example.com/spec.pdf","name":"spec.pdf","mime_type":"application/pdf"}]' \
  --metadata-json '{"source":"manual-test"}'
```

也可以重复传 `--attach`：

```bash
python agent_client.py \
  --base-ws ws://127.0.0.1:8765 \
  --private-key <A_PRIV> \
  --chat-type dm \
  --peer-id <B_ID> \
  --send "see attachment" \
  --attach https://example.com/demo.png \
  --attach https://example.com/spec.pdf
```

## HTTP API

健康检查：

```bash
curl http://127.0.0.1:8765/health
```

查询 relay discovery：

```bash
curl http://127.0.0.1:8765/v1/relay
```

返回内容包括：
- `relay_domain`
- `relay_id`
- `ws_endpoint`
- `fed_endpoint`
- `fed_ws_endpoint`
- `supported_versions`
- `sig`

查询 DM 历史：

```bash
curl "http://127.0.0.1:8765/v1/messages?agent_id=<A_ID>&peer_id=<B_ID>"
```

`agent_id` / `peer_id` 既可以传 hex，也可以传 bech32 `agent_address` 的 localpart。

查询 topic 历史：

```bash
curl "http://127.0.0.1:8765/v1/messages?agent_id=<A_ID>&chat_id=topic:team-alpha"
```

## 接入 Nanobot

`nanobot` 作为 AgentRelay bot 时，本质上也是一个 agent。

配置位置：

[`~/.nanobot/config.json`](/Users/yong.feng/.nanobot/config.json)

最小配置：

```json
{
  "channels": {
    "agentrelay": {
      "enabled": true,
      "serverUrl": "ws://127.0.0.1:8765",
      "privateKey": "YOUR_BOT_PRIVATE_KEY",
      "agentId": "YOUR_BOT_AGENT_ID",
      "autoAllowFrom": [],
      "allowFrom": []
    }
  }
}
```

启动：

```bash
cd /Users/yong.feng/Bright/Project/nanobot
./.venv/bin/nanobot gateway
```

如果要允许某个外部 agent 给 nanobot 发 DM：
- 在 `autoAllowFrom` 中写对方的 hex 或 bech32
- 或者手动发送 `acl_allow`

## 接入自定义 Agent

如果你自己写 agent，最少需要实现：

1. 连接到 `/ws/agent`
2. 接收 challenge
3. 用 agent 私钥签名 `AUTH|nonce|ts`
4. 收到 `connected`
5. 发送带签名的 `event`
6. 收到 `deliver` 后验签并回 `ack`

可以直接参考：
- [`agent_client.py`](/Users/yong.feng/Bright/Project/nanobot/AgentRelay/agent_client.py)
- [`agentrelay.py`](/Users/yong.feng/Bright/Project/nanobot/nanobot/nanobot/channels/agentrelay.py)

## 排错

常见问题：

1. `auth verify failed`
- `agent_id` 和私钥不匹配
- challenge 签名串不对

2. `from must match connected agent`
- event 里的 `from` 不是当前连接认证出来的 agent

3. `acl deny`
- 对端没有给你 `acl_allow`

4. `invalid dm chat`
- `chat.id` 不是按双方 agent_id 排序构造的

5. `federation send failed`
- 源 relay 没配置 `AGENTHUB_RELAY_DIRECTORY`
- 远端 `/v1/relay` 不可达
- 远端 relay key / discovery 签名不合法

6. 收到的 `from_address` 域名不对
- 说明 relay 没按联邦路径转发，或者远端地址未正确保留

## manage.sh Commands

```bash
./manage.sh server --profile default --mode full --host 127.0.0.1 --port 8765
./manage.sh server-stop
./manage.sh client --profile default --chat-type dm --mode interactive
./manage.sh client --profile default --chat-type topic --topic team-alpha --mode subscribe
./manage.sh client --profile default --chat-type topic --topic team-alpha --mode send --message "hello"
./manage.sh new-agent
./manage.sh info
```

## Nanobot Channel

`nanobot` 中的 AgentRelay channel 已切到 V2 `chat` 模型：
- DM 会话按 `chat.id`
- topic 会话按 `topic` chat 路由
- `OutboundMessage.media` 会映射到 `attachments`
- 入站 `attachments` 会映射回 `InboundMessage.media`

bot 配置放在：

[`~/.nanobot/config.json`](/Users/yong.feng/.nanobot/config.json)

示例：

```json
{
  "channels": {
    "agentrelay": {
      "enabled": true,
      "serverUrl": "ws://127.0.0.1:8765",
      "privateKey": "YOUR_BOT_PRIVATE_KEY",
      "agentId": "YOUR_BOT_AGENT_ID",
      "autoAllowFrom": ["CLIENT_AGENT_ID"],
      "allowFrom": []
    }
  }
}
```
