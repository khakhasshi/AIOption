import { t } from './i18n/index.js';

export const quickPrompts = [
  t('config.quickPrompt.1'),
  t('config.quickPrompt.2'),
  t('config.quickPrompt.3'),
  t('config.quickPrompt.4'),
  t('config.quickPrompt.5'),
];

export const PROMPT_PREVIEW_LIMIT = 4;
export const TERMS_VERSION = '2026-05-11';
export const ONBOARDING_GUIDE_VERSION = '2026-05-11-analyzer';

export const emptyProvider = {
  name: '',
  base_url: '',
  model: '',
  api_key_env: '',
  temperature: 0.25,
  provider_type: 'openai',
};

export const emptyUserProvider = {
  name: '',
  label: '',
  base_url: 'https://api.deepseek.com',
  model: 'deepseek-v4-flash',
  api_key: '',
  temperature: 0.25,
  provider_type: 'openai',
  is_default: false,
};

export const emptyAccount = {
  name: '',
  label: '',
  app_key: '',
  app_secret: '',
  access_token: '',
  set_default: false,
};

export const emptyAlpacaAccount = {
  name: '',
  label: '',
  api_key: '',
  api_secret: '',
  paper: true,
  set_default: false,
};

export const emptyUsmartAccount = {
  name: '',
  label: '',
  channel: '',
  sign_private_key: '',
  encrypt_public_key: '',
  phone: '',
  area_code: '852',
  trade_password: '',
  paper: true,
  set_default: false,
};

export const emptyAuthUserForm = {
  username: '',
  password: '',
  can_analyze: true,
  can_trade: false,
  is_admin: false,
  remaining_days: 7,
  max_daily_scans: 50,
  max_daily_ai_scans: 20,
  max_daily_ai_chat: 30,
  max_watchlists: 20,
  max_scan_loop_instances: 20,
  max_notification_channels: 10,
  max_longbridge_accounts: 2,
};

export const defaultAnalysisModules = {
  intraday: true,
  greeks: true,
  gex: true,
  execution: true,
  volatility: true,
  market_structure: true,
  strategy: true,
  scenario: true,
  risk: true,
};

export const analysisModuleItems = [
  ['intraday', t('config.module.intraday')],
  ['greeks', t('config.module.greeks')],
  ['gex', t('config.module.gex')],
  ['execution', t('config.module.execution')],
  ['volatility', t('config.module.volatility')],
  ['market_structure', t('config.module.market_structure')],
  ['strategy', t('config.module.strategy')],
  ['scenario', t('config.module.scenario')],
  ['risk', t('config.module.risk')],
];

export const strategyModeItems = [
  ['single_leg', t('strategy.family.single_leg')],
  ['spread', t('strategy.family.spread')],
  ['straddle', t('strategy.family.straddle')],
  ['strangle', t('strategy.family.strangle')],
  ['collar', t('strategy.family.collar')],
  ['covered_call', t('strategy.family.covered_call')],
  ['cash_secured_put', t('strategy.family.cash_secured_put')],
  ['credit_spread', t('strategy.family.credit_spread')],
  ['calendar', t('strategy.family.calendar')],
  ['diagonal', t('strategy.family.diagonal')],
  ['poor_mans_covered_call', t('strategy.family.poor_mans_covered_call')],
  ['iron_condor', t('strategy.family.iron_condor')],
  ['butterfly', t('strategy.family.butterfly')],
];

export const marketDataSourceItems = [
  ['thetadata', t('config.marketDataSource.thetadata')],
  ['yfinance', t('config.marketDataSource.yfinance')],
  ['longbridge', t('config.marketDataSource.longbridge')],
];

export const optionDataSourceItems = [
  ['thetadata', t('config.optionDataSource.thetadata')],
  ['longbridge', t('config.optionDataSource.longbridge')],
  ['yfinance', t('config.optionDataSource.yfinance')],
];

export const orderTypeItems = [
  ['market', t('trading2.orderTypeMarket')],
  ['limit', t('trading2.orderTypeLimit')],
  ['adaptive', t('trading2.orderTypeAdaptive')],
];

export const aiProviderTypeItems = [
  ['openai', 'OpenAI Compatible'],
  ['claude', 'Claude Compatible'],
];

export const routeItems = [
  ['auto', t('login.auto')],
  ['primary', t('login.primary')],
  ['secondary', t('login.secondary')],
];

