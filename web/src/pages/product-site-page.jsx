import React from 'react';
import {
  Activity,
  ArrowRight,
  BarChart3,
  BellRing,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  Container,
  Cpu,
  Database,
  Gauge,
  Globe,
  Layers3,
  LineChart,
  LockKeyhole,
  Radar,
  Server,
  ShieldCheck,
  Sparkles,
  Terminal,
  Workflow,
  Zap,
} from 'lucide-react';
import { t } from '../i18n/index.js';

export function ProductSitePage({ onOpenApp }) {
  const navItems = [
    [t('site.product.nav.workflow'), '#workflow'],
    [t('site.product.nav.capabilities'), '#capabilities'],
    [t('site.product.nav.council'), '#council'],
    [t('site.product.nav.safety'), '#safety'],
    [t('site.product.nav.stack'), '#stack'],
  ];

  const capabilityCards = [
    { icon: BrainCircuit, title: t('site.product.cap1Title'), body: t('site.product.cap1Body') },
    { icon: Layers3, title: t('site.product.cap2Title'), body: t('site.product.cap2Body') },
    { icon: BellRing, title: t('site.product.cap3Title'), body: t('site.product.cap3Body') },
    { icon: ShieldCheck, title: t('site.product.cap4Title'), body: t('site.product.cap4Body') },
  ];

  const workflowSteps = [
    ['01', t('site.product.step1Title'), t('site.product.step1Body')],
    ['02', t('site.product.step2Title'), t('site.product.step2Body')],
    ['03', t('site.product.step3Title'), t('site.product.step3Body')],
    ['04', t('site.product.step4Title'), t('site.product.step4Body')],
  ];

  const proofMetrics = [
    ['3', t('site.product.proof1Label'), t('site.product.proof1Detail')],
    ['13+', t('site.product.proof2Label'), t('site.product.proof2Detail')],
    ['7', t('site.product.proof3Label'), t('site.product.proof3Detail')],
    ['P0', t('site.product.proof4Label'), t('site.product.proof4Detail')],
  ];

  const councilRoles = [
    { icon: Zap, title: t('site.product.council1Title'), role: 'Aggressive Advisor', body: t('site.product.council1Body'), color: '#4feea4' },
    { icon: ShieldCheck, title: t('site.product.council2Title'), role: 'Risk-Conscious Advisor', body: t('site.product.council2Body'), color: '#60a5fa' },
    { icon: Gauge, title: t('site.product.council3Title'), role: 'Skeptic Advisor', body: t('site.product.council3Body'), color: '#f59e0b' },
    { icon: BrainCircuit, title: t('site.product.council4Title'), role: 'Moderator', body: t('site.product.council4Body'), color: '#a78bfa' },
  ];

  const safetyFeatures = [
    { icon: LockKeyhole, title: 'Readiness Gate', desc: t('site.product.safety1Desc') },
    { icon: CheckCircle2, title: t('site.product.safety2Title'), desc: t('site.product.safety2Desc') },
    { icon: Activity, title: t('site.product.safety3Title'), desc: t('site.product.safety3Desc') },
    { icon: BarChart3, title: t('site.product.safety4Title'), desc: t('site.product.safety4Desc') },
  ];

  const stackItems = [
    [t('site.product.stack1Title'), t('site.product.stack1Body')],
    [t('site.product.stack2Title'), t('site.product.stack2Body')],
    [t('site.product.stack3Title'), t('site.product.stack3Body')],
    [t('site.product.stack4Title'), t('site.product.stack4Body')],
    [t('site.product.stack5Title'), t('site.product.stack5Body')],
  ];

  const deployModes = [
    { icon: Container, title: t('site.product.deploy1Title'), desc: t('site.product.deploy1Desc') },
    { icon: Server, title: t('site.product.deploy2Title'), desc: t('site.product.deploy2Desc') },
    { icon: Terminal, title: t('site.product.deploy3Title'), desc: t('site.product.deploy3Desc') },
  ];

  return (
    <main className="site-page">
      {/* ── Hero ── */}
      <section className="site-hero" aria-label={t('site.product.heroAria')}>
        <nav className="site-nav">
          <a className="site-brand" href="/site" aria-label={t('site.product.brandAria')}>
            <img src="/logo.svg" alt="" />
            <span>AI Option</span>
          </a>
          <div className="site-nav-links">
            {navItems.map(([label, href]) => <a href={href} key={href}>{label}</a>)}
          </div>
          <button className="site-nav-cta" type="button" onClick={onOpenApp}>
            <LockKeyhole size={16} />
            <span>{t('site.product.enterWorkbench')}</span>
          </button>
        </nav>

        <div className="site-hero-copy">
          <div className="site-kicker">
            <Sparkles size={16} />
            <span>AI-powered options workflow</span>
          </div>
          <h1>AI Option</h1>
          <div className="site-hero-statement">
            {t('site.product.heroStatement1')}
            <br />
            {t('site.product.heroStatement2')}
          </div>
          <div className="site-hero-rule">Markets move. Volatility shifts. Static plans fail.</div>
          <p>
            {t('site.product.heroPara')}
          </p>
          <div className="site-hero-actions">
            <button className="site-primary-action" type="button" onClick={onOpenApp}>
              <Zap size={18} />
              <span>{t('site.product.runFirstStrategy')}</span>
              <ArrowRight size={18} />
            </button>
            <a className="site-secondary-action" href="#workflow">
              <Workflow size={18} />
              <span>{t('site.product.viewWorkflow')}</span>
            </a>
          </div>
          <div className="site-trust-line">
            <span>{t('site.product.trust1')}</span>
            <span>{t('site.product.trust2')}</span>
            <span>{t('site.product.trust3')}</span>
          </div>
        </div>
      </section>

      {/* ── Proof Metrics ── */}
      <section className="site-proof-strip" aria-label={t('site.product.metricsAria')}>
        {proofMetrics.map(([value, label, detail]) => (
          <div className="site-proof-item" key={label}>
            <strong>{value}</strong>
            <span>{label}</span>
            <small>{detail}</small>
          </div>
        ))}
      </section>

      {/* ── Workflow ── */}
      <hr className="site-section-divider" />
      <section className="site-section site-workflow" id="workflow">
        <div className="site-section-head">
          <span>Trading Workflow</span>
          <h2>{t('site.product.workflowHeading')}</h2>
        </div>
        <div className="site-workflow-grid">
          {workflowSteps.map(([number, title, body]) => (
            <article className="site-step" key={title}>
              <span>{number}</span>
              <h3>{title}</h3>
              <p>{body}</p>
              <ChevronRight size={18} />
            </article>
          ))}
        </div>
      </section>

      {/* ── Capabilities ── */}
      <hr className="site-section-divider" />
      <section className="site-section site-split" id="capabilities">
        <div className="site-section-head">
          <span>Core Capabilities</span>
          <h2>{t('site.product.capabilitiesHeading')}</h2>
        </div>
        <div className="site-capability-grid">
          {capabilityCards.map(({ icon: Icon, title, body }) => (
            <article className="site-capability-card" key={title}>
              <div className="site-card-icon"><Icon size={22} /></div>
              <h3>{title}</h3>
              <p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      {/* ── AI Council ── */}
      <hr className="site-section-divider" />
      <section className="site-section" id="council">
        <div className="site-section-head">
          <span>AI Council</span>
          <h2>{t('site.product.councilHeading')}</h2>
          <p className="site-section-sub">
            {t('site.product.councilSub')}
          </p>
        </div>
        <div className="site-council-grid">
          {councilRoles.map(({ icon: Icon, title, role, body, color }) => (
            <article className="site-council-card" key={title} style={{ '--council-color': color }}>
              <div className="site-council-icon"><Icon size={24} /></div>
              <div>
                <h3>{title}</h3>
                <span>{role}</span>
              </div>
              <p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      {/* ── Safety ── */}
      <hr className="site-section-divider" />
      <section className="site-section" id="safety">
        <div className="site-section-head">
          <span>Risk &amp; Safety</span>
          <h2>{t('site.product.safetyHeading')}</h2>
          <p className="site-section-sub">
            {t('site.product.safetySub')}
          </p>
        </div>
        <div className="site-safety-grid">
          {safetyFeatures.map(({ icon: Icon, title, desc }) => (
            <article className="site-safety-card" key={title}>
              <div className="site-safety-icon"><Icon size={20} /></div>
              <div>
                <h3>{title}</h3>
                <p>{desc}</p>
              </div>
            </article>
          ))}
        </div>
      </section>

      {/* ── Stack ── */}
      <hr className="site-section-divider" />
      <section className="site-section site-stack" id="stack">
        <div className="site-stack-visual" aria-hidden="true">
          <div><Radar size={26} /><span>Radar</span></div>
          <div><LineChart size={26} /><span>Market</span></div>
          <div><Bot size={26} /><span>AI</span></div>
          <div><Gauge size={26} /><span>Risk</span></div>
          <div><Activity size={26} /><span>Broker</span></div>
        </div>
        <div className="site-stack-copy">
          <div className="site-section-head">
            <span>Product Stack</span>
            <h2>{t('site.product.stackHeading')}</h2>
          </div>
          <div className="site-stack-list">
            {stackItems.map(([title, body]) => (
              <div key={title}>
                <CheckCircle2 size={17} />
                <strong>{title}</strong>
                <span>{body}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Deployment ── */}
      <section className="site-section">
        <div className="site-section-head">
          <span>Deployment</span>
          <h2>{t('site.product.deployHeading')}</h2>
        </div>
        <div className="site-deploy-grid">
          {deployModes.map(({ icon: Icon, title, desc }) => (
            <article className="site-deploy-card" key={title}>
              <div className="site-deploy-icon"><Icon size={22} /></div>
              <h3>{title}</h3>
              <p>{desc}</p>
            </article>
          ))}
        </div>
      </section>

      {/* ── Final CTA ── */}
      <section className="site-final-band">
        <div>
          <span><BarChart3 size={18} /> {t('site.product.finalBadge')}</span>
          <h2>{t('site.product.finalHeading')}</h2>
          <p>
            {t('site.product.finalPara')}
          </p>
        </div>
        <button className="site-primary-action" type="button" onClick={onOpenApp}>
          <span>{t('site.product.enterApp')}</span>
          <ArrowRight size={18} />
        </button>
      </section>

      {/* ── Footer ── */}
      <footer className="site-footer">
        <div className="site-footer-inner">
          <div className="site-footer-brand">
            <img src="/logo.svg" alt="" />
            <span>AI Option</span>
            <small>AI-powered options trading workflow</small>
          </div>
          <div className="site-footer-links">
            <div>
              <strong>{t('site.product.footerProduct')}</strong>
              <a href="#workflow">{t('site.product.nav.workflow')}</a>
              <a href="#capabilities">{t('site.product.footerCapabilities')}</a>
              <a href="#council">{t('site.product.nav.council')}</a>
              <a href="#safety">{t('site.product.footerSafety')}</a>
              <a href="#stack">{t('site.product.footerStack')}</a>
            </div>
            <div>
              <strong>{t('site.product.footerDeploy')}</strong>
              <span>Docker Compose</span>
              <span>{t('site.product.deploy3Title')}</span>
              <span>{t('site.product.footerFleet')}</span>
              <span>{t('site.product.footerAwsState')}</span>
            </div>
            <div>
              <strong>{t('site.product.footerIntegration')}</strong>
              <span>ThetaData</span>
              <span>Longbridge</span>
              <span>Alpaca</span>
              <span>DeepSeek / OpenAI</span>
            </div>
          </div>
        </div>
        <div className="site-footer-bottom">
          <span>{t('site.product.footerCopyright')}</span>
        </div>
      </footer>
    </main>
  );
}
