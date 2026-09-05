import { useEffect, useRef } from 'react';

/**
 * Move focus INTO an overlay when it opens, and give it back when it closes.
 *
 * The pattern is the product gallery's lightbox, lifted out of it so that every
 * overlay this app opens over a page answers a keyboard the same way. Attach
 * the returned ref to the element that carries `role="dialog"`, give that
 * element `tabIndex={-1}` so it can hold focus, and pass whether it is open.
 *
 * The call sites, eight of them — **five dialogs**: the import dialog
 * (`ImportProductDialog`), the product card dialog's shell
 * (`ProductCardDialog`), the stock correction dialog (`AdjustDialog`, in
 * `components/products/ProductStock.tsx`) and the shells of BOTH halves of the
 * model card modal (`ModelCardModal` carries an archive card and a library-file
 * card, each its own component); and **three lightboxes**: one in each half of
 * the model card modal and the product gallery's own (`ProductGallery`).
 * Counting them in the comment at each call site is what made the number wrong;
 * grep this file's name instead.
 *
 * ⚠️ **This is NOT a focus trap and must not be described as one.** Tab still
 * walks out of the overlay and into the page behind it; what the hook fixes is
 * the two ends. Without it a keyboard user who opened the overlay starts at the
 * top of the document — every control of the page behind comes before anything
 * in the dialog — and on close the focus ring is left on `<body>`, which is
 * nowhere: the next Tab restarts from the top of the page rather than from the
 * control that opened the dialog. Trapping properly means inert-ing the rest of
 * the document, which is a change to every page that opens one of these; it is
 * deliberately not done here and no comment in this codebase claims it is.
 *
 * ⚠️ The element to return focus TO is read at OPEN, not at close: by the time
 * the overlay unmounts `document.activeElement` is whatever the overlay left
 * focused, i.e. the overlay itself.
 */
export function useDialogFocus<T extends HTMLElement>(open: boolean) {
  const ref = useRef<T | null>(null);
  const returnFocusTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    returnFocusTo.current = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    return () => {
      // `?.` on the method as well as on the ref: jsdom hands back elements
      // that have been detached from the document, and a page that navigated
      // away has nothing left to focus.
      returnFocusTo.current?.focus?.();
      returnFocusTo.current = null;
    };
  }, [open]);

  return ref;
}
