export function createApiClient({ clientUserId, onAuthExpired = () => {}, formatError = defaultFormatError } = {}) {
  // In-flight dedup: a rapid second GET of the same URL piggy-backs on the same Promise
  // instead of firing a duplicate fetch. We only dedup safe (GET) idempotent requests.
  const inflight = new Map();

  async function request(path, options = {}, { notifyAuthExpired = false } = {}) {
    const { headers, ...fetchOptions } = options;
    const method = (fetchOptions.method || 'GET').toUpperCase();
    const dedupKey = method === 'GET' && !fetchOptions.body ? `GET ${path}` : null;
    if (dedupKey && inflight.has(dedupKey)) {
      return inflight.get(dedupKey);
    }
    const promise = (async () => {
      const response = await fetch(path, {
        credentials: 'same-origin',
        ...fetchOptions,
        headers: { 'Content-Type': 'application/json', 'X-AI-Option-User': clientUserId, ...(headers ?? {}) },
      });
      if (!response.ok) {
        if (notifyAuthExpired && response.status === 401) {
          onAuthExpired();
        }
        const detail = await response.json().catch(() => ({}));
        throw new Error(formatError(detail.detail || `${window._t('apierr.requestFailed')}${response.status}`));
      }
      return response.json();
    })();
    if (dedupKey) {
      inflight.set(dedupKey, promise);
      promise.finally(() => {
        // Release immediately after settle so subsequent calls re-fetch fresh data.
        if (inflight.get(dedupKey) === promise) inflight.delete(dedupKey);
      });
    }
    return promise;
  }

  return {
    api: (path, options = {}) => request(path, options, { notifyAuthExpired: true }),
    authApi: (path, options = {}) => request(path, options),
  };
}

function defaultFormatError(value) {
  return String(value || '');
}
