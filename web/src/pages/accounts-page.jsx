import React, { useEffect, useState } from 'react';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';
import { Toggle, SectionTitle } from '../components/common.jsx';
import { aiProviderTypeItems, emptyAccount, emptyAlpacaAccount, emptyUsmartAccount, emptyUserProvider, emptyProvider } from '../config.js';
import { requestOAuthCredential } from '../utils/oauth-clients.js';
import { t } from '../i18n/index.js';

function providerTypeLabel(value) {
  return value === 'claude' ? 'Claude Compatible' : 'OpenAI Compatible';
}

export function AccountsPage({
  api,
  session,
  providers = [],
  refreshProviders,
  onNotifications,
}) {
  const { embedded } = useAppShell();
  const canAdmin = Boolean(session?.is_admin);
  const canTrade = Boolean(session?.can_trade);
  const userProviders = providers.filter((p) => p.server_managed === false);
  const serverProviders = providers.filter((p) => p.server_managed !== false);

  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // Longbridge state
  const [lbAccounts, setLbAccounts] = useState([]);
  const [lbForm, setLbForm] = useState(emptyAccount);

  // Alpaca state (uses unified /api/brokers/accounts)
  const [brokerRows, setBrokerRows] = useState([]);
  const [apForm, setApForm] = useState(emptyAlpacaAccount);
  const [usForm, setUsForm] = useState(emptyUsmartAccount);

  // User AI provider form
  const [upForm, setUpForm] = useState(emptyUserProvider);

  // Admin server-provider form
  const [svForm, setSvForm] = useState(emptyProvider);

  // OAuth login bindings (Google / Apple)
  const [oauthLinks, setOauthLinks] = useState([]);
  const [oauthHasPassword, setOauthHasPassword] = useState(true);
  const [oauthConfig, setOauthConfig] = useState({ enabled: false, providers: [] });
  const [oauthBusy, setOauthBusy] = useState('');

  useEffect(() => {
    refreshAll();
    refreshOAuth();
  }, []);

  async function refreshAll() {
    setError('');
    try {
      const [lb, brokers] = await Promise.all([
        canTrade ? api('/api/longbridge/accounts') : Promise.resolve([]),
        canTrade ? api('/api/brokers/accounts') : Promise.resolve([]),
      ]);
      setLbAccounts(lb || []);
      setBrokerRows(brokers || []);
    } catch (e) {
      setError(e.message);
    }
  }

  async function refreshOAuth() {
    try {
      const [config, links] = await Promise.all([
        api('/api/auth/oauth/config'),
        api('/api/auth/oauth/links'),
      ]);
      setOauthConfig({ enabled: Boolean(config?.enabled), providers: Array.isArray(config?.providers) ? config.providers : [] });
      setOauthLinks(Array.isArray(links?.identities) ? links.identities : []);
      setOauthHasPassword(Boolean(links?.has_password));
    } catch {
      setOauthConfig({ enabled: false, providers: [] });
    }
  }

  async function linkOAuth(provider, clientId) {
    setError('');
    setOauthBusy(provider);
    try {
      const { credential, nonce } = await requestOAuthCredential(provider, clientId);
      await api('/api/auth/oauth/links', {
        method: 'POST',
        body: JSON.stringify({ provider, credential, nonce }),
      });
      await refreshOAuth();
    } catch (e) {
      setError(e.message);
    } finally {
      setOauthBusy('');
    }
  }

  async function unlinkOAuth(provider) {
    setError('');
    setOauthBusy(provider);
    try {
      await api(`/api/auth/oauth/links/${encodeURIComponent(provider)}`, { method: 'DELETE' });
      await refreshOAuth();
    } catch (e) {
      setError(e.message);
    } finally {
      setOauthBusy('');
    }
  }

  // === Longbridge handlers ===
  async function addLb(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const rows = await api('/api/longbridge/accounts', {
        method: 'POST',
        body: JSON.stringify({
          name: lbForm.name,
          label: lbForm.label || null,
          app_key: lbForm.app_key,
          app_secret: lbForm.app_secret,
          access_token: lbForm.access_token,
          set_default: lbForm.set_default,
        }),
      });
      setLbAccounts(rows);
      setLbForm(emptyAccount);
    } catch (e2) { setError(e2.message); } finally { setBusy(false); }
  }
  async function deleteLb(name) {
    if (!confirm(t('admin2.confirmDeleteLb').replace('{name}', name))) return;
    try {
      const rows = await api(`/api/longbridge/accounts/${encodeURIComponent(name)}`, { method: 'DELETE' });
      setLbAccounts(rows);
    } catch (e) { setError(e.message); }
  }
  async function setLbDefault(name) {
    try {
      const rows = await api(`/api/longbridge/accounts/${encodeURIComponent(name)}/default`, { method: 'POST' });
      setLbAccounts(rows);
    } catch (e) { setError(e.message); }
  }

  // === Alpaca handlers ===
  const alpacaRows = brokerRows.filter((r) => r.broker === 'alpaca');
  async function addAlpaca(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const rows = await api('/api/brokers/accounts', {
        method: 'POST',
        body: JSON.stringify({
          broker: 'alpaca',
          name: apForm.name,
          label: apForm.label || null,
          api_key: apForm.api_key,
          api_secret: apForm.api_secret,
          paper: apForm.paper,
          set_default: apForm.set_default,
        }),
      });
      setBrokerRows(rows);
      setApForm(emptyAlpacaAccount);
    } catch (e2) { setError(e2.message); } finally { setBusy(false); }
  }
  async function deleteAlpaca(name) {
    if (!confirm(t('admin2.confirmDeleteAlpaca').replace('{name}', name))) return;
    try {
      const rows = await api(`/api/brokers/accounts/alpaca/${encodeURIComponent(name)}`, { method: 'DELETE' });
      setBrokerRows(rows);
    } catch (e) { setError(e.message); }
  }
  async function setAlpacaDefault(name) {
    try {
      const rows = await api(`/api/brokers/accounts/alpaca/${encodeURIComponent(name)}/default`, { method: 'POST' });
      setBrokerRows(rows);
    } catch (e) { setError(e.message); }
  }

  // === uSMART handlers ===
  const usmartRows = brokerRows.filter((r) => r.broker === 'usmart');
  async function addUsmart(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const rows = await api('/api/brokers/accounts', {
        method: 'POST',
        body: JSON.stringify({
          broker: 'usmart',
          name: usForm.name,
          label: usForm.label || null,
          channel: usForm.channel,
          sign_private_key: usForm.sign_private_key,
          encrypt_public_key: usForm.encrypt_public_key,
          phone: usForm.phone,
          area_code: usForm.area_code || '852',
          trade_password: usForm.trade_password,
          paper: usForm.paper,
          set_default: usForm.set_default,
        }),
      });
      setBrokerRows(rows);
      setUsForm(emptyUsmartAccount);
    } catch (e2) { setError(e2.message); } finally { setBusy(false); }
  }
  async function deleteUsmart(name) {
    if (!confirm(t('admin2.confirmDeleteUsmart').replace('{name}', name))) return;
    try {
      const rows = await api(`/api/brokers/accounts/usmart/${encodeURIComponent(name)}`, { method: 'DELETE' });
      setBrokerRows(rows);
    } catch (e) { setError(e.message); }
  }
  async function setUsmartDefault(name) {
    try {
      const rows = await api(`/api/brokers/accounts/usmart/${encodeURIComponent(name)}/default`, { method: 'POST' });
      setBrokerRows(rows);
    } catch (e) { setError(e.message); }
  }

  // === User provider handlers ===
  async function addUp(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api('/api/user-providers', { method: 'POST', body: JSON.stringify(upForm) });
      setUpForm(emptyUserProvider);
      await refreshProviders?.();
    } catch (e2) { setError(e2.message); } finally { setBusy(false); }
  }
  async function deleteUp(name) {
    if (!confirm(t('admin2.confirmDeleteAiKey').replace('{name}', name))) return;
    try {
      const rawName = String(name || '').replace(/^user:/, '');
      await api(`/api/user-providers/${encodeURIComponent(rawName)}`, { method: 'DELETE' });
      await refreshProviders?.();
    } catch (e) { setError(e.message); }
  }

  // === Server provider (admin) handlers ===
  async function addSv(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api('/api/providers', { method: 'POST', body: JSON.stringify(svForm) });
      setSvForm(emptyProvider);
      await refreshProviders?.();
    } catch (e2) { setError(e2.message); } finally { setBusy(false); }
  }
  async function deleteSv(name) {
    if (!confirm(t('admin2.confirmDeleteServerModel').replace('{name}', name))) return;
    try {
      await api(`/api/providers/${encodeURIComponent(name)}`, { method: 'DELETE' });
      await refreshProviders?.();
    } catch (e) { setError(e.message); }
  }

  return (
    <main className="shell accounts-shell">
      {embedded && (
        <PageHeader
          eyebrow="Connections"
          title={t('admin2.accountsTitle')}
          subtitle={t('admin2.accountsSubtitle')}
          actions={
            <button className="ghost" type="button" onClick={refreshAll} disabled={busy}>
              {busy ? t('admin2.refreshingEllipsis') : t('admin2.refresh')}
            </button>
          }
        />
      )}

      {error && <div className="error-banner">{error}</div>}

      <div className="accounts-grid">
        {/* 券商账号 */}
        {canTrade ? (
          <>
            <BrokerCard
              brand="Longbridge"
              kind={t('admin2.lbKind')}
              docsHref="https://open.longbridge.com/"
              rows={lbAccounts}
              onDelete={deleteLb}
              onSetDefault={setLbDefault}
              describeRow={(r) => `${r.sdk_credentials_configured ? `API ****${r.sdk_app_key_suffix || '----'}` : t('admin2.apiNotConfigured')}`}
            >
              <form className="account-form" onSubmit={addLb}>
                <input placeholder={t('admin2.accountCodePlaceholderLb')} value={lbForm.name} onChange={(e) => setLbForm({ ...lbForm, name: e.target.value })} required />
                <input placeholder={t('admin2.displayNameOptional')} value={lbForm.label} onChange={(e) => setLbForm({ ...lbForm, label: e.target.value })} />
                <input placeholder="App Key" value={lbForm.app_key} onChange={(e) => setLbForm({ ...lbForm, app_key: e.target.value.trim() })} required />
                <input type="password" placeholder="App Secret" value={lbForm.app_secret} onChange={(e) => setLbForm({ ...lbForm, app_secret: e.target.value })} autoComplete="off" required />
                <input type="password" placeholder="Access Token" value={lbForm.access_token} onChange={(e) => setLbForm({ ...lbForm, access_token: e.target.value })} autoComplete="off" required />
                <Toggle checked={lbForm.set_default} onChange={(v) => setLbForm({ ...lbForm, set_default: v })} label={t('admin2.setDefaultLb')} />
                <button type="submit" disabled={busy}>{t('admin2.saveLbAccount')}</button>
              </form>
            </BrokerCard>

            <BrokerCard
              brand="Alpaca"
              kind={t('admin2.alpacaKind')}
              docsHref="https://alpaca.markets/"
              rows={alpacaRows}
              onDelete={deleteAlpaca}
              onSetDefault={setAlpacaDefault}
              describeRow={(r) => `${r.paper ? t('admin2.paperMode') : t('admin2.liveMode')}${r.api_key_suffix ? ` · API ****${r.api_key_suffix}` : ` · ${t('admin2.apiNotConfigured')}`}`}
            >
              <form className="account-form" onSubmit={addAlpaca}>
                <input placeholder={t('admin2.accountCodePlaceholderAlpaca')} value={apForm.name} onChange={(e) => setApForm({ ...apForm, name: e.target.value })} required />
                <input placeholder={t('admin2.displayNameOptional')} value={apForm.label} onChange={(e) => setApForm({ ...apForm, label: e.target.value })} />
                <input placeholder="Alpaca API Key" value={apForm.api_key} onChange={(e) => setApForm({ ...apForm, api_key: e.target.value.trim() })} required />
                <input type="password" placeholder="Alpaca API Secret" value={apForm.api_secret} onChange={(e) => setApForm({ ...apForm, api_secret: e.target.value })} autoComplete="off" required />
                <Toggle checked={apForm.paper} onChange={(v) => setApForm({ ...apForm, paper: v })} label={t('admin2.paperToggle')} />
                <Toggle checked={apForm.set_default} onChange={(v) => setApForm({ ...apForm, set_default: v })} label={t('admin2.setDefaultAlpaca')} />
                <button type="submit" disabled={busy}>{t('admin2.saveAlpacaAccount')}</button>
              </form>
            </BrokerCard>

            <BrokerCard
              brand="uSMART"
              kind={t('admin2.usmartKind')}
              docsHref="https://api-doc.usmart8.com/zh-cn/"
              rows={usmartRows}
              onDelete={deleteUsmart}
              onSetDefault={setUsmartDefault}
              describeRow={(r) => `${r.paper ? t('admin2.paperMode') : t('admin2.liveMode')}${r.api_key_suffix ? ` · CH ****${r.api_key_suffix}` : ` · ${t('admin2.apiNotConfigured')}`}`}
            >
              <form className="account-form" onSubmit={addUsmart}>
                <input placeholder={t('admin2.accountCodePlaceholderUsmart')} value={usForm.name} onChange={(e) => setUsForm({ ...usForm, name: e.target.value })} required />
                <input placeholder={t('admin2.displayNameOptional')} value={usForm.label} onChange={(e) => setUsForm({ ...usForm, label: e.target.value })} />
                <input placeholder={t('admin2.usmartChannel')} value={usForm.channel} onChange={(e) => setUsForm({ ...usForm, channel: e.target.value.trim() })} required />
                <textarea className="account-key-area" placeholder={t('admin2.usmartSignKey')} value={usForm.sign_private_key} onChange={(e) => setUsForm({ ...usForm, sign_private_key: e.target.value })} autoComplete="off" required />
                <textarea className="account-key-area" placeholder={t('admin2.usmartEncryptKey')} value={usForm.encrypt_public_key} onChange={(e) => setUsForm({ ...usForm, encrypt_public_key: e.target.value })} autoComplete="off" required />
                <input placeholder={t('admin2.usmartAreaCode')} value={usForm.area_code} onChange={(e) => setUsForm({ ...usForm, area_code: e.target.value.trim() })} />
                <input placeholder={t('admin2.usmartPhone')} value={usForm.phone} onChange={(e) => setUsForm({ ...usForm, phone: e.target.value.trim() })} required />
                <input type="password" placeholder={t('admin2.usmartTradePassword')} value={usForm.trade_password} onChange={(e) => setUsForm({ ...usForm, trade_password: e.target.value })} autoComplete="off" />
                <Toggle checked={usForm.paper} onChange={(v) => setUsForm({ ...usForm, paper: v })} label={t('admin2.paperToggle')} />
                <Toggle checked={usForm.set_default} onChange={(v) => setUsForm({ ...usForm, set_default: v })} label={t('admin2.setDefaultUsmart')} />
                <button type="submit" disabled={busy}>{t('admin2.saveUsmartAccount')}</button>
              </form>
            </BrokerCard>
          </>
        ) : (
          <section className="panel">
            <SectionTitle title={t('admin2.brokerAccounts')} />
            <p className="muted">{t('admin2.tradeNotEnabled')}</p>
          </section>
        )}

        {/* 我的 AI Key */}
        <section className="panel">
          <SectionTitle title={t('admin2.myAiKey')} />
          <p className="muted">{t('admin2.myAiKeyDesc')}</p>
          <div className="provider-list">
            {userProviders.map((p) => (
              <div className="provider" key={p.name}>
                <div>
                  <strong>{p.label || p.raw_name}</strong>
                  <span>{providerTypeLabel(p.provider_type)} · {p.model} · ****{p.api_key_suffix || '----'}{p.is_default ? ` · ${t('admin2.defaultTag')}` : ''}</span>
                </div>
                <button type="button" onClick={() => deleteUp(p.name)}>{t('common.delete')}</button>
              </div>
            ))}
            {!userProviders.length && <p className="muted">{t('admin2.noOwnAiKey')}</p>}
          </div>
          <details className="account-form-disclosure" open={!userProviders.length}>
            <summary>{t('admin2.saveMyAiKey')}</summary>
            <form className="provider-form" onSubmit={addUp}>
              <input placeholder={t('admin2.codePlaceholderClaude')} value={upForm.name} onChange={(e) => setUpForm({ ...upForm, name: e.target.value })} required />
              <input placeholder={t('admin2.displayNameOptional')} value={upForm.label} onChange={(e) => setUpForm({ ...upForm, label: e.target.value })} />
              <select value={upForm.provider_type} onChange={(e) => setUpForm({ ...upForm, provider_type: e.target.value })}>
                {aiProviderTypeItems.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <input placeholder={upForm.provider_type === 'claude' ? 'https://api.anthropic.com' : t('admin2.apiEndpoint')} value={upForm.base_url} onChange={(e) => setUpForm({ ...upForm, base_url: e.target.value })} required />
              <input placeholder={upForm.provider_type === 'claude' ? 'claude-sonnet-4-5' : t('admin2.modelName')} value={upForm.model} onChange={(e) => setUpForm({ ...upForm, model: e.target.value })} required />
              <input type="password" placeholder="API Key" value={upForm.api_key} onChange={(e) => setUpForm({ ...upForm, api_key: e.target.value })} autoComplete="off" required />
              <Toggle checked={upForm.is_default} onChange={(v) => setUpForm({ ...upForm, is_default: v })} label={t('admin2.setDefaultOwnModel')} />
              <button type="submit" disabled={busy}>{t('admin2.saveMyAiKey')}</button>
            </form>
          </details>
        </section>

        {/* 登录绑定（Google / Apple） */}
        {oauthConfig.enabled && (
          <section className="panel">
            <SectionTitle title={t('admin2.oauthTitle')} />
            <p className="muted">{t('admin2.oauthDesc')}</p>
            <div className="provider-list">
              {oauthConfig.providers.map(({ provider, client_id: clientId }) => {
                const linked = oauthLinks.find((row) => row.provider === provider);
                const isLastMethod = !oauthHasPassword && oauthLinks.length <= 1;
                return (
                  <div className="provider" key={provider}>
                    <div>
                      <strong>{provider === 'google' ? 'Google' : provider === 'apple' ? 'Apple' : provider}</strong>
                      <span>{linked ? `${t('admin2.oauthLinked')}${linked.email ? ` · ${linked.email}` : ''}` : t('admin2.oauthNotLinked')}</span>
                    </div>
                    {linked ? (
                      <button
                        type="button"
                        disabled={Boolean(oauthBusy) || isLastMethod}
                        title={isLastMethod ? t('admin2.oauthLastMethod') : ''}
                        onClick={() => unlinkOAuth(provider)}
                      >
                        {t('admin2.oauthUnlink')}
                      </button>
                    ) : (
                      <button type="button" disabled={Boolean(oauthBusy)} onClick={() => linkOAuth(provider, clientId)}>
                        {t('admin2.oauthLink')}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* AI 模型管理（管理员） */}
        {canAdmin && (
          <section className="panel">
            <SectionTitle title={t('admin2.aiModelMgmt')} />
            <p className="muted">{t('admin2.aiModelMgmtDesc')}</p>
            <div className="provider-list">
              {serverProviders.map((p) => (
                <div className="provider" key={p.name}>
                  <div>
                    <strong>{p.name}</strong>
                    <span>{providerTypeLabel(p.provider_type)} · {p.model}{p.configured === false ? ` · ${t('admin2.serverKeyNotConfigured')}` : ` · ${t('admin2.serverKeyConfigured')}`}</span>
                  </div>
                  <button type="button" disabled={p.name === 'deepseek'} onClick={() => deleteSv(p.name)}>{t('common.delete')}</button>
                </div>
              ))}
            </div>
            <details className="account-form-disclosure">
              <summary>{t('admin2.addServerModel')}</summary>
              <form className="provider-form" onSubmit={addSv}>
                <input placeholder={t('admin2.modelCodePlaceholder')} value={svForm.name} onChange={(e) => setSvForm({ ...svForm, name: e.target.value })} required />
                <select value={svForm.provider_type} onChange={(e) => setSvForm({ ...svForm, provider_type: e.target.value })}>
                  {aiProviderTypeItems.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
                <input placeholder={svForm.provider_type === 'claude' ? 'https://api.anthropic.com' : t('admin2.apiEndpoint')} value={svForm.base_url} onChange={(e) => setSvForm({ ...svForm, base_url: e.target.value })} required />
                <input placeholder={svForm.provider_type === 'claude' ? 'claude-sonnet-4-5' : t('admin2.modelName')} value={svForm.model} onChange={(e) => setSvForm({ ...svForm, model: e.target.value })} required />
                <input placeholder={svForm.provider_type === 'claude' ? t('admin2.keyEnvClaude') : t('admin2.keyEnvOpenai')} value={svForm.api_key_env} onChange={(e) => setSvForm({ ...svForm, api_key_env: e.target.value })} required />
                <button type="submit" disabled={busy}>{t('admin2.addServerModel')}</button>
              </form>
            </details>
          </section>
        )}
      </div>
    </main>
  );
}

function BrokerCard({ brand, kind, docsHref, rows = [], onDelete, onSetDefault, describeRow, children }) {
  return (
    <section className="panel broker-card">
      <header className="broker-card-head">
        <div>
          <strong>{brand}</strong>
          <small>{kind}</small>
        </div>
        {docsHref && (
          <a className="ghost" href={docsHref} target="_blank" rel="noreferrer">{t('admin2.openPlatform')}</a>
        )}
      </header>
      <div className="account-list">
        {rows.map((r) => (
          <div className={`account ${r.is_default ? 'active' : ''}`} key={`${r.broker || brand}-${r.name}`}>
            <div className="account-info">
              <strong>{r.label || r.name}</strong>
              <span>{r.name}{r.is_default ? ` · ${t('admin2.defaultTag')}` : ''} · {describeRow ? describeRow(r) : ''}</span>
            </div>
            <div className="schedule-profile-actions">
              <button className="ghost" type="button" disabled={r.is_default} onClick={() => onSetDefault(r.name)}>{t('admin2.setAsDefault')}</button>
              <button className="ghost" type="button" onClick={() => onDelete(r.name)}>{t('common.delete')}</button>
            </div>
          </div>
        ))}
        {!rows.length && <p className="muted">{t('admin2.noBrokerAccount').replace('{brand}', brand)}</p>}
      </div>
      <details className="account-form-disclosure" open={!rows.length}>
        <summary>{t('admin2.addAccount') || t('common.add')}</summary>
        {children}
      </details>
    </section>
  );
}
