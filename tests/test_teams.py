"""Multi-team: per-call team routing in the Liara client, cards carry and re-enter their team, per-team precheck,
no cross-team wiring, chat team mentions, unknown teams refused."""
import asyncio, json
import httpx
import pytest
from negahban import approvals, store, teams, tools
from negahban.config import settings
from negahban.liara import Liara

PROD, TEST = 'a' * 24, 'b' * 24


def run(c):
    return asyncio.run(c)


@pytest.fixture
def two_teams(monkeypatch, fresh_db):
    monkeypatch.setattr(settings, 'liara_team', PROD)
    monkeypatch.setenv('LIARA_TEAMS', json.dumps([{'id': PROD, 'name': 'دوخت'}, {'id': TEST, 'name': 'لیارا', 'token_env': 'LIARA_API_TOKEN_2', 'test': True}]))
    monkeypatch.setenv('LIARA_API_TOKEN_2', 'tok-test-team-999999')

    async def fp(app):
        return 'fp'
    monkeypatch.setattr(approvals, 'state_fingerprint', fp)
    return None


def test_client_routes_each_call_to_the_operation_team(two_teams):
    seen = []

    def h(req):
        seen.append((req.url.params.get('teamID'), req.headers['authorization']))
        return httpx.Response(200, json={'projects': []})
    c = Liara(token='tok-prod-111111', transport=httpx.MockTransport(h))  # not bound: follows the team context
    run(c.list_apps())
    with teams.use('لیارا'):
        run(c.list_apps())
    run(c.get('/v2/teams', team=''))
    assert seen[0] == (PROD, 'Bearer tok-prod-111111')
    assert seen[1] == (TEST, 'Bearer tok-test-team-999999')  # per-team token override
    assert seen[2][0] is None


def test_cards_carry_their_team_and_execute_inside_it(two_teams, monkeypatch):
    got = {}

    async def fake_restart(a, ctx):
        got['team'], got['args'] = teams.current(), dict(a)
        return {'ok': True}
    monkeypatch.setitem(tools.TOOLS['scale_app'], 'fn', fake_restart)

    async def impact(tool, a):
        return {'downtime': '-', 'cost': '-', 'risk': '-'}, 'rb'
    monkeypatch.setattr(approvals, 'impact', impact)
    r_test = run(tools.run('scale_app', {'app': 'ngh-test-app', 'on': False, 'team': 'لیارا'}, {'origin': 'chat'}))
    r_prod = run(tools.run('scale_app', {'app': 'shop', 'on': False}, {'origin': 'chat'}))
    pt, pp = store.get_proposal(r_test['proposal']['id']), store.get_proposal(r_prod['proposal']['id'])
    assert pt['args']['team'] == TEST and 'team' not in pp['args']
    assert approvals.public(pt)['team']['test'] and approvals.public(pp)['team']['default']
    assert r_test['proposal']['team']['name'] == 'لیارا'
    with teams.use(PROD):  # approving from a dashboard showing the other team still runs in the card's team
        run(approvals.execute(pt['id'], 'owner'))
    assert got['team'] == TEST and 'team' not in got['args']


def test_unknown_team_refused(two_teams):
    r = run(tools.run('list_apps', {'team': 'نامعلوم'}, {}))
    assert 'شناخته نیست' in r['error']


def test_registered_app_team_is_used_without_a_team_arg(two_teams):
    with teams.use(TEST):
        store.upsert_app('ngh-test-app', 'monitored')
    assert teams.team_of_app('ngh-test-app') == TEST
    store.upsert_app('shop', 'monitored')
    assert teams.team_of_app('shop') == PROD


def test_precheck_is_per_team(two_teams, monkeypatch):
    from negahban.diag import liara_data

    def h(req):
        t = req.url.params.get('teamID')
        if req.url.path == '/v1/billing':
            return httpx.Response(200, json={'team': {'balance': 1_760_000 if t == PROD else 2_000_000}})
        return httpx.Response(200, json={'totalHourlyPrice': 10_000 if t == PROD else 0})
    fake = Liara(token='tok-prod-111111', transport=httpx.MockTransport(h), base='https://api.test')
    monkeypatch.setattr(liara_data, 'liara', fake)
    prod = run(liara_data.week_precheck(1041))
    with teams.use(TEST):
        test = run(liara_data.week_precheck(1041))
    assert prod['ok'] is False and 'دوخت' in prod['message_fa']
    assert test['ok'] is True and 'لیارا' in test['team']


def test_no_cross_team_wiring(two_teams):
    def h(req):  # ngh-test-app exists only in the test team
        if req.url.path == '/v1/projects/ngh-test-app' and req.url.params.get('teamID') == TEST:
            return httpx.Response(200, json={'project': {'_id': 'x'}})
        return httpx.Response(404, json={'message': 'Not Found'})
    fake = Liara(token='tok-prod-111111', transport=httpx.MockTransport(h), base='https://api.test')
    with pytest.raises(ValueError) as e:
        run(teams.require_app_in_team('ngh-test-app', fake))  # current team = production
    assert 'لیارا' in str(e.value) and 'بین دو تیم' in str(e.value)
    with teams.use(TEST):
        run(teams.require_app_in_team('ngh-test-app', fake))  # same team: fine


def test_chat_team_mentions(two_teams):
    assert teams.mentioned('در تیم لیارا یک Postgres بساز') == [TEST]
    assert teams.mentioned('این را روی لیارا مستقر کن') == []  # «لیارا» alone is the platform, not the team
    assert teams.mentioned('وضعیت تیم دوخت') == [PROD]
    assert teams.get('test')['id'] == TEST and teams.get('production')['id'] == PROD


def test_team_param_on_every_tool():
    assert all('team' in t['schema']['properties'] for t in tools.TOOLS.values())
