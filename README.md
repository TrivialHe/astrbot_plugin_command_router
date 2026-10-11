# Ones MAA 维护版

这是 TrivialHe 针对本机 AstrBot → n8n → maa-cli 工作流维护的 fork。保留原作者与 AGPL-3.0 许可证；上游说明保留在本文末尾。

只路由 `astrbot_plugin_n8n:maa`，需安装 [MAA 桥接维护版](https://github.com/TrivialHe/astrbot_plugin_n8n)。明确执行请求才会路由；模型无法擅自增加药量或关卡。模糊药量先追问，五分钟内可补全或取消。

在插件配置中将 `allowed_senders` 填为 `平台ID|发送者UID` 列表，默认为空；配置会话模型或 `text_provider_id`。请按自己的平台填写，不要复制他人的标识。

隔离验证需要两个仓库作为同级目录，不挂载生产配置、不连接网络：

```sh
docker run --rm --network none --read-only --tmpfs /tmp --tmpfs /AstrBot/data \
  -v "$PWD:/review:ro" --entrypoint python soulter/astrbot:latest \
  /review/astrbot_plugin_command_router/test_runtime.py
```

此 fork 属于个人工作流适配，不向原仓库提交 PR。

---

# 智能指令路由器

一个基于人工智能的自然语言指令解析插件，允许用户使用自然语言调用 AstrBot 中已注册的任何指令。

> 如果本插件启用期间增加了新的插件，需要重载本插件以应用。其它情况都无需手动重载本插件，插件列表不匹配会自动尝试更新。

## ✨ 核心特性

- **🤖 智能意图识别**：利用大型语言模型（LLM）理解用户自然语言，准确匹配对应指令
- **🔧 双重触发模式**：
  - **全局模式**：自动解析所有消息（可配置唤醒条件）
  - **指令模式**：通过 `/解析` 或 `/parse` 命令显式调用
- **🔒 权限安全继承**：自动检查原始指令的权限设置，防止越权访问
- **📝 自动参数提取**：从用户消息中智能提取并转换指令所需参数
- **🔄 全插件兼容**：支持所有符合 AstrBot 插件规范的指令

## 📦 安装方法

> 如果你的AstrBot版本`>= 4.18.2`，请手动下载对应beta版本。

1. **下载插件**
   ```bash
   # 从 GitHub 克隆或下载插件压缩包
   git clone https://github.com/PyuraMazo/astrbot_plugin_command_router.git
   ```

2. **安装到 AstrBot**
   - 将整个插件目录复制到 `AstrBot/data/plugins/astrbot_plugin_command_router/`
   - 或通过 AstrBot 管理面板的插件市场安装

3. **配置 LLM 供应商**
   - 在插件配置页面设置 `text_provider_id`
   - 或确保当前会话已有可用的 LLM 供应商

## ⚙️ 配置说明

插件提供以下配置选项，可在 AstrBot 管理面板的插件配置页面进行设置：

### 基础配置
| 配置项 | 类型 | 默认值 | 描述                                          |
|--------|------|--------|---------------------------------------------|
| `text_provider_id` | 字符串 | 空 | **优先使用的 LLM 供应商 ID**<br>留空时自动使用当前会话的 LLM 供应商 |
| `enable_global_match` | 布尔值 | `true` | **启用全局匹配模式**<br>开启后将对符合条件的消息进行自动解析          |
| `activate_by_wake` | 布尔值 | `true` | **唤醒触发限制**<br>仅当消息包含唤醒前缀或@机器人时才触发全局模式       |
| `matched_tips` | 布尔值 | `false` | **匹配成功提示**<br>匹配成功后向用户显示识别的指令和参数            |
| `always_llm` | 布尔值 | `false` | **总是唤醒LLM**<br>不论是否匹配到指令都最终把文本信息传输给当前会话的LLM |

## 🚀 使用指南


### 全局模式（自动解析）
当配置 `启用全局匹配模式: true` 时：

```
用户: @机器人 查询帮助
机器人: [自动调用内置help指令]
```

当配置 `启用全局匹配模式: false` 时：

```
用户: 给XXX授权
机器人: [自动调用内置op指令，解析参数：XXX]
```

### 指令模式（显式调用）
使用 `/解析` 或 `/parse` 命令：

```
用户: /解析 取消给XXX的授权
机器人: [自动调用内置deop指令，解析参数：XXX]

```
