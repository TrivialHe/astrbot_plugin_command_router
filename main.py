import inspect
import asyncio
import time
import json
import re
from collections.abc import AsyncIterator, Coroutine
from typing import Any

from pydantic import BaseModel

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Reply
from astrbot.core.star import StarMetadata, command_management
from astrbot.core.star.filter.command import CommandFilter

PLUGIN_ALTER_EVENT = False
try:
    from astrbot.core.star.register import (
        register_on_plugin_loaded,
        register_on_plugin_unloaded,
    )

    PLUGIN_ALTER_EVENT = True
except ImportError:
    PLUGIN_ALTER_EVENT = False


# 自定义异常
class PluginBaseException(Exception):
    pass


class NoProviderException(PluginBaseException):
    pass


class CommandInfo(BaseModel):
    handler_full_name: str
    handler_name: str
    plugin: str
    plugin_display_name: str | None
    module_path: str
    description: str
    type: str
    parent_signature: str
    parent_group_handler: str
    original_command: str
    current_fragment: str
    effective_command: str
    aliases: list[str]
    permission: str
    enabled: bool
    is_group: bool
    has_conflict: bool
    reserved: bool
    sub_commands: list["CommandInfo"]


class CommandBrief(BaseModel):
    full_description: str
    command_name: str
    aliases: list[str]
    args: dict[str, Any]
    id: int


class LLMResponse(BaseModel):
    matched: bool
    id: int | None = None
    parameters: dict[str, Any] | None = None
    confidence: float | None = None
    reason: str | None = None


class CommandParser:
    def __init__(self, context: Context, allowed_commands=None):
        self.context = context
        self.allowed_commands = set(allowed_commands or [])

        self.max_id = 1  # 当前id记录
        self.id_dict: dict[int, str] = {}  # 指令的id对应关系
        self.commands: dict[int, CommandInfo] = {}  # 指令的数据集
        self.brief_map: dict[int, CommandBrief] = {}  # 指令的简介信息
        self.plugin_meta: dict[str, StarMetadata] = {}  # 插件元素据
        self.plugin_desc: dict[str, str] = {}  # 插件的描述
        self.plugin_contain: dict[str, list[int]] = {}  # 一个插件包含的所有指令

    async def initialize(self):
        self.clear()
        cmd_list = await command_management.list_commands()
        descriptors = command_management._collect_descriptors(include_sub_commands=True)
        params = {
            desc.handler_full_name: desc.filter_ref.handler_params
            for desc in descriptors
            if isinstance(desc.filter_ref, CommandFilter)
        }
        for cmd in cmd_list:
            info = CommandInfo(**cmd)
            self._build_dicts(info, params)

    def clear(self):
        self.max_id = 1
        self.id_dict = {}
        self.commands = {}
        self.brief_map = {}
        self.plugin_meta = {}
        self.plugin_desc = {}
        self.plugin_contain = {}

    def _build_dicts(
        self,
        info: CommandInfo,
        params: dict[str, Any],
    ):
        allowed = lambda item: f"{item.plugin}:{item.original_command}" in self.allowed_commands and item.enabled and not item.has_conflict
        if (not info.is_group and not allowed(info)) or (info.is_group and not any(allowed(sub) for sub in info.sub_commands)):
            return
        meta = self.context.get_registered_star(info.plugin)
        if meta is None or not info.enabled or not meta.activated:
            return

        self.plugin_meta[info.plugin] = meta
        self.plugin_desc[info.plugin] = meta.desc

        start = self.max_id
        if info.is_group:
            main_desc = info.description
            for sub_command in info.sub_commands:
                if not sub_command.enabled or sub_command.has_conflict or f"{sub_command.plugin}:{sub_command.original_command}" not in self.allowed_commands:
                    continue
                args = params.get(sub_command.handler_full_name, {})
                arg = {
                    name: (args[name].__name__ if isinstance(args[name], type) else type(args[name]).__name__)
                    for name in args
                }
                self.id_dict[self.max_id] = (
                    f"{sub_command.plugin}:{sub_command.original_command}"
                )
                self.commands[self.max_id] = sub_command
                self.brief_map[self.max_id] = CommandBrief(
                    full_description=f"插件描述：{meta.desc}\n"
                    f"指令组描述：{main_desc}\n"
                    f"本指令描述：{sub_command.description}",
                    command_name=sub_command.current_fragment,
                    aliases=sub_command.aliases,
                    args=arg,
                    id=self.max_id,
                )
                self.max_id += 1
        else:
            if info.has_conflict or f"{info.plugin}:{info.original_command}" not in self.allowed_commands:
                return
            args = params.get(info.handler_full_name, {})
            arg = {
                name: (args[name].__name__ if isinstance(args[name], type) else type(args[name]).__name__)
                for name in args
            }

            self.id_dict[self.max_id] = f"{info.plugin}:{info.original_command}"
            self.commands[self.max_id] = info
            self.brief_map[self.max_id] = CommandBrief(
                full_description=f"插件描述：{meta.desc}\n指令描述：{info.description}",
                command_name=info.current_fragment,
                aliases=info.aliases,
                args=arg,
                id=self.max_id,
            )
            self.max_id += 1
        end = self.max_id
        plu = self.plugin_contain.get(info.plugin, [])
        if plu:
            plu.extend(list(range(start, end)))
        else:
            self.plugin_contain[info.plugin] = list(range(start, end))


