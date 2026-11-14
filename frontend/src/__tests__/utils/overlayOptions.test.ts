import { describe, it, expect } from 'vitest';
import { overlayColour, progressFill } from '../../utils/overlayOptions';

describe('overlayColour', () => {
  it('accepts six hex digits, with or without the hash', () => {
    expect(overlayColour('#00AE42')).toBe('#00ae42');
    expect(overlayColour('00ae42')).toBe('#00ae42');
  });

  // The value goes into a style: nothing but six hex digits may reach it.
  it.each([
    null,
    undefined,
    '',
    'red',
    '#fff',
    '#00ae4',
    '#00ae42ff',
    '#00ae4g',
    ' #00ae42',
    '#00ae42 ',
    '#00ae42;background:url(//evil)',
    'url(javascript:alert(1))',
    'rgb(0,0,0)',
    'var(--accent)',
    '#00ae42\n',
  ])('refuses %j', (value) => {
    expect(overlayColour(value)).toBeNull();
  });
});

describe('progressFill', () => {
  it('is a left-to-right gradient of two valid colours', () => {
    expect(progressFill('ff0000', '#0000FF')).toBe('linear-gradient(to right, #ff0000, #0000ff)');
  });

  it('is one colour when only one is valid', () => {
    expect(progressFill('ff0000', null)).toBe('linear-gradient(to right, #ff0000, #ff0000)');
    expect(progressFill('nope', '0000ff')).toBe('linear-gradient(to right, #0000ff, #0000ff)');
  });

  it('is nothing when neither is valid, so the overlay keeps its own colour', () => {
    expect(progressFill(null, null)).toBeUndefined();
    expect(progressFill('red', 'url(x)')).toBeUndefined();
  });
});
