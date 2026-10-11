"""Run in a disposable AstrBot image, never in the production process."""
import asyncio
import sys
from types import SimpleNamespace as NS
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astrbot_plugin_command_router.main import CommandRouterPlugin, CommandInfo, CommandBrief, CommandParser
from astrbot_plugin_n8n.main import MyPlugin

UID='demo_sender'
class Event:
    def __init__(self,text,platform='demo_qq_official',sender=UID):
        self.message_str=text;self.unified_msg_origin=platform+':FriendMessage:'+sender;self.sender=sender;self._has_send_oper=False;self.is_at_or_wake_command=True;self.extra={};self.stopped=False
    def get_sender_id(self):return self.sender
    def is_admin(self):return True
    def set_extra(self,k,v):self.extra[k]=v
    def get_extra(self,k,d=None):return self.extra.get(k,d)
    def plain_result(self,text):return text
    def stop_event(self):self.stopped=True
    def get_sender_name(self):return '隔离测试'
class Bridge(MyPlugin):
    def __init__(self):self.commands=[]
    async def dispatch(self,event,message):self.commands.append(message);yield 'accepted'
class Model:
    def __init__(self,response):self.response=response;self.n=0
    async def submit(self,*a,**kw):
        self.n+=1
        return {'plugins':['astrbot_plugin_n8n']} if self.n==1 else self.response

def info():
    return CommandInfo(handler_full_name='fake.maa_task',handler_name='maa_task',plugin='astrbot_plugin_n8n',plugin_display_name='MAA',module_path='fake',description=MyPlugin.maa_task.__doc__,type='command',parent_signature='',parent_group_handler='',original_command='maa',current_fragment='maa',effective_command='maa',aliases=[],permission='member',enabled=True,is_group=False,has_conflict=False,reserved=False,sub_commands=[])
def router(response):
    target=Bridge();p=CommandRouterPlugin(NS(),{'allowed_senders':['demo_qq_official|'+UID],'activate_by_wake':False})
    p.parser.plugin_desc={'astrbot_plugin_n8n':'MAA'};p.parser.plugin_contain={'astrbot_plugin_n8n':[1]};p.parser.commands={1:info()}
    p.parser.brief_map={1:CommandBrief(full_description=MyPlugin.maa_task.__doc__,command_name='maa',aliases=[],args={'task':'str','stage':'str','medicine':'int'},id=1)}
    p.parser.plugin_meta={'astrbot_plugin_n8n':NS(star_cls=target,activated=True)};p.llm=Model(response)
    return p,target
async def main():
    for text,params,expected in [
        ('帮我完成明日方舟今天的每日任务',{'task':'daily','stage':'','medicine':0},'maa daily'),
        ('帮我打开明日方舟刷取关卡',{'task':'fight','stage':'','medicine':0},'maa run fight'),
        ('帮我在明日方舟吃两瓶理智药刷1-7',{'task':'fight','stage':'1-7','medicine':2},'maa run fight stage=1-7 medicine=2')]:
        p,target=router({'matched':True,'id':1,'parameters':params,'confidence':0.99})
        out=[r async for r in p.core_handler(Event(text))]
        assert target.commands==[expected], (out,target.commands)
        print('路由通过',text,target.commands)
    for label,resp in [
        ('低置信度',{'matched':True,'id':1,'parameters':{'task':'fight'},'confidence':0.1}),
        ('越界ID',{'matched':True,'id':9,'parameters':{},'confidence':1}),
        ('类型错误',{'matched':True,'id':1,'parameters':{'task':'fight','medicine':'2'},'confidence':1}),
        ('擅自吃药',{'matched':True,'id':1,'parameters':{'task':'fight','medicine':2},'confidence':1})]:
        p,target=router(resp);out=[r async for r in p.core_handler(Event('帮我打开明日方舟刷关卡'))]
        assert not target.commands;print('拒绝通过',label,out)
    p,target=router({'matched':True,'id':1,'parameters':{'task':'fight','medicine':2},'confidence':1})
    out=[r async for r in p.core_handler(Event('帮我在明日方舟吃几瓶药刷关卡'))]
    assert '明确' in out[0] and not target.commands
    out=[r async for r in p.core_handler(Event('两瓶'))]
    assert target.commands==['maa run fight medicine=2'], (out,target.commands)
    print('数量追问补全通过')
    p,target=router({});out=[r async for r in p.core_handler(Event('帮我刷明日方舟',platform='小号',sender='other_sender'))]
    assert not out and not target.commands;print('NapCat 拒绝通过')
    parser=CommandParser(NS(get_registered_star=lambda _:NS(activated=True,desc='MAA')),['astrbot_plugin_n8n:maa'])
    parser._build_dicts(info(),{'fake.maa_task':{'task':'daily','stage':'','medicine':0}})
    assert parser.brief_map[1].args=={'task':'str','stage':'str','medicine':'int'}
    print('默认参数类型提取通过')
    print('隔离验证完成，未执行 Webhook / MAA / QQ 发送')
if __name__=='__main__':asyncio.run(main())
