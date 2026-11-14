import { vi } from 'vitest';

/**
 * jsdom lays nothing out, so every element measures 0x0. Gives the elements
 * `isBox` picks a layout size, and every other element none — a test then
 * proves which box a camera picture was fitted to. Returns the undo.
 */
export function stubBoxSize(isBox: (element: HTMLElement) => boolean, width: number, height: number): () => void {
  const widthSpy = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return isBox(this) ? width : 0;
  });
  const heightSpy = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return isBox(this) ? height : 0;
  });
  return () => {
    widthSpy.mockRestore();
    heightSpy.mockRestore();
  };
}

/** The element that directly holds an image with this alt text. */
export function holdsImage(alt: string): (element: HTMLElement) => boolean {
  return (element) => element.querySelector(`:scope > img[alt="${alt}"]`) !== null;
}
