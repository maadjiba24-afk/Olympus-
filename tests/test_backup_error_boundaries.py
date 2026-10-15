"""Native lease failures redact attached errors and still drain all authority."""
from contextlib import contextmanager
import uuid
import pytest
from olympus import backup_publication as pub, backup_restore as restore


@contextmanager
def lease(module,destination,live,op):
    if module is pub:
        with module.locked(destination,initialize=True,operation_id=op) as root:
            yield root
    else:
        with module._authority(destination,live,(),op,initialize=True) as root:
            yield root


@pytest.mark.parametrize('module',[pub,restore],ids=['publication','restore'])
@pytest.mark.parametrize('boundary',['initialization','body','close'])
@pytest.mark.parametrize('typed',[False,True],ids=['native-error','prior-public-error'])
def test_generic_error_has_no_private_message_chain_or_previous_traceback(tmp_path,monkeypatch,module,boundary,typed):
    destination=tmp_path/'owned';live=tmp_path/'live';live.mkdir()
    if module is pub:destination.mkdir()
    op=uuid.uuid4().hex
    error_type=pub.PublicationError if module is pub else restore.RestoreError
    roots=[];prior=[];armed=True
    def raise_owned_private_fault():
        nonlocal armed
        if not armed:return
        armed=False
        try:raise OSError('owned private native sentinel')
        except OSError as cause:
            error=error_type(op,'owned private public sentinel') if typed else ValueError('owned private value sentinel')
            prior.append(error)
            raise error from cause
    if boundary=='initialization':
        original=module._Root.check
        def fault_check(self):
            original(self)
            if armed:
                roots.append(self)
                raise_owned_private_fault()
        monkeypatch.setattr(module._Root,'check',fault_check)
    if boundary=='close':
        original=module._Root.close
        def fault_close(self):
            original(self)
            raise_owned_private_fault()
        monkeypatch.setattr(module._Root,'close',fault_close)
    with pytest.raises(error_type) as caught:
        with lease(module,destination,live,op) as root:
            roots.append(root)
            if boundary=='body':raise_owned_private_fault()
    error=caught.value
    assert not armed and prior and error is not prior[0]
    assert error.operation_id==op and 'sentinel' not in str(error)
    assert error.__cause__ is None and error.__context__ is None
    frames=[];tb=error.__traceback__
    while tb:
        frames.append(tb.tb_frame.f_code.co_name);tb=tb.tb_next
    assert 'raise_owned_private_fault' not in frames
    assert roots and all(root.closed and root.fd is None and root.directories==[] for root in roots)


@pytest.mark.parametrize('module',[pub,restore],ids=['publication','restore'])
@pytest.mark.parametrize('interruption',[KeyboardInterrupt,SystemExit])
def test_system_interruption_identity_survives_complete_lease_cleanup(tmp_path,module,interruption):
    destination=tmp_path/'owned';live=tmp_path/'live';live.mkdir()
    if module is pub:destination.mkdir()
    op=uuid.uuid4().hex;signal=interruption('owned stop')
    with pytest.raises(interruption) as caught:
        with lease(module,destination,live,op) as root:
            raise signal
    assert caught.value is signal and signal.operation_id==op
    assert root.closed and root.fd is None and root.directories==[]