class LLM:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        brief_map: dict[int, CommandBrief],
        plugin_desc: dict[str, str],
    ):
        self.context = context
        self.config = config
        self.brief_map = brief_map
        self.plugin_desc = plugin_desc
        self.response_pattern = re.compile(r"\{.*}", re.S)

    def build_prompt(self, user_message: str, **kwargs) -> str:
        plugins = kwargs.get("plugins", [])
        if not plugins:
            return f"""
                    你是一个智能指令解析器。请分析用户的消息，根据以下备选插件列表的插件描述，判断用户是否想要调用插件中的功能。
                    要求：最多选择3个。如果不符合任何插件的描述，返回空列表。
                    
                    备选插件列表（键为插件名，值为插件描述）：
                    {self.plugin_desc}

                    用户消息: "{user_message}"

                    请按以下格式返回：
                       {{
                         "plugins": [key1, key2, ...] // 插件名，list[str]
                       }}

                    请确保只返回JSON格式的字符串结果，不要有任何其他的文本信息。
                    """
        else:
            return f"""
                    你是一个智能指令解析器。请分析用户的消息，判断用户意图是否匹配以下指令的效果。
                    要求：
                    1. 只匹配一个指令
                    2. 无论是否有参数都返回字典类型（键值对）
                    3. 返回的参数符合指令要求的参数个数、类型
                    4. 如果一个参数类型是GreedyStr，说明接受一个以空格作为分隔符的含有多个内容的字符串

                    可用指令列表（args字典键是参数名，值是参数类型）：
                    {plugins}

                    用户消息: "{user_message}"

                    请按以下格式分析：
                    1. 如果匹配某个指令，请返回：
                       {{
                         "matched": true,
                         "id": xxx, // 匹配指令的唯一id, int
                         "parameters": {{'arg1': v1, 'arg2': v2, ...}}, // 匹配指令的参数, dict[str, Any]
                         "confidence": number  // 匹配置信度, float, 取值0 ~ 1
                       }}

                    2. 如果不匹配任何指令，请返回：
                       {{
                         "matched": false,
                         "reason": "xxx" // 不匹配的原因简要说明
                       }}

                    请确保参数提取准确，只返回JSON格式的字符串结果，不要有任何其他的文本信息。
                    """

    async def get_provider(self, umo: str):
        provider_id = self.config.get("text_provider_id", "")
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                raise NoProviderException("LLM供应商获取错误！")
        return provider_id

    async def submit(self, event: AstrMessageEvent, **kwargs):
        llm_resp = await asyncio.wait_for(self.context.llm_generate(
            chat_provider_id=await self.get_provider(event.unified_msg_origin),
            prompt=self.build_prompt(event.message_str, **kwargs),
            system_prompt="只路由用户明确要求现在执行的明日方舟任务。询问、否定、假设、引用指令不执行。只使用备选指令。不得猜测关卡或用药量；未明确要求用药时 medicine=0，关卡未指定 stage=空字符串。daily 不包含 fight。参数不确定返回 matched=false。",
        ), timeout=45)
        text_resp = llm_resp.completion_text.strip()
        start = text_resp.find("{")
        if start < 0:
            raise PluginBaseException("任务未识别，请明确要执行的任务、关卡及用药数量。")
        data, _ = json.JSONDecoder().raw_decode(text_resp[start:])
        if not isinstance(data, dict):
            raise PluginBaseException("模型返回格式无效。")
        return data


class CommandRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.wake_prefix = []

        self.always_llm = self.config.get("always_llm", False)
        self.enable_global_match = self.config.get("enable_global_match", True)
        self.activate_by_wake = self.config.get("activate_by_wake", True)

        self.parser = CommandParser(context, config.get("allowed_commands", ["astrbot_plugin_n8n:maa"]))
        self.pending = {}
        self.allowed_sessions = set(config.get("allowed_senders", []))
        self.llm: LLM | None = None

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass

    async def lazy_init(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        await self.parser.initialize()
        self.llm = LLM(
            self.context, self.config, self.parser.brief_map, self.parser.plugin_desc
        )

    def get_wake_prefix(self):
        cfg = self.context.get_config()
        self.wake_prefix = cfg.get("wake_prefix")

    def permitted(self, event):
        platform = str(event.unified_msg_origin).split(':', 1)[0]
        return platform + '|' + str(event.get_sender_id()) in self.allowed_sessions

    def prepare_request(self, event):
        if not self.permitted(event):
            return False, None
        text = event.message_str.strip()
        key = str(event.unified_msg_origin) + '|' + str(event.get_sender_id())
        pending = self.pending.get(key)
        if pending and time.monotonic() - pending[0] > 300:
            self.pending.pop(key, None); pending = None
        if pending and text in ('取消', '不用了', '算了'):
            self.pending.pop(key, None)
            return False, '已取消待补充的任务。'
        if pending and re.fullmatch(r'[0-9零一二两三四五六七八九十]+\s*(?:瓶|份|个)(?:理智药|药剂|药)?[，,。\s]*(?:[A-Za-z0-9@-]+)?', text):
            event.message_str = pending[1] + '。补充数量：' + text
            text = event.message_str
            self.pending.pop(key, None)
        if not re.search(r'明日方舟|方舟|\bmaa\b',text,re.I) or not re.search(r'帮我|请|执行|完成|刷|打开|启动|领取|公招|换班|购买|每日|日常', text):
            return False, None
        if re.search(r'^(?:请问|如何|怎么|为什么)|(?:不要|别|取消).*(?:执行|刷|打开|启动|完成)|(?:能不能|是否|假如|如果|例如|比如)',text):
            return False, None
        counts = re.findall(r'([0-9]{1,3}|[零一二两三四五六七八九十]{1,3})\s*(?:瓶|份|个)\s*(?:理智药|药剂|药)?',text)
        wants_medicine = bool(re.search(r'(?:吃|用|喝|服用|消耗).{0,8}(?:理智药|药|瓶)',text)) and not re.search(r'(?:不|别|禁止|无需).{0,4}(?:吃|用|喝|药)',text)
        if wants_medicine and not counts:
            self.pending[key] = (time.monotonic(),text)
            return False, '请明确理智药数量，例如回复“两瓶”。未指定关卡则刷上一次关卡。'
        return True, None

    def match_filter(self, event: AstrMessageEvent):
        if not self.permitted(event) or re.match(r'^/?(?:n8n|maa|解析|parse)(?:\s|$)',event.message_str):
            return False
        if (
            event._has_send_oper  # 已经有指令向用户发送过消息，触发指令的识别标志
            or not self.enable_global_match  # 未开启全局模式
            or not event.message_str  # 发送空文本
        ):
            return False

        # 唤醒触发
        if self.activate_by_wake:
            if not event.is_at_or_wake_command:
                return False

        return True

    def permission_filter(self, event: AstrMessageEvent, require: str):
        if require == "admin" and not event.is_admin():
            return False

        return True

    def describe_resp(self, resp: LLMResponse):
        return f"{self.parser.id_dict[resp.id].split(':', 1)[1]}，参数：{' '.join(str(v) for v in (resp.parameters or {}).values()) or '无'}"

    def reply(self, event: AstrMessageEvent, message: str):
        return event.plain_result(message)

    async def core_handler(self, event: AstrMessageEvent, **kwargs):
        ready, clarification = self.prepare_request(event)
        if clarification:
            event.stop_event()
            yield self.reply(event, clarification)
            return
        if not ready:
            return
        if self.llm is None:
            await self.lazy_init()
        plugins: list[str] = (await self.llm.submit(event))["plugins"]
        validated = []
        for i in plugins:
            if i in self.parser.plugin_desc:
                for j in self.parser.plugin_contain[i]:
                    validated.append(self.parser.brief_map[j].model_dump())
        if not validated:
            return
        brief_body = LLMResponse.model_validate(
            await self.llm.submit(event, plugins=validated)
        )
        if brief_body.matched:
            candidate_ids = {item['id'] for item in validated}
            if brief_body.id not in candidate_ids or brief_body.confidence is None or not 0.85 <= brief_body.confidence <= 1 or not isinstance(brief_body.parameters, dict):
                event.stop_event()
                yield self.reply(event, '任务识别不确定，请明确任务、关卡和用药数量。')
                return
            whole_body = self.parser.commands[brief_body.id]
            meta = self.parser.plugin_meta[whole_body.plugin]

            plugin_class = meta.star_cls
            function_name = whole_body.handler_name
            args = brief_body.parameters
            is_sync = (
                False if kwargs.get("try_again", True) and not meta.activated else True
            )
            if hasattr(plugin_class, function_name) and is_sync:
                if not meta.activated:
                    return

                actual_command = getattr(plugin_class, function_name)
                sig = inspect.signature(actual_command)
                try:
                    param = sig.bind(event, **args)
                    param.apply_defaults()
                    for name, value in param.arguments.items():
                        annotation = sig.parameters[name].annotation
                        if name != 'event' and annotation in (str, int, bool, float) and type(value) is not annotation:
                            raise TypeError('参数类型无效')
                    event.set_extra('ones_router_request', True)
                    if not self.permission_filter(event, whole_body.permission):
                        yield self.reply(
                            event,
                            f"成功匹配指令：{self.describe_resp(brief_body)}，但你无权调用。",
                        )
                        return
                    if self.config.get("matched_tips", False):
                        yield self.reply(
                            event, f"成功匹配指令：{self.describe_resp(brief_body)}。"
                        )

                    result = actual_command(**param.arguments)
                    if isinstance(result, Coroutine):
                        yield await result
                    elif isinstance(result, AsyncIterator):
                        async for res in result:
                            yield res
                except TypeError:
                    event.stop_event()
                    yield self.reply(event, "任务参数不完整或格式错误，请明确任务、关卡和用药数量。")
                    logger.warning("路由参数绑定失败")
                    return

            else:
                if kwargs.get("try_again", True):
                    logger.warn("指令状态异常，正在尝试同步...")
                    await self.lazy_init()
                    async for res in self.core_handler(event, try_again=False):
                        yield res
                else:
                    raise PluginBaseException("自动同步无效！")

    async def handle_meta_change(self, alter: StarMetadata = None):
        if alter is None:
            old = set(self.parser.plugin_meta)
            await self.lazy_init()
            new = set(self.parser.plugin_meta)
            if old == new:
                return "同步成功！未发现增删插件。"
            else:
                return f"同步成功！\n新增插件：{'、'.join(new - old) or '无'}\n删除插件：{'、'.join(old - new) or '无'}"
        else:
            await self.lazy_init()
            return f"同步成功！变动插件：{alter.name}"

    @filter.on_astrbot_loaded()
    async def lazy_initialize(self):
        """延迟初始化"""
        await self.lazy_init()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def global_parser(self, event: AstrMessageEvent):
        """全局响应模式"""
        if not self.match_filter(event):
            return

        try:
            sent = False
            async for res in self.core_handler(event):
                sent = True
                yield res

            # Only mark the event as handled (to block further handlers) if this plugin
            # actually sent a response. If `always_llm` is enabled we keep allowing
            # other handlers to run (don't mark as handled).
            event._has_send_oper = sent and not self.always_llm
        except PluginBaseException as e1:
            # We do send a reply in this error case, so mark the event handled
            # (unless always_llm is set).
            yield self.reply(event, str(e1))
            event._has_send_oper = True and not self.always_llm
            logger.error(e1, exc_info=True)
        except Exception as e2:
            yield self.reply(event, "自然语言解析暂时失败，请稍后重试或使用 /n8n maa 指令。")
            event.stop_event()
            logger.error(type(e2).__name__, exc_info=True)

    @filter.command("解析", alias={"parse"})
    async def command_parser(self, event: AstrMessageEvent):
        """指令响应模式"""
        try:
            sent = False
            async for res in self.core_handler(event):
                sent = True
                yield res

            # Same behavior as global_parser — only mark handled when something was sent.
            event._has_send_oper = sent and not self.always_llm
        except PluginBaseException as e1:
            # We do send a reply in this error case, so mark the event handled
            # (unless always_llm is set).
            yield self.reply(event, str(e1))
            event._has_send_oper = True and not self.always_llm
            logger.error(e1, exc_info=True)
        except Exception as e2:
            yield self.reply(event, "自然语言解析暂时失败，请稍后重试或使用 /n8n maa 指令。")
            event.stop_event()
            logger.error(type(e2).__name__, exc_info=True)

    if PLUGIN_ALTER_EVENT:

        @filter.on_plugin_loaded()
        async def plugin_loaded(self, metadata: StarMetadata):
            logger.info(await self.handle_meta_change(metadata))

        @filter.on_plugin_unloaded()
        async def plugin_unloaded(self, metadata: StarMetadata):
            logger.info(await self.handle_meta_change(metadata))
    else:

        @filter.command("同步", alias={"更新"})
        async def data_sync(self, event: AstrMessageEvent):
            """同步因新增插件导致的缓存列表不一致的情况，也可用作意外导致的不同步情况"""
            yield event.plain_result(await self.handle_meta_change())
