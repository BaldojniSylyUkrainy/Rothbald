(function exposeUpdateFlow(root, factory) {
  const flow = factory();
  if (typeof module === 'object' && module.exports) module.exports = flow;
  root.RothbaldUpdateFlow = flow;
}(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  const POLL_DELAYS = { checking: 800, downloading: 500 };

  function decide({ status, previousStatus = '', modalOpen = false, manual = false, snoozed = false }) {
    const name = status?.status || 'idle';
    const render = ['available', 'downloading', 'downloaded', 'error'].includes(name);
    const changed = name !== previousStatus;
    let open = modalOpen;
    let notification = '';

    if (name === 'available') open = manual || !snoozed;
    if (name === 'downloaded') {
      open = modalOpen || (manual && previousStatus !== 'downloading');
      if (changed && !open) notification = 'downloaded';
    }
    if (name === 'error') {
      open = modalOpen || (manual && previousStatus !== 'downloading');
      if (changed && !open) notification = 'error';
    }
    if (name === 'up_to_date' && manual) notification = 'up_to_date';

    return {
      render,
      open,
      notification,
      pollDelay: POLL_DELAYS[name] || 0,
    };
  }

  function laterLabel(status) {
    if (status === 'downloading') return 'Згорнути';
    if (['downloaded', 'error'].includes(status)) return 'Закрити';
    return 'Пізніше';
  }

  function shortcutLabel(status, percent = 0) {
    if (status === 'downloading') return `Оновлення ${Math.max(0, Math.min(100, Math.round(+percent || 0)))}%`;
    if (status === 'available') return 'Доступне оновлення';
    if (status === 'downloaded') return 'Оновлення готове';
    if (status === 'error') return 'Помилка оновлення';
    return 'Перевірити оновлення';
  }

  function isSnoozed(record, version, now = Date.now()) {
    return Boolean(record && record.version === version && Number(record.until) > now);
  }

  return { decide, laterLabel, shortcutLabel, isSnoozed };
}));
