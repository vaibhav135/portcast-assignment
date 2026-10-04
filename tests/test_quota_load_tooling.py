import json
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from scripts.quota_load import cleanup, compare_summary, owned_ids, prepare, reconcile
from src.consumer.schemas import ScheduleSearchRequest
from src.shared.models import APIQuotaMap, APIFeature, FeatureQuotaMonthly, Organization
from src.shared.quota import finalize_reservation, release_reservation, reserve_quota


@pytest.fixture
def load_fixture(db_session: Session):
    with db_session.begin():
        if db_session.scalar(select(APIQuotaMap).where(APIQuotaMap.feature == APIFeature.SAILING_SCHEDULE)) is None:
            db_session.execute(insert(APIQuotaMap).values(
                feature=APIFeature.SAILING_SCHEDULE, unit_cost=1, lease_duration_sec=30))
        return prepare(db_session.connection(), org_count=2, monthly_units=5, credit_units=7)


def reserve(session, manifest, *, units=1, behavior='success'):
    return reserve_quota(
        session, org_id=manifest['organizations'][0]['id'], feature=APIFeature.SAILING_SCHEDULE,
        units=units, idempotency_key=uuid4(),
        request_payload=ScheduleSearchRequest(
            routes=[f'DEMO-ROUTE-{i}' for i in range(units)], demo_behavior=behavior,
        ).model_dump(mode='json'))


def test_reconcile_mixed_success_release_and_summary(db_session, load_fixture):
    operation = reserve(db_session, load_fixture, units=7)
    result = {'results': [{'route': f'DEMO-ROUTE-{i}', 'sailings': ['demo-sailing-001']} for i in range(7)]}
    finalize_reservation(db_session, operation_id=operation.id, claim_version=0, result_payload=result)
    failed = reserve(db_session, load_fixture, units=2, behavior='fail')
    release_reservation(db_session, operation_id=failed.id, claim_version=0)
    with db_session.begin():
        report = reconcile(db_session.connection(), load_fixture)
    assert report['operations'] == {'RESERVED': 0, 'DONE': 1, 'RELEASED': 1}
    assert report['allocations']['DONE'] == {'monthly': 5, 'extra': 2}
    assert json.loads(json.dumps(report)) == report
    summary = {'fixture_run_id': load_fixture['run_id'], 'summary': {'metrics': {
        'new_paid_completed': {'values': {'count': 1}},
        'paid_units': {'values': {'count': 7}},
        'expected_failures': {'values': {'count': 1}},
        'checks': {'values': {'rate': 1, 'passes': 8, 'fails': 0}},
    }}}
    assert compare_summary(report, load_fixture, summary)['accounting_verified']
    summary['summary']['metrics']['new_paid_completed']['values']['count'] = 0
    assert not compare_summary(report, load_fixture, summary)['accounting_verified']


def test_owned_rows_required_before_cleanup(db_session, load_fixture):
    load_fixture['organizations'][0]['name'] = 'somebody-elses-fixture'
    with db_session.begin(), pytest.raises(ValueError, match='ownership'):
        owned_ids(db_session.connection(), load_fixture)


def test_unresolved_holds_are_reconciled_but_never_deleted(db_session, load_fixture):
    reserve(db_session, load_fixture)
    with db_session.begin():
        report = reconcile(db_session.connection(), load_fixture)
        assert report['accounting_consistent']
        assert not report['settled']
        with pytest.raises(ValueError, match='Unresolved holds'):
            cleanup(db_session.connection(), load_fixture)


def test_counter_corruption_is_detected(db_session, load_fixture):
    with db_session.begin():
        db_session.execute(update(FeatureQuotaMonthly).where(
            FeatureQuotaMonthly.org_id == load_fixture['organizations'][0]['id'],
        ).values(units_consumed=1, units_remaining=4))
        with pytest.raises(ValueError, match='disagrees'):
            reconcile(db_session.connection(), load_fixture)


def test_cleanup_preserves_unrelated_org(db_session, load_fixture):
    with db_session.begin():
        other = db_session.scalar(insert(Organization).values(name='unrelated').returning(Organization.id))
        cleanup(db_session.connection(), load_fixture)
        assert db_session.scalar(select(Organization.id).where(Organization.id == other)) == other
        assert not db_session.scalar(select(Organization.id).where(
            Organization.id.in_([org['id'] for org in load_fixture['organizations']])))


def test_summary_from_different_fixture_is_rejected(db_session, load_fixture):
    with db_session.begin():
        report = reconcile(db_session.connection(), load_fixture)
    with pytest.raises(ValueError, match='different fixture'):
        compare_summary(report, load_fixture, {'fixture_run_id': str(uuid4())})
