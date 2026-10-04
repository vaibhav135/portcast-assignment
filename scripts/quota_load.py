"""Isolated fixtures and reconciliation for k6; never a load generator itself."""

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, func, insert, select

from src.shared.config import get_database_config
from src.shared.database import create_database_engine
from src.shared.models import (
    APIQuotaMap, APIFeature, FeatureQuotaExtra, FeatureQuotaMonthly,
    FeatureQuotaStatus, Organization, QuotaUsagePerRequest,
)

ROOT = Path(__file__).resolve().parents[1]
FEATURE = APIFeature.SAILING_SCHEDULE


def revision_metadata():
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()

    files = [ROOT / 'benchmarks/quota_load.js', ROOT / 'scripts/quota_load.py']
    files += sorted((ROOT / 'src').rglob('*.py'))
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return {
        'revision': git('rev-parse', 'HEAD'),
        'dirty': bool(git('status', '--porcelain')),
        'source_sha256': digest.hexdigest(),
        'host_platform': platform.platform(),
        'python': platform.python_version(),
    }


def database_identity():
    config = get_database_config()
    return {'host': config.host, 'port': config.port, 'name': config.name}


def prepare(connection, *, org_count, monthly_units, credit_units):
    if org_count < 1 or monthly_units < 0 or credit_units < 0:
        raise ValueError('Positive org count and nonnegative balances required')
    cost = connection.scalar(select(APIQuotaMap.unit_cost).where(APIQuotaMap.feature == FEATURE))
    if cost is None:
        raise ValueError('Run the serial demo seed on portcast_load first')
    run_id = str(uuid4())
    now = connection.scalar(select(func.clock_timestamp()))
    reset = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + relativedelta(months=1)
    organizations = []
    for start in range(0, org_count, 1000):
        rows = [{'name': f'quota-load-{run_id}:{i}'} for i in range(start, min(start + 1000, org_count))]
        organizations.extend(dict(row._mapping) for row in connection.execute(
            insert(Organization).returning(Organization.id, Organization.name), rows))
    monthly = [dict(org_id=org['id'], feature=FEATURE, total_allocated=monthly_units,
                    units_consumed=0, units_remaining=monthly_units, resets_on=reset)
               for org in organizations]
    credits = [dict(org_id=org['id'], feature=FEATURE, total_allocated=credit_units,
                    units_consumed=0, units_remaining=credit_units,
                    expires_on=now + relativedelta(days=90)) for org in organizations]
    for start in range(0, org_count, 1000):
        connection.execute(insert(FeatureQuotaMonthly), monthly[start:start + 1000])
        if credit_units:
            connection.execute(insert(FeatureQuotaExtra), credits[start:start + 1000])
    return {
        'run_id': run_id, 'feature': FEATURE.value, 'unit_cost': cost,
        'created_at': now.isoformat(), 'period_end': reset.isoformat(),
        'monthly_units_per_org': monthly_units, 'credit_units_per_org': credit_units,
        'organizations': organizations,
    }


def owned_ids(connection, manifest):
    run_id = str(UUID(manifest['run_id']))
    orgs = manifest['organizations']
    ids = [org['id'] for org in orgs]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('Manifest requires unique organization IDs')
    actual = dict(connection.execute(select(Organization.id, Organization.name).where(
        Organization.id.in_(ids))).all())
    expected = {org['id']: org['name'] for org in orgs}
    if actual != expected or any(not name.startswith(f'quota-load-{run_id}:') for name in expected.values()):
        raise ValueError('Fixture ownership mismatch; refusing to operate')
    if manifest['feature'] != FEATURE.value:
        raise ValueError('Unexpected feature')
    return ids


