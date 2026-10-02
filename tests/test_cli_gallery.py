"""Owned M06 CLI surface: exact owner, typed exit and stable IDs."""
from types import SimpleNamespace
import json
import pytest
from olympus import cli, gallery, media, memory
from olympus.gallery_state import GalleryError

ID = 'a' * 32
REV = 'b' * 64

def args(**changes):
    values = dict(action='list', owner=None, name=None, prompt=[], operation_id=None,
                  expected_id=None, expected_revision=None, review_digest=None,
                  operator_reviewed=False, offset=0)
    return SimpleNamespace(**(values | changes))


def test_list_uses_explicit_cli_owner(monkeypatch, capsys):
    def listing(owner):
        assert owner == 'cli'
        return {'status': 'missing', 'images': []}
    monkeypatch.setattr(gallery, 'list_result', listing)
    with memory.user_context('unrelated'):
        assert cli._gallery_command(args()) == 0
    assert json.loads(capsys.readouterr().out)['images'] == []


@pytest.mark.parametrize('state,expected', [('complete', 0), ('deleted', 0), ('indeterminate', 2), ('reserved', 2), ('failed', 1)])
def test_typed_exits(monkeypatch, state, expected):
    monkeypatch.setattr(gallery, 'operation_status', lambda operation_id, owner: {'status': state})
    assert cli._gallery_command(args(action='status', name=ID)) == expected


def test_id_printed_before_call_and_exact_owner_restored(monkeypatch, capsys):
    def edit(prompt, name, **kw):
        assert capsys.readouterr().out == 'Operation ID: ' + ID + '\n'
        assert kw['owner'] == 'Case.Owner' and kw['source_revision'] == REV
        assert memory.current_owner() == 'outer'
        return {'status': 'indeterminate', 'operation_id': ID}
    monkeypatch.setattr(media, 'edit_image_result', edit)
    with memory.user_context('outer'):
        assert cli._gallery_command(args(action='edit', owner='Case.Owner', name='shot.png', prompt=['blue'],
            expected_id=ID, expected_revision=REV, operation_id=ID)) == 2
        assert memory.current_owner() == 'outer'


def test_delete_missing_expectation_nonzero(monkeypatch):
    monkeypatch.setattr(gallery, 'delete_image', lambda *a, **kw: pytest.fail('must not delete'))
    assert cli._gallery_command(args(action='delete', name='shot.png')) == 1


def test_legacy_claim_requires_explicit_operator_owner(monkeypatch):
    monkeypatch.setattr(gallery, 'claim_legacy', lambda *a, **kw: pytest.fail('must not attribute'))
    assert cli._gallery_command(args(action='claim', operator_reviewed=True, name='manifest.json', review_digest=REV)) == 1


def test_unavailable_nonzero(monkeypatch, capsys):
    def listing(owner): raise GalleryError('unavailable', 'unreadable', 503)
    monkeypatch.setattr(gallery, 'list_result', listing)
    assert cli._gallery_command(args()) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'unavailable'


def test_real_parser_preserves_growth_and_requires_claim_owner(monkeypatch):
    parser = cli.build_parser()
    assert parser.parse_args(['growth']).owner == 'cli'
    parsed = parser.parse_args(['gallery', 'claim', 'review.json', '--operator-reviewed', '--review-digest', REV])
    assert parsed.owner is None
    monkeypatch.setattr(gallery, 'claim_legacy', lambda *a, **kw: pytest.fail('No implicit attribution'))
    assert cli._gallery_command(parsed) == 1
    assert parser.parse_args(['gallery', 'list']).owner is None


def test_cli_entrypoint_routes_gallery_and_exits(monkeypatch, capsys):
    from olympus import firstrun, opconfig
    monkeypatch.setattr(firstrun, 'load_env_file', lambda: None)
    monkeypatch.setattr(opconfig, 'apply_secrets', lambda: None)
    monkeypatch.setenv('OLYMPUS_ENV', 'development')
    monkeypatch.setenv('OLYMPUS_SOVEREIGN', '0')
    monkeypatch.setattr(gallery, 'operation_status', lambda operation_id, owner: {'status': 'indeterminate', 'operation_id': operation_id})
    assert cli.main(['gallery', 'status', ID]) == 2
    assert json.loads(capsys.readouterr().out)['operation_id'] == ID


def test_claim_batch_identity_visible_before_call(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / 'review.json'
    manifest.write_text(json.dumps({'digest': REV}))
    def claim(owner, *, review, review_digest):
        assert owner == 'Case.Owner' and review_digest == REV
        notice = capsys.readouterr().out
        assert REV in notice and 'same unchanged manifest' in notice
        return {'status': 'complete', 'results': [{'status': 'complete', 'operation_id': 'claim-' + REV}]}
    monkeypatch.setattr(gallery, 'claim_legacy', claim)
    assert cli._gallery_command(args(action='claim', owner='Case.Owner', operator_reviewed=True,
        name=str(manifest), review_digest=REV)) == 0


@pytest.mark.parametrize('code', ['publication_unconfirmed', 'ledger_unavailable'])
def test_uncertain_publication_is_pending_exit(monkeypatch, code):
    def recover(*a): raise GalleryError(code, 'Publication acknowledgement unavailable', 503, ID)
    monkeypatch.setattr(gallery, 'recover_operation', recover)
    assert cli._gallery_command(args(action='recover', name=ID)) == 2
