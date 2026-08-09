import React from 'react';
import { CollapsiblePanel, Toggle } from './common.jsx';
import { t } from '../i18n/index.js';
import { aiProviderTypeItems } from '../config.js';

export function TradingAccountPanel({
  accountForm,
  accountPanelOpen,
  accounts,
  addLongbridgeAccount,
  auth,
  authLoading,
  deleteLongbridgeAccount,
  forceRefreshAuth,
  longbridgeAccount,
  setAccountForm,
  setAccountPanelOpen,
  setDefaultLongbridgeAccount,
  setLongbridgeAccount,
}) {
  return (
    <CollapsiblePanel title={t('scanner2.tradingAccountConnect')} open={accountPanelOpen} onToggle={() => setAccountPanelOpen(!accountPanelOpen)}>
      <div className="account-help">
        <div>
          <strong>{t('scanner2.lbApiInfoSource')}</strong>
          <p>{t('scanner2.lbApiInfoDesc')}</p>
        </div>
        <a className="ghost" href="https://open.longbridge.com/" target="_blank" rel="noreferrer">{t('scanner2.openPlatform')}</a>
      </div>
      <div className="auth-status">
        <span>{auth?.account?.label || longbridgeAccount}</span>
        <strong className={auth?.session?.token === 'valid' ? 'ok' : ''}>
          {auth?.session?.token ?? '--'}
        </strong>
      </div>
      <p className="auth-detail">{auth?.session?.detail || t('scanner2.checkConnHint')}</p>
      {auth?.account?.identity_fingerprint && (
        <div className="auth-box compact">
          <span>{t('scanner2.accountFingerprint')}</span>
          <strong className="code">{auth.account.identity_fingerprint}</strong>
          <small>{auth.account.region || 'region --'} · {auth.account.identity_updated_at || '--'}</small>
        </div>
      )}
      {auth?.account?.sdk_credentials_configured && (
        <div className="auth-box">
          <span>{t('scanner2.apiCredentials')}</span>
          <strong className="code">APP KEY · ****{auth.account.sdk_app_key_suffix || '----'}</strong>
          <small>{auth.account.sdk_credentials_updated_at || '--'}</small>
        </div>
      )}
      <div className="auth-actions">
        <button className="ghost" type="button" disabled={authLoading || !longbridgeAccount} onClick={forceRefreshAuth}>{t('scanner2.checkStatus')}</button>
      </div>
      <div className="account-list">
        {accounts.map((account) => (
          <div className={`account ${account.name === longbridgeAccount ? 'active' : ''}`} key={account.name}>
            <button type="button" onClick={() => setLongbridgeAccount(account.name)}>
              <strong>{account.label || account.name}</strong>
              <span>{account.name}{account.is_default ? ` · ${t('scanner2.default')}` : ''}{account.sdk_credentials_configured ? ` · API ****${account.sdk_app_key_suffix || '----'}` : ` · ${t('scanner2.apiNotConfigured')}`}</span>
            </button>
            <div className="schedule-profile-actions">
              <button className="ghost" type="button" disabled={account.is_default} onClick={() => setDefaultLongbridgeAccount(account.name)}>{t('scanner2.default')}</button>
              <button className="ghost" type="button" disabled={account.name === 'default'} onClick={() => deleteLongbridgeAccount(account.name)}>{t('common.delete')}</button>
            </div>
          </div>
        ))}
        {!accounts.length && <p className="muted">{t('scanner2.noTradingAccount')}</p>}
      </div>
      <form className="account-form" onSubmit={addLongbridgeAccount}>
        <input placeholder={t('scanner2.accountCodePlaceholder')} value={accountForm.name} onChange={(event) => setAccountForm({ ...accountForm, name: event.target.value })} required />
        <input placeholder={t('scanner2.accountLabelPlaceholder')} value={accountForm.label} onChange={(event) => setAccountForm({ ...accountForm, label: event.target.value })} />
        <input placeholder="Longbridge App Key" value={accountForm.app_key} onChange={(event) => setAccountForm({ ...accountForm, app_key: event.target.value.trim() })} required />
        <input type="password" placeholder="Longbridge App Secret" value={accountForm.app_secret} onChange={(event) => setAccountForm({ ...accountForm, app_secret: event.target.value })} autoComplete="off" required />
        <input type="password" placeholder="Longbridge Access Token" value={accountForm.access_token} onChange={(event) => setAccountForm({ ...accountForm, access_token: event.target.value })} autoComplete="off" required />
        <Toggle checked={accountForm.set_default} onChange={(checked) => setAccountForm({ ...accountForm, set_default: checked })} label={t('scanner2.setAsDefault')} />
        <button>{t('scanner2.saveApiAccount')}</button>
      </form>
    </CollapsiblePanel>
  );
}

