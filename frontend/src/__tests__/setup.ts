/**
 * Test setup file for Vitest.
 * Configures testing environment, mocks, and MSW server.
 */

import '@testing-library/jest-dom';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { server } from './mocks/server';

// Initialize i18n for tests (suppresses react-i18next warnings)
import '../i18n';

// Setup MSW server
beforeAll(() =>
  server.listen({
    // Bypass unhandled requests silently (don't warn, just let them through)
    // Handlers use wildcard (*) prefix to match any origin
    onUnhandledRequest: 'bypass',
  })
);
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

// Mock window.matchMedia for responsive components
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Mock ResizeObserver
class ResizeObserverMock {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
vi.stubGlobal('ResizeObserver', ResizeObserverMock);

// Mock IntersectionObserver
class IntersectionObserverMock {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  root = null;
  rootMargin = '';
  thresholds = [];
}
vi.stubGlobal('IntersectionObserver', IntersectionObserverMock);

// Mock WebSocket
class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.OPEN;
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  url: string;
  constructor(url: string) {
    this.url = url;
    setTimeout(() => this.onopen?.(new Event('open')), 0);
  }

  send = vi.fn();
  close = vi.fn();
}
vi.stubGlobal('WebSocket', MockWebSocket);

// Mock scrollTo
window.scrollTo = vi.fn();

// localStorage: use jsdom's built-in storage instead of an empty vi.fn() stub.
// The previous vi.fn() mock stored nothing, so anything that persisted state
// (auth_token, user preferences, sidebar order) silently lost data between
// reads. Real jsdom localStorage is per-jsdom-environment and isolated per test
// file by default, which is what we want anyway. Any individual test that needs
// to observe calls can still spyOn(Storage.prototype, 'setItem').

// Suppress console output during tests (reduces noise)
// Remove these lines if you need to debug test output
vi.spyOn(console, 'log').mockImplementation(() => {});
vi.spyOn(console, 'warn').mockImplementation(() => {});
vi.spyOn(console, 'error').mockImplementation(() => {});
