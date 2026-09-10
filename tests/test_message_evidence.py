from __future__ import annotations
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
