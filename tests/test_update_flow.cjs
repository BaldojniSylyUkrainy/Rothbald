const assert = require('node:assert/strict');
const test = require('node:test');
const flow = require('../static/update_flow.js');

const status = (name, extra = {}) => ({ status: name, version: '0.3.0.0', ...extra });

test('available update opens automatically unless that version is snoozed', () => {
  assert.equal(flow.decide({ status: status('available') }).open, true);
  assert.equal(flow.decide({ status: status('available'), snoozed: true }).open, false);
  assert.equal(flow.decide({ status: status('available'), snoozed: true, manual: true }).open, true);
});

test('a manually started check respects an explicitly dismissed window', () => {
  const decision = flow.decide({
    status: status('available'),
    previousStatus: 'checking',
    manual: true,
    modalOpen: false,
    dismissed: true,
  });
  assert.equal(decision.open, false);
  assert.equal(decision.render, true);
});

test('downloading keeps polling but cannot be dismissed mid-install', () => {
  const hidden = flow.decide({ status: status('downloading'), previousStatus: 'downloading', modalOpen: false });
  const visible = flow.decide({ status: status('downloading'), previousStatus: 'downloading', modalOpen: true });
  assert.equal(hidden.open, false);
  assert.equal(visible.open, true);
  assert.equal(hidden.pollDelay, 500);
  assert.equal(flow.laterLabel('downloading'), 'Оновлюю…');
  assert.equal(flow.canDismiss('downloading'), false);
  assert.equal(flow.canDismiss('downloaded'), true);
});

test('background completion and failure notify without reopening the modal', () => {
  for (const terminal of ['downloaded', 'error']) {
    const decision = flow.decide({
      status: status(terminal),
      previousStatus: 'downloading',
      modalOpen: false,
    });
    assert.equal(decision.open, false);
    assert.equal(decision.notification, terminal);
    const afterManualStart = flow.decide({
      status: status(terminal),
      previousStatus: 'downloading',
      modalOpen: false,
      manual: true,
    });
    assert.equal(afterManualStart.open, false);
    assert.equal(afterManualStart.notification, terminal);
  }
});

test('visible download errors stay visible and expose a terminal state', () => {
  const decision = flow.decide({
    status: status('error'),
    previousStatus: 'downloading',
    modalOpen: true,
  });
  assert.equal(decision.open, true);
  assert.equal(decision.render, true);
  assert.equal(decision.notification, '');
});

test('snooze is version-bound and expires', () => {
  const now = 1_000;
  assert.equal(flow.isSnoozed({ version: '0.3.0.0', until: 2_000 }, '0.3.0.0', now), true);
  assert.equal(flow.isSnoozed({ version: '0.3.0.0', until: 900 }, '0.3.0.0', now), false);
  assert.equal(flow.isSnoozed({ version: '0.2.0.0', until: 2_000 }, '0.3.0.0', now), false);
});

test('footer shortcut reports background updater state', () => {
  assert.equal(flow.shortcutLabel('available'), 'Доступне оновлення');
  assert.equal(flow.shortcutLabel('downloading', 42.4), 'Оновлення 42%');
  assert.equal(flow.shortcutLabel('downloaded'), 'Оновлення готове');
  assert.equal(flow.shortcutLabel('error'), 'Помилка оновлення');
});
