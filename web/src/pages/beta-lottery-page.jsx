import React, { useEffect, useState } from 'react';
import { CopyableId } from '../components/copyable-id.jsx';
import { countdownText, formatBjDisplay } from '../utils/display.js';
import { getDeviceFingerprint } from '../utils/fingerprint.js';
import { useVisibilityInterval } from '../hooks/use-visibility-interval.js';
import { t } from '../i18n/index.js';

export function BetaLotteryPage({ onBack, onLogin, authApi }) {
  const TOKEN_KEY = 'ai_option_beta_lottery_token';
  const [nickname, setNickname] = useState('');
  const [contact, setContact] = useState('');
  const [entryToken, setEntryToken] = useState(() => new URLSearchParams(window.location.search).get('entry_token') || new URLSearchParams(window.location.search).get('token') || window.localStorage.getItem(TOKEN_KEY) || '');
  const [lookupToken, setLookupToken] = useState('');
  const [status, setStatus] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [tick, setTick] = useState(Date.now());
  const [fingerprint, setFingerprint] = useState('');

  useEffect(() => {
    let active = true;
    getDeviceFingerprint()
      .then((fp) => {
        if (active) setFingerprint(fp);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    async function refresh() {
      try {
        const qs = entryToken ? `?entry_token=${encodeURIComponent(entryToken)}` : '';
        const row = await authApi(`/api/beta-lottery/status${qs}`);
        if (!active) return;
        setStatus(row);
        if (row?.entry?.entry_token && row.entry.entry_token !== entryToken) {
          setEntryToken(row.entry.entry_token);
          window.localStorage.setItem(TOKEN_KEY, row.entry.entry_token);
        }
      } catch (refreshError) {
        if (active) setError(refreshError.message);
      }
    }
    refresh();
    const tickTimer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => {
      active = false;
      window.clearInterval(tickTimer);
    };
  }, [entryToken]);

  useVisibilityInterval(
    async () => {
      try {
        const qs = entryToken ? `?entry_token=${encodeURIComponent(entryToken)}` : '';
        const row = await authApi(`/api/beta-lottery/status${qs}`);
        setStatus(row);
        if (row?.entry?.entry_token && row.entry.entry_token !== entryToken) {
          setEntryToken(row.entry.entry_token);
          window.localStorage.setItem(TOKEN_KEY, row.entry.entry_token);
        }
      } catch (refreshError) {
        setError(refreshError.message);
      }
    },
    15000,
  );

  async function submit(event) {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    setMessage('');
    try {
      const row = await authApi('/api/beta-lottery/enter', {
        method: 'POST',
        body: JSON.stringify({
          nickname: nickname.trim(),
          contact: contact.trim(),
          entry_token: entryToken || null,
          fingerprint: fingerprint || (await getDeviceFingerprint().catch(() => '')),
        }),
      });
      setStatus(row);
      setMessage(row.message || t('site.lottery.registerSuccess'));
      if (row?.entry?.entry_token) {
        setEntryToken(row.entry.entry_token);
        window.localStorage.setItem(TOKEN_KEY, row.entry.entry_token);
      }
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setSubmitting(false);
    }
  }

  function lookupEntryToken(event) {
    event.preventDefault();
    const token = lookupToken.trim();
    if (!token) return;
    setEntryToken(token);
    window.localStorage.setItem(TOKEN_KEY, token);
    window.history.replaceState({}, '', `/beta-lottery?entry_token=${encodeURIComponent(token)}`);
  }

  const ownEntry = status?.entry || null;
  const winners = status?.winners || [];
  const announced = Boolean(status?.announced);
  const countDown = announced ? t('site.lottery.drawn') : countdownText(status?.announce_at_bj, tick);
  const alreadyJoined = Boolean(ownEntry?.entry_token);
  const slotCount = status?.slot_count ?? 15;
  const validDays = status?.user_valid_days ?? 7;
  const announceLabel = status?.announce_at_bj ? formatBjDisplay(status.announce_at_bj) : t('site.lottery.awaitTime');

  return (
    <main className="shell guide-shell lottery-shell">
      <section className="hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Public Beta Lottery</div>
            <h1>{t('site.lottery.heroTitle')}</h1>
            <p>{t('site.lottery.heroDesc').replace('{slots}', slotCount).replace('{time}', announceLabel)}</p>
          </div>
        </div>
        <button className="ghost nav-action" type="button" onClick={onLogin}>{t('site.lottery.backToLogin')}</button>
        <button className="primary nav-action" type="button" onClick={onBack}>{t('site.lottery.backToSite')}</button>
      </section>

      <section className="panel lottery-panel">
        <div className="lottery-intro">
          <div className="lottery-countdown">
            <span>{t('site.lottery.countdownLabel')}</span>
            <strong>{countDown}</strong>
            <small>{announceLabel}</small>
          </div>
          <div className="lottery-thanks">
            <strong>{t('site.lottery.noticeTitle')}</strong>
            <p>{t('site.lottery.noticeBody').replace('{days}', validDays)}</p>
          </div>
          <div className="lottery-meta">
            <span>{t('site.lottery.slots').replace('{n}', slotCount)}</span>
            <span>{t('site.lottery.registered').replace('{n}', status?.entry_count ?? 0)}</span>
            <span>{t('site.lottery.won').replace('{n}', status?.winner_count ?? 0)}</span>
            <span>{announced ? t('site.lottery.resultPublished') : t('site.lottery.resultPending')}</span>
          </div>
        </div>

        <form className="lottery-form" onSubmit={submit}>
          <label>
            {t('site.lottery.nicknameLabel')}
            <input value={nickname} onChange={(event) => setNickname(event.target.value)} placeholder={t('site.lottery.nicknamePlaceholder')} required />
          </label>
          <label>
            {t('site.lottery.contactLabel')}
            <input value={contact} onChange={(event) => setContact(event.target.value)} placeholder={t('site.lottery.contactPlaceholder')} />
          </label>
          {message && <div className="permission-note">{message}</div>}
          {error && <div className="error">{error}</div>}
          <button className="primary" disabled={submitting}>
            {submitting ? t('site.lottery.submitting') : alreadyJoined ? t('site.lottery.updateAndKeep') : t('site.lottery.drawAndRegister')}
          </button>
        </form>
      </section>

      <section className="panel lottery-panel">
        <div className="lottery-columns">
          <div className="lottery-card">
            <strong>{t('site.lottery.myResultTitle')}</strong>
            <form className="lottery-form compact" onSubmit={lookupEntryToken}>
              <label>
                {t('site.lottery.lookupTokenLabel')}
                <input value={lookupToken} onChange={(event) => setLookupToken(event.target.value)} placeholder={t('site.lottery.lookupPlaceholder')} />
              </label>
              <button className="ghost" type="submit" disabled={!lookupToken.trim()}>{t('site.lottery.lookupBtn')}</button>
            </form>
            {ownEntry ? (
              <>
                <div className="lottery-result-line"><span>{t('site.lottery.regToken')}</span><CopyableId value={ownEntry.entry_token} label="Token" compact /></div>
                <div className="lottery-result-line"><span>{t('site.lottery.regNickname')}</span><b>{ownEntry.nickname || '--'}</b></div>
                <div className="lottery-result-line"><span>{t('site.lottery.winStatus')}</span><b className={ownEntry.selected ? 'ok' : 'muted'}>{ownEntry.selected ? t('site.lottery.selected') : t('site.lottery.notSelected')}</b></div>
                <div className="lottery-result-line"><span>{t('site.lottery.grantedAccess')}</span><b>{ownEntry.selected ? t('site.lottery.analyzerOnly') : t('site.lottery.awaitDraw')}</b></div>
                {announced && ownEntry.selected && ownEntry.assigned_username && (
                  <>
                    <div className="lottery-result-line"><span>{t('site.lottery.assignedAccount')}</span><b>{ownEntry.assigned_username}</b></div>
                    <div className="lottery-result-line"><span>{t('site.lottery.initialPassword')}</span><b>{ownEntry.password || '--'}</b></div>
                  </>
                )}
              </>
            ) : (
              <p className="muted">{t('site.lottery.notRegistered')}</p>
            )}
          </div>
          <div className="lottery-card">
            <strong>{t('site.lottery.winnersTitle')}</strong>
            {announced ? (
              winners.length ? winners.map((winner, index) => (
                <div className="lottery-winner-row" key={`${winner.assigned_username}-${index}`}>
                  <span>{winner.display_name}</span>
                  <b>{winner.assigned_username}</b>
                </div>
              )) : <p className="muted">{t('site.lottery.noResultYet')}</p>
            ) : (
              <p className="muted">{t('site.lottery.hiddenBeforeDraw')}</p>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
