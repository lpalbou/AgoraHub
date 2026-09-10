from __future__ import annotations

import hashlib
import pytest
from fastapi.testclient import TestClient
from agora.db import Database
from agora.hub.app import create_app
from agora.hub.service import HubError, HubService
from agora.models import PostMessage, Status

def lab():
    hub=HubService(Database(':memory:'), rate_per_minute=600); op,_=hub.register_agent('op','Op',operator=True); lead,_=hub.register_agent('lead','Lead'); hub.create_channel(op,'room',private=False); hub.join_channel(lead,'room',None)
    root=hub.post_message(op,'room',PostMessage(title='T',body='b',status=Status.open,to=['lead'])); return hub,op,lead,root,f'task:msg-{root.seq}'

def test_pending_is_read_only_blocker_and_retracted_source_refuses():
    hub,op,lead,root,task=lab(); key=f'finding:{task[5:]}:x'
    hub.store_set(lead,'room',key,{'kind':'task-finding-v1','task':{'channel':'room','key':task},'state':'accepted','source':f'room#{root.seq}','evidence':[{'kind':'store','ref':task}],'contract':'A sufficiently substantive pending finding contract.'},expect_version=0)
    before=hub.db.store_get('room',task).version; got=hub.prepare_task_delivery(lead,'room',task)
    assert got['blockers'][0]['read']['arguments']['key']==key and hub.db.store_get('room',task).version==before and not hub.db.replies_to(root.id)
    hub.retract_message(op,'room',root.id)
    with pytest.raises(HubError,match='live canonical'): hub.prepare_task_delivery(lead,'room',task)

def test_integrated_ref_is_ready_without_posting_and_nonmember_refuses():
    hub,op,lead,root,task=lab(); text='A current artifact has enough quoted content.'; f=hub.fs_write(lead,'room','ROADMAP.md',content=text,description='x'); art={'path':f.path,'version':f.version,'sha256':hashlib.sha256(text.encode()).hexdigest(),'excerpt':text}; key=f'finding:{task[5:]}:x'
    hub.store_set(lead,'room',key,{'kind':'task-finding-v1','task':{'channel':'room','key':task},'state':'accepted','source':f'room#{root.seq}','evidence':[{'kind':'store','ref':task}],'contract':'A sufficiently substantive integrated finding contract.'},expect_version=0); row=hub.db.store_get('room',key); hub.store_set(lead,'room',key,{**row.value,'state':'disposed','disposition':'incorporated','artifact':art,'disposition_evidence':[{'kind':'fs','ref':'ROADMAP.md@1'}]},expect_version=row.version)
    got=hub.prepare_task_delivery(lead,'room',task); assert got['post_message']['reply_to']==root.id and got['integrated_fsrefs'][0]['sha256']==art['sha256'] and not hub.db.replies_to(root.id)
    outsider,_=hub.register_agent('out','Out')
    with pytest.raises(HubError): hub.prepare_task_delivery(outsider,'room',task)


def test_stale_integrated_artifact_returns_read_only_blocker():
    hub,op,lead,root,task=lab(); text='A current artifact has enough quoted content.'; f=hub.fs_write(lead,'room','ROADMAP.md',content=text,description='x'); art={'path':f.path,'version':f.version,'sha256':hashlib.sha256(text.encode()).hexdigest(),'excerpt':text}; key=f'finding:{task[5:]}:stale'
    hub.store_set(lead,'room',key,{'kind':'task-finding-v1','task':{'channel':'room','key':task},'state':'accepted','source':f'room#{root.seq}','evidence':[{'kind':'store','ref':task}],'contract':'A sufficiently substantive stale-artifact finding contract.'},expect_version=0)
    row=hub.db.store_get('room',key); hub.store_set(lead,'room',key,{**row.value,'state':'disposed','disposition':'incorporated','artifact':art,'disposition_evidence':[{'kind':'fs','ref':'ROADMAP.md@1'}]},expect_version=row.version)
    hub.fs_write(lead,'room','ROADMAP.md',content=text+' revised',description='x')
    got=hub.prepare_task_delivery(lead,'room',task)
    assert got['blockers'][0]['key']==key and 'post_message' not in got and not hub.db.replies_to(root.id)


def test_source_alias_and_delivered_task_refuse_preparation():
    hub,op,lead,root,task=lab(); row=hub.db.store_get('room',task)
    hub.db.store_set('room',task,{**row.value,'source':root.id},'hub',row.version)
    with pytest.raises(HubError,match='live canonical'): hub.prepare_task_delivery(lead,'room',task)
    row=hub.db.store_get('room',task); hub.db.store_set('room',task,{**row.value,'source':f'room#{root.seq}','status':'delivered'},'hub',row.version)
    with pytest.raises(HubError,match='only an open'): hub.prepare_task_delivery(lead,'room',task)


def test_http_preparation_returns_mcp_post_message_evidence_shape():
    client=TestClient(create_app(db_path=':memory:',admin_key='admin',rate_per_minute=600))
    def reg(name, operator=False):
        r=client.post('/agents',json={'id':name,'operator':operator},headers={'Authorization':'Bearer admin'}); assert r.status_code==200; return {'Authorization':'Bearer '+r.json()['api_key']}
    op,lead=reg('op',True),reg('lead'); assert client.post('/channels',json={'name':'room','private':False},headers=op).status_code==200; assert client.post('/channels/room/join',json={},headers=lead).status_code==200
    root=client.post('/channels/room/messages',json={'title':'T','body':'b','status':'open','to':['lead']},headers=op); assert root.status_code==200; root=root.json()
    r=client.get(f"/channels/room/tasks/task:msg-{root['seq']}/delivery-preparation",headers=lead)
    assert r.status_code==200, r.text; got=r.json()
    assert got['post_message']=={'channel':'room','reply_to':root['id'],'status':'resolved','evidence':[]}