export const candidateSortItems = [
  ['decision_score', t('config.sort.decision_score')],
  ['alpha_score', t('config.sort.alpha_score')],
  ['execution_score', t('config.sort.execution_score')],
  ['analysis_score', t('config.sort.analysis_score')],
  ['ask', t('config.sort.ask')],
  ['spread_pct', t('config.sort.spread_pct')],
  ['volume', t('config.sort.volume')],
  ['open_interest', t('config.sort.open_interest')],
  ['probability_breakeven', t('config.sort.probability_breakeven')],
];

export const defaultTradingConfig = {
  live_enabled: false,
  total_capital: 10000,
  run_time_et: '10:30',
  single_instance_enabled: true,
  multi_instance_enabled: false,
  schedule_profile: 'single_run',
  schedule_slots: [],
  universe: ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'QQQ', 'SPY'],
  prompt_template: t('config.tradingPromptTemplate'),
  top_n: 5,
  max_per_symbol: 1,
  default_stop_loss_pct: 25,
  default_take_profit_pct: 30,
  tiered_take_profit_enabled: false,
  default_take_profit_1_pct: 20,
  default_take_profit_2_pct: 35,
  use_ai: true,
  council: true,
  ai_adjust_allocation: false,
  ai_adjust_stop_loss: true,
  ai_adjust_take_profit: false,
  software_stop_enabled: true,
  software_take_profit_enabled: true,
  risk_max_daily_runs: 3,
  risk_max_consecutive_failures: 2,
  risk_max_unprotected_quantity: 0,
  risk_max_single_stop_loss_pct: 45,
  risk_require_protection_for_market_order: true,
  low_gate_enabled: false,
  ai_provider: 'deepseek',
  broker: 'longbridge',
  broker_account: '',
  longbridge_account: '',
  market_data_source: 'yfinance',
  option_data_source: 'thetadata',
  analysis_modules: defaultAnalysisModules,
  strategy_modes: ['single_leg'],
  strategy_auto_execute_enabled: false,
  strategy_unwind_on_failure: true,
  wait_for_fill_seconds: 8,
  entry_order_type: 'market',
  exit_order_type: 'market',
};

export const defaultScheduleSlots = [
  { slot_id: 'open_confirmation', label: t('config.slot.open_confirmation'), time_et: '09:45', action: 'scan_open', strategy_modes: ['single_leg', 'spread'], capital_pct: 0.25, gate_profile: 'strict_momentum', allow_new_positions: true, force_no_overnight: false, enabled: true },
  { slot_id: 'midday_structure', label: t('config.slot.midday_structure'), time_et: '12:45', action: 'open_or_adjust', strategy_modes: ['calendar', 'iron_condor', 'strangle', 'butterfly'], capital_pct: 0.35, gate_profile: 'structure_specific', allow_new_positions: true, force_no_overnight: false, enabled: true },
  { slot_id: 'power_hour_risk', label: t('config.slot.power_hour_risk'), time_et: '15:10', action: 'reduce_or_exit', strategy_modes: ['single_leg', 'spread'], capital_pct: 0.15, gate_profile: 'no_overnight', allow_new_positions: false, force_no_overnight: true, enabled: true },
];

export const instanceDetailTabs = [
  ['overview', t('config.instanceTab.overview')],
  ['candidates', t('config.instanceTab.candidates')],
  ['decision', t('config.instanceTab.decision')],
  ['trace', t('config.instanceTab.trace')],
  ['risk', t('config.instanceTab.risk')],
  ['events', t('config.instanceTab.events')],
  ['review', t('config.instanceTab.review')],
  ['raw', t('config.instanceTab.raw')],
];

export const scannerResultTabs = [
  ['charts', t('config.scannerTab.charts')],
  ['candidates', t('config.scannerTab.candidates')],
  ['strategies', t('config.scannerTab.strategies')],
  ['trace', t('config.scannerTab.trace')],
  ['details', t('config.scannerTab.details')],
  ['raw', t('config.scannerTab.raw')],
];

export const SCAN_HISTORY_PAGE_SIZE = 8;
export const SCAN_POLL_INTERVAL_MS = 5000;
export const TRADING_POLL_INTERVAL_MS = 6000;
export const PROTECTION_POLL_INTERVAL_MS = 15000;

export const instanceFilterItems = [
  ['all', t('config.instanceFilter.all')],
  ['active', t('config.instanceFilter.active')],
  ['attention', t('config.instanceFilter.attention')],
  ['protected', t('config.instanceFilter.protected')],
  ['closed', t('config.instanceFilter.closed')],
];
