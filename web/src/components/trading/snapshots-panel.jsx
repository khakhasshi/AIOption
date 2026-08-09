import React from 'react';
import { Pair, SectionTitle } from '../common.jsx';
import { ChartCard } from '../scanner-widgets.jsx';
import { fmt, pct, userErrorLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function SnapshotsPanel({ curve, refreshTradingSnapshots, snapshots, strategySnapshot }) {
  return (
<div className="snapshot-layout">
  <div className="panel snapshot-panel">
    <div className="answer-head">
      <SectionTitle title={t('trading2.strategyCapitalSnapshot')} />
      <button className="ghost" type="button" onClick={() => refreshTradingSnapshots(true)}>{t('trading2.refreshSnapshot')}</button>
    </div>
    <dl className="intent snapshot-intent">
      <Pair k={t('trading2.strategyCapital')} v={`$${fmt(strategySnapshot.total_capital)}`} />
      <Pair k={t('trading2.usableStrategyCapital')} v={`$${fmt(strategySnapshot.usable_capital)}`} />
      <Pair k={t('trading2.netAssets')} v={`$${fmt(strategySnapshot.net_assets)}`} />
      <Pair k={t('trading2.cash')} v={`$${fmt(strategySnapshot.total_cash)}`} />
      <Pair k={t('trading2.buyingPower')} v={`$${fmt(strategySnapshot.buy_power)}`} />
      <Pair k={t('trading2.coverage')} v={pct(strategySnapshot.capital_coverage_pct)} />
    </dl>
    {snapshots?.error && <p className="error compact-error">{userErrorLabel(snapshots.error)}</p>}
  </div>
  <ChartCard title={t('trading2.dailyCapitalCurve')} data={curve} field="net_assets" secondField="total_capital" />
</div>
  );
}
