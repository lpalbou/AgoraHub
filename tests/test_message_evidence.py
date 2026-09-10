from __future__ import annotations
import hashlib
import pytest
from agora.db import Database
from agora.hub.service import HubError, HubService
from agora.models import PostMessage

def test_live_peer_message_is_stamped_but_self_operator_hub_and_retracted_are_not_peer_proof():
    hub=HubService(Database(':memory:'),rate_per_minute=600); op,_=hub.register_agent('op','Op',operator=True); lead,_=hub.register_agent('lead','Lead'); peer,_=hub.register_agent('peer','Peer'); hub.create_channel(op,'audit',private=False); hub.join_channel(lead,'audit',None); hub.join_channel(peer,'audit',None)
    review=hub.post_message(peer,'audit',PostMessage(body='Peer review found the current roadmap acceptable.'))
    stamped=hub._validate_evidence('audit',[{'kind':'message','ref':f'audit#{review.seq}'}],agent_id='lead')[0]
    assert stamped['sender']=='peer' and stamped['ref']==f'audit#{review.seq}' and stamped['verified']
    hub.retract_message(peer,'audit',review.id)
    with pytest.raises(HubError,match='live message'): hub._validate_evidence('audit',[{'kind':'message','ref':review.id}],agent_id='lead')
    with pytest.raises(HubError): hub._validate_evidence('audit',[{'kind':'message','ref':'other#1'}],agent_id='lead')


def test_peer_review_message_enables_delegate_delivery_without_duplicate_artifact():
    hub=HubService(Database(':memory:'),rate_per_minute=600)
    op,_=hub.register_agent('op','Op',operator=True,mission='operate'); lead,_=hub.register_agent('lead','Lead',mission='deliver'); peer,_=hub.register_agent('peer','Peer',mission='review')
    hub.create_channel(op,'audit',private=False); hub.join_channel(lead,'audit',None); hub.join_channel(peer,'audit',None)
    hub.set_delegation('lead',['reporting'],scope='audit')
    root=hub.post_message(op,'audit',PostMessage(title='Commission',body='deliver',status='open',to=['lead']))
    task=f'task:msg-{root.seq}'; text='The integrated roadmap keeps this finding accountable.'
    f=hub.fs_write(lead,'audit','ROADMAP.md',content=text,description='final')
    artifact={'path':'ROADMAP.md','version':f.version,'sha256':hashlib.sha256(text.encode()).hexdigest(),'excerpt':text}
    finding=f'finding:msg-{root.seq}:one'
    hub.store_set(lead,'audit',finding,{'kind':'task-finding-v1','task':{'channel':'audit','key':task},'state':'accepted','source':f'audit#{root.seq}','evidence':[{'kind':'store','ref':task}],'contract':'A substantive accepted finding that the roadmap must retain.'},expect_version=0)
    row=hub.db.store_get('audit',finding)
    hub.store_set(lead,'audit',finding,{**row.value,'state':'disposed','disposition':'incorporated','artifact':artifact,'disposition_evidence':[{'kind':'fs','ref':f'ROADMAP.md@{f.version}'}]},expect_version=row.version)
    hub.store_set(lead,'audit','plan:delivery',{'scope':'review then final delivery'},expect_version=0)
    base={'kind':'fs','ref':f'ROADMAP.md@{f.version}'}
    plan={'kind':'store','ref':'plan:delivery'}
    with pytest.raises(HubError,match='uncontested delivery'):
        hub.post_message(lead,'audit',PostMessage(title='Final',body='truthful report',status='resolved',reply_to=root.id,data={'evidence':[base,plan]}))
    assert not hub.db.replies_to(root.id), 'baseline must not append a false delivery'
    self_note=hub.post_message(lead,'audit',PostMessage(body='my own review'))
    operator_note=hub.post_message(op,'audit',PostMessage(body='operator review'))
    hub_note=hub.db.insert_message('audit','hub',kind='message',status='fyi',urgency='inbox',title='',body='hub notice',data=None,reply_to=None)
    for note in (self_note,operator_note,hub_note):
        with pytest.raises(HubError,match='uncontested delivery'):
            hub.post_message(lead,'audit',PostMessage(title='Final',body='truthful report',status='resolved',reply_to=root.id,data={'evidence':[base,plan,{'kind':'message','ref':note.id}]}))
    assert not hub.db.replies_to(root.id), 'non-peer citations must not append a false delivery'
    review=hub.post_message(peer,'audit',PostMessage(title='Cold review',body='Reviewed the current roadmap and found it ready.',status='fyi'))
    report=hub.post_message(lead,'audit',PostMessage(title='Final',body='truthful report',status='resolved',reply_to=root.id,data={'evidence':[base,plan,{'kind':'message','ref':review.id}]}))
    assert hub.db.store_get('audit',task).value['report']==f'audit#{report.seq}'
    assert hub.db.store_get('audit',task).value['status']=='delivered'
    assert len([m for m in hub.db.replies_to(root.id) if m.status.value=='resolved'])==1
    assert report.data['evidence'][-1]['ref']==f'audit#{review.seq}'