def reconcile(connection, manifest):
    ids = owned_ids(connection, manifest)
    period = datetime.fromisoformat(manifest['period_end'])
    now = connection.scalar(select(func.clock_timestamp()))
    if now >= period:
        raise ValueError('Benchmark crossed UTC reset; use a new fixture')
    live_cost = connection.scalar(select(APIQuotaMap.unit_cost).where(APIQuotaMap.feature == FEATURE))
    if live_cost != manifest['unit_cost']:
        raise ValueError('Feature price changed during benchmark')
    monthly = connection.execute(select(FeatureQuotaMonthly).where(
        FeatureQuotaMonthly.org_id.in_(ids))).mappings().all()
    extra = connection.execute(select(FeatureQuotaExtra).where(
        FeatureQuotaExtra.org_id.in_(ids))).mappings().all()
    groups = connection.execute(select(
        QuotaUsagePerRequest.org_id, QuotaUsagePerRequest.status,
        func.count().label('count'),
        func.sum(QuotaUsagePerRequest.used_from_monthly).label('monthly'),
        func.sum(QuotaUsagePerRequest.used_from_extra).label('extra'),
    ).where(QuotaUsagePerRequest.org_id.in_(ids)).group_by(
        QuotaUsagePerRequest.org_id, QuotaUsagePerRequest.status)).mappings().all()
    consumed = {org_id: {'monthly': 0, 'extra': 0} for org_id in ids}
    counts = {status.value: 0 for status in FeatureQuotaStatus}
    allocations = {status.value: {'monthly': 0, 'extra': 0} for status in FeatureQuotaStatus}
    for row in groups:
        status = row['status'].value
        counts[status] += row['count']
        for source in ('monthly', 'extra'):
            # PostgreSQL SUM(bigint) is numeric/Decimal; these are integer units.
            units = int(row[source])
            allocations[status][source] += units
            if row['status'] != FeatureQuotaStatus.RELEASED:
                consumed[row['org_id']][source] += units
    if len(monthly) != len(ids) or len(extra) != (len(ids) if manifest['credit_units_per_org'] else 0):
        raise ValueError('Fixture balances missing or unexpected feature rows')
    for source, rows, allocated in (
        ('monthly', monthly, manifest['monthly_units_per_org']),
        ('extra', extra, manifest['credit_units_per_org']),
    ):
        for row in rows:
            if row['feature'] != FEATURE or row['total_allocated'] != allocated:
                raise ValueError('Fixture configuration changed')
            if source == 'monthly' and row['resets_on'] != period:
                raise ValueError('Fixture period changed')
            if source == 'extra' and row['expires_on'] <= now:
                raise ValueError('Fixture credits expired')
            if row['units_consumed'] != consumed[row['org_id']][source]:
                raise ValueError(f'{source} balance disagrees with durable allocations')
            if row['units_consumed'] < 0 or row['units_remaining'] < 0 or row['units_consumed'] + row['units_remaining'] != allocated:
                raise ValueError('Balance invariant violated')
    if not extra and any(value['extra'] for value in consumed.values()):
        raise ValueError('Extra allocations without credit balances')
    # API input normalization is trusted, but check every persisted operation's
    # feature, charged quantity, and terminal result shape in bounded chunks.
    operations = connection.execute(select(
        QuotaUsagePerRequest.feature, QuotaUsagePerRequest.status,
        QuotaUsagePerRequest.used_from_monthly, QuotaUsagePerRequest.used_from_extra,
        QuotaUsagePerRequest.request_payload, QuotaUsagePerRequest.result_payload,
    ).where(QuotaUsagePerRequest.org_id.in_(ids)).execution_options(yield_per=1000))
    for op in operations.mappings():
        payload = op['request_payload']
        if op['feature'] != FEATURE or not payload or not isinstance(payload.get('routes'), list):
            raise ValueError('Unexpected operation context')
        if op['used_from_monthly'] + op['used_from_extra'] != len(payload['routes']) * live_cost:
            raise ValueError('Partial/wrong batch allocation')
        if op['status'] == FeatureQuotaStatus.DONE:
            expected = {'results': [{'route': route, 'sailings': ['demo-sailing-001']} for route in payload['routes']]}
            if payload.get('demo_behavior') != 'success' or op['result_payload'] != expected:
                raise ValueError('Invalid durable success result')
        if op['status'] == FeatureQuotaStatus.RELEASED and payload.get('demo_behavior') not in ('fail', 'timeout'):
            raise ValueError('Unexpected release of successful work')
    return {'operations': counts, 'allocations': allocations, 'organizations': len(ids),
            'active_organizations': len({row['org_id'] for row in groups}),
            'accounting_consistent': True, 'settled': counts['RESERVED'] == 0}


