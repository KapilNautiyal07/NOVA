import assert from 'node:assert/strict';
import { test } from 'node:test';
import { autoConnects, expectsAnswer } from '../src/lib/wake.js';

const connect = (auto) => ({ status: 'needs_authorization', client_actions: [{ type: 'connect_calendar', payload: auto ? { auto: true } : {} }] });

test('NOVA keeps listening after it asks a question', () => {
  assert.equal(expectsAnswer({ status: 'needs_clarification' }), true); // "What time tomorrow?"
  assert.equal(expectsAnswer({ status: 'needs_confirmation' }), true); // "Should I schedule…?"
  assert.equal(expectsAnswer(connect(false)), true); // "Would you like to connect your Google Calendar?"
});

test('but not while the user is on Google’s sign-in page, or after a final answer', () => {
  assert.equal(expectsAnswer(connect(true)), false);
  assert.equal(autoConnects(connect(true)), true);
  assert.equal(autoConnects(connect(false)), false);
  for (const status of ['done', 'error', 'needs_permission']) assert.equal(expectsAnswer({ status }), false, status);
  assert.equal(expectsAnswer(null), false);
});
