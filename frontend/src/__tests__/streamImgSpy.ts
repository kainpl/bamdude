/**
 * Records every camera stream URL given to an <img>, and whether that img was
 * in the document at that moment.
 *
 * React sets props while it renders, before it commits anything: a `src`
 * passed as a prop lands on an img that is not on the page yet — and on the
 * img of every render React later throws away, whose request the browser
 * opens all the same and nobody can close. A stream URL may only ever be set
 * on an attached img (upstream #3431).
 */
export interface StreamSrcRecord {
  img: HTMLImageElement;
  src: string;
  attached: boolean;
}

const STREAM = /\/(camera|cameras\/\d+)\/stream\?/;

export function spyOnStreamSrc() {
  const records: StreamSrcRecord[] = [];
  const proto = HTMLImageElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'src')!;
  const setAttribute = Element.prototype.setAttribute;
  const note = (img: HTMLImageElement, value: unknown) => {
    const src = String(value);
    if (STREAM.test(src)) records.push({ img, src, attached: img.isConnected });
  };
  Object.defineProperty(proto, 'src', {
    ...descriptor,
    set(this: HTMLImageElement, value: string) {
      note(this, value);
      descriptor.set!.call(this, value);
    },
  });
  Element.prototype.setAttribute = function (this: Element, name: string, value: string) {
    if (this instanceof HTMLImageElement && name === 'src') note(this, value);
    return setAttribute.call(this, name, value);
  };
  return {
    records,
    /** Stream URLs set on an img that was not in the document. */
    detached: () => records.filter((record) => !record.attached),
    /** Imgs outside the document that still hold a stream URL: open requests. */
    leaked: () =>
      [...new Set(records.map((record) => record.img))].filter(
        (img) => !img.isConnected && STREAM.test(img.getAttribute('src') ?? ''),
      ),
    restore: () => {
      Object.defineProperty(proto, 'src', descriptor);
      Element.prototype.setAttribute = setAttribute;
    },
  };
}