def compare_summary(report, manifest, summary):
    if summary['fixture_run_id'] != manifest['run_id']:
        raise ValueError('k6 summary belongs to a different fixture')
    metrics = summary['summary']['metrics']
    def count(name):
        return metrics.get(name, {}).get('values', {}).get('count', 0)
    report['http_acknowledged'] = {
        'new_paid': count('new_paid_completed'), 'replays': count('replay_completed'),
        'paid_units': count('paid_units'), 'confirmed_failures': count('expected_failures'),
        'denials': count('quota_denials'), 'unexpected': count('unexpected_responses'),
        'dropped_iterations': count('dropped_iterations'),
    }
    done = report['allocations']['DONE']
    report['acknowledgements_match'] = (
        report['operations']['DONE'] == count('new_paid_completed') and
        report['operations']['RELEASED'] == count('expected_failures') and
        done['monthly'] + done['extra'] == count('paid_units'))
    checks = metrics.get('checks', {}).get('values', {})
    report['response_checks_passed'] = checks.get('rate') == 1 and checks.get('passes', 0) > 0 and checks.get('fails') == 0
    report['k6_thresholds_passed'] = all(
        threshold['ok'] for metric in metrics.values()
        for threshold in metric.get('thresholds', {}).values())
    report['schedule_fully_generated'] = count('dropped_iterations') == 0
    # A timeout/lost response is NOT proof no commit occurred. Preserve the fixture
    # and report the discrepancy rather than refunding or deleting uncertain work.
    report['accounting_verified'] = (report['settled'] and report['acknowledgements_match']
                                     and report['response_checks_passed'] and count('unexpected_responses') == 0)
    return report


def cleanup(connection, manifest):
    ids = owned_ids(connection, manifest)
    pending = connection.scalar(select(func.count()).select_from(QuotaUsagePerRequest).where(
        QuotaUsagePerRequest.org_id.in_(ids), QuotaUsagePerRequest.status == FeatureQuotaStatus.RESERVED))
    if pending:
        raise ValueError('Unresolved holds remain; stop traffic and resolve them before cleanup')
    for model in (QuotaUsagePerRequest, FeatureQuotaExtra, FeatureQuotaMonthly, Organization):
        column = model.id if model is Organization else model.org_id
        connection.execute(delete(model).where(column.in_(ids)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'verify', 'cleanup'])
    parser.add_argument('--manifest', type=Path, default=ROOT / 'benchmarks/results/fixture.json')
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--output', type=Path, help='Optional reconciliation JSON output')
    parser.add_argument('--orgs', type=int, default=5000)
    parser.add_argument('--monthly-units', type=int, default=1_000_000)
    parser.add_argument('--credit-units', type=int, default=0)
    parser.add_argument('--allow-demo-writes', action='store_true')
    args = parser.parse_args()
    if get_database_config().name != 'portcast_load':
        parser.error('This CLI requires DB_NAME=portcast_load; never use demo/test/production databases')
    if args.action != 'verify' and not args.allow_demo_writes:
        parser.error('Fixture writes require --allow-demo-writes')
    engine = create_database_engine()
    engine.hide_parameters = True
    try:
        if args.action == 'prepare':
            if args.manifest.exists():
                parser.error('Manifest already exists; use a new path or clean up the prior fixture')
            if not args.manifest.parent.is_dir():
                parser.error('Manifest parent directory must already exist')
            metadata = revision_metadata()
            # Reserve the file first; transaction failure removes it. If a process
            # crashes, the run UUID in organization names permits operator recovery.
            with args.manifest.open('x') as output:
                try:
                    with engine.begin() as connection:
                        manifest = prepare(connection, org_count=args.orgs,
                                           monthly_units=args.monthly_units, credit_units=args.credit_units)
                        manifest.update(database=database_identity(), environment=metadata,
                                        postgres_version=connection.scalar(select(func.version())))
                        output.write(json.dumps(manifest, indent=2) + '\n')
                        output.flush()
                except BaseException:
                    # Keep a populated manifest if commit acknowledgement is
                    # ambiguous; losing it would erase fixture ownership evidence.
                    if output.tell() == 0:
                        args.manifest.unlink()
                    raise
            print(json.dumps({'manifest': str(args.manifest), 'run_id': manifest['run_id'], 'orgs': args.orgs}))
        else:
            manifest = json.loads(args.manifest.read_text())
            if manifest['database'] != database_identity():
                parser.error('Configured database differs from manifest')
            with engine.begin() as connection:
                if args.action == 'cleanup':
                    cleanup(connection, manifest)
                    print('Deleted only fixture-owned rows; manifest retained as evidence. Do not reuse it.')
                    return
                connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                report = reconcile(connection, manifest)
            if args.summary:
                report = compare_summary(report, manifest, json.loads(args.summary.read_text()))
            text = json.dumps(report, indent=2) + '\n'
            if args.output:
                args.output.write_text(text)
            print(text, end='')
            if not report.get('accounting_verified', report['settled']):
                raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
