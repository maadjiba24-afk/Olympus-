"""M07 owned fixtures. Passing here never qualifies live promotion."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from olympus import config, ctxheat as heat, ctxheat_state as state, memory, usermem

REAL_REGISTRY_ENTRY = heat._registry_entry


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setenv('OLYMPUS_CTXHEAT','on')
    registry = heat._registry_entry()
    registry.update(status='active',last_tested='2020-01-01',next_review='2099-01-01',
                    outcome='OWNED MOCK ONLY: no deployment qualification')
    monkeypatch.setattr(heat,'_registry_entry',lambda:copy.deepcopy(registry))
    monkeypatch.setattr(heat,'PROVISIONAL',False)
    return 'tenant:a.b'


def seed(owner, text='Owned memory source', accepts=1):
    iid=usermem.add_memory(owner,type='project',content=text,confidence=.9)['id']
    assert heat.record(iid,'memory',retrieved=True,user=owner)
    for n in range(accepts):
        assert verify(owner,iid,event=f'{iid}-{n}')
    return iid


def verify(owner,iid,event='event-1',accepted=True,**changes):
    evidence={'owner':owner,'item':iid,'kind':'memory',
              'revision':heat.resolve_source(owner,'memory',iid)['revision'],
              'source':'direct_verify','run_id':'owned-run','event_id':event,
              'accepted':accepted,'observed_at':1700000000.0}
    evidence.update(changes)
    return heat.record_verifier_outcome(iid,accepted,source='direct_verify',kind='memory',
                                         user=owner,evidence=evidence)


def publish(owner,op='apply-1',gate=lambda before,after:True):
    proposals=heat.propose_pins(user=owner)
    result=heat.apply_pins(proposals,gate,user=owner,operation_id=op)
    return proposals,result


def snapshot(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('owner',['a.b','a@b','a-b','A.B','a b','é','e\u0301','x'*100+'1','x'*100+'2','../other','shared'])
def test_exact_owner_artifacts_and_copied_envelope_refusal(owned,owner):
    iid=seed(owner)
    assert heat.entry(iid,'memory',user=owner)['verifier_ok']==1
    other=owner+'-other'
    seed(other)
    assert str(state.path(owner)).casefold()!=str(state.path(other)).casefold()
    # Keep the deliberately long fixture; raw test IO needs native Windows
    # extended paths, while the actual API above uses held relative handles.
    source_path=heat.ledger_path(owner);other_path=heat.ledger_path(other)
    if os.name=='nt':
        source_path=Path('\\\\?\\'+str(source_path.absolute()))
        other_path=Path('\\\\?\\'+str(other_path.absolute()))
    raw=source_path.read_bytes()
    other_path.write_bytes(raw)
    assert heat.status(other)['status']=='unavailable'
    assert source_path.read_bytes()==raw


@pytest.mark.parametrize('bad',[None,'',True,7,[],{}])
def test_null_and_nonreference_items_are_refused_without_state(owned,bad):
    assert not heat.record(bad,bad,user=owned,retrieved=True)
    assert not state.path(owned).exists()


def test_ambient_exact_owner_and_blank_rejection(owned):
    with memory.user_context(owned):
        assert heat.scope_of(None)=='owner:'+owned
        assert heat.ledger_path()==heat.ledger_path(owned)
    assert heat.status('')['status']=='invalid_owner'


@pytest.mark.parametrize('owner',['shared','a.b'])
def test_legacy_is_unclaimed_and_preserved(owned,owner):
    legacy=config.MEMORY_DIR if owner=='shared' else config.MEMORY_DIR/'users'/memory.safe_id(owner)
    legacy.mkdir(parents=True)
    p=legacy/'context_heat.json';p.write_bytes(b'{legacy evidence}')
    before=snapshot(config.MEMORY_DIR)
    assert heat.status(owner)['status']=='unclaimed'
    assert not heat.record('x','wiki',retrieved=True,user=owner)
    assert snapshot(config.MEMORY_DIR)==before


@pytest.mark.parametrize('raw',[b'{',b'{"version":2,"version":2}',b'{"x":NaN}',b'['*40+b']'*40,b' '* (state.MAX_BYTES+1)],
                         ids=['malformed','duplicate','nonfinite','depth','oversized'])
def test_damaged_state_never_resets_or_quarantines(owned,raw):
    seed(owned)
    p=heat.ledger_path(owned);p.write_bytes(raw)
    before=snapshot(config.MEMORY_DIR)
    assert heat.status(owned)['status']=='unavailable'
    with pytest.raises(state.StateError):heat.entries(owned)
    assert not heat.record('x','wiki',retrieved=True,user=owned)
    assert snapshot(config.MEMORY_DIR)==before


def test_missing_initialized_authority_is_not_new_history(owned):
    seed(owned)
    p=heat.ledger_path(owned);p.rename(p.with_name('retained-state.json'))
    before=snapshot(config.MEMORY_DIR)
    assert heat.status(owned)['status']=='unavailable'
    assert not heat.record('x','wiki',retrieved=True,user=owned)
    assert snapshot(config.MEMORY_DIR)==before


def test_status_and_doctor_are_read_only_and_do_not_claim_self_reuse(owned):
    from olympus import doctor
    iid=seed(owned)
    heat.record(iid,'memory',reused=True,user=owned,avoided_cost_usd=2)
    with memory.user_context(owned):
        before=snapshot(config.MEMORY_DIR)
        assert heat.status()['status']=='available'
        row=doctor._liveness_context_heat()
        assert row['activated'] is False
        assert snapshot(config.MEMORY_DIR)==before


def test_verifier_requires_owned_completed_receipt_and_deduplicates(owned):
    iid=seed(owned,accepts=0)
    assert not heat.record_verifier_outcome(iid,True,source='direct_verify',kind='memory',user=owned)
    assert not verify(owned,iid,accepted='false')
    assert verify(owned,iid)
    first=heat.ledger_path(owned).read_bytes()
    assert verify(owned,iid)
    assert heat.ledger_path(owned).read_bytes()==first
    assert not verify(owned,iid,accepted=False)
    assert heat.entry(iid,'memory',user=owned)['verifier_ok']==1


def test_source_revision_changes_and_reverts_preserve_attribution(owned):
    iid=seed(owned,text='Owned version A')
    _,result=publish(owned);assert result.applied
    original=heat.resolve_source(owned,'memory',iid)['revision']
    usermem.touch(owned,iid)
    assert heat.applied_pins(owned)
    usermem.update_content(owned,iid,'Owned version B')
    assert not heat.applied_pins(owned)
    assert not heat.pinned_ids(heat.propose_pins(user=owned))
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    assert heat.entry(iid,'memory',user=owned)['verifier_ok']==0
    usermem.update_content(owned,iid,'Owned version A')
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    assert heat.resolve_source(owned,'memory',iid)['revision']==original
    assert heat.entry(iid,'memory',user=owned)['verifier_ok']==1
    assert len(state.read(owned)['retired'])==2


def test_recall_compares_actual_candidate_revision(owned):
    from olympus import recall
    iid=seed(owned)
    _,result=publish(owned);assert result.applied
    current=usermem.get_memory(owned,iid)
    stale=dict(current,content='different already-loaded content')
    other=dict(current,id='other',content='other content')
    scored=[(2,other),(1,stale)]
    assert recall._heat_order(heat,owned,scored)==scored
    assert recall._heat_order(heat,owned,[(2,other),(1,current)])[0][1]['id']==iid


def test_manual_or_underpriced_proposals_cannot_bypass_budget(owned):
    iid=seed(owned)
    called=[]
    result=heat.apply_pins([heat.PinProposal('fake','memory','add',1,0,100,'highest_value_density')],
                           lambda *a:called.append(a) or True,user=owned,operation_id='manual')
    assert not result.applied and not called
    with pytest.raises(ValueError):heat.propose_pins(user=owned,est_tokens={iid:.5})
    assert heat.est_tokens_for({'kind':'memory','id':'x'},{'x':.5})==1


@pytest.mark.parametrize('verdict',[False,1,'true',{'passed':True},None])
def test_gate_pass_is_strict_boolean(owned,verdict):
    seed(owned)
    proposals,result=publish(owned,gate=lambda *a:verdict)
    assert not result.applied and result.gate_called
    assert not heat.applied_pins(owned)


def test_gate_retry_is_nonreplaying_and_result_is_immutable(owned):
    seed(owned)
    calls=[]
    proposals,result=publish(owned,gate=lambda *a:calls.append(a) or True)
    assert result.applied and result.active and len(calls)==1
    assert heat.rollback_pins(user=owned,operation_id='rollback-1').reason=='rolled_back'
    replay=heat.apply_pins(proposals,lambda *a:calls.append(a) or True,user=owned,operation_id='apply-1')
    assert replay.applied and not replay.active and replay.pins==result.pins and len(calls)==1
    assert not heat.applied_pins(owned)


def test_interrupted_gate_remains_unconfirmed_without_replay(owned):
    seed(owned);proposals=heat.propose_pins(user=owned);calls=[]
    def crash(*args):
        calls.append(args);raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):heat.gate_pins(proposals,crash,user=owned,operation_id='crashed')
    result=heat.gate_pins(proposals,crash,user=owned,operation_id='crashed')
    assert result.reason=='evaluating' and len(calls)==1
    assert heat.status(owned,operation_id='crashed')['operation']['status']=='evaluating'
    assert not heat.apply_gate('crashed',user=owned).applied


def test_gate_cas_refuses_reentrant_changes(owned):
    iid=seed(owned)
    def change(*args):
        assert heat.record(iid,'memory',retrieved=True,user=owned)
        return True
    _,result=publish(owned,gate=change)
    assert not result.applied and result.reason=='stale'


def test_gate_cas_refuses_source_or_policy_change(owned,monkeypatch):
    iid=seed(owned)
    def change(*args):
        usermem.update_content(owned,iid,'Changed during benchmark')
        return True
    _,result=publish(owned,gate=change)
    assert result.reason=='stale' and not result.applied
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    assert verify(owned,iid,event='new-revision')
    _,result=publish(owned,op='policy-change',gate=lambda *a:monkeypatch.setenv('OLYMPUS_CTXHEAT','shadow') or True)
    assert result.reason=='stale' and not result.applied


def test_lost_publish_acknowledgement_confirms_receipt_without_gate_replay(owned,monkeypatch):
    seed(owned);proposals=heat.propose_pins(user=owned)
    assert heat.gate_pins(proposals,lambda *a:True,user=owned,operation_id='ack').reason=='qualified'
    real=state.Directory.publish
    def lost(directory,name,data):
        real(directory,name,data)
        raise OSError('owned post-replace failure')
    monkeypatch.setattr(state.Directory,'publish',lost)
    assert heat.apply_gate('ack',user=owned).reason=='publication_unconfirmed'
    monkeypatch.setattr(state.Directory,'publish',real)
    confirms=[];confirm=state.Directory.confirm
    def confirmed(directory,name):
        confirms.append(name);return confirm(directory,name)
    monkeypatch.setattr(state.Directory,'confirm',confirmed)
    assert heat.apply_gate('ack',user=owned).applied
    assert state.MARKER in confirms and state.STATE in confirms


def test_gate_status_tamper_and_verifier_digest_tamper_are_unavailable(owned):
    seed(owned);proposals=heat.propose_pins(user=owned)
    def crash(*a):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):heat.gate_pins(proposals,crash,user=owned,operation_id='pending')
    p=heat.ledger_path(owned);data=json.loads(p.read_bytes())
    data['operations']['pending']['status']='qualified'
    p.write_text(json.dumps(data),encoding='utf-8')
    assert heat.status(owned)['status']=='unavailable'
    data['operations']['pending']['status']='evaluating'
    next(iter(data['events'].values()))['evidence_digest']='0'*64
    p.write_text(json.dumps(data),encoding='utf-8')
    assert heat.status(owned)['status']=='unavailable'


def test_concurrent_records_are_serialized(owned):
    iid=seed(owned,accepts=0);outcomes=[]
    def worker():
        for _ in range(5):outcomes.append(heat.record(iid,'memory',retrieved=True,user=owned))
    threads=[threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(20);assert not thread.is_alive()
    assert outcomes==[True]*20
    assert heat.entry(iid,'memory',user=owned)['hits']==21


def test_default_registry_prevents_promotion_even_with_passing_gate(monkeypatch):
    monkeypatch.setenv('OLYMPUS_CTXHEAT','on')
    owner='owned-provisional';seed(owner);calls=[]
    _,result=publish(owner,gate=lambda *a:calls.append(a) or True)
    assert result.reason=='promotion_unqualified' and not calls


def test_real_cli_status_gate_apply_and_rollback(owned,capsys):
    from olympus import cli
    assert cli.main(['ctxheat','status','--owner',owned])==0
    assert json.loads(capsys.readouterr().out)['owner']==owned
    assert cli.main(['ctxheat','gate','--owner',owned,'--operation-id','cli-gate'])==1
    assert json.loads(capsys.readouterr().out)['reason']=='owned_benchmark_adapter_unavailable'
    seed(owned);_,result=publish(owned,op='cli-apply');assert result.applied
    assert cli.main(['ctxheat','apply','--owner',owned,'--operation-id','cli-apply'])==0
    assert json.loads(capsys.readouterr().out)['applied']
    assert cli.main(['ctxheat','rollback','--owner',owned,'--operation-id','cli-rollback'])==0
    assert json.loads(capsys.readouterr().out)['reason']=='rolled_back'


def test_hardlink_leaf_and_nonregular_state_refused(owned,tmp_path):
    seed(owned);p=heat.ledger_path(owned)
    retained=p.with_name('retained.json');p.rename(retained)
    os.link(retained,p)
    assert heat.status(owned)['status']=='unavailable'
    assert not heat.record('x','wiki',retrieved=True,user=owned)


def test_counter_overflow_and_finite_decay(owned):
    iid=seed(owned);entry=heat.entry(iid,'memory',user=owned)
    entry.update(last=1700000000.0,first=1700000000.0)
    start=heat.score(entry,now=1700000000.0)
    assert heat.score(entry,now=1700000000.0+30*86400)==pytest.approx(start/2)
    entry['hits']=10**10000
    assert not heat._entry_ok(entry)
    assert heat.score(entry,now=1700000000.0)>=0


def test_recall_does_not_credit_retrieved_old_revision_to_new_content(owned):
    from olympus import recall
    iid=seed(owned,accepts=0)
    selected=usermem.get_memory(owned,iid)
    before=heat.entry(iid,'memory',user=owned)
    usermem.update_content(owned,iid,'Edited after retrieval selection')
    recall._heat_record(heat,owned,[selected])
    assert heat.entry(iid,'memory',user=owned)==before
    assert not heat.propose_pins(user=owned)


def test_rollback_needs_no_registry_or_current_source(owned,monkeypatch):
    iid=seed(owned);_,result=publish(owned);assert result.applied
    usermem.tombstone(owned,iid)
    def unavailable():raise state.StateError('qualification_unavailable')
    monkeypatch.setattr(heat,'_registry_entry',unavailable)
    result=heat.rollback_pins(user=owned,operation_id='registry-down')
    assert result.reason=='rolled_back' and result.active
    assert state.read(owned)['pins']==[]
    assert heat.rollback_pins(user=owned,operation_id='registry-down').active


def test_old_applied_pointer_cannot_override_later_rollback(owned):
    seed(owned);_,result=publish(owned);assert result.applied
    old=state.read(owned)
    assert heat.rollback_pins(user=owned,operation_id='later-rollback').reason=='rolled_back'
    current=state.read(owned)
    current['pins']=old['pins'];current['gate']=old['gate']
    path=heat.ledger_path(owned);path.write_text(json.dumps(current),encoding='utf-8')
    assert heat.status(owned)['status']=='unavailable'
    with pytest.raises(state.StateError):heat.applied_pins(owned)


def test_completed_verifier_receipt_recovers_after_source_disappears(owned):
    iid=seed(owned)
    event=next(iter(state.read(owned)['events'].values()))
    evidence={k:v for k,v in event.items() if k not in ('evidence_digest','recorded_serial')}
    usermem.tombstone(owned,iid)
    before=heat.ledger_path(owned).read_bytes()
    assert heat.record_verifier_outcome(iid,True,source='direct_verify',kind='memory',user=owned,evidence=evidence)
    assert heat.ledger_path(owned).read_bytes()==before
    assert not heat.propose_pins(user=owned)


def test_recovery_does_not_claim_success_while_barrier_remains_failed(owned,monkeypatch):
    seed(owned);_,result=publish(owned);assert result.applied
    real=state.Directory.confirm
    def fail(directory,name):
        if name==state.STATE:raise OSError('owned persistent barrier failure')
        return real(directory,name)
    monkeypatch.setattr(state.Directory,'confirm',fail)
    replay=heat.apply_gate('apply-1',user=owned)
    assert not replay.applied and replay.reason=='unavailable'
    # Pure status is still able to report the visible historical receipt.
    assert heat.status(owned,operation_id='apply-1')['operation']['status']=='applied'


@pytest.mark.parametrize('field,value',[
    ('hits',True),('hits',-1),('hits',state.MAX_COUNT+1),
    ('avoided_cost',-1),('avoided_cost',float('inf')),('avoided_cost',101),
    ('avoided_latency',86400001),('first',state.MAX_TIME+1),
    ('last',float('nan')),('id',None),('kind',None),('provenance',[]),
],ids=['bool-counter','negative-counter','huge-counter','negative-cost','infinite-cost',
       'cost-cap','latency-cap','time-cap','nonfinite-time','null-id','null-kind','bad-provenance'])
def test_malformed_entry_fields_preserve_unavailable_authority(owned,field,value):
    iid=seed(owned)
    p=heat.ledger_path(owned);data=json.loads(p.read_bytes())
    data['entries']['memory:'+iid][field]=value
    p.write_text(json.dumps(data),encoding='utf-8');before=p.read_bytes()
    assert heat.status(owned)['status']=='unavailable'
    assert not heat.record('other','wiki',retrieved=True,user=owned)
    assert p.read_bytes()==before


def test_process_death_after_reservation_is_nonreplaying_on_restart(owned):
    seed(owned)
    source=Path(heat.__file__).resolve().parent.parent
    code='''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from olympus import config,ctxheat
config.MEMORY_DIR=Path(sys.argv[2])
registry=ctxheat._registry_entry()
registry.update(status='active',last_tested='2020-01-01',next_review='2099-01-01',outcome='OWNED MOCK ONLY: no deployment qualification')
ctxheat._registry_entry=lambda:dict(registry)
ctxheat.PROVISIONAL=False
ctxheat.gate_pins(ctxheat.propose_pins(user=sys.argv[3]),lambda *args:os._exit(23),user=sys.argv[3],operation_id='process-death')
raise RuntimeError('owned crash callback did not execute')
'''
    child=subprocess.run([sys.executable,'-I','-B','-c',code,str(source),str(config.MEMORY_DIR),owned],
                         capture_output=True,timeout=30)
    assert child.returncode==23,child.stderr.decode(errors='replace')[-1000:]
    stored=state.read(owned)['operations']['process-death']
    assert stored['status']=='evaluating'
    proposals=heat.ProposalSet(tuple(heat.PinProposal(item_id=row['id'],kind=row['kind'],action=row['action'],
        score=row['score'],est_tokens=row['est_tokens'],verifier_ok=row['verifier_ok'],reason=row['reason'])
        for row in stored['proposals']),stored['binding'])
    calls=[]
    result=heat.gate_pins(proposals,lambda *a:calls.append(a) or True,user=owned,operation_id='process-death')
    assert result.reason=='evaluating' and not calls
    assert not heat.apply_gate('process-death',user=owned).applied


@pytest.mark.parametrize('retired',[False,True],ids=['current','retired'])
def test_rejected_receipts_impose_correction_floor(owned,retired):
    iid=seed(owned,text='Version A')
    assert verify(owned,iid,event='rejected',accepted=False)
    if retired:
        usermem.update_content(owned,iid,'Version B')
        assert heat.record(iid,'memory',retrieved=True,user=owned)
    p=heat.ledger_path(owned);data=json.loads(p.read_bytes())
    entry=data['retired'][0]['entry'] if retired else data['entries']['memory:'+iid]
    entry['corrections']=0
    p.write_text(json.dumps(data),encoding='utf-8');before=p.read_bytes()
    assert heat.status(owned)['status']=='unavailable'
    if retired:
        usermem.update_content(owned,iid,'Version A')
    assert not heat.record(iid,'memory',retrieved=True,user=owned)
    assert p.read_bytes()==before


def test_retired_receipt_cutoffs_preserve_repeated_revision_history(owned):
    iid=seed(owned,text='Version A')
    assert verify(owned,iid,event='reject-a-1',accepted=False)
    assert heat.record(iid,'memory',corrected=True,user=owned)
    usermem.update_content(owned,iid,'Version B')
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    usermem.update_content(owned,iid,'Version A')
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    assert heat.entry(iid,'memory',user=owned)['corrections']==2
    assert verify(owned,iid,event='reject-a-2',accepted=False)
    assert verify(owned,iid,event='accept-a-2')
    usermem.update_content(owned,iid,'Version C')
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    usermem.update_content(owned,iid,'Version A')
    assert heat.record(iid,'memory',retrieved=True,user=owned)
    current=heat.entry(iid,'memory',user=owned)
    assert (current['corrections'],current['verifier_ok'])==(3,2)
    assert heat.status(owned)['status']=='available'
    old=state.read(owned)['retired'][0]['entry']
    assert (old['corrections'],old['verifier_ok'])==(2,1)


@pytest.mark.parametrize('failure',['missing','malformed','denied'])
def test_registry_failure_does_not_hide_rollback_receipt(owned,monkeypatch,capsys,failure):
    from olympus import cli
    seed(owned);_,result=publish(owned);assert result.applied
    real_read=state.Directory.read
    def damaged(directory,name,limit):
        if name=='experiments.json':
            if failure=='missing':raise FileNotFoundError('owned missing registry')
            if failure=='denied':raise PermissionError('owned denied registry')
            return b'{'
        return real_read(directory,name,limit)
    monkeypatch.setattr(heat,'_registry_entry',REAL_REGISTRY_ENTRY)
    monkeypatch.setattr(state.Directory,'read',damaged)
    assert heat.rollback_pins(user=owned,operation_id='registry-failed').reason=='rolled_back'
    before=snapshot(config.MEMORY_DIR)
    status=heat.status(owned,operation_id='registry-failed')
    assert status['status']=='available'
    assert status['operation']['status']=='rolled_back' and status['operation']['active']
    assert not status['promotion_qualified']
    assert status['qualification_status']=='qualification_unavailable'
    assert cli.main(['ctxheat','receipt','--owner',owned,'--operation-id','registry-failed'])==0
    assert json.loads(capsys.readouterr().out)==status
    assert snapshot(config.MEMORY_DIR)==before