export function UserProviderPanel({
  addUserProvider,
  deleteUserProvider,
  setUserProviderForm,
  setUserProviderPanelOpen,
  userProviderForm,
  userProviderPanelOpen,
  userProviders,
}) {
  return (
    <CollapsiblePanel title={t('scanner2.myAiKey')} open={userProviderPanelOpen} onToggle={() => setUserProviderPanelOpen(!userProviderPanelOpen)}>
      <div className="account-help">
        <div>
          <strong>{t('scanner2.platformKeyCoexist')}</strong>
          <p>{t('scanner2.platformKeyCoexistDesc')}</p>
        </div>
      </div>
      <div className="provider-list">
        {userProviders.map((provider) => (
          <div className="provider" key={provider.name}>
            <div>
              <strong>{provider.label || provider.raw_name}</strong>
              <span>{providerTypeLabel(provider.provider_type)} · {provider.model} · ****{provider.api_key_suffix || '----'}{provider.is_default ? ` · ${t('scanner2.default')}` : ''}</span>
            </div>
            <button type="button" onClick={() => deleteUserProvider(provider.name)}>{t('common.delete')}</button>
          </div>
        ))}
        {!userProviders.length && <p className="muted">{t('scanner2.noOwnAiKey')}</p>}
      </div>
      <form className="provider-form" onSubmit={addUserProvider}>
        <input placeholder={t('scanner2.providerCodePlaceholder')} value={userProviderForm.name} onChange={(event) => setUserProviderForm({ ...userProviderForm, name: event.target.value })} required />
        <input placeholder={t('scanner2.providerLabelPlaceholder')} value={userProviderForm.label} onChange={(event) => setUserProviderForm({ ...userProviderForm, label: event.target.value })} />
        <select value={userProviderForm.provider_type} onChange={(event) => setUserProviderForm({ ...userProviderForm, provider_type: event.target.value })}>
          {aiProviderTypeItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <input placeholder={userProviderForm.provider_type === 'claude' ? t('scanner2.baseUrlClaudePlaceholder') : t('scanner2.baseUrlPlaceholder')} value={userProviderForm.base_url} onChange={(event) => setUserProviderForm({ ...userProviderForm, base_url: event.target.value })} required />
        <input placeholder={userProviderForm.provider_type === 'claude' ? t('scanner2.modelNameClaudePlaceholder') : t('scanner2.modelNamePlaceholder')} value={userProviderForm.model} onChange={(event) => setUserProviderForm({ ...userProviderForm, model: event.target.value })} required />
        <input type="password" placeholder="API Key" value={userProviderForm.api_key} onChange={(event) => setUserProviderForm({ ...userProviderForm, api_key: event.target.value })} autoComplete="off" required />
        <Toggle checked={userProviderForm.is_default} onChange={(checked) => setUserProviderForm({ ...userProviderForm, is_default: checked })} label={t('scanner2.saveAsDefaultModel')} />
        <button>{t('scanner2.saveMyAiKey')}</button>
      </form>
    </CollapsiblePanel>
  );
}

export function AdminProviderPanel({
  addProvider,
  canAdmin,
  deleteProvider,
  providerForm,
  providerPanelOpen,
  providers,
  setProviderForm,
  setProviderPanelOpen,
}) {
  if (!canAdmin) return null;
  return (
    <CollapsiblePanel title={t('scanner2.aiModelAdmin')} open={providerPanelOpen} onToggle={() => setProviderPanelOpen(!providerPanelOpen)}>
      <div className="provider-list">
        {providers.filter((provider) => provider.server_managed !== false).map((provider) => (
          <div className="provider" key={provider.name}>
            <div>
              <strong>{provider.name}</strong>
              <span>{providerTypeLabel(provider.provider_type)} · {provider.model}{provider.configured === false ? ` · ${t('scanner2.serverKeyMissing')}` : ` · ${t('scanner2.serverKeyConfigured')}`}</span>
            </div>
            <button disabled={provider.name === 'deepseek'} onClick={() => deleteProvider(provider.name)}>
              {t('common.delete')}
            </button>
          </div>
        ))}
      </div>
      <form className="provider-form" onSubmit={addProvider}>
        <input placeholder={t('scanner2.modelCodePlaceholder')} value={providerForm.name} onChange={(event) => setProviderForm({ ...providerForm, name: event.target.value })} required />
        <select value={providerForm.provider_type} onChange={(event) => setProviderForm({ ...providerForm, provider_type: event.target.value })}>
          {aiProviderTypeItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <input placeholder={providerForm.provider_type === 'claude' ? t('scanner2.baseUrlClaudePlaceholder') : t('scanner2.baseUrlPlaceholder')} value={providerForm.base_url} onChange={(event) => setProviderForm({ ...providerForm, base_url: event.target.value })} required />
        <input placeholder={providerForm.provider_type === 'claude' ? t('scanner2.modelNameClaudePlaceholder') : t('scanner2.modelNamePlaceholder')} value={providerForm.model} onChange={(event) => setProviderForm({ ...providerForm, model: event.target.value })} required />
        <input placeholder={providerForm.provider_type === 'claude' ? t('scanner2.apiKeyEnvClaudePlaceholder') : t('scanner2.apiKeyEnvPlaceholder')} value={providerForm.api_key_env} onChange={(event) => setProviderForm({ ...providerForm, api_key_env: event.target.value })} required />
        <button>{t('scanner2.addAi')}</button>
      </form>
    </CollapsiblePanel>
  );
}

function providerTypeLabel(value) {
  return value === 'claude' ? 'Claude Compatible' : 'OpenAI Compatible';
}
