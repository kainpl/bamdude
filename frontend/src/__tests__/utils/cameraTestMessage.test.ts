import { describe, expect, it } from 'vitest';
import i18n from '../../i18n';
import { cameraTestMessage } from '../../utils/cameraTestMessage';

describe('cameraTestMessage', () => {
  const en = i18n.getFixedT('en');
  const uk = i18n.getFixedT('uk');

  // The toast read "Camera connected640x480": the resolution was glued on.
  it('sets the resolution apart from the message', () => {
    expect(cameraTestMessage(en, '640x480')).toBe('Camera connected (640x480)');
    expect(cameraTestMessage(uk, '640x480')).toBe('Камеру підключено (640x480)');
  });

  it('says only that the camera connected when no resolution came back', () => {
    for (const none of [undefined, null, '', '   ']) {
      expect(cameraTestMessage(en, none)).toBe('Camera connected');
      expect(cameraTestMessage(uk, none)).toBe('Камеру підключено');
    }
  });
});
