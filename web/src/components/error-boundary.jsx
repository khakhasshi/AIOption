import React from 'react';
import { isChunkLoadError, reloadOnce } from '../utils/lazy-with-retry.js';

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // A stale-chunk load error after a deploy is recoverable: force one reload
    // to pull the fresh index.html + chunk hashes instead of stranding the user
    // on the crash screen. reloadOnce() guards against a reload loop.
    if (isChunkLoadError(error) && reloadOnce()) return;
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary]', error, info?.componentStack);
  }

  handleReload = () => {
    this.setState({ error: null });
    if (typeof this.props.onReset === 'function') this.props.onReset();
    else window.location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;
    const chunkError = isChunkLoadError(this.state.error);
    const message = chunkError
      ? window._t('errboundary.newVersion')
      : (this.state.error?.message || String(this.state.error) || window._t('errboundary.renderError'));
    return (
      <div role="alert" className="error-boundary">
        <div className="error-boundary-card">
          <h2>{chunkError ? window._t('errboundary.updating') : window._t('errboundary.crashed')}</h2>
          <p className="muted">{message}</p>
          {!chunkError && (
            <p className="muted" style={{ fontSize: 12 }}>
              {window._t('errboundary.hint')}
            </p>
          )}
          <button type="button" className="primary" onClick={this.handleReload}>
            {window._t('errboundary.reload')}
          </button>
        </div>
      </div>
    );
  }
}
