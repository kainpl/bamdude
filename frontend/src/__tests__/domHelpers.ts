/**
 * Bare text nodes reading exactly "0".
 *
 * `0 && <jsx>` evaluates to 0 and React renders the NUMBER, leaving a stray zero
 * where a conditional block used to be. Testing Library's *ByText queries walk
 * ELEMENTS, so they cannot see it: the zero is a text node sitting among an
 * element's other children, and that element's own text is everything else too.
 * A test written with queryAllByText passes with the bug still in place — this
 * one was, and did.
 */
export function strayZeroTextNodes(root: HTMLElement = document.body): string[] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const found: string[] = [];
  while (walker.nextNode()) {
    if (walker.currentNode.textContent?.trim() === '0') {
      const parent = walker.currentNode.parentElement;
      found.push(`${parent?.tagName}.${parent?.className}`);
    }
  }
  return found;
}
