import React from 'react';
import { ArrowLeft, Bell, Bot, CheckCircle2, KeyRound, Link2, Mail, MessageCircle, Send, ShieldCheck } from 'lucide-react';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { t } from '../i18n/index.js';

// Channel guides. Display copy (title/summary/field descriptions/steps/test) is
// resolved from the i18n dictionary at render time under notify.guide.channel.*.
// Field key names and brand titles stay literal.
const channelGuides = [
  {
    step: 'TG',
    key: 'telegram',
    title: 'Telegram',
    icon: Bot,
    fields: ['type', 'botToken', 'chatId', 'secret'],
    stepCount: 4,
  },
  {
    step: 'SL',
    key: 'slack',
    title: 'Slack',
    icon: MessageCircle,
    fields: ['type', 'webhookUrl', 'secret'],
    stepCount: 3,
  },
  {
    step: 'DC',
    key: 'discord',
    title: 'Discord',
    icon: MessageCircle,
    fields: ['type', 'webhookUrl', 'secret'],
    stepCount: 3,
  },
  {
    step: 'FS',
    key: 'feishu',
    title: '飞书',
    icon: MessageCircle,
    fields: ['type', 'webhookUrl', 'secret'],
    stepCount: 3,
  },
  {
    step: 'WA',
    key: 'whatsapp',
    title: 'WhatsApp',
    icon: Send,
    fields: ['type', 'phoneNumberId', 'accessToken', 'recipient', 'template'],
    stepCount: 3,
  },
  {
    step: 'EM',
    key: 'email',
    title: 'Email',
    icon: Mail,
    fields: ['type', 'recipientEmail', 'serverSmtp'],
    stepCount: 3,
  },
  {
    step: 'WH',
    key: 'webhook',
    titleKey: 'notify.guide.webhookTitle',
    icon: Link2,
    fields: ['type', 'webhookUrl', 'secret', 'signatureHeader'],
    stepCount: 3,
  },
];

export function NotificationGuidePage({ onBack, onNotifications, session, onLogout, routeMode, onRouteModeChange }) {
  return (
    <main className="shell guide-shell notification-guide-shell">
      <section className="hero app-hero">
        <div className="hero-brand">
          <div className="radar-logo"><Bell size={28} /></div>
          <div>
            <div className="eyebrow">Notification Setup Guide</div>
            <h1>{t('notify.guide.title')}</h1>
            <p>{t('notify.guide.intro')}</p>
          </div>
        </div>
        <div className="hero-controls">
          <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
          <div className="hero-actions">
            <button className="primary nav-action" type="button" onClick={onNotifications}><Bell size={14} /> {t('notify.guide.openCenter')}</button>
            <button className="ghost nav-action" type="button" onClick={onBack}><ArrowLeft size={14} /> {t('notify.guide.backToRadar')}</button>
          </div>
        </div>
      </section>

      <section className="guide-hero panel notification-guide-hero">
        <div>
          <span className="guide-step">{t('notify.guide.flow')}</span>
          <h2>{t('notify.guide.flowHeading')}</h2>
          <p>{t('notify.guide.flowDesc')}</p>
        </div>
        <div className="guide-prompt">
          <strong>{t('notify.guide.recommendedOrder')}</strong>
          <code>{t('notify.guide.order1')}</code>
          <code>{t('notify.guide.order2')}</code>
          <code>{t('notify.guide.order3')}</code>
          <code>{t('notify.guide.order4')}</code>
        </div>
      </section>

      <section className="preset-guide-toolbar panel notification-guide-toolbar">
        <div>
          <strong><KeyRound size={14} /> {t('notify.guide.sensitiveTitle')}</strong>
          <span>{t('notify.guide.sensitiveDesc')}</span>
        </div>
        <div>
          <strong><CheckCircle2 size={14} /> {t('notify.guide.testFirstTitle')}</strong>
          <span>{t('notify.guide.testFirstDesc')}</span>
        </div>
        <div>
          <strong><ShieldCheck size={14} /> {t('notify.guide.securityTitle')}</strong>
          <span>{t('notify.guide.securityDesc')}</span>
        </div>
      </section>

      <div className="notification-guide-grid">
        {channelGuides.map((guide) => {
          const Icon = guide.icon;
          const title = guide.titleKey ? t(guide.titleKey) : guide.title;
          const stepKeys = Array.from({ length: guide.stepCount || 0 }, (_, i) => i + 1);
          return (
            <section className="panel notification-guide-card" key={guide.key}>
              <div className="notification-guide-card-head">
                <span className="guide-step">{guide.step}</span>
                <Icon size={18} />
              </div>
              <h2>{title}</h2>
              <p>{t(`notify.guide.channel.${guide.key}.summary`)}</p>
              <div className="notification-field-table">
                {guide.fields.map((fieldKey) => (
                  <div key={`${guide.key}-${fieldKey}`}>
                    <strong>{t(`notify.guide.channel.${guide.key}.field.${fieldKey}.label`)}</strong>
                    <span>{t(`notify.guide.channel.${guide.key}.field.${fieldKey}.value`)}</span>
                  </div>
                ))}
              </div>
              <div className="guide-list">
                {stepKeys.map((n) => <p key={`${guide.key}-step-${n}`}>{t(`notify.guide.channel.${guide.key}.step.${n}`)}</p>)}
              </div>
              <small>{t(`notify.guide.channel.${guide.key}.test`)}</small>
            </section>
          );
        })}
      </div>
    </main>
  );
}
