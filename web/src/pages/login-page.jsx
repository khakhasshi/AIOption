import React, { useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { TERMS_VERSION, routeItems } from '../config.js';
import { requestOAuthCredential } from '../utils/oauth-clients.js';
import { renderTurnstile } from '../utils/turnstile-widget.js';


export function LoginPage({ loading, onLogin, onOAuthLogin, oauthConfig, turnstileConfig, routeMode, onRouteModeChange }) {

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [oauthBusy, setOauthBusy] = useState('');
  const [error, setError] = useState('');
  const [captchaToken, setCaptchaToken] = useState('');
  const captchaRef = useRef(null);
  const widgetRef = useRef(null);
  const turnstileEnabled = Boolean(turnstileConfig?.enabled) && Boolean(turnstileConfig?.site_key);
  const captchaReady = !turnstileEnabled || Boolean(captchaToken);
  const canSubmit = !loading && !submitting && Boolean(username.trim()) && Boolean(password) && acceptedTerms && captchaReady;
  const oauthProviders = Array.isArray(oauthConfig?.providers) ? oauthConfig.providers : [];
  const oauthEnabled = Boolean(oauthConfig?.enabled) && oauthProviders.length > 0 && typeof onOAuthLogin === 'function';

  // Render the Cloudflare Turnstile widget once we know it's enabled. The token
  // is single-use and short-lived, so on expiry we clear it and the widget
  // auto-refreshes a new one via its callback.
  useEffect(() => {
    if (!turnstileEnabled || !captchaRef.current) return undefined;
    let cancelled = false;
    renderTurnstile(captchaRef.current, turnstileConfig.site_key, {
      onToken: (token) => { if (!cancelled) setCaptchaToken(token); },
      onExpire: () => { if (!cancelled) setCaptchaToken(''); },
      onError: () => { if (!cancelled) setCaptchaToken(''); },
    })
      .then((handle) => {
        if (cancelled) { handle.remove(); return; }
        widgetRef.current = handle;
      })
      .catch(() => { /* widget failed to load; backend still rejects a missing token */ });
    return () => {
      cancelled = true;
      widgetRef.current?.remove();
      widgetRef.current = null;
    };
  }, [turnstileEnabled, turnstileConfig?.site_key]);

  function resetCaptcha() {
    setCaptchaToken('');
    widgetRef.current?.reset();
  }

  async function submit(event) {
    event.preventDefault();
    if (!acceptedTerms) {
      setError(window._t('site.login.mustAcceptTerms'));
      return;
    }
    if (turnstileEnabled && !captchaToken) {
      setError(window._t('login.captchaRequired'));
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await onLogin(username.trim(), password, acceptedTerms, captchaToken);
    } catch (loginError) {
      setError(loginError.message || window._t('site.login.loginFailed'));
      resetCaptcha();
    } finally {
      setSubmitting(false);
    }
  }

  async function oauthSignIn(provider, clientId) {
    // The provider verifies the human; we still require the same terms consent
    // and captcha as password login before issuing a session.
    if (!acceptedTerms) {
      setError(window._t('site.login.mustAcceptTerms'));
      return;
    }
    if (turnstileEnabled && !captchaToken) {
      setError(window._t('login.captchaRequired'));
      return;
    }
    setError('');
    setOauthBusy(provider);
    try {
      const { credential, nonce } = await requestOAuthCredential(provider, clientId);
      await onOAuthLogin(provider, credential, nonce, acceptedTerms, captchaToken);
    } catch (oauthError) {
      setError(oauthError.message || window._t('login.oauthFailed'));
      resetCaptcha();
    } finally {
      setOauthBusy('');
    }
  }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <div>
          <div className="eyebrow">AI Option Scanner</div>
          <h1>{window._t('login.heading')}</h1>
          <p>{window._t('login.desc')}</p>
        </div>
        <div className="route-box">
          <strong>{window._t('login.routeLabel')}</strong>
          <p>{window._t('login.routeHint')}</p>
          <div className="route-pills">
            {routeItems.map(([value, label]) => (
              <button key={value} type="button" className={`route-pill ${routeMode === value ? 'active' : ''}`} onClick={() => onRouteModeChange(value)} disabled={loading}>
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="terms-box" aria-label={window._t('site.login.termsAria')}>
          <strong>{window._t('site.login.disclaimerTitle')}</strong>
          <p>{window._t('site.login.disclaimerP1')}</p>
          <p>{window._t('site.login.disclaimerP2')}</p>
          <p>{window._t('site.login.disclaimerP3')}</p>
          <p>{window._t('site.login.disclaimerP4')}</p>
          <p>{window._t('site.login.disclaimerP5')}</p>
          <small>{window._t('site.login.termsVersion')} {TERMS_VERSION}</small>
        </div>
        <form className="form login-form" onSubmit={submit}>
          <label>
            {window._t('login.account')}
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" disabled={loading || submitting} />
          </label>
          <label>
            {window._t('login.password')}
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" disabled={loading || submitting} />
          </label>
          <label className="terms-check">
            <input type="checkbox" checked={acceptedTerms} onChange={(event) => setAcceptedTerms(event.target.checked)} disabled={loading || submitting} />
            <span>{window._t('site.login.consentText').replace('{version}', TERMS_VERSION)}</span>
          </label>
          {error && <div className="error compact-error">{error}</div>}
          {turnstileEnabled && <div className="turnstile-box" ref={captchaRef} />}
          <button className="primary" disabled={!canSubmit}>
            {(loading || submitting) && <Loader2 className="spin" size={14} />}
            {loading ? window._t('login.checking') : submitting ? window._t('login.loggingIn') : acceptedTerms ? window._t('login.agreeLogin') : window._t('login.agreeRequired')}
          </button>
        </form>
        {oauthEnabled && (
          <div className="oauth-box">
            <div className="oauth-divider"><span>{window._t('login.oauthDivider')}</span></div>
            {oauthProviders.map(({ provider, client_id: clientId }) => (
              <button
                key={provider}
                type="button"
                className={`oauth-btn oauth-${provider}`}
                onClick={() => oauthSignIn(provider, clientId)}
                disabled={loading || submitting || Boolean(oauthBusy)}
              >
                {oauthBusy === provider && <Loader2 className="spin" size={14} />}
                {provider === 'google' ? window._t('login.oauthGoogle') : provider === 'apple' ? window._t('login.oauthApple') : provider}
              </button>
            ))}
            {!acceptedTerms && <small className="oauth-hint">{window._t('login.oauthTermsHint')}</small>}
          </div>
        )}
      </section>
    </main>
  );
}
